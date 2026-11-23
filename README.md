# Parrot — software-only anti-spoofing defense for the CAN bus (bit-level simulation)

Course project for **Cyber-Physical Systems and IoT Security**.
Reference paper: *Parrot, a software-only anti-spoofing defense system for the CAN bus.*
Authors: Jose Rivera and Daniel Carrasco Moreno.
Repo: <https://github.com/sournachos/parrot>

## What this is

A **bit-level, data-link-layer CAN simulator** written from scratch in Python,
used to implement and evaluate the Parrot defense. It reproduces the part of CAN
that the defense actually depends on and that the ICSim lab's `vcan0` does **not**
model: bit-wise arbitration, bit monitoring, bit stuffing, the Transmit/Receive
Error Counters (TEC/REC) and the `error-active → error-passive → bus-off` state
machine.

> Why not `vcan0`? SocketCAN's virtual bus only forwards frames. It has no
> arbitration, no error counters and no bus-off, so Parrot's mechanism is
> literally invisible on it. Demonstrating Parrot requires simulating the
> data-link layer — which is what this project does.

## The mechanism in one paragraph

CAN has no authentication: any node can inject a frame with another ECU's ID
(spoofing). Parrot runs on the legitimate owner of an ID. When it sees a frame
carrying **its own ID that it did not send**, it reacts by contending for that
ID with an all-dominant payload. Because the bus is wired-AND (dominant 0 wins)
and the owner wins every data-bit contention, the spoofer reads back a dominant
bit where it drove a recessive one → **bit error** → `TEC += 8`. After
`ceil(256/8) = 32` contested injections the attacker crosses `TEC > 255` and the
CAN controller forces it **bus-off** — it removes itself from the bus. The
defender, winning each exchange, keeps its own TEC near zero.

## Files

```
parrot_sim/
  can_core.py     bus wired-AND, frame encoding + CRC15 + bit stuffing,
                  TEC/REC state machine, collision resolver
  simulator.py    EcuNode / Attacker nodes + bus scheduler (+ Parrot reaction)
selftest.py       8 tests that the layer behaves like CAN (run this first)
experiments.py    runs all scenarios, writes out/*.csv and out/summary.json
make_plots.py     renders out/*.png from the CSVs
out/              generated results (CSV, JSON, PNG)
```

## Run it

```bash
python3 selftest.py       # all 8 must PASS before trusting any result
python3 experiments.py    # writes out/summary.json + out/*.csv
python3 make_plots.py     # writes out/fig1..4.png
```
No third-party dependencies except `matplotlib` (plots only). Python 3.10+.

## Results (500 kbit/s, standard 11-bit frames)

| Scenario | Defense | Spoofs that reached the bus | Attacker | Defender |
|---|---|---|---|---|
| E1 baseline | none | **100** land | healthy | — |
| E2 Parrot | Parrot | **0** land | **bus-off @ ~94 ms** (32 injections) | never error-passive |
| E3 defender cost | Parrot (E2 run) | — | — | **TEC peaks at 121 @ 46 ms, stays error-active, recovers to 21** |
| E5 false-positive | Parrot, no attacker | — | — | no node bus-off, max TEC = 0 |

- **fig1** attacker TEC vs time — Parrot vs no defense
- **fig2** defender TEC vs time under Parrot — the cost of active error flags
- **fig3** time-to-bus-off vs injection rate (≈ 32 / rate)
- **fig4** spoofed frames delivered: 100 → 0

## Honest limitations (discuss in the report)

1. **Error frames are not emitted bit-for-bit** — the TEC/REC deltas they would
   produce are applied directly. Standard testbed simplification; does not change
   the counter dynamics that cause bus-off.
2. **Per-collision TEC bookkeeping follows ISO 11898-1 error flags**
   (`apply_collision_errors` in `can_core.py`). While the attacker is
   error-active its Active Error Flag aborts the frame and *both* transmitters
   take TEC += 8; once it is error-passive its flag is recessive, the owner's
   frame completes and only the attacker is charged. An earlier version used a
   hand-tuned `CollisionModel` that assumed the owner was never charged; the
   ISO model shows Parrot's real cost to the defender (E3, fig2).
3. Timing is event-level (frame + IFS in bit-times), not analog CAN-H/CAN-L
   waveforms. Appropriate for counter/bus-off dynamics, not for physical-layer
   effects.

See `REPORT_DRAFT.md` for text mapped to the course report template.
