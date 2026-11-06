# Report draft — maps to "Project Template.docx"

Fill the template's four sections with the text below. Keep the whole thing
≤10 pages. Replace the bracketed notes, and reconcile the one modeling knob
(error-counter deltas) with the Parrot paper before final submission.

---

## Title of the reference paper
Parrot, a software-only anti-spoofing defense system for the CAN bus.

## Name of who is involved in the project
Jose Rivera and Daniel Carrasco Moreno.

## Objectives
We implement and evaluate **Parrot**, a software-only defense that ejects a
spoofing node from the CAN bus by exploiting CAN's own error-handling. Because
the defense lives entirely in the data-link layer (arbitration, bit monitoring,
error counters, bus-off) — which SocketCAN's `vcan0` does not model — we built a
**bit-level CAN simulator** as our testbed. Concretely we set out to:
1. reproduce a CAN masquerade/spoofing attack on an unauthenticated bus;
2. implement Parrot's detect-and-contend reaction;
3. measure whether and how fast it forces the attacker to **bus-off**, at what
   cost to the defender, and with what false-positive behaviour.

## System Setup
Custom-built, so described here (per the template's instructions).

- **Simulator (`can_core.py`, `simulator.py`).** A discrete, bit-time model of a
  single CAN segment at 500 kbit/s, standard 11-bit data frames. It implements:
  the wired-AND bus (dominant `0` wins over recessive `1`); full frame encoding
  (SOF, ID, RTR, control, data, CRC-15 poly `0x4599`, delimiters, ACK, EOF) with
  **bit stuffing**; **bit-wise arbitration**; **bit monitoring** (a transmitter
  that drives recessive but reads dominant loses arbitration in the arbitration
  field, or takes a **bit error** after it); and the per-node error-counter state
  machine — `TEC += 8` on transmit error, `TEC -= 1` on success, thresholds
  `TEC>127 → error-passive`, `TEC>255 → bus-off` (ISO 11898-1).
- **Nodes.** `EcuNode` (owns an ID, transmits periodically; `parrot=True` adds
  the defense); `Attacker` (injects spoofed frames with a victim's ID at a fixed
  rate).
- **Parrot reaction.** When a defended owner observes its own ID being
  transmitted by another node, it contends in the same bus slot with an
  all-dominant payload for that ID.
- **Validation.** `selftest.py` asserts 13 properties of the layer (bus-off after
  exactly 32 transmit errors; error-passive at TEC=128; stuffing never allows 6
  equal bits; lower ID wins arbitration; same-ID all-dominant owner beats a
  spoofer in the data field; end-to-end attacker bus-off). All pass.
- Well-known tools: Python 3 + `matplotlib` (plots only).

## Experiments
Code: https://github.com/sournachos/parrot — `experiments.py` runs every
scenario and writes `out/*.csv` + `out/summary.json`; `make_plots.py` renders
the figures.

- **E1 — Baseline, no defense.** Owner `0x120` periodic; attacker spoofs `0x120`.
- **E2 — Parrot ON.** Same, defense enabled.
- **E3 — Naive mirror.** The owner echoes but *both* colliding transmitters take
  the error (`CollisionModel mode="strict_both"`) — to show why a naive echo is
  not enough.
- **E4 — Injection-rate sweep.** Time-to-bus-off vs attacker rate (83–1250 fps).
- **E5 — False positives.** Three legitimate ECUs, Parrot ON, no attacker.

## Results and Discussion
*(insert fig1–fig4 from `out/`)*

- **Attack works without the defense (E1).** 100 spoofed frames reach the bus;
  the attacker stays healthy (never bus-off). This is the expected consequence of
  CAN having no source authentication — the ICSim replay lab in the folder is the
  same property seen as replay.
- **Parrot ejects the attacker (E2, fig1).** The attacker's TEC climbs by 8 per
  contested injection and crosses the bus-off threshold after exactly
  **32 injections ≈ 94 ms**; **0** spoofed frames reach the bus. `ceil(256/8)=32`
  matches the theory exactly.
- **Parrot protects the defender; naive mirroring does not (fig2).** Under Parrot
  the defender's TEC stays ~0 (it wins every exchange, each a successful
  transmit). Under a naive mirror the defender is dragged to ~240 TEC
  (error-passive, near bus-off) as collateral damage. This is the design point of
  Parrot and a good discussion of *why* the asymmetry matters.
- **Rate scaling (E4, fig3).** Time-to-bus-off ≈ 32 / injection-rate, so a more
  aggressive attacker is ejected *faster* — the defense scales against its worst
  case. Injections-to-bus-off is invariant at 32.
- **No false positives (E5).** With legitimate-only traffic and Parrot active on
  every ECU, no node is ever bus-off and all TECs stay 0 — Parrot reacts only to
  a foreign frame on an ID it owns.

**Why replay/spoofing worked at all:** no authentication, no source address, no
freshness — identical to the discussion questions in the ICSim lab. A MAC with a
freshness value (AUTOSAR SecOC) would defeat the spoof outright but adds per-frame
computation and payload overhead; Parrot instead needs no bus changes, which is
its appeal and the reason it is "software-only."

**Limitations / critical evaluation (the graded part).**
- Error frames are modeled by their counter effect, not emitted bit-for-bit.
- The per-collision TEC bookkeeping is an explicit, tunable model
  (`CollisionModel`); **the exact deltas must be reconciled with the paper's
  error-handling analysis.** We state this openly and expose it as one parameter.
- Event-level timing, not analog waveforms — fine for bus-off dynamics, not for
  physical-layer attacks.
- Per the course FAQ, these are testbed-quality limitations, not bugs: the
  mechanism, thresholds, and scaling all match first-principles CAN behaviour.
