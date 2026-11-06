"""
simulator.py -- nodes + bus scheduler built on can_core.

Nodes
-----
EcuNode      : a legitimate ECU. Owns an ID and transmits a payload
               periodically. If `parrot=True` it also runs the Parrot defense:
               when it sees a frame carrying ITS OWN id that it did not
               originate, it reacts by contending for that id with an
               all-dominant payload, forcing the spoofer to take the transmit
               error on every attempt.
Attacker     : injects spoofed frames carrying a victim's id at a fixed rate.

Bus scheduler
-------------
Time is measured in bit-times. At every point the bus is idle, every node with
a frame ready starts together and arbitrates (real CAN behaviour). The Parrot
reaction is the one explicit addition: a defender injects its contending frame
into the SAME slot as a detected spoof of its id.
"""

from __future__ import annotations
from dataclasses import dataclass, field

from .can_core import (
    Frame, ErrorState, NodeState, CollisionModel, resolve_collision, encode,
)

IFS_BITS = 3  # inter-frame space


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------
class Node:
    def __init__(self, name: str):
        self.name = name
        self.estate = ErrorState()

    @property
    def bus_off(self) -> bool:
        return self.estate.state is NodeState.BUS_OFF


class EcuNode(Node):
    def __init__(self, name, owned_id, payload=b"\x11\x22\x33\x44",
                 period_bits=2000, phase=0, parrot=False):
        super().__init__(name)
        self.owned_id = owned_id
        self.payload = payload
        self.period_bits = period_bits
        self.next_tx = phase
        self.parrot = parrot

    def ready(self, t) -> bool:
        return (not self.bus_off) and t >= self.next_tx

    def frame(self) -> Frame:
        return Frame(self.owned_id, self.payload)

    def scheduled_sent(self, t):
        # advance to the next period boundary at or after t
        while self.next_tx <= t:
            self.next_tx += self.period_bits

    def parrot_frame(self) -> Frame:
        # all-dominant payload: the owner wins every data-bit contention, so a
        # spoofer sending any recessive data bit takes the bit error.
        return Frame(self.owned_id, b"\x00" * max(1, len(self.payload)))


class Attacker(Node):
    def __init__(self, name, target_id, spoof_payload=b"\xDE\xAD\xBE\xEF",
                 inject_period_bits=1500, phase=500, max_injections=None):
        super().__init__(name)
        self.target_id = target_id
        self.spoof_payload = spoof_payload
        self.inject_period_bits = inject_period_bits
        self.next_tx = phase
        self.injections = 0
        self.max_injections = max_injections

    def ready(self, t) -> bool:
        if self.bus_off or t < self.next_tx:
            return False
        if self.max_injections is not None and self.injections >= self.max_injections:
            return False
        return True

    def frame(self) -> Frame:
        return Frame(self.target_id, self.spoof_payload)

    def scheduled_sent(self, t):
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
    def __init__(self, nodes, bitrate=500_000, collision_model=None):
        self.nodes = nodes
        self.bit_period_s = 1.0 / bitrate
        self.model = collision_model or CollisionModel()
        self.log: list[Snapshot] = []
        self.spoof_success = 0
        self.spoof_suppressed = 0

    def _counters(self):
        return {n.name: (n.estate.tec, n.estate.rec, n.estate.state.value)
                for n in self.nodes}

    def _record(self, t, event, winner, spoof_landed):
        self.log.append(Snapshot(
            t_bits=t, t_ms=t * self.bit_period_s * 1000.0,
            event=event, winner=winner,
            counters=self._counters(), spoof_landed=spoof_landed,
        ))

    def run(self, duration_bits=400_000):
        t = 0
        while t < duration_bits:
            ready = [n for n in self.nodes if hasattr(n, "ready") and n.ready(t)]
            if not ready:
                t += 1
                continue

            # frames each ready node wants to send this slot
            contenders = [(n, n.frame()) for n in ready]

            # --- Parrot reaction -------------------------------------------
            # any legitimate owner running the defense that sees its own id
            # being spoofed this slot joins the contention with an all-dominant
            # frame for that id.
            spoofed_ids = {f.arb_id for (n, f) in contenders if isinstance(n, Attacker)}
            for n in self.nodes:
                if isinstance(n, EcuNode) and n.parrot and not n.bus_off:
                    if n.owned_id in spoofed_ids and n not in [c[0] for c in contenders]:
                        contenders.append((n, n.parrot_frame()))

            # --- resolve ----------------------------------------------------
            if len(contenders) == 1:
                node, frame = contenders[0]
                winner, losers_bit, losers_arb = node, [], []
                result_field = "solo"
            else:
                entries = [(n, encode(f)) for (n, f) in contenders]
                res = resolve_collision(entries, self.model)
                winner = res.winner
                losers_bit = res.bit_error_losers
                losers_arb = res.arb_losers
                result_field = res.resolved_at_field
                if self.model.mode == "strict_both":
                    # naive mirroring: owner ALSO takes the hit
                    losers_bit = list({*losers_bit, winner}) if any(
                        isinstance(n, Attacker) for n, _ in contenders) else losers_bit

            winning_frame = dict((id(n), f) for (n, f) in contenders)[id(winner)]

            # counter updates. In naive "strict_both" the winner is also in
            # losers_bit (it took the error too) and must NOT get a success
            # credit -- that is exactly why naive mirroring is self-defeating.
            if winner not in losers_bit:
                winner.estate.transmit_ok()
            for ln in losers_bit:
                delta = (self.model.attacker_delta if isinstance(ln, Attacker)
                         else self.model.defender_delta)
                ln.estate.transmit_error(delta)
            # receivers of a clean frame
            for n in self.nodes:
                if n is winner or n in losers_bit or n in losers_arb:
                    continue
                if not n.bus_off:
                    n.estate.receive_ok()

            # did an attacker's spoofed value reach the bus?
            spoof_landed = isinstance(winner, Attacker)
            if any(isinstance(n, Attacker) for n, _ in contenders):
                if spoof_landed:
                    self.spoof_success += 1
                else:
                    self.spoof_suppressed += 1

            # bookkeeping: winner + arbitration-losers consumed their slot
            winner.scheduled_sent(t)
            for ln in losers_arb:
                ln.scheduled_sent(t)        # retry at their next natural period
            for ln in losers_bit:
                if hasattr(ln, "injections"):
                    ln.injections += 1
                ln.scheduled_sent(t)        # spoofer moves on to next attempt
            if isinstance(winner, Attacker):
                winner.injections += 1

            frame_len = len(encode(winning_frame))
            self._record(t, f"tx({result_field})", winner.name, spoof_landed)
            t += frame_len + IFS_BITS

        return self.log
