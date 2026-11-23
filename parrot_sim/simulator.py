"""
simulator.py -- nodes + bus scheduler built on can_core.

Models ISO 11898-1 timing and scheduling rules:
  * Automatic retransmission on arbitration loss or transmission error.
  * Interframe Space (IFS = 3 recessive bits).
  * Suspend Transmission (+8 recessive bits for Error-Passive transmitters).
  * Per-controller minimum inter-transmission gap (`min_tx_gap_us`) and
    interframe recessive bit accounting for pending Passive Error Flags.
"""

from __future__ import annotations
from dataclasses import dataclass

from .can_core import (
    Frame,
    ErrorState,
    NodeState,
    resolve_collision,
    apply_collision_errors,
    encode,
)

IFS_BITS = 3                   # Intermission (3 recessive bits)
SUSPEND_TX_BITS = 8            # Extra recessive bits for Error-Passive transmitters
FRAME_TAIL_RECESSIVE_BITS = 8  # ACK delimiter (1) + EOF (7)
ACTIVE_ERROR_OVERHEAD_BITS = 14  # 6 dominant Active Error Flag + 8 Error Delimiter


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------
class Node:
    def __init__(self, name: str, min_tx_gap_us: float = 0.0):
        self.name = name
        self.estate = ErrorState()
        self.min_tx_gap_us = min_tx_gap_us
        self.earliest_tx_t = 0
        self.pending_frame: Frame | None = None

    @property
    def bus_off(self) -> bool:
        return self.estate.state is NodeState.BUS_OFF

    def _gap_bits(self, bitrate: int) -> int:
        gap = max(IFS_BITS, int(round(self.min_tx_gap_us * bitrate / 1e6)))
        if self.estate.state is NodeState.ERROR_PASSIVE:
            gap += SUSPEND_TX_BITS
        return gap

    def set_next_tx_cooldown(self, frame_end_t: int, bitrate: int) -> None:
        self.earliest_tx_t = max(self.earliest_tx_t, frame_end_t + self._gap_bits(bitrate))


class EcuNode(Node):
    def __init__(
        self,
        name: str,
        owned_id: int,
        payload: bytes = b"\x11\x22\x33\x44",
        period_bits: int = 2000,
        phase: int = 0,
        parrot: bool = False,
        min_tx_gap_us: float = 0.0,
    ):
        super().__init__(name, min_tx_gap_us=min_tx_gap_us)
        self.owned_id = owned_id
        self.payload = payload
        self.period_bits = period_bits
        self.next_tx = phase
        self.parrot = parrot

    def ready(self, t: int) -> bool:
        if self.bus_off or t < self.earliest_tx_t:
            return False
        return (self.pending_frame is not None) or (t >= self.next_tx)

    def frame(self) -> Frame:
        if self.pending_frame is not None:
            return self.pending_frame
        return Frame(self.owned_id, self.payload)

    def scheduled_sent(self, t: int) -> None:
        self.pending_frame = None
        while self.next_tx <= t:
            self.next_tx += self.period_bits

    def parrot_frame(self) -> Frame:
        return Frame(self.owned_id, b"\x00" * max(1, len(self.payload)))


class Attacker(Node):
    def __init__(
        self,
        name: str,
        target_id: int,
        spoof_payload: bytes = b"\xDE\xAD\xBE\xEF",
        inject_period_bits: int = 1500,
        phase: int = 500,
        max_injections: int | None = None,
        min_tx_gap_us: float = 0.0,
    ):
        super().__init__(name, min_tx_gap_us=min_tx_gap_us)
        self.target_id = target_id
        self.spoof_payload = spoof_payload
        self.inject_period_bits = inject_period_bits
        self.next_tx = phase
        self.injections = 0
        self.max_injections = max_injections

    def ready(self, t: int) -> bool:
        if self.bus_off or t < self.earliest_tx_t:
            return False
        if self.pending_frame is not None:
            return True
        if t < self.next_tx:
            return False
        if self.max_injections is not None and self.injections >= self.max_injections:
            return False
        return True

    def frame(self) -> Frame:
        if self.pending_frame is not None:
            return self.pending_frame
        return Frame(self.target_id, self.spoof_payload)

    def scheduled_sent(self, t: int) -> None:
        self.pending_frame = None
        while self.next_tx <= t:
            self.next_tx += self.inject_period_bits


# ---------------------------------------------------------------------------
# Bus
# ---------------------------------------------------------------------------
@dataclass
class Snapshot:
    t_bits: int
    t_ms: float
    event: str
    winner: str
    counters: dict          # name -> (tec, rec, state)
    spoof_landed: bool


