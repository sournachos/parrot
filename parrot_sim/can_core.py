"""
can_core.py -- A bit-level data-link-layer simulator for the CAN bus.

Why this exists
---------------
SocketCAN's `vcan0` (used in the ICSim lab) only *forwards* frames. It models
NONE of the data-link layer that the Parrot defense depends on: no bit-wise
arbitration, no bit monitoring, no error frames, no Transmit/Receive Error
Counters (TEC/REC), and no bus-off state. Parrot works *entirely* inside that
machinery, so it cannot be demonstrated on vcan0.

This module re-implements that layer at the bit level so the mechanism is
observable and measurable:

  * dominant = 0, recessive = 1
  * the bus carries the wired-AND of everything driven onto it (0 wins)
  * a transmitter that drives a recessive bit but reads back a dominant one
    either LOSES ARBITRATION (if still in the arbitration field) or detects a
    BIT ERROR (anywhere after it)
  * TEC += 8 on a transmit error, TEC -= 1 on a successful transmit
  * ERROR_ACTIVE (TEC<=127) -> ERROR_PASSIVE (TEC>127) -> BUS_OFF (TEC>255)

Faithfulness / known simplifications (discuss these in the report):
  * Error *frames* are not emitted bit-for-bit; the counter deltas they would
    produce are applied directly. This is the standard testbed simplification
    and does not change the counter dynamics that drive bus-off.
  * The per-collision TEC bookkeeping is made explicit and TUNABLE
    (see CollisionModel). The exact accounting in the reference paper must be
    reconciled against it -- this is the one knob to verify against Parrot.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum

DOMINANT = 0
RECESSIVE = 1

# ----- error-counter thresholds (ISO 11898-1) -------------------------------
TEC_PASSIVE_THRESHOLD = 127   # TEC > 127  -> error-passive
TEC_BUSOFF_THRESHOLD = 255    # TEC > 255  -> bus-off
TEC_ERR_DELTA = 8             # transmit error
TEC_OK_DELTA = -1             # successful transmit
REC_ERR_DELTA = 1             # receive error
REC_OK_DELTA = -1             # successful receive


class NodeState(Enum):
    ERROR_ACTIVE = "error_active"
    ERROR_PASSIVE = "error_passive"
    BUS_OFF = "bus_off"


# ---------------------------------------------------------------------------
# CAN frame + bit-level encoding (standard 11-bit identifier, 2.0A data frame)
# ---------------------------------------------------------------------------
@dataclass
class Frame:
    arb_id: int                       # 11-bit identifier
    data: bytes = b""                 # 0..8 bytes
    rtr: int = DOMINANT               # 0 = data frame

    @property
    def dlc(self) -> int:
        return len(self.data)

    def __post_init__(self):
        assert 0 <= self.arb_id <= 0x7FF, "11-bit ID only"
        assert 0 <= self.dlc <= 8, "DLC 0..8"


def _bits(value: int, width: int) -> list[int]:
    return [(value >> (width - 1 - i)) & 1 for i in range(width)]


def _crc15(bits: list[int]) -> list[int]:
    """CAN CRC-15, polynomial 0x4599, over the (unstuffed) bit stream."""
    crc = 0
    for b in bits:
        inb = b ^ ((crc >> 14) & 1)
        crc = (crc << 1) & 0x7FFF
        if inb:
            crc ^= 0x4599
    return _bits(crc, 15)


# each encoded bit is tagged with the field it belongs to, so region logic
# (arbitration vs monitored vs delimiter) never depends on a raw index.
ARBITRATION_FIELDS = {"sof", "id", "rtr"}
# a transmitter monitors its own bits for bit errors across these fields:
MONITORED_FIELDS = {"sof", "id", "rtr", "ctrl", "data", "crc"}


def encode(frame: Frame) -> list[tuple[int, str]]:
    """Return the on-wire bit stream as (bit, field) pairs, with bit stuffing."""
    raw: list[tuple[int, str]] = []
    raw.append((DOMINANT, "sof"))
    raw += [(b, "id") for b in _bits(frame.arb_id, 11)]
    raw.append((frame.rtr, "rtr"))
    raw.append((DOMINANT, "ctrl"))          # IDE (0 = standard)
    raw.append((DOMINANT, "ctrl"))          # reserved r0
    raw += [(b, "ctrl") for b in _bits(frame.dlc, 4)]
    for byte in frame.data:
        raw += [(b, "data") for b in _bits(byte, 8)]

    crc_input = [b for (b, _f) in raw]      # SOF..data
    raw += [(b, "crc") for b in _crc15(crc_input)]

    # bit stuffing applies SOF..CRC: after 5 equal bits insert one opposite
    stuffed: list[tuple[int, str]] = []
    run_val, run_len = None, 0
    for (b, f) in raw:
        stuffed.append((b, f))
        if b == run_val:
            run_len += 1
        else:
            run_val, run_len = b, 1
        if run_len == 5:
            stuffed.append((1 - b, "stuff"))
            run_val, run_len = 1 - b, 1

    # unstuffed trailer
    stuffed.append((RECESSIVE, "crcdelim"))
    stuffed.append((RECESSIVE, "ack"))       # transmitter drives recessive here
    stuffed.append((RECESSIVE, "ackdelim"))
    stuffed += [(RECESSIVE, "eof")] * 7
    return stuffed


# ---------------------------------------------------------------------------
# Error-counter state machine (one per node)
# ---------------------------------------------------------------------------
@dataclass
class ErrorState:
    tec: int = 0
    rec: int = 0
    state: NodeState = NodeState.ERROR_ACTIVE

    def _reclassify(self):
        if self.tec > TEC_BUSOFF_THRESHOLD:
            self.state = NodeState.BUS_OFF
        elif self.tec > TEC_PASSIVE_THRESHOLD or self.rec > TEC_PASSIVE_THRESHOLD:
            self.state = NodeState.ERROR_PASSIVE
        else:
            self.state = NodeState.ERROR_ACTIVE

    def transmit_error(self, delta: int = TEC_ERR_DELTA):
        if self.state is NodeState.BUS_OFF:
            return
        self.tec += delta
        self._reclassify()

    def transmit_ok(self):
        if self.state is NodeState.BUS_OFF:
            return
        self.tec = max(0, self.tec + TEC_OK_DELTA)
        self._reclassify()

    def receive_error(self):
        if self.state is NodeState.BUS_OFF:
            return
        self.rec += REC_ERR_DELTA
        self._reclassify()

    def receive_ok(self):
        if self.state is NodeState.BUS_OFF:
            return
        self.rec = max(0, self.rec + REC_OK_DELTA)
        self._reclassify()


# ---------------------------------------------------------------------------
# Collision resolution -- the heart of the mechanism
# ---------------------------------------------------------------------------
@dataclass
class CollisionModel:
    """
    Tunable per-collision TEC bookkeeping.

    mode="parrot"       : the legitimate owner wins the contention for its own
                          ID; the spoofing transmitter takes the transmit error
                          (TEC += attacker_delta) and the owner's frame is the
                          one that lands. This represents Parrot's intended
                          asymmetry -- verify the exact counter accounting
                          against the reference paper and adjust the deltas.
    mode="strict_both"  : naive mirroring -- BOTH colliding transmitters take a
                          transmit error. Useful to show *why* a naive "just
                          echo the attacker" defense fails (both go bus-off),
                          motivating Parrot's design.
    """
    mode: str = "parrot"
    attacker_delta: int = TEC_ERR_DELTA   # 8
    defender_delta: int = TEC_ERR_DELTA   # 8 (used by strict_both)


@dataclass
class CollisionResult:
    winner: object                        # Node whose frame reached the bus
    bit_error_losers: list                # nodes that took a transmit bit error
    arb_losers: list                      # nodes that merely lost arbitration
    resolved_at_field: str                # where the contention was decided


def resolve_collision(entries, model: CollisionModel) -> CollisionResult:
    """
    entries: list of (node, stuffed_bitstream). Walk the streams in lockstep;
    the bus bit is the wired-AND. A node driving recessive while the bus is
    dominant drops out -- as an arbitration loss inside the arbitration field,
    or as a bit error anywhere in the monitored region.
    """
    streams = [(n, bs) for (n, bs) in entries]
    active = list(range(len(streams)))
    arb_losers, bit_err_losers = [], []
    resolved_field = "eof"

    i = 0
    while len(active) > 1:
        # a node whose stream has ended can no longer be contending
        active = [k for k in active if i < len(streams[k][1])]
        if len(active) <= 1:
            break
        bus = min(streams[k][1][i][0] for k in active)  # wired-AND
        field = streams[active[0]][1][i][1]
        dropped = []
        for k in active:
            bit, _f = streams[k][1][i]
            if bit == RECESSIVE and bus == DOMINANT:
                dropped.append(k)
        if dropped:
            resolved_field = field
            for k in dropped:
                if field in ARBITRATION_FIELDS:
                    arb_losers.append(k)
                else:
                    bit_err_losers.append(k)
            active = [k for k in active if k not in dropped]
        i += 1

    winner_idx = active[0] if active else 0
    return CollisionResult(
        winner=streams[winner_idx][0],
        bit_error_losers=[streams[k][0] for k in bit_err_losers],
        arb_losers=[streams[k][0] for k in arb_losers],
        resolved_at_field=resolved_field,
    )
