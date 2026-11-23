"""
can_core.py -- Bit-level simulator of the link layer (ISO 11898-1)
designed to analyse the Parrot defence 

Why this module exists
-------------------------
Virtual interfaces such as `vcan0` (SocketCAN) forward complete frames but do not
simulate bit-by-bit arbitration, bit monitoring, bit stuffing, error flags,
TEC/REC counters or transitions to bus-off. Parrot’s mechanism relies
precisely on these low-level CAN protocol rules.

Rules modelled here:
  * Dominant bit = 0, recessive bit = 1; the bus applies a wired-AND (0 wins).
  * In the arbitration field (SOF, ID, RTR): sending a 1 and reading a 0 results in losing
    arbitration without a transmission error.
  * Outside arbitration (in monitored fields): sending a 1 and reading a 0 causes
    a BIT ERROR.
  * Counters: TEC += 8 on a transmission error, TEC -= 1 on a successful
    transmission; REC += 1 on a reception error, REC -= 1 on a valid reception.
  * States: ERROR_ACTIVE (TEC <= 127 and REC <= 127), ERROR_PASSIVE
    (TEC > 127 or REC > 127), BUS_OFF (TEC > 255).

"""



from __future__ import annotations
from dataclasses import dataclass
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

# A passive error flag requires 6 flag bits + 8 delimiter bits = 14
# consecutive recessive bits, without interruption, to be completed.
PASSIVE_ERROR_FLAG_BITS = 14

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
    """Encode a frame as (bit, field_name) pairs with bit stuffing.
    Stuffing is applied from SOF through the end of the CRC.
    """
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
    # True when the node (already in error-passive) has issued a Passive
    # Error Flag and requires 14 free recesive bits to complete the process without
    # incurring a Form Error.
    pending_passive_flag: bool = False

    def _reclassify(self):
        if self.tec > TEC_BUSOFF_THRESHOLD:
            self.state = NodeState.BUS_OFF
            self.pending_passive_flag = False
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

    def arm_passive_error_flag(self):
        """Indicates that the node has triggered a 14-bit recessive Passive Error Flag."""
        if self.state is not NodeState.BUS_OFF:
            self.pending_passive_flag = True

    def consume_interframe_recessive_bits(self, available_recessive_bits: int) -> bool:
        """
        Checks whether the available recessive gap between frames allows the
        pending Passive Error Flag (14 bits) to be completed.

        Returns True if a Form Error occurred due to an interruption (< 14 bits),
        or False if the flag was completed successfully (>= 14 bits).
        """
        if not self.pending_passive_flag or self.state is NodeState.BUS_OFF:
            return False
        if available_recessive_bits < PASSIVE_ERROR_FLAG_BITS:
            # The following transmission causes a Delimiter Error on the node:
            # Form Error -> TEC += 8.
            self.transmit_error(TEC_ERR_DELTA)  # +8 TEC
            return True
        else:
            # Passive Error Flag completed successfully
            self.pending_passive_flag = False
            return False


# ---------------------------------------------------------------------------
# Bit-level collision resolution and the application of CAN rules
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Bit-level collision resolution and application of CAN rules
# ---------------------------------------------------------------------------
@dataclass
class CollisionResult:
    winner: object                          # Winning node (or None if an active error flag aborts)
    bit_error_losers: list                  # Nodes with a bit error outside arbitration
    arb_losers: list                        # Nodes that lost arbitration (without an error)
    resolved_at_field: str                  # Field where contention was resolved
    aborted_by_active_error: bool = False   # True if an error-active node destroyed the frame