class Bus:
    def __init__(self, nodes: list[Node], bitrate: int = 500_000):
        self.nodes = nodes
        self.bitrate = bitrate
        self.bit_period_s = 1.0 / bitrate
        self.log: list[Snapshot] = []
        self.spoof_success = 0
        self.spoof_suppressed = 0

    def _counters(self) -> dict:
        return {
            n.name: (n.estate.tec, n.estate.rec, n.estate.state.value)
            for n in self.nodes
        }

    def _record(self, t: int, event: str, winner: str, spoof_landed: bool) -> None:
        self.log.append(
            Snapshot(
                t_bits=t,
                t_ms=t * self.bit_period_s * 1000.0,
                event=event,
                winner=winner,
                counters=self._counters(),
                spoof_landed=spoof_landed,
            )
        )

    def _evaluate_interframe_gaps(self, available_recessive_bits: int) -> None:
        """Check pending Passive Error Flags against the recessive gap before SOF."""
        for n in self.nodes:
            if n.estate.pending_passive_flag and not n.bus_off:
                n.estate.consume_interframe_recessive_bits(available_recessive_bits)

    def run(self, duration_bits: int = 400_000) -> list[Snapshot]:
        t = 0
        last_frame_end_t: int | None = None

        while t < duration_bits:
            ready = [n for n in self.nodes if hasattr(n, "ready") and n.ready(t)]
            if not ready:
                t += 1
                continue

            # Evaluate recessive gap since the previous frame ended
            if last_frame_end_t is not None:
                idle_bits = max(0, t - last_frame_end_t)
                self._evaluate_interframe_gaps(FRAME_TAIL_RECESSIVE_BITS + idle_bits)
                # Re-filter in case a Form Error drove a ready node to BUS_OFF
                ready = [n for n in ready if not n.bus_off]
                if not ready:
                    t += 1
                    continue

            contenders = [(n, n.frame()) for n in ready]

            # --- Temporary Parrot hook (to be replaced by Algorithm 1) -----
            spoofed_ids = {f.arb_id for (n, f) in contenders if isinstance(n, Attacker)}
            for n in self.nodes:
                if isinstance(n, EcuNode) and n.parrot and not n.bus_off:
                    if n.owned_id in spoofed_ids and n not in [c[0] for c in contenders]:
                        contenders.append((n, n.parrot_frame()))

            # --- Resolve & apply CAN error rules ---------------------------
            if len(contenders) == 1:
                node, frame = contenders[0]
                winner, losers_bit, losers_arb = node, [], []
                result_field = "solo"
                node.estate.transmit_ok()
                for obs in self.nodes:
                    if obs is not node and not obs.bus_off:
                        obs.estate.receive_ok()
            else:
                entries = [(n, encode(f)) for (n, f) in contenders]
                contender_nodes = [n for n, _ in contenders]
                observers = [n for n in self.nodes if n not in contender_nodes]
                res = resolve_collision(entries)
                res = apply_collision_errors(contender_nodes, res, observers)
                winner = res.winner
                losers_bit = res.bit_error_losers
                losers_arb = res.arb_losers
                result_field = res.resolved_at_field

            # --- Case 1: Active Error Flag aborted the frame ---------------
            if winner is None:
                if any(isinstance(n, Attacker) for n, _ in contenders):
                    self.spoof_suppressed += 1

                frame_end_t = t + ACTIVE_ERROR_OVERHEAD_BITS
                for n, f in contenders:
                    if not n.bus_off:
                        # Automatic retransmission: keep frame pending
                        n.pending_frame = f
                        n.set_next_tx_cooldown(frame_end_t, self.bitrate)
                    else:
                        n.pending_frame = None

                self._record(t, f"abort({result_field})", "NONE", False)
                last_frame_end_t = frame_end_t
                t = frame_end_t + IFS_BITS
                continue

            # --- Case 2: A winning frame completed -------------------------
            frame_by_node = {id(n): f for (n, f) in contenders}
            winning_frame = frame_by_node[id(winner)]
            frame_len = len(encode(winning_frame))
            frame_end_t = t + frame_len

            spoof_landed = isinstance(winner, Attacker)
            if any(isinstance(n, Attacker) for n, _ in contenders):
                if spoof_landed:
                    self.spoof_success += 1
                else:
                    self.spoof_suppressed += 1

            # Winner succeeded: clear pending frame and advance period
            winner.scheduled_sent(t)
            winner.set_next_tx_cooldown(frame_end_t, self.bitrate)
            if isinstance(winner, Attacker):
                winner.injections += 1

            # Arbitration losers & passive bit-error losers: auto-retransmit
            for ln in losers_arb:
                if not ln.bus_off:
                    ln.pending_frame = frame_by_node[id(ln)]
                    ln.earliest_tx_t = max(ln.earliest_tx_t, frame_end_t + IFS_BITS)
            for ln in losers_bit:
                if not ln.bus_off:
                    ln.pending_frame = frame_by_node[id(ln)]
                    ln.set_next_tx_cooldown(frame_end_t, self.bitrate)
                else:
                    ln.pending_frame = None

            self._record(t, f"tx({result_field})", winner.name, spoof_landed)
            last_frame_end_t = frame_end_t
            t = frame_end_t + IFS_BITS

        return self.log