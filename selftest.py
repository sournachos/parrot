"""
selftest.py -- Unit and protocol-rule checks for parrot_sim/can_core.py.

Run from the repository root:
    python selftest.py
"""

from __future__ import annotations

from parrot_sim.can_core import (
    DOMINANT,
    RECESSIVE,
    ErrorState,
    Frame,
    NodeState,
    apply_collision_errors,
    encode,
    resolve_collision,
)


class _DummyNode:
    """Minimal node stand-in for testing can_core without the full bus scheduler."""

    def __init__(self, name: str, bus_off: bool = False):
        self.name = name
        self.estate = ErrorState()
        self.bus_off = bus_off


def _max_consecutive_run(bits: list[int]) -> int:
    """Return the longest run of identical consecutive bits in a sequence."""
    if not bits:
        return 0
    best = 1
    current = 1
    for prev, curr in zip(bits, bits[1:]):
        if curr == prev:
            current += 1
            if current > best:
                best = current
        else:
            current = 1
    return best


def test_frame_encoding_and_bit_stuffing() -> None:
    """Verify standard frame fields, stuffing limits, and unstuffed trailer."""
    frame = Frame(arb_id=0x00F, data=b"\x00" * 7)
    assert frame.dlc == 7

    stream = encode(frame)
    # Must start with dominant SOF (0) and end with 7 recessive EOF bits (1)
    assert stream[0] == (DOMINANT, "sof")
    assert stream[-7:] == [(RECESSIVE, "eof")] * 7

    # In the stuffed region (before crcdelim), no run of equal bits may exceed 5
    stuffed_prefix = [
        bit for (bit, field_name) in stream
        if field_name not in {"crcdelim", "ack", "ackdelim", "eof"}
    ]
    assert _max_consecutive_run(stuffed_prefix) <= 5


def test_arbitration_lower_id_wins_without_bit_error() -> None:
    """Verify dominant 0 wins wired-AND arbitration with no bit error."""
    high_prio = _DummyNode("HighPrio")   # ID 0x100 -> binary 001_0000_0000
    low_prio = _DummyNode("LowPrio")     # ID 0x200 -> binary 010_0000_0000
    observer = _DummyNode("Observer")

    s_high = encode(Frame(0x100, b"\xAA"))
    s_low = encode(Frame(0x200, b"\x55"))

    res = resolve_collision([(high_prio, s_high), (low_prio, s_low)])
    assert res.winner is high_prio
    assert res.arb_losers == [low_prio]
    assert res.bit_error_losers == []
    assert res.resolved_at_field == "id"

    res = apply_collision_errors([high_prio, low_prio], res, [observer])
    assert res.aborted_by_active_error is False
    assert high_prio.estate.tec == 0
    assert low_prio.estate.tec == 0
    assert observer.estate.rec == 0


def test_sixteen_active_collisions_reach_error_passive() -> None:
    """Verify 16 active-error collisions drive both colliding nodes to TEC=128."""
    alice = _DummyNode("Alice")
    eve = _DummyNode("Eve")
    bob = _DummyNode("Bob")

    alice_stream = encode(Frame(0x00F, b"\x00" * 7))
    eve_stream = encode(Frame(0x00F, b"\xFF" * 7))

    for step in range(1, 17):
        res = resolve_collision([(alice, alice_stream), (eve, eve_stream)])
        res = apply_collision_errors([alice, eve], res, [bob])
        assert res.aborted_by_active_error is True, f"Expected active abort at step {step}"
        assert res.winner is None
        assert alice.estate.tec == step * 8
        assert eve.estate.tec == step * 8
        assert bob.estate.rec == step

    assert alice.estate.tec == 128
    assert eve.estate.tec == 128
    assert alice.estate.state is NodeState.ERROR_PASSIVE
    assert eve.estate.state is NodeState.ERROR_PASSIVE


def test_seventeenth_collision_breaks_symmetry() -> None:
    """Verify passive error flags let dominant Alice complete while Eve hits TEC=136."""
    alice = _DummyNode("Alice")
    eve = _DummyNode("Eve")
    bob = _DummyNode("Bob")

    # Bring both nodes to TEC=128 (ERROR_PASSIVE)
    alice.estate.tec = 128
    alice.estate._reclassify()
    eve.estate.tec = 128
    eve.estate._reclassify()
    assert alice.estate.state is NodeState.ERROR_PASSIVE
    assert eve.estate.state is NodeState.ERROR_PASSIVE

    alice_stream = encode(Frame(0x00F, b"\x00" * 7))
    eve_stream = encode(Frame(0x00F, b"\xFF" * 7))

    res17 = resolve_collision([(alice, alice_stream), (eve, eve_stream)])
    res17 = apply_collision_errors([alice, eve], res17, [bob])

    assert res17.aborted_by_active_error is False
    assert res17.winner is alice
    assert alice.estate.tec == 127
    assert alice.estate.state is NodeState.ERROR_ACTIVE
    assert eve.estate.tec == 136
    assert eve.estate.state is NodeState.ERROR_PASSIVE
    assert eve.estate.pending_passive_flag is True


