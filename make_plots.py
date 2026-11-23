"""
make_plots.py -- renders the report figures from out/*.csv into out/*.png.
Run experiments.py first, then:  python3 make_plots.py
"""
import csv, json, os
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

OUT = os.path.join(os.path.dirname(__file__), "out")

# colourblind-safe, consistent across every figure
C_ATK = "#D55E00"     # attacker  (orange/red)
C_DEF = "#0072B2"     # defender/owner (blue)
C_BASE = "#999999"    # baseline / no-defense (grey)
C_THRESH = "#B00020"  # bus-off threshold

plt.rcParams.update({
    "figure.dpi": 130, "font.size": 11, "axes.grid": True,
    "grid.alpha": 0.3, "axes.spines.top": False, "axes.spines.right": False,
})


def load(path):
    with open(path) as f:
        return list(csv.DictReader(f))


def fnum(rows, key):
    out = []
    for r in rows:
        v = r.get(key, "")
        out.append(float(v) if v not in ("", None) else None)
    return out


# --- Fig 1: attacker TEC -- Parrot drives it to bus-off, baseline does not ---
p = load(f"{OUT}/trace_parrot.csv")
b = load(f"{OUT}/trace_baseline.csv")
fig, ax = plt.subplots(figsize=(7.2, 4.2))
ax.plot(fnum(b, "t_ms"), fnum(b, "attacker_tec"), color=C_BASE, lw=2,
        label="No defense (attacker healthy, spoofs land)")
ax.plot(fnum(p, "t_ms"), fnum(p, "attacker_tec"), color=C_ATK, lw=2.2,
        label="Parrot ON (attacker forced to bus-off)")
ax.axhline(255, color=C_THRESH, ls="--", lw=1.3, label="bus-off threshold (TEC>255)")
ax.axhline(127, color="#888", ls=":", lw=1, label="error-passive (TEC>127)")
ax.set_xlabel("time (ms)"); ax.set_ylabel("Attacker Transmit Error Counter (TEC)")
ax.set_title("Attacker TEC over time: Parrot vs no defense")
ax.legend(fontsize=9, loc="center right")
fig.tight_layout(); fig.savefig(f"{OUT}/fig1_attacker_tec.png"); plt.close(fig)

# --- Fig 2: defender collateral -- Parrot's real cost to the owner ----------
summ = json.load(open(f"{OUT}/summary.json"))
cost = summ["E3_defender_cost"]
fig, ax = plt.subplots(figsize=(7.2, 4.2))
ax.plot(fnum(p, "t_ms"), fnum(p, "owner_tec"), color=C_DEF, lw=2.2,
        label="Defender (owner) TEC under Parrot")
ax.plot(fnum(p, "t_ms"), fnum(p, "attacker_tec"), color=C_ATK, lw=1.4,
        alpha=0.6, label="Attacker TEC (for reference)")
ax.axhline(127, color="#888", ls=":", lw=1, label="error-passive (TEC>127)")
ax.annotate(f"peak {cost['owner_peak_tec']}",
            (cost["owner_peak_t_ms"], cost["owner_peak_tec"]),
            textcoords="offset points", xytext=(10, 10), fontsize=9, color=C_DEF,
            arrowprops=dict(arrowstyle="-", color=C_DEF, lw=0.8))
ax.set_xlabel("time (ms)"); ax.set_ylabel("Transmit Error Counter (TEC)")
ax.set_title("Cost to the defender: TEC spikes, stays error-active, recovers")
ax.legend(fontsize=9, loc="center right", bbox_to_anchor=(1.0, 0.72))
fig.tight_layout(); fig.savefig(f"{OUT}/fig2_defender_collateral.png"); plt.close(fig)

# --- Fig 3: time-to-bus-off vs injection rate -------------------------------
s = load(f"{OUT}/sweep.csv")
rate = [float(r["inject_rate_hz"]) for r in s]
ttb = [float(r["time_to_busoff_ms"]) for r in s]
fig, ax = plt.subplots(figsize=(7.2, 4.2))
ax.plot(rate, ttb, "o-", color=C_ATK, lw=2, ms=6)
ax.set_xlabel("attacker injection rate (frames/s)")
ax.set_ylabel("time to attacker bus-off (ms)")
ax.set_title("Parrot: faster the attacker injects, faster it is ejected")
for x, y in zip(rate, ttb):
    ax.annotate(f"{y:.0f}", (x, y), textcoords="offset points",
                xytext=(0, 7), fontsize=8, ha="center")
fig.tight_layout(); fig.savefig(f"{OUT}/fig3_time_to_busoff.png"); plt.close(fig)

# --- Fig 4: spoofs that reached the bus -------------------------------------
landed = [summ["E1_baseline"]["spoofs_landed"], summ["E2_parrot"]["spoofs_landed"]]
fig, ax = plt.subplots(figsize=(5.4, 4.2))
bars = ax.bar(["No defense", "Parrot ON"], landed, color=[C_BASE, C_DEF], width=0.55)
ax.bar_label(bars, padding=3)
ax.set_ylabel("spoofed frames that reached the bus")
ax.set_title("Spoof frames delivered to the bus")
fig.tight_layout(); fig.savefig(f"{OUT}/fig4_spoof_success.png"); plt.close(fig)

print("wrote:")
for f in sorted(os.listdir(OUT)):
    if f.endswith(".png"):
        print("  out/" + f)
