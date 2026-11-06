"""
selftest.py -- sanity checks that the data-link layer behaves like CAN before
we trust any Parrot result. Run:  python3 selftest.py
"""
from parrot_sim.can_core import (
    Frame, ErrorState, NodeState, CollisionModel, resolve_collision, encode,
    TEC_BUSOFF_THRESHOLD,
)
from parrot_sim.simulator import EcuNode, Attacker, Bus

PASS, FAIL = "PASS", "FAIL"
results = []


def check(name, cond):
    results.append((name, PASS if cond else FAIL))


# 1) Error-counter -> bus-off at TEC > 255 (8 per transmit error => 32 errors)
es = ErrorState()
n = 0
while es.state is not NodeState.BUS_OFF and n < 1000:
    es.transmit_error()
    n += 1
check("bus-off reached", es.state is NodeState.BUS_OFF)
check("bus-off after 32 transmit errors", n == 32)
check("TEC past threshold at bus-off", es.tec > TEC_BUSOFF_THRESHOLD)

# 2) error-passive crossing at TEC > 127
es2 = ErrorState()
for _ in range(16):          # 16*8 = 128 > 127
    es2.transmit_error()
check("error-passive at TEC=128", es2.state is NodeState.ERROR_PASSIVE)

# 3) bit stuffing: never 6 identical bits in the stuffed monitored region
bits = encode(Frame(0x100, b"\x00\x00\x00\x00"))  # all-zero data stresses stuffing
mon = [b for (b, f) in bits if f not in ("crcdelim", "ack", "ackdelim", "eof")]
run, worst, prev = 0, 0, None
for b in mon:
    run = run + 1 if b == prev else 1
    prev = b
    worst = max(worst, run)
check("no 6 consecutive equal bits (stuffing works)", worst <= 5)

# 4) arbitration: lower ID wins, neither errors
a = EcuNode("ecu_lo", 0x100)
b = EcuNode("ecu_hi", 0x200)
res = resolve_collision([(a, encode(a.frame())), (b, encode(b.frame()))],
                        CollisionModel())
check("lower id wins arbitration", res.winner is a)
check("arbitration loss is NOT a bit error", b in res.arb_losers and not res.bit_error_losers)

# 5) same-id collision: all-dominant owner beats spoofer -> spoofer bit error
owner = EcuNode("owner", 0x120, parrot=True)
atk = Attacker("atk", 0x120, spoof_payload=b"\xDE\xAD\xBE\xEF")
res = resolve_collision([(atk, encode(atk.frame())),
                         (owner, encode(owner.parrot_frame()))], CollisionModel())
check("owner wins its own id", res.winner is owner)
check("spoofer takes the bit error", atk in res.bit_error_losers)
check("contention resolved in data field", res.resolved_at_field == "data")

# 6) end-to-end: with Parrot, attacker is driven bus-off and spoof is suppressed
owner = EcuNode("ECU_0x120", 0x120, period_bits=2000, parrot=True)
atk = Attacker("ATTACKER", 0x120, inject_period_bits=1500, phase=500)
bus = Bus([owner, atk], collision_model=CollisionModel(mode="parrot"))
bus.run(duration_bits=200_000)
check("attacker ends bus-off (Parrot on)", atk.bus_off)
check("owner survives (Parrot on)", not owner.bus_off)
check("no spoof landed (Parrot on)", bus.spoof_success == 0)

print(f"{'SELF-TEST':<48}RESULT")
print("-" * 56)
ok = True
for name, r in results:
    print(f"{name:<48}{r}")
    ok = ok and r == PASS
print("-" * 56)
print("ALL PASS" if ok else "SOME CHECKS FAILED")
raise SystemExit(0 if ok else 1)