def test_short_gaps_drive_bus_off_and_long_gap_escapes() -> None:
    """Verify <14 recessive bits cause form errors to bus-off, while >=14 clears flag."""
    # Case A: 15 consecutive short gaps (11 recessive bits < 14) -> 136 + 15*8 = 256 (BUS_OFF)
    eve_trapped = _DummyNode("EveTrapped")
    eve_trapped.estate.tec = 136
    eve_trapped.estate._reclassify()
    eve_trapped.estate.arm_passive_error_flag()

    for _ in range(15):
        form_err = eve_trapped.estate.consume_interframe_recessive_bits(11)
        assert form_err is True

    assert eve_trapped.estate.tec == 256
    assert eve_trapped.estate.state is NodeState.BUS_OFF
    assert eve_trapped.estate.pending_passive_flag is False

    # Case B: Wide gap (39 recessive bits >= 14, e.g. slow controller) -> flag completes & escapes
    eve_escaped = _DummyNode("EveEscaped")
    eve_escaped.estate.tec = 136
    eve_escaped.estate._reclassify()
    eve_escaped.estate.arm_passive_error_flag()

    form_err_long = eve_escaped.estate.consume_interframe_recessive_bits(39)
    assert form_err_long is False
    assert eve_escaped.estate.pending_passive_flag is False
    assert eve_escaped.estate.tec == 136
    assert eve_escaped.estate.state is NodeState.ERROR_PASSIVE


def test_collision_counter_edge_cases() -> None:
    """Verify arb-only losers, observers, and already bus-off nodes."""
    winner = _DummyNode("Winner")        # ID 0x00F, data 0x00
    bit_loser = _DummyNode("BitLoser")   # ID 0x00F, data 0xFF
    arb_loser = _DummyNode("ArbLoser")   # ID 0x200 (loses in ID field)
    observer = _DummyNode("Observer")
    dead_node = _DummyNode("DeadNode", bus_off=True)
    dead_node.estate.tec = 256
    dead_node.estate._reclassify()

    s_win = encode(Frame(0x00F, b"\x00"))
    s_bit = encode(Frame(0x00F, b"\xFF"))
    s_arb = encode(Frame(0x200, b"\x55"))

    contenders = [winner, bit_loser, arb_loser]
    res = resolve_collision([(winner, s_win), (bit_loser, s_bit), (arb_loser, s_arb)])
    res = apply_collision_errors(contenders, res, [observer, dead_node])

    assert res.aborted_by_active_error is True
    assert winner.estate.tec == 8
    assert bit_loser.estate.tec == 8
    assert arb_loser.estate.tec == 0     # arbitration loser takes no error
    assert observer.estate.rec == 1      # active observer increments REC
    assert dead_node.estate.rec == 0     # bus-off node does not update counters


def test_bus_integration_smoke() -> None:
    """Verify Bus, EcuNode, and Attacker integrate cleanly without CollisionModel."""
    from parrot_sim.simulator import Attacker, Bus, EcuNode

    owner = EcuNode("ECU_0x120", 0x120, period_bits=2000, parrot=True)
    atk = Attacker("ATTACKER", 0x120, inject_period_bits=1500, phase=500)
    bus = Bus([owner, atk])
    log = bus.run(duration_bits=50_000)

    assert len(log) > 0
    assert atk.estate.tec > 0




def run_all() -> None:
    tests = [
        test_frame_encoding_and_bit_stuffing,
        test_arbitration_lower_id_wins_without_bit_error,
        test_sixteen_active_collisions_reach_error_passive,
        test_seventeenth_collision_breaks_symmetry,
        test_short_gaps_drive_bus_off_and_long_gap_escapes,
        test_collision_counter_edge_cases,
        test_bus_integration_smoke
    ]
    for fn in tests:
        fn()
        print(f"[PASS] {fn.__name__}")
    print(f"\nAll {len(tests)} selftests passed.")


if __name__ == "__main__":
    run_all()