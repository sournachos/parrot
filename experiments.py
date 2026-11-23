"""
experiments.py -- runs the scenarios and writes CSV/JSON into out/.
Run:  python3 experiments.py

Scenarios
  E1 baseline  : legitimate ECU + attacker, NO defense   -> spoofs land
  E2 parrot    : same, Parrot defense ON                 -> attacker -> bus-off
  E3 cost      : defender's own TEC under Parrot (E2 run)  -> spikes, stays
                 error-active, recovers
  E4 sweep     : time-to-bus-off vs injection rate
  E5 falsepos  : legit-only bus, Parrot ON, no attacker  -> nobody bus-off
"""
import csv, json, os
from parrot_sim.can_core import NodeState
from parrot_sim.simulator import EcuNode, Attacker, Bus

OUT = os.path.join(os.path.dirname(__file__), "out")
os.makedirs(OUT, exist_ok=True)
BITRATE = 500_000
BIT_US = 1e6 / BITRATE


def trace_rows(bus, owner_name, attacker_name):
    rows = []
    for s in bus.log:
        o = s.counters.get(owner_name)
        a = s.counters.get(attacker_name)
        rows.append({
            "t_ms": round(s.t_ms, 4),
            "owner_tec": o[0] if o else "",
            "owner_state": o[2] if o else "",
            "attacker_tec": a[0] if a else "",
            "attacker_state": a[2] if a else "",
            "spoof_landed": int(s.spoof_landed),
        })
    return rows


def write_csv(path, rows, header):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=header)
        w.writeheader()
        w.writerows(rows)


def time_to_busoff_ms(bus, name):
    for s in bus.log:
        c = s.counters.get(name)
        if c and c[2] == NodeState.BUS_OFF.value:
            return s.t_ms
    return None


summary = {"bitrate_bps": BITRATE}
HEADER = ["t_ms", "owner_tec", "owner_state", "attacker_tec",
          "attacker_state", "spoof_landed"]

# ---- E1 baseline: no defense ----------------------------------------------
owner = EcuNode("ECU_0x120", 0x120, period_bits=2000, parrot=False)
atk = Attacker("ATTACKER", 0x120, inject_period_bits=1500, phase=500)
bus = Bus([owner, atk], BITRATE)
bus.run(200_000)
write_csv(f"{OUT}/trace_baseline.csv", trace_rows(bus, owner.name, atk.name), HEADER)
summary["E1_baseline"] = {
    "defense": "none",
    "spoofs_landed": bus.spoof_success,
    "spoofs_suppressed": bus.spoof_suppressed,
    "attacker_busoff": atk.bus_off,
    "attacker_final_tec": atk.estate.tec,
}

# ---- E2 parrot: defense ON -------------------------------------------------
owner = EcuNode("ECU_0x120", 0x120, period_bits=2000, parrot=True)
atk = Attacker("ATTACKER", 0x120, inject_period_bits=1500, phase=500)
bus = Bus([owner, atk], BITRATE)
bus.run(200_000)
write_csv(f"{OUT}/trace_parrot.csv", trace_rows(bus, owner.name, atk.name), HEADER)
summary["E2_parrot"] = {
    "defense": "parrot",
    "spoofs_landed": bus.spoof_success,
    "spoofs_suppressed": bus.spoof_suppressed,
    "attacker_busoff": atk.bus_off,
    "owner_busoff": owner.bus_off,
    "time_to_attacker_busoff_ms": round(time_to_busoff_ms(bus, atk.name) or -1, 3),
    "injections_until_busoff": atk.injections,
}

# ---- E3 defender cost: what Parrot costs the owner (same run as E2) --------
# Under ISO 11898-1 rules, while the attacker is still error-active every
# collision is aborted by its Active Error Flag and BOTH transmitters take
# TEC += 8. Once the attacker turns error-passive its flag is recessive, the
# owner's frame completes (TEC -= 1) and only the attacker keeps climbing.
owner_tec = [(s.t_ms, s.counters[owner.name][0], s.counters[owner.name][2])
             for s in bus.log if owner.name in s.counters]
peak_t, peak_tec, _ = max(owner_tec, key=lambda r: r[1])
summary["E3_defender_cost"] = {
    "defense": "parrot",
    "owner_peak_tec": peak_tec,
    "owner_peak_t_ms": round(peak_t, 3),
    "owner_final_tec": owner.estate.tec,
    "owner_final_state": owner.estate.state.value,
    "owner_ever_error_passive": any(st != NodeState.ERROR_ACTIVE.value
                                    for _, _, st in owner_tec),
    "owner_busoff": owner.bus_off,
    "note": ("while the attacker is error-active, Active Error Flags abort "
             "every collision and charge the owner too; the owner's TEC "
             "spikes but stays below the error-passive threshold, then "
             "decays once the attacker is error-passive / bus-off"),
}

# ---- E4 sweep: time-to-bus-off vs injection rate ---------------------------
sweep = []
for period in [400, 700, 1000, 1500, 2500, 4000, 6000]:
    o = EcuNode("ECU_0x120", 0x120, period_bits=2000, parrot=True)
    a = Attacker("ATTACKER", 0x120, inject_period_bits=period, phase=200)
    b = Bus([o, a], BITRATE)
    b.run(600_000)
    rate_hz = 1.0 / (period * BIT_US * 1e-6)
    sweep.append({
        "inject_period_bits": period,
        "inject_rate_hz": round(rate_hz, 1),
        "time_to_busoff_ms": round(time_to_busoff_ms(b, a.name) or -1, 3),
        "injections_until_busoff": a.injections,
    })
write_csv(f"{OUT}/sweep.csv", sweep,
          ["inject_period_bits", "inject_rate_hz",
           "time_to_busoff_ms", "injections_until_busoff"])
summary["E4_sweep"] = sweep

# ---- E5 false positives: legit-only bus, Parrot ON, no attacker ------------
e0 = EcuNode("ECU_0x120", 0x120, period_bits=2000, phase=0, parrot=True)
e1 = EcuNode("ECU_0x200", 0x200, period_bits=2600, phase=300, parrot=True)
e2 = EcuNode("ECU_0x300", 0x300, period_bits=3100, phase=700, parrot=True)
bus = Bus([e0, e1, e2], BITRATE)
bus.run(300_000)
summary["E5_false_positive"] = {
    "scenario": "3 legit ECUs, Parrot ON, no attacker",
    "any_busoff": any(n.bus_off for n in [e0, e1, e2]),
    "max_tec": max(n.estate.tec for n in [e0, e1, e2]),
    "spoofs_detected": bus.spoof_success + bus.spoof_suppressed,
}

with open(f"{OUT}/summary.json", "w") as f:
    json.dump(summary, f, indent=2)

print(json.dumps(summary, indent=2))