def resolve_collision(entries: list[tuple[object, list[tuple[int, str]]]]) -> CollisionResult:
    """
    Processes the encoded bit streams of contending nodes in parallel.
    Applies wired-AND (0 dominates over 1):
      - If a node sends 1 and reads 0 within ARBITRATION_FIELDS, it loses
        arbitration without recording a bit error.
      - If a node sends 1 and reads 0 outside ARBITRATION_FIELDS, it suffers a
        bit error and stops competing in that frame.
    """
    streams = list(entries)
    active = list(range(len(streams)))
    arb_losers: list[int] = []
    bit_err_losers: list[int] = []
    resolved_field = "eof"

    bit_idx = 0
    while len(active) > 1:
        # Filter out nodes whose bit stream has already ended
        active = [k for k in active if bit_idx < len(streams[k][1])]
        if len(active) <= 1:
            break

        # Apply wired-AND to the active bits in this slot
        bus_bit = min(streams[k][1][bit_idx][0] for k in active)
        current_field = streams[active[0]][1][bit_idx][1]

        dropped_this_step: list[int] = []
        for k in active:
            tx_bit, _fld = streams[k][1][bit_idx]
            if tx_bit == RECESSIVE and bus_bit == DOMINANT:
                dropped_this_step.append(k)

        if dropped_this_step:
            resolved_field = current_field
            for k in dropped_this_step:
                if current_field in ARBITRATION_FIELDS:
                    arb_losers.append(k)
                else:
                    bit_err_losers.append(k)
            active = [k for k in active if k not in dropped_this_step]

        bit_idx += 1

    winner_obj = streams[active[0]][0] if active else None
    return CollisionResult(
        winner=winner_obj,
        bit_error_losers=[streams[k][0] for k in bit_err_losers],
        arb_losers=[streams[k][0] for k in arb_losers],
        resolved_at_field=resolved_field,
        aborted_by_active_error=False,
    )


def apply_collision_errors(
    contenders: list[object],
    res: CollisionResult,
    observers: list[object],
) -> CollisionResult:
    """
    Apply state and counter consequences after resolving a collision:
      * If any node in bit_error_losers is in ERROR_ACTIVE, it emits an
        Active Error Flag (6 zeros) that aborts the frame: all nodes that
        participated in the transmission (potential winner + bit-error
        losers) add TEC += 8, and active observers add REC += 1.
      * If all nodes in bit_error_losers are in ERROR_PASSIVE, they emit only
        Passive Error Flags (recessive): the winner completes its frame
        (TEC -= 1), the bit-error losers add TEC += 8 and set their pending
        passive error flag, and observers subtract REC -= 1.
    """
    if not res.bit_error_losers:
        # No bit error: only a clean arbitration loss could have occurred
        if res.winner is not None and hasattr(res.winner, "estate"):
            res.winner.estate.transmit_ok()
        for obs in observers:
            if hasattr(obs, "estate") and not getattr(obs, "bus_off", False):
                obs.estate.receive_ok()
        return res

    any_active_error = any(
        getattr(getattr(n, "estate", None), "state", None) is NodeState.ERROR_ACTIVE
        for n in res.bit_error_losers
    )

    if any_active_error:
        # An active error flag destroys the frame: all non-arbitration losers
        # (the dominating winner + those that suffered a bit error) incur an error.
        affected_tx = set(res.bit_error_losers)
        if res.winner is not None and res.winner not in res.arb_losers:
            affected_tx.add(res.winner)

        for n in contenders:
            if n in affected_tx and hasattr(n, "estate"):
                n.estate.transmit_error(TEC_ERR_DELTA)

        for obs in observers:
            if obs not in contenders and hasattr(obs, "estate"):
                if not getattr(obs, "bus_off", False):
                    obs.estate.receive_error()

        return CollisionResult(
            winner=None,
            bit_error_losers=res.bit_error_losers,
            arb_losers=res.arb_losers,
            resolved_at_field=res.resolved_at_field,
            aborted_by_active_error=True,
        )

    # All nodes with a bit error are in ERROR_PASSIVE: their error flag is
    # recessive and does not prevent the winner (with dominant bits) from completing the frame.
    if res.winner is not None and hasattr(res.winner, "estate"):
        res.winner.estate.transmit_ok()

    for ln in res.bit_error_losers:
        if hasattr(ln, "estate"):
            ln.estate.transmit_error(TEC_ERR_DELTA)
            ln.estate.arm_passive_error_flag()

    for obs in observers:
        if obs not in contenders and hasattr(obs, "estate"):
            if not getattr(obs, "bus_off", False):
                obs.estate.receive_ok()

    return res