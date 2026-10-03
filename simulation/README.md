# Anheart machine simulation

A standalone simulation and scenario-test framework for the Anheart centrifuge
training machine. It drives the **real** runtime from `raspberry-pi/src`
against the **real** simulators there, records everything the variateur
receives, checks physical invariants on every trace, and plays the result back
in a 2D top-down view.

Nothing here re-implements the machine. The physics and the control all come
from `raspberry-pi/src`:

| role | production code used |
|---|---|
| control law, safety supervisor, motion profiler, every exit path | `training/runtime.py` (`TrainingRuntime`) |
| the ATV320 variateur (CiA402, ramps, coast-down, ttO watchdog) | `motor/simulated.py` (`SimulatedDrive`) |
| the rider's heart (g-driven, lag, drift, scripted events) | `sim/physiology.py` (`Physiology`) |
| ECG, BITalino, DSP (in `dsp` mode) | `sim/bitalino.py`, `sim/ecg.py`, `signal_processing.py` via `ecg_pipeline.EcgBridge` |
| geometry and every unit conversion | `geometry.py`, `units.py` |
| programmes, profiles, anti-nausea limits | `training/plan.py`, `config/profiles.default.json`, `config/motion_limits.json` |

`raspberry-pi/` is only read. The simulation adds the loop, the scenario
actions, a recording wrapper around the drive, a fast heart-rate sensor model,
the invariant checker and the viewer.

```
simulation/
  cad/extract_geometry.py     STEP -> cad/machine_geometry.json (OCP, own venv)
  cad/machine_geometry.json   the extracted geometry, with provenance
  rig.py                      CAD geometry + rider parameters (reference and leg-tip radius)
  scenario.py  jsondoc.py     scenarios as data (JSON), strict parser
  harness.py                  Layer 1: the loop (runtime + SimulatedDrive + physiology + ECG)
  recording.py                DriveBackend wrapper that logs every frame sent to the drive
  sensors.py                  DIRECT heart-rate sensor model (same output contract as the DSP)
  invariants.py               physical invariants, scenario expectations, metrics
  tracefile.py                JSONL / CSV traces
  run.py                      CLI: python -m simulation.run
  live.py                     2D viewer server + live streaming (stdlib only)
  viewer/index.html           Layer 2: the 2D view (single static file)
  scenarios/*.json            the battery (scenarios/_profiles.json: extra profiles)
  faultdrive.py               DriveBackend wrapper: refused command words, echo mismatch, stuck RFRD, frozen status
  faultsource.py              BITalino sample corruption before the REAL DSP (flat, saturated, noise, hum, stops, gaps)
  failures.py                 the failure-injection matrix (199 cases) and its judge
  cohort/generate.py          the seeded 30-subject cohort generator -> cohort/cohort.json
  cohort/battery.py           every subject x {auto jog, auto standard 30 min, manual 27 rpm bench}
  quick.py                    CLI: python -m simulation.quick (instant verdicts + report.html/json)
  SESSION_TESTS.md            proposed acceptance tests per session type, and which are implemented
  tests/                      pytest + hypothesis
  scripts/check.sh            the gate (ruff, basedpyright strict, mypy strict, tests, 100% coverage)
```

## Setup

The simulation uses the raspberry-pi venv (same interpreter and tool versions
as the code under test). Create it once as `raspberry-pi/README.md` describes.
Everything below runs from the repository root, with both the root and
`raspberry-pi/` importable:

```sh
export PYTHONPATH=.:raspberry-pi
PY=raspberry-pi/.venv/bin/python
```

## The geometry, from the CAD

```sh
cd simulation/cad
uv venv --python 3.12 .venv-cad && uv pip install --python .venv-cad/bin/python -r requirements.txt
.venv-cad/bin/python extract_geometry.py ../../CAO/Gaura_Assy_2907.STEP machine_geometry.json
```

The extractor reads the STEP through OCCT's XCAF reader (every part with its
assembly placement and product name), computes world bounding boxes, and
ray-casts the capsule solid to find its inner walls. `machine_geometry.json`
records, for every number, the part it came from, plus a confidence note. A
test re-runs the extractor and checks the committed JSON is reproduced exactly
(skipped when the CAD venv or the STEP file is absent).

What the CAD says (millimetres in the file, metres below):

| quantity | value | from | confidence |
|---|---|---|---|
| rotation axis | vertical (+Y), through (0, 0) | shaft `AXE KZBF45`, coaxial with both 6009 bearings, the 81209 thrust bearing, the brake disc and the spacer (0.000 mm offset) | high |
| arm, outboard tip | 1.840 m | two `Profile Polyester` beams, Z -1160..1840 mm (named "4000mm", modelled 3000 mm: check which is built) | high |
| arm, inboard tip / counterweight centroid | 1.160 m / 0.936 m | same beams / `Support Poids` | high |
| support wheels | 1.115 m and 1.795 m | `CGZJ50-N` | high |
| capsule, inner far (outboard) wall | 2.378 m at the floor, 2.425 m max | `Human_Capsule_v2`, ray casts at 5..195 mm above its floor | high |
| capsule, inner near wall | 0.328 m, on the far side of the axis | same | high |
| occupant posture | lying along the arm, head inboard (under the canopy), feet outboard | inferred from the capsule shape and the `Human_Capsule_Top` canopy position | low-medium |
| rider | **not modelled in the CAD** | no body/mannequin product in the file | - |

Because there is no rider in the CAD, the two radii the simulation needs are
**parameters**, both flagged `must_be_measured`:

* **leg-tip radius** (the farthest point of the rider, where g is highest):
  default **2.4254 m**, the capsule's inner far wall, an *upper bound*
  (feet against the wall). A stature-based estimate is also recorded:
  head crown against the near wall plus an assumed 1.75 m = **1.42 m**.
  Scenarios can override it (`"geometry": {"leg_tip_radius_m": ...}`).
* **reference radius** (`ARM_RADIUS_M`, where the runtime quotes g and applies
  the g-dot limit): **1.5 m**, the value raspberry-pi's tests and console use.
  It does not come from the CAD (the occupant lies down; there is no seat).

g-load (centripetal) at the two speeds asked about:

| output rpm | motor rpm | drive Hz | g at 1.5 m (reference) | g at 2.4254 m (leg tip, CAD bound) | g at 1.42 m (leg tip, stature estimate) |
|---|---|---|---|---|---|
| 27.0 | 1344 | 48.7 | 1.223 | 1.977 | 1.159 |
| 27.7 (nameplate) | 1380 | 50.0 | 1.289 | 2.083 | 1.221 |
| 32.0 | 1593 | 57.7 | 1.718 | 2.777 | 1.628 |

32 output rpm needs 57.7 Hz, above the motor nameplate (1380 rpm at 50 Hz),
above `MOTOR_MAX_RPM`'s ceiling and above the 50 Hz HSP the runtime arms
against. The runtime refuses it, which the battery asserts as the correct
behaviour (`manual_32_rpm_refused`, `manual_27_then_32_refused`, and a start
with a 1593 rpm ceiling is refused `PlanUnusable`).

## Layer 1: the variateur input simulation

`simulation.harness.Session` wires, per scenario:

* `TrainingRuntime` with the console's `RUNTIME_LIMITS`, the shipped motion
  limits, and the profile's (or the console's) cardiac tiers; a test asserts
  these equal `src.local_panel`'s own constants;
* `SimulatedDrive` behind `RecordingDrive`: every command word, speed setpoint,
  emergency zero, open and close is logged with its instant and whether the
  drive acknowledged it;
* `Physiology` fed the measured speed, as `LocalPanel.control_step` does;
* the ECG: `direct` (fast sensor model: 1 Hz readings, advancing seq, rate only
  when GOOD; dropouts, quality, repeated seq, forced values) or `dsp` (the
  production path, ~50 ms of CPU per simulated second).

The loop runs a 15 s pre-roll (the idle console, read-only), the start
command, the scenario horizon, then exits like the console (`shutdown`), lets
the plant run on for the teardown window with nobody ticking, and finally
probes the drive over a fresh link. The battery runs on `ManualClock`
(deterministic); the live viewer runs the same loop paced in real time.

A cross-check test builds the real composition root
(`src.local_panel.build_panel`) and shows a manual climb to 27 rpm walks through
exactly the same setpoints as the harness.

### Invariants checked on every trace

* no NaN or infinity anywhere;
* setpoint in `{0} U [min_run, ceiling]`; every LFRD **written to the drive** in
  the same domain; measured speed within HSP; no reverse rotation;
* setpoint rises and falls within what the runtime promises (programme: the
  15 rpm/s slew over the time since the last change; manual: the motion
  profiler's rate at the faster end, +1 carried rpm), the emergency zero excepted;
* the measured shaft never beats the drive's own ramp;
* a verdict at FREEZE or above: the setpoint never rises; once an ending began,
  it never rises; during a scripted vasovagal collapse (+60 s), it never rises;
* manual sessions: g-dot at the reference radius within 0.03 g/s, and the
  measured arm within 0.25 output rpm/s and 0.03 g/s over 1 s windows;
* after going silent, not one frame, reads included;
* a refused start never moved anything;
* **every exit path**: after teardown the drive produces no torque, the shaft
  reads 0 rpm, and (where a frame could be delivered) LFRD is 0 and the runtime
  commands nothing.

## Running

```sh
simulation/scripts/check.sh                      # the gate: lint, both type checkers, battery, 100% coverage
$PY -m pytest simulation/tests -q                # the battery only (~15 min; the dsp scenarios dominate)
$PY -m pytest simulation/tests/test_cohort.py -q     # the cohort battery (~30 s, every CPU)
$PY -m pytest simulation/tests/test_failures.py -q   # the failure matrix (~3 min, 7 real-DSP cases)
$PY -m simulation.cohort.generate [--check]      # regenerate (or verify) cohort/cohort.json
$PY -m simulation.run --list                     # the scenarios
$PY -m simulation.run manual_27_rpm --csv        # one scenario -> simulation/out/manual_27_rpm.{jsonl,csv}
$PY -m simulation.run --all                      # every scenario + simulation/out/summary.md
```

`run` prints the start/end, peak speed, peak g at both radii, peak setpoint
rate, peak measured arm acceleration and g-dot, time in zone, the safety rules
seen, and every violated invariant or expectation. Exit code 0 means every
invariant and expectation held (a `known_defect` scenario that fails as
documented counts as expected; one that passes asks for the key to be removed).

## Instant results: `python -m simulation.quick`

```sh
$PY -m simulation.quick manual_27_rpm             # a scenario file, by name
$PY -m simulation.quick drive_fault_overcurrent_manual   # one failure case
$PY -m simulation.quick S07                       # one cohort subject, its three sessions
$PY -m simulation.quick --cohort                  # 30 subjects x 3 sessions
$PY -m simulation.quick --failures [--dsp]        # the failure matrix (DSP cases need --dsp)
$PY -m simulation.quick --all --dsp               # scenarios + failures + cohort
```

The fast path: the DIRECT sensor model (a `dsp` scenario runs in `direct` mode
unless `--dsp`; one whose actions need the real DSP is skipped), one process per
CPU, the deterministic clock, logging off. It prints a compact verdict table and
writes `simulation/out/report.json` and a self-contained
`simulation/out/report.html` (per run: output and setpoint rpm, the measured and
true heart rate against the zone band, g at 1.5 m and at the leg tip, the
operator messages; light and dark themes). Exit code 0 when every run is PASS or
a documented defect (XFAIL).

Measured wall time (Apple silicon, 8 cores):

| run | wall |
|---|---|
| one scenario (`manual_27_rpm`, 420 s simulated) | 1.1 s for the run, 2.1 s with interpreter start |
| one 30-min programme (`auto_jog_150_nominal`) | 1.5 s |
| the cohort (90 runs, 60 of them 30-min programmes) | 20.3 s (21.6 s with start-up) |
| the failure matrix without the DSP (77 runs) | 11.4 s |
| the 7 real-DSP failure cases (300 s each) | ~15 s each, ~90 s serial |

## Layer 2: the 2D view

```sh
$PY -m simulation.live                            # http://127.0.0.1:8765/
```

* live: `http://127.0.0.1:8765/?live=manual_27_rpm&speed=20` (or pick a scenario
  in the header). Deterministic simulated time shown at N x; add `&clock=sim`
  for the codebase's `SimClock` (real time scaled; keep the speed modest, a
  host pause then becomes a loop stall the runtime reacts to, as it should);
* playback: `http://127.0.0.1:8765/?trace=out/manual_27_rpm.jsonl`, or open
  `viewer/index.html` directly and load a `.jsonl` with the file picker, or
  `cd simulation && python -m http.server` and use `?trace=out/...`.

The view shows the arm from above rotating at the recorded output rpm
(beams, counterweight, capsule, rider silhouette with the reference radius and
the leg tip marked), live readouts (output and setpoint rpm, motor rpm, drive
Hz, g at the reference radius and at the leg tip, true and measured heart rate
against the zone, phase, mode, drive state, standing verdict, how the machine
was left), four time-series charts with a cursor, and the event list (click to
seek). No dependencies; light and dark themes.

## Scenarios as data

One JSON file per scenario in `scenarios/`; the parser rejects unknown keys so
a typo cannot silently turn a fault scenario into a nominal one. See the
docstring of `scenario.py` for the full schema. Actions (`"do"`):

`manual_target` (`output_rpm`, `expect`: accepted/refused/any), `operator_stop`,
`remote_stop`, `estop`, `acknowledge` (`estop_released`), `drive_fault`
(`fault`: a `DriveFault` name), `comms_loss` (`duration_s`), `drive_latency`
(`latency_s`, `duration_s`), `drive_reverse`, `register_offset`, `ecg_dropout`,
`ecg_quality` (`quality`), `ecg_repeat_seq`, `ecg_value` (`bpm`) (the four `ecg_*`
need `direct` mode), `bitalino_disconnect`, `attendant_leaves`, `tick_exception`,
`shutdown`. Subject events (`subject_events`) are the plant's own scripted
events: `vasovagal_drop`, `hr_spike`, `electrode_off`, `mains_burst`, `nonresponder`.
A drive fault already latched at the start is `"drive": {"initial_fault": ...}`.

Failure-injection actions (the vocabulary of `failures.py`, all in the same schema):
`drive_refuse_command` (`word`: a `ControlWord` name; the drive answers it with a
Modbus exception from then on), `drive_echo_mismatch` (the LFRD echo stops
following), `drive_speed_stuck` (RFRD frozen), `drive_status_frozen` (every read
answers `Ok` with a stale status), `loop_stall` (`duration_s`: no tick, no
keepalive, the plant runs on), `clock_jump` (`jump_s`: the WALL clock steps),
`ecg_seq_gap` (`gap`), `ecg_silent_stop` (readings stop, link still "up"),
`bitalino_signal` (`signal`: flat/saturated/corrupted/mains/stopped/gaps,
`duration_s`; `dsp` mode only: the raw samples are corrupted before the real
DSP), `start_again` (a second START; `expect`), `fault_reset` (`expect`). New
keys: `preroll_s` (idle console time before START, default 15), `ecg`:
`ectopic_rate`, `ectopic_bpm`, `motion_noise_bpm_per_g`, `connect_fails`;
`drive`: `initial_enabled_rpm` (found OPERATION_ENABLED and turning, left there
by a crashed predecessor), `refuse_commands`.

The harness records what the OPERATOR is told (`RunResult.messages`, and
`operator` events in the trace): every new verdict with its sentence, every drive
fault with its mnemonic and LFT code, every refused start/target/reset worded by
`src.local_panel.describe_*`, and the shutdown report.

`known_defect` marks a scenario whose expectations describe correct behaviour
that `raspberry-pi/src` does not deliver today: the battery runs it as a strict
xfail (it turns red the day the defect is fixed).

## The battery

| scenario | kind | what it exercises | end | peak out rpm | peak g leg tip | safety rules seen | battery |
|---|---|---|---|---|---|---|---|
| `attendant_absent_auto` | auto | The attendant stops pinging the console mid-HOLD. | safety_verdict | 19.5 | 1.03 | attendant_absent | pass |
| `auto_jog_150_dsp` | auto / dsp | Short jog programme with the REAL ECG path: simulated BITalino -> SignalTreatment (BioSPPy) -> EcgBridge -> runtime. | programme_complete | 11.7 | 0.37 | hr_stale | pass |
| `auto_jog_150_leg_tip_estimate` | auto | Jog zone with the leg tip at the stature-based estimate (1.42 m) instead of the CAD upper bound. | programme_complete | 19.5 | 0.60 | - | pass |
| `auto_jog_150_nominal` | auto | The ~150 bpm 'simulate a jog' zone (145-155), nameplate ceiling, nominal subject: the controller must hold the zone for most of HOLD. | programme_complete | 19.5 | 1.03 | - | pass |
| `auto_jog_150_radius_2m` | auto | Jog zone with the reference radius at 2.0 m: the same heart response needs less speed. | programme_complete | 17.0 | 0.78 | - | pass |
| `auto_jog_noisy_ecg` | auto | Heart rate delivered with +/-4 bpm Gaussian measurement noise (seeded). | programme_complete | 18.0 | 0.87 | - | pass |
| `auto_jog_subject_fast_responder` | auto | Fast cardiac response (tau 12 s up, 25 s down). | programme_complete | 19.4 | 1.02 | - | pass |
| `auto_jog_subject_fit` | auto | Fit subject: low resting rate, weaker response per g. | programme_complete | 23.7 | 1.53 | - | pass |
| `auto_jog_subject_nonresponder` | auto | Non-responder (gain x0.35): the zone is unreachable. The controller saturates at the ceiling and must never exceed it (integrator windup case). | programme_complete | 27.7 | 2.08 | - | pass |
| `auto_jog_subject_slow_responder` | auto | Slow cardiac response (tau 60 s up, 110 s down): the overshoot case. | programme_complete | 19.8 | 1.07 | - | pass |
| `auto_jog_subject_strong_drift` | auto | Strong cardiac drift (+25 bpm, 5 min): the controller must unload at constant speed. | programme_complete | 17.6 | 0.84 | - | pass |
| `auto_jog_subject_unfit` | auto | Deconditioned subject: high resting rate, strong response, fatigue. | programme_complete | 14.4 | 0.57 | - | pass |
| `auto_standard_30_min` | auto | Shipped 30-min profile, nominal subject. Its 276 motor-rpm ceiling (5.5 output rpm) cannot reach the 118-138 bpm zone on the modelled subject: the correct behaviour is to saturate at the ceiling, never exceed it, and complete. | programme_complete | 5.5 | 0.08 | - | pass |
| `auto_standard_45_min` | auto | Shipped 45-min profile, nominal subject (same ceiling finding as the 30-min one). | programme_complete | 5.5 | 0.08 | - | pass |
| `estop_auto` | auto | E-stop mid-HOLD: QUICK_STOP, reference zeroed at once, nothing resumes. | emergency_stop | 19.5 | 1.03 | operator_estop | pass |
| `estop_during_ramp_then_retarget_refused` | manual | E-stop mid-climb; a later target is refused (no resumption); the operator acknowledges. | emergency_stop | 10.5 | 0.30 | operator_estop | pass |
| `estop_manual_27` | manual | E-stop at 27 rpm on the bench. | emergency_stop | 27.0 | 1.98 | operator_estop | pass |
| `fault_bitalino_disconnect_auto` | auto | The BITalino link drops mid-HOLD and never returns: hr_stale escalates FREEZE -> REDUCE -> RAMP_DOWN. | safety_verdict | 19.5 | 1.03 | hr_stale | pass |
| `fault_bitalino_disconnect_dsp` | auto / dsp | Real ECG path: the simulated BITalino disconnects during WARMUP and never returns. | safety_verdict | 2.1 | 0.01 | hr_stale | pass |
| `fault_drive_already_faulted` | manual | The drive is in fault before the start: the start is refused and nothing is written. | refused (DriveInFault) | 0.0 | 0.00 | drive_fault | pass |
| `fault_drive_comm_blip` | manual | A 0.3 s comms blip at 27 rpm (one or two failed exchanges): below the comms_lost threshold, the session carries on. | operator_stop | 27.0 | 1.98 | - | pass |
| `fault_drive_comm_timeout_auto` | auto | Modbus link dies for 40 s mid-HOLD of the jog programme. | safety_verdict | 19.5 | 1.03 | comms_lost | pass |
| `fault_drive_comm_timeout_manual` | manual | Modbus link dies for 40 s at 27 rpm: GO_SILENT, no further frame, the drive's ttO (SLF) ramps it down. | safety_verdict | 27.0 | 1.98 | comms_lost | pass |
| `fault_drive_dc_bus_overvoltage_manual` | manual | The drive latches DC_BUS_OVERVOLTAGE at 27 rpm. | safety_verdict | 27.0 | 1.98 | drive_fault | pass |
| `fault_drive_latency` | manual | Every exchange takes 1 s (over the 0.5 s response timeout) at 27 rpm: comms lost. | safety_verdict | 27.0 | 1.98 | comms_lost | pass |
| `fault_drive_motor_overload_manual` | manual | The drive latches MOTOR_OVERLOAD at 27 rpm. | safety_verdict | 27.0 | 1.98 | drive_fault | pass |
| `fault_drive_motor_phase_loss_manual` | manual | The drive latches MOTOR_PHASE_LOSS at 27 rpm. | safety_verdict | 27.0 | 1.98 | drive_fault | pass |
| `fault_drive_overcurrent_manual` | manual | The drive latches OVERCURRENT at 27 rpm. | safety_verdict | 27.0 | 1.98 | drive_fault | pass |
| `fault_drive_overload_auto` | auto | The drive latches MOTOR_OVERLOAD mid-HOLD of the jog programme. | safety_verdict | 19.5 | 1.03 | drive_fault | pass |
| `fault_drive_register_offset` | manual | Register map one off: writes are acknowledged but land in the wrong parameter; the speed never follows. | safety_verdict | 0.0 | 0.00 | setpoint_unconfirmed | pass |
| `fault_drive_reverse` | manual | Motor phases swapped: a positive setpoint turns the shaft backwards. | safety_verdict | 0.6 | 0.00 | reverse_rotation | pass |
| `fault_ecg_dropout_short` | auto | A 15 s ECG gap mid-HOLD: stale past 10 s (FREEZE), then fresh again. | programme_complete | 19.5 | 1.03 | hr_stale | pass |
| `fault_ecg_electrode_off_dsp` | auto / dsp | Real ECG path: a lead comes off for 30 s during WARMUP (artifact rendered by the ECG synthesiser): no usable rate, hr_stale. | programme_complete | 11.8 | 0.38 | hr_stale | pass |
| `fault_ecg_mains_burst_dsp` | auto / dsp | Real ECG path: 30 s of 50 Hz hum during WARMUP. The steady hum must not be taken for a heart rate. | programme_complete | 11.7 | 0.37 | hr_stale | pass |
| `fault_ecg_no_signal` | auto | Electrodes off (no_signal) for 120 s mid-HOLD. | safety_verdict | 19.5 | 1.03 | hr_stale | pass |
| `fault_ecg_noisy_quality` | auto | The DSP grades the signal 'noisy' for 90 s: no usable rate, so stale. | safety_verdict | 19.5 | 1.03 | hr_stale | pass |
| `fault_ecg_repeat_seq` | auto | The DSP re-emits its previous metrics (same seq) for 45 s: 'unchanged' must not read as 'still true'. | programme_complete | 19.5 | 1.03 | hr_stale, hr_unresponsive | pass |
| `fault_no_heart_rate_at_start_auto` | auto | The ECG never delivers a rate: BASELINE measures no resting rate, so the machine must never leave standstill. | safety_verdict | 0.0 | 0.00 | hr_stale | pass |
| `hr_above_hard_max` | auto | The heart rate reads 168 bpm (hard max 165) for 20 s mid-HOLD: hr_hard_max REDUCE, the speed comes down. | safety_verdict | 19.5 | 1.03 | hr_hard_max | pass |
| `hr_critical` | auto | The heart rate reads 180 bpm (critical 175): QUICK_STOP, the session ends. | safety_verdict | 19.5 | 1.03 | hr_critical | pass |
| `hr_critical_manual_occupied` | manual | Person on board, manual at 19 rpm: the heart rate reads 170 (critical 158 on the console): QUICK_STOP. | safety_verdict | 17.1 | 0.79 | hr_rate, hr_critical | pass |
| `hr_spike_artifact` | auto | A +40 bpm-in-2 s cardiac spike (scripted in the plant, so the TRUE rate reaches ~187 bpm) mid-HOLD: above the 175 bpm critical tier, so QUICK_STOP ends the session. | safety_verdict | 19.5 | 1.03 | hr_critical | pass |
| `manual_27_leg_tip_estimate` | manual | 27 output rpm with the leg tip at the stature estimate (1.42 m): the g at the leg tip is 1.16 instead of 1.98. | operator_stop | 27.0 | 1.16 | - | pass |
| `manual_27_rpm` | manual | Bench manual session to 27 output rpm (1344 motor rpm, 48.7 Hz), hold, operator stop. | operator_stop | 27.0 | 1.98 | - | pass |
| `manual_27_then_32_refused` | manual | At 27 rpm, the operator asks for 32: refused, the machine keeps 27, then stops. | operator_stop | 27.0 | 1.98 | - | pass |
| `manual_32_rpm_refused` | manual | 32 output rpm = 1593 motor rpm = 57.7 Hz: above the nameplate / HSP 50 Hz ceiling. Refusal is the correct behaviour; nothing moves. | shutdown | 0.0 | 0.00 | - | pass |
| `manual_below_min_run_refused` | manual | 0.5 output rpm = 25 motor rpm is inside the gap (0, min_run=55): refused rather than rounded; 0 is accepted. | shutdown | 0.0 | 0.00 | - | pass |
| `manual_ladder` | manual | Operator ladder 5 -> 10 -> 15 -> 20 -> 25 -> 27 -> 12 -> 0 output rpm. | operator_stop | 27.0 | 1.98 | - | pass |
| `manual_low_ceiling_300` | manual | Console default MOTOR_MAX_RPM=300 (6.0 output rpm): 27 is refused, 6 is accepted. | operator_stop | 6.0 | 0.10 | - | pass |
| `manual_nameplate_27_7` | manual | The nameplate point: 27.716 output rpm = 1380 motor rpm = 50 Hz, the highest target accepted. | operator_stop | 27.7 | 2.08 | - | pass |
| `manual_occupied_ceiling` | manual | Person on board: the ceiling is the first-trial 1.2 g resultant at the reference radius (~19.9 output rpm at 1.5 m). 27 is refused, 19 accepted. | operator_stop | 17.1 | 0.79 | hr_rate | pass |
| `manual_retarget_mid_ramp` | manual | Climbing to 27, the operator changes their mind to 10 at 40 s: the setpoint turns round at the motion limits. | operator_stop | 10.5 | 0.30 | - | pass |
| `shutdown_sigterm_auto` | auto | SIGTERM mid-HOLD of the jog programme. | shutdown | 19.5 | 1.03 | - | pass |
| `shutdown_sigterm_manual_27` | manual | The process is told to exit (SIGTERM) at 27 rpm: emergency zero, link closed, the drive's own ramp and ttO stop it. | shutdown | 27.0 | 1.98 | - | pass |
| `stop_operator_auto` | auto | Operator STOP mid-HOLD of the jog programme: cooldown ramp, recovery, finished. | operator_stop | 19.5 | 1.03 | - | pass |
| `stop_remote_auto` | auto | Remote end request (the EndSession path) mid-HOLD. | operator_stop | 19.5 | 1.03 | - | pass |
| `stop_remote_manual` | manual | Remote end request at 27 rpm. | operator_stop | 27.0 | 1.98 | - | pass |
| `tick_exception_manual_27` | manual | An exception inside the tick at 27 rpm: fail closed, the console exits through shutdown. | tick_exception | 27.0 | 1.98 | - | pass |
| `vasovagal_auto_hold` | auto | Vasovagal collapse (-30 bpm in 20 s) mid-HOLD: the falling rate reads 'below zone' but the machine must NOT speed up; hr_drop ends the session. | safety_verdict | 19.5 | 1.03 | hr_drop | pass |
| `vasovagal_auto_warmup` | auto | Vasovagal collapse during WARMUP, while the controller is still accelerating. | safety_verdict | 15.4 | 0.65 | hr_drop | pass |

Plus, in `tests/`: parametrized checks per scenario (invariants + expectations,
the exit-path check, the JSONL round trip), hypothesis properties (any sequence
of manual targets, any ending at any moment, any plausible subject, any heart
rate the sensor reports, the sensor never fabricating evidence), the checker's
own tests (every invariant must fire on a trace corrupted to break it), the CAD
geometry and its reproducibility, the schema's refusals, the CLI, the live
server, and the composition-root cross-check.

## The cohort (`cohort/`)

`cohort/generate.py` draws 30 fake subjects from one seed (reproducible bit for
bit; a test regenerates the file and compares) and commits them as
`cohort/cohort.json`: age 10-50 (five under 16, the first exactly 10), sex,
fitness, resting HR, HRmax = Tanaka `208 - 0.7 x age` plus an individual
deviation ~N(0, 7) clipped to +/- 15, gain `k_g`, `tau_up` / `tau_down`, drift,
fatigue, ECG noise, and eleven special conditions (2 slow responders, 2 fast,
1 non-responder, 2 vasovagal-prone, 2 ectopic, 2 high motion artefact). Each
maps onto the production plant's own knobs (`PhysiologyConfig`), the DIRECT
sensor's artefact model, and the plant's scripted events (`nonresponder`,
`vasovagal_drop` in HOLD); nothing is added to the physiology. The docstring
states every distribution.

`cohort/battery.py` runs every subject x {auto jog 145-155 bpm 30 min, auto
`standard_30_min`, manual 27 rpm bench}: the programme is first resolved for the
rider's HRmax through `ProfileStore.resolve` (the call the panel makes for a
dashboard launch), which must refuse exactly when `zone_high > 0.9 x HRmax`;
then every invariant on every trace, and for auto runs: never above the
programme ceiling, `hr_drop` must fire for a vasovagal-prone subject and must
NOT fire for anybody else (it is the presyncope rule). Tests:
`tests/test_cohort.py` (the 90 pairs over every CPU, ~20 s).

Result: 89 PASS, 1 XFAIL (S07's jog, README finding 1's residual), 0 FAIL. Every
rider under `MIN_RIDER_AGE` (default 18: the 8 subjects aged 10-17) is refused
for both programmes before anything turns (finding 8), and S08's jog is refused
on HRmax (172 < 155 / 0.9). The false `hr_drop` pairs (13 COOLDOWN ends, two
ectopic subjects stuck in BASELINE, two motion-artefact subjects) are gone
(finding 9). Manual bench sessions are identical by design (nobody rides a bench
session): 27.0 rpm, 1.98 g at the leg tip for all 30.

| subject | age | sex | fitness | condition | HRmax est / true | jog: end | jog: in zone | jog: peak rpm | jog: g 1.5 m / leg tip | standard: end | manual 27: peak rpm / g leg tip | violations | result |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| S01 | 10 | M | high | none | 201 / 196 | refused | - | - | - | refused | 27.0 / 1.98 | 0 | PASS/PASS/PASS |
| S02 | 13 | M | low | none | 199 / 210 | refused | - | - | - | refused | 27.0 / 1.98 | 0 | PASS/PASS/PASS |
| S03 | 15 | F | low | none | 198 / 208 | refused | - | - | - | refused | 27.0 / 1.98 | 0 | PASS/PASS/PASS |
| S04 | 15 | F | athlete | vasovagal_prone | 198 / 191 | refused | - | - | - | refused | 27.0 / 1.98 | 0 | PASS/PASS/PASS |
| S05 | 11 | M | low | none | 200 / 197 | refused | - | - | - | refused | 27.0 / 1.98 | 0 | PASS/PASS/PASS |
| S06 | 35 | F | high | motion_artefact | 184 / 193 | programme_complete | 0.00 | 21.6 | 0.78 / 1.27 | programme_complete | 27.0 / 1.98 | 0 | PASS/PASS/PASS |
| S07 | 48 | F | average | vasovagal_prone | 174 / 175 | safety_verdict | 0.00 | 16.8 | 0.47 / 0.77 | safety_verdict | 27.0 / 1.98 | 5 | XFAIL/PASS/PASS |
| S08 | 42 | M | low | none | 179 / 172 | refused | - | - | - | programme_complete | 27.0 / 1.98 | 0 | PASS/PASS/PASS |
| S09 | 17 | F | athlete | motion_artefact | 196 / 185 | refused | - | - | - | refused | 27.0 / 1.98 | 0 | PASS/PASS/PASS |
| S10 | 19 | M | average | none | 195 / 192 | programme_complete | 0.57 | 19.6 | 0.65 / 1.04 | programme_complete | 27.0 / 1.98 | 0 | PASS/PASS/PASS |
| S11 | 33 | F | average | none | 185 / 187 | programme_complete | 0.57 | 20.2 | 0.68 / 1.11 | programme_complete | 27.0 / 1.98 | 0 | PASS/PASS/PASS |
| S12 | 45 | M | average | none | 176 / 176 | programme_complete | 0.43 | 19.0 | 0.60 / 0.98 | programme_complete | 27.0 / 1.98 | 0 | PASS/PASS/PASS |
| S13 | 43 | M | average | none | 178 / 175 | programme_complete | 0.59 | 21.6 | 0.78 / 1.26 | programme_complete | 27.0 / 1.98 | 0 | PASS/PASS/PASS |
| S14 | 17 | F | high | none | 196 / 187 | refused | - | - | - | refused | 27.0 / 1.98 | 0 | PASS/PASS/PASS |
| S15 | 18 | F | low | slow_responder | 195 / 197 | programme_complete | 0.57 | 15.9 | 0.42 / 0.69 | programme_complete | 27.0 / 1.98 | 0 | PASS/PASS/PASS |
| S16 | 47 | M | average | none | 175 / 179 | programme_complete | 0.45 | 18.2 | 0.55 / 0.89 | programme_complete | 27.0 / 1.98 | 0 | PASS/PASS/PASS |
| S17 | 21 | M | low | fast_responder | 193 / 198 | programme_complete | 0.34 | 14.3 | 0.34 / 0.56 | programme_complete | 27.0 / 1.98 | 0 | PASS/PASS/PASS |
| S18 | 27 | F | average | nonresponder | 189 / 198 | programme_complete | 0.00 | 27.7 | 1.29 / 2.08 | programme_complete | 27.0 / 1.98 | 0 | PASS/PASS/PASS |
| S19 | 50 | F | average | none | 173 / 177 | programme_complete | 0.38 | 18.4 | 0.57 / 0.92 | programme_complete | 27.0 / 1.98 | 0 | PASS/PASS/PASS |
| S20 | 38 | M | low | ectopic | 181 / 177 | programme_complete | 0.00 | 15.1 | 0.38 / 0.62 | programme_complete | 27.0 / 1.98 | 0 | PASS/PASS/PASS |
| S21 | 20 | M | low | none | 194 / 186 | programme_complete | 0.56 | 15.9 | 0.42 / 0.68 | programme_complete | 27.0 / 1.98 | 0 | PASS/PASS/PASS |
| S22 | 40 | F | average | none | 180 / 180 | programme_complete | 0.32 | 19.0 | 0.60 / 0.98 | programme_complete | 27.0 / 1.98 | 0 | PASS/PASS/PASS |
| S23 | 36 | M | high | none | 183 / 188 | programme_complete | 0.50 | 24.3 | 0.99 / 1.60 | programme_complete | 27.0 / 1.98 | 0 | PASS/PASS/PASS |
| S24 | 20 | M | low | none | 194 / 189 | programme_complete | 0.24 | 15.4 | 0.40 / 0.64 | programme_complete | 27.0 / 1.98 | 0 | PASS/PASS/PASS |
| S25 | 41 | M | high | none | 179 / 177 | programme_complete | 0.47 | 23.0 | 0.89 / 1.44 | programme_complete | 27.0 / 1.98 | 0 | PASS/PASS/PASS |
| S26 | 18 | M | low | fast_responder | 195 / 195 | programme_complete | 0.62 | 15.5 | 0.40 / 0.65 | programme_complete | 27.0 / 1.98 | 0 | PASS/PASS/PASS |
| S27 | 31 | M | low | ectopic | 186 / 188 | programme_complete | 0.02 | 12.5 | 0.26 / 0.42 | programme_complete | 27.0 / 1.98 | 0 | PASS/PASS/PASS |
| S28 | 16 | F | low | slow_responder | 197 / 188 | refused | - | - | - | refused | 27.0 / 1.98 | 0 | PASS/PASS/PASS |
| S29 | 48 | M | average | none | 174 / 174 | programme_complete | 0.51 | 21.1 | 0.75 / 1.21 | programme_complete | 27.0 / 1.98 | 0 | PASS/PASS/PASS |
| S30 | 33 | F | high | none | 185 / 200 | programme_complete | 0.20 | 26.2 | 1.15 / 1.87 | programme_complete | 27.0 / 1.98 | 0 | PASS/PASS/PASS |

("in zone" is the share of HOLD ticks with a usable heart rate inside 145-155;
`standard_30_min` never reaches its zone: README finding 7.)

## The failure matrix (`failures.py`)

199 cases, each a scenario DOCUMENT in the ordinary schema plus what must be
true afterwards. For every case `tests/test_failures.py` asserts: every
invariant (motor at 0 after teardown, no torque, LFRD 0 where writable, no NaN,
safety dominating, no frame after going silent), the output disabled or - when
the runtime went silent - the drive's own ttO visibly took over, the end reason,
the verdicts, an operator message containing the named words, the phase the
injection landed in, a deadline for detection where it matters, and (ECG signal
cases) no false heart rate graded usable (within 15 bpm of the truth). A second
test asserts no case, known defect or not, leaves torque or a turning shaft.
Result: 199 PASS, 0 XFAIL. Every one of the drive's 66 fault codes (the complete Schneider LFT
enumeration) plus an unknown code is injected in an auto and a manual session,
and every one ends in a controlled stop with the code named to the operator.

| case | category | expected end | verdict / rules | operator must read | result |
|---|---|---|---|---|---|
| `drive_comm_timeout_auto_baseline` | drive | safety_verdict | comms_lost | comms_lost; ttO | PASS |
| `drive_comm_timeout_auto_warmup` | drive | safety_verdict | comms_lost | comms_lost; ttO | PASS |
| `drive_comm_timeout_auto_hold` | drive | safety_verdict | comms_lost | comms_lost; ttO | PASS |
| `drive_comm_timeout_auto_cooldown` | drive | safety_verdict | comms_lost | comms_lost; ttO | PASS |
| `drive_comm_timeout_auto_recovery` | drive | safety_verdict | comms_lost | comms_lost; ttO | PASS |
| `drive_comm_timeout_manual_ramp_up` | drive | safety_verdict | comms_lost | comms_lost; ttO | PASS |
| `drive_comm_timeout_manual_at_speed` | drive | safety_verdict | comms_lost | comms_lost; ttO | PASS |
| `drive_comm_timeout_manual_ramp_down` | drive | operator_stop | comms_lost | comms_lost; ttO | PASS |
| `drive_fault_internal_auto` | drive | safety_verdict | drive_fault | InF (LFT 1); drive_fault | PASS |
| `drive_fault_internal_manual` | drive | safety_verdict | drive_fault | InF (LFT 1); drive_fault | PASS |
| `drive_fault_control_eeprom_auto` | drive | safety_verdict | drive_fault | EEF1 (LFT 2); drive_fault | PASS |
| `drive_fault_control_eeprom_manual` | drive | safety_verdict | drive_fault | EEF1 (LFT 2); drive_fault | PASS |
| `drive_fault_incorrect_config_auto` | drive | safety_verdict | drive_fault | CFF (LFT 3); drive_fault | PASS |
| `drive_fault_incorrect_config_manual` | drive | safety_verdict | drive_fault | CFF (LFT 3); drive_fault | PASS |
| `drive_fault_invalid_config_auto` | drive | safety_verdict | drive_fault | CFI (LFT 4); drive_fault | PASS |
| `drive_fault_invalid_config_manual` | drive | safety_verdict | drive_fault | CFI (LFT 4); drive_fault | PASS |
| `drive_fault_modbus_comm_loss_auto` | drive | safety_verdict | drive_fault | SLF1 (LFT 5); drive_fault | PASS |
| `drive_fault_modbus_comm_loss_manual` | drive | safety_verdict | drive_fault | SLF1 (LFT 5); drive_fault | PASS |
| `drive_fault_internal_com_link_auto` | drive | safety_verdict | drive_fault | ILF (LFT 6); drive_fault | PASS |
| `drive_fault_internal_com_link_manual` | drive | safety_verdict | drive_fault | ILF (LFT 6); drive_fault | PASS |
| `drive_fault_com_network_auto` | drive | safety_verdict | drive_fault | CnF (LFT 7); drive_fault | PASS |
| `drive_fault_com_network_manual` | drive | safety_verdict | drive_fault | CnF (LFT 7); drive_fault | PASS |
| `drive_fault_external_fault_input_auto` | drive | safety_verdict | drive_fault | EPF1 (LFT 8); drive_fault | PASS |
| `drive_fault_external_fault_input_manual` | drive | safety_verdict | drive_fault | EPF1 (LFT 8); drive_fault | PASS |
| `drive_fault_overcurrent_auto` | drive | safety_verdict | drive_fault | OCF (LFT 9); drive_fault | PASS |
| `drive_fault_overcurrent_manual` | drive | safety_verdict | drive_fault | OCF (LFT 9); drive_fault | PASS |
| `drive_fault_precharge_auto` | drive | safety_verdict | drive_fault | CrF (LFT 10); drive_fault | PASS |
| `drive_fault_precharge_manual` | drive | safety_verdict | drive_fault | CrF (LFT 10); drive_fault | PASS |
| `drive_fault_speed_feedback_loss_auto` | drive | safety_verdict | drive_fault | SPF (LFT 11); drive_fault | PASS |
| `drive_fault_speed_feedback_loss_manual` | drive | safety_verdict | drive_fault | SPF (LFT 11); drive_fault | PASS |
| `drive_fault_drive_overheat_auto` | drive | safety_verdict | drive_fault | OHF (LFT 16); drive_fault | PASS |
| `drive_fault_drive_overheat_manual` | drive | safety_verdict | drive_fault | OHF (LFT 16); drive_fault | PASS |
| `drive_fault_motor_overload_auto` | drive | safety_verdict | drive_fault | OLF (LFT 17); drive_fault | PASS |
| `drive_fault_motor_overload_manual` | drive | safety_verdict | drive_fault | OLF (LFT 17); drive_fault | PASS |
| `drive_fault_dc_bus_overvoltage_auto` | drive | safety_verdict | drive_fault | ObF (LFT 18); drive_fault | PASS |
| `drive_fault_dc_bus_overvoltage_manual` | drive | safety_verdict | drive_fault | ObF (LFT 18); drive_fault | PASS |
| `drive_fault_mains_overvoltage_auto` | drive | safety_verdict | drive_fault | OSF (LFT 19); drive_fault | PASS |
| `drive_fault_mains_overvoltage_manual` | drive | safety_verdict | drive_fault | OSF (LFT 19); drive_fault | PASS |
| `drive_fault_output_phase_loss_auto` | drive | safety_verdict | drive_fault | OPF1 (LFT 20); drive_fault | PASS |
| `drive_fault_output_phase_loss_manual` | drive | safety_verdict | drive_fault | OPF1 (LFT 20); drive_fault | PASS |
| `drive_fault_input_phase_loss_auto` | drive | safety_verdict | drive_fault | PHF (LFT 21); drive_fault | PASS |
| `drive_fault_input_phase_loss_manual` | drive | safety_verdict | drive_fault | PHF (LFT 21); drive_fault | PASS |
| `drive_fault_undervoltage_auto` | drive | safety_verdict | drive_fault | USF (LFT 22); drive_fault | PASS |
| `drive_fault_undervoltage_manual` | drive | safety_verdict | drive_fault | USF (LFT 22); drive_fault | PASS |
| `drive_fault_motor_short_circuit_auto` | drive | safety_verdict | drive_fault | SCF1 (LFT 23); drive_fault | PASS |
| `drive_fault_motor_short_circuit_manual` | drive | safety_verdict | drive_fault | SCF1 (LFT 23); drive_fault | PASS |
| `drive_fault_overspeed_auto` | drive | safety_verdict | drive_fault | SOF (LFT 24); drive_fault | PASS |
| `drive_fault_overspeed_manual` | drive | safety_verdict | drive_fault | SOF (LFT 24); drive_fault | PASS |
| `drive_fault_auto_tuning_auto` | drive | safety_verdict | drive_fault | tnF (LFT 25); drive_fault | PASS |
| `drive_fault_auto_tuning_manual` | drive | safety_verdict | drive_fault | tnF (LFT 25); drive_fault | PASS |
| `drive_fault_rating_error_auto` | drive | safety_verdict | drive_fault | InF1 (LFT 26); drive_fault | PASS |
| `drive_fault_rating_error_manual` | drive | safety_verdict | drive_fault | InF1 (LFT 26); drive_fault | PASS |
| `drive_fault_power_calibration_auto` | drive | safety_verdict | drive_fault | InF2 (LFT 27); drive_fault | PASS |
| `drive_fault_power_calibration_manual` | drive | safety_verdict | drive_fault | InF2 (LFT 27); drive_fault | PASS |
| `drive_fault_internal_serial_link_auto` | drive | safety_verdict | drive_fault | InF3 (LFT 28); drive_fault | PASS |
| `drive_fault_internal_serial_link_manual` | drive | safety_verdict | drive_fault | InF3 (LFT 28); drive_fault | PASS |
| `drive_fault_internal_mfg_area_auto` | drive | safety_verdict | drive_fault | InF4 (LFT 29); drive_fault | PASS |
| `drive_fault_internal_mfg_area_manual` | drive | safety_verdict | drive_fault | InF4 (LFT 29); drive_fault | PASS |
| `drive_fault_power_eeprom_auto` | drive | safety_verdict | drive_fault | EEF2 (LFT 30); drive_fault | PASS |
| `drive_fault_power_eeprom_manual` | drive | safety_verdict | drive_fault | EEF2 (LFT 30); drive_fault | PASS |
| `drive_fault_impedant_short_circuit_auto` | drive | safety_verdict | drive_fault | SCF2 (LFT 31); drive_fault | PASS |
| `drive_fault_impedant_short_circuit_manual` | drive | safety_verdict | drive_fault | SCF2 (LFT 31); drive_fault | PASS |
| `drive_fault_ground_short_circuit_auto` | drive | safety_verdict | drive_fault | SCF3 (LFT 32); drive_fault | PASS |
| `drive_fault_ground_short_circuit_manual` | drive | safety_verdict | drive_fault | SCF3 (LFT 32); drive_fault | PASS |
| `drive_fault_three_phase_loss_auto` | drive | safety_verdict | drive_fault | OPF2 (LFT 33); drive_fault | PASS |
| `drive_fault_three_phase_loss_manual` | drive | safety_verdict | drive_fault | OPF2 (LFT 33); drive_fault | PASS |
| `drive_fault_canopen_comm_loss_auto` | drive | safety_verdict | drive_fault | COF (LFT 34); drive_fault | PASS |
| `drive_fault_canopen_comm_loss_manual` | drive | safety_verdict | drive_fault | COF (LFT 34); drive_fault | PASS |
| `drive_fault_brake_control_auto` | drive | safety_verdict | drive_fault | bLF (LFT 35); drive_fault | PASS |
| `drive_fault_brake_control_manual` | drive | safety_verdict | drive_fault | bLF (LFT 35); drive_fault | PASS |
| `drive_fault_external_fault_com_auto` | drive | safety_verdict | drive_fault | EPF2 (LFT 38); drive_fault | PASS |
| `drive_fault_external_fault_com_manual` | drive | safety_verdict | drive_fault | EPF2 (LFT 38); drive_fault | PASS |
| `drive_fault_brake_feedback_auto` | drive | safety_verdict | drive_fault | brF (LFT 41); drive_fault | PASS |
| `drive_fault_brake_feedback_manual` | drive | safety_verdict | drive_fault | brF (LFT 41); drive_fault | PASS |
| `drive_fault_pc_comm_loss_auto` | drive | safety_verdict | drive_fault | SLF2 (LFT 42); drive_fault | PASS |
| `drive_fault_pc_comm_loss_manual` | drive | safety_verdict | drive_fault | SLF2 (LFT 42); drive_fault | PASS |
| `drive_fault_encoder_coupling_auto` | drive | safety_verdict | drive_fault | ECF (LFT 43); drive_fault | PASS |
| `drive_fault_encoder_coupling_manual` | drive | safety_verdict | drive_fault | ECF (LFT 43); drive_fault | PASS |
| `drive_fault_torque_current_limit_auto` | drive | safety_verdict | drive_fault | SSF (LFT 44); drive_fault | PASS |
| `drive_fault_torque_current_limit_manual` | drive | safety_verdict | drive_fault | SSF (LFT 44); drive_fault | PASS |
| `drive_fault_hmi_comm_loss_auto` | drive | safety_verdict | drive_fault | SLF3 (LFT 45); drive_fault | PASS |
| `drive_fault_hmi_comm_loss_manual` | drive | safety_verdict | drive_fault | SLF3 (LFT 45); drive_fault | PASS |
| `drive_fault_power_removal_auto` | drive | safety_verdict | drive_fault | PrF (LFT 46); drive_fault | PASS |
| `drive_fault_power_removal_manual` | drive | safety_verdict | drive_fault | PrF (LFT 46); drive_fault | PASS |
| `drive_fault_ptc_probe_auto` | drive | safety_verdict | drive_fault | PtFL (LFT 49); drive_fault | PASS |
| `drive_fault_ptc_probe_manual` | drive | safety_verdict | drive_fault | PtFL (LFT 49); drive_fault | PASS |
| `drive_fault_ptc_overheat_auto` | drive | safety_verdict | drive_fault | OtFL (LFT 50); drive_fault | PASS |
| `drive_fault_ptc_overheat_manual` | drive | safety_verdict | drive_fault | OtFL (LFT 50); drive_fault | PASS |
| `drive_fault_internal_current_measure_auto` | drive | safety_verdict | drive_fault | InF9 (LFT 51); drive_fault | PASS |
| `drive_fault_internal_current_measure_manual` | drive | safety_verdict | drive_fault | InF9 (LFT 51); drive_fault | PASS |
| `drive_fault_internal_mains_circuit_auto` | drive | safety_verdict | drive_fault | InFA (LFT 52); drive_fault | PASS |
| `drive_fault_internal_mains_circuit_manual` | drive | safety_verdict | drive_fault | InFA (LFT 52); drive_fault | PASS |
| `drive_fault_internal_thermal_sensor_auto` | drive | safety_verdict | drive_fault | InFb (LFT 53); drive_fault | PASS |
| `drive_fault_internal_thermal_sensor_manual` | drive | safety_verdict | drive_fault | InFb (LFT 53); drive_fault | PASS |
| `drive_fault_igbt_overheat_auto` | drive | safety_verdict | drive_fault | tJF (LFT 54); drive_fault | PASS |
| `drive_fault_igbt_overheat_manual` | drive | safety_verdict | drive_fault | tJF (LFT 54); drive_fault | PASS |
| `drive_fault_igbt_short_circuit_auto` | drive | safety_verdict | drive_fault | SCF4 (LFT 55); drive_fault | PASS |
| `drive_fault_igbt_short_circuit_manual` | drive | safety_verdict | drive_fault | SCF4 (LFT 55); drive_fault | PASS |
| `drive_fault_motor_short_circuit_2_auto` | drive | safety_verdict | drive_fault | SCF5 (LFT 56); drive_fault | PASS |
| `drive_fault_motor_short_circuit_2_manual` | drive | safety_verdict | drive_fault | SCF5 (LFT 56); drive_fault | PASS |
| `drive_fault_torque_timeout_auto` | drive | safety_verdict | drive_fault | SrF (LFT 57); drive_fault | PASS |
| `drive_fault_torque_timeout_manual` | drive | safety_verdict | drive_fault | SrF (LFT 57); drive_fault | PASS |
| `drive_fault_output_contactor_stuck_auto` | drive | safety_verdict | drive_fault | FCF1 (LFT 58); drive_fault | PASS |
| `drive_fault_output_contactor_stuck_manual` | drive | safety_verdict | drive_fault | FCF1 (LFT 58); drive_fault | PASS |
| `drive_fault_output_contactor_open_auto` | drive | safety_verdict | drive_fault | FCF2 (LFT 59); drive_fault | PASS |
| `drive_fault_output_contactor_open_manual` | drive | safety_verdict | drive_fault | FCF2 (LFT 59); drive_fault | PASS |
| `drive_fault_ai2_input_auto` | drive | safety_verdict | drive_fault | AI2F (LFT 61); drive_fault | PASS |
| `drive_fault_ai2_input_manual` | drive | safety_verdict | drive_fault | AI2F (LFT 61); drive_fault | PASS |
| `drive_fault_input_contactor_auto` | drive | safety_verdict | drive_fault | LCF (LFT 64); drive_fault | PASS |
| `drive_fault_input_contactor_manual` | drive | safety_verdict | drive_fault | LCF (LFT 64); drive_fault | PASS |
| `drive_fault_differential_current_auto` | drive | safety_verdict | drive_fault | dCF (LFT 66); drive_fault | PASS |
| `drive_fault_differential_current_manual` | drive | safety_verdict | drive_fault | dCF (LFT 66); drive_fault | PASS |
| `drive_fault_igbt_desaturation_auto` | drive | safety_verdict | drive_fault | HdF (LFT 67); drive_fault | PASS |
| `drive_fault_igbt_desaturation_manual` | drive | safety_verdict | drive_fault | HdF (LFT 67); drive_fault | PASS |
| `drive_fault_internal_option_auto` | drive | safety_verdict | drive_fault | InF6 (LFT 68); drive_fault | PASS |
| `drive_fault_internal_option_manual` | drive | safety_verdict | drive_fault | InF6 (LFT 68); drive_fault | PASS |
| `drive_fault_internal_cpu_auto` | drive | safety_verdict | drive_fault | InFE (LFT 69); drive_fault | PASS |
| `drive_fault_internal_cpu_manual` | drive | safety_verdict | drive_fault | InFE (LFT 69); drive_fault | PASS |
| `drive_fault_ai3_current_loss_auto` | drive | safety_verdict | drive_fault | LFF3 (LFT 71); drive_fault | PASS |
| `drive_fault_ai3_current_loss_manual` | drive | safety_verdict | drive_fault | LFF3 (LFT 71); drive_fault | PASS |
| `drive_fault_cards_pairing_auto` | drive | safety_verdict | drive_fault | HCF (LFT 73); drive_fault | PASS |
| `drive_fault_cards_pairing_manual` | drive | safety_verdict | drive_fault | HCF (LFT 73); drive_fault | PASS |
| `drive_fault_load_fault_auto` | drive | safety_verdict | drive_fault | dLF (LFT 76); drive_fault | PASS |
| `drive_fault_load_fault_manual` | drive | safety_verdict | drive_fault | dLF (LFT 76); drive_fault | PASS |
| `drive_fault_bad_config_transfer_auto` | drive | safety_verdict | drive_fault | CFI2 (LFT 77); drive_fault | PASS |
| `drive_fault_bad_config_transfer_manual` | drive | safety_verdict | drive_fault | CFI2 (LFT 77); drive_fault | PASS |
| `drive_fault_channel_switch_auto` | drive | safety_verdict | drive_fault | CSF (LFT 99); drive_fault | PASS |
| `drive_fault_channel_switch_manual` | drive | safety_verdict | drive_fault | CSF (LFT 99); drive_fault | PASS |
| `drive_fault_process_underload_auto` | drive | safety_verdict | drive_fault | ULF (LFT 100); drive_fault | PASS |
| `drive_fault_process_underload_manual` | drive | safety_verdict | drive_fault | ULF (LFT 100); drive_fault | PASS |
| `drive_fault_process_overload_auto` | drive | safety_verdict | drive_fault | OLC (LFT 101); drive_fault | PASS |
| `drive_fault_process_overload_manual` | drive | safety_verdict | drive_fault | OLC (LFT 101); drive_fault | PASS |
| `drive_fault_angle_error_auto` | drive | safety_verdict | drive_fault | ASF (LFT 105); drive_fault | PASS |
| `drive_fault_angle_error_manual` | drive | safety_verdict | drive_fault | ASF (LFT 105); drive_fault | PASS |
| `drive_fault_safety_function_auto` | drive | safety_verdict | drive_fault | SAFF (LFT 107); drive_fault | PASS |
| `drive_fault_safety_function_manual` | drive | safety_verdict | drive_fault | SAFF (LFT 107); drive_fault | PASS |
| `drive_fault_fieldbus_auto` | drive | safety_verdict | drive_fault | FbE (LFT 108); drive_fault | PASS |
| `drive_fault_fieldbus_manual` | drive | safety_verdict | drive_fault | FbE (LFT 108); drive_fault | PASS |
| `drive_fault_fieldbus_stop_auto` | drive | safety_verdict | drive_fault | FbES (LFT 109); drive_fault | PASS |
| `drive_fault_fieldbus_stop_manual` | drive | safety_verdict | drive_fault | FbES (LFT 109); drive_fault | PASS |
| `drive_fault_unknown_auto` | drive | safety_verdict | drive_fault | ? (LFT 251); drive_fault | PASS |
| `drive_fault_unknown_manual` | drive | safety_verdict | drive_fault | ? (LFT 251); drive_fault | PASS |
| `drive_stuck_enabled_at_startup` | drive | refused | drive_precommanded | variateur deja en marche; drive_precommanded | PASS |
| `drive_stuck_enabled_idle_console` | drive | refused | drive_precommanded | drive_precommanded | PASS |
| `drive_refuses_enable_at_start` | drive | refused | - | demarrage refuse; ENABLE_OPERATION | PASS |
| `drive_refuses_switch_on_at_start` | drive | refused | - | demarrage refuse; SWITCH_ON | PASS |
| `drive_refuses_switch_on_at_stop` | drive | operator_stop | disable_refused | SWITCH_ON | PASS |
| `drive_setpoint_echo_mismatch` | drive | safety_verdict | setpoint_unconfirmed | echo | PASS |
| `drive_register_offset_at_speed` | drive | operator_stop/safety_verdict | setpoint_unconfirmed, tracking_error | tracking_error; echo; ttO | PASS |
| `drive_speed_not_following_ramp` | drive | safety_verdict | tracking_error | tracking_error | PASS |
| `drive_speed_not_following_at_speed` | drive | operator_stop/safety_verdict | tracking_error | tracking_error | PASS |
| `drive_status_frozen_during_stop` | drive | operator_stop/safety_verdict | tracking_error | tracking_error; ttO | PASS |
| `drive_status_frozen_auto_hold` | drive | safety_verdict/shutdown | tracking_error | tracking_error; ttO | PASS |
| `ecg_disconnect_baseline` | ecg | safety_verdict | hr_stale | hr_stale | PASS |
| `ecg_disconnect_warmup` | ecg | safety_verdict | hr_stale | hr_stale | PASS |
| `ecg_disconnect_hold` | ecg | safety_verdict | hr_stale | hr_stale | PASS |
| `ecg_disconnect_cooldown` | ecg | safety_verdict | hr_stale | hr_stale | PASS |
| `ecg_disconnect_recovery` | ecg | programme_complete/safety_verdict | hr_stale | hr_stale | PASS |
| `ecg_connect_failure` | ecg | safety_verdict | hr_stale | none has ever arrived | PASS |
| `ecg_connect_failure_dsp` | ecg | safety_verdict | hr_stale | none has ever arrived | PASS |
| `ecg_frames_stop_silently` | ecg | safety_verdict | hr_stale | hr_stale | PASS |
| `ecg_sequence_gap` | ecg | programme_complete | - | - | PASS |
| `ecg_electrodes_off` | ecg | safety_verdict | hr_stale | hr_stale | PASS |
| `ecg_dsp_flat` | ecg | safety_verdict/shutdown | hr_stale | hr_stale | PASS |
| `ecg_dsp_saturated` | ecg | safety_verdict/shutdown | hr_stale | hr_stale | PASS |
| `ecg_dsp_mains` | ecg | safety_verdict/shutdown | hr_stale | hr_stale | PASS |
| `ecg_dsp_stopped` | ecg | safety_verdict/shutdown | hr_stale | hr_stale | PASS |
| `ecg_dsp_corrupted` | ecg | safety_verdict/shutdown | hr_stale | hr_stale | PASS |
| `ecg_dsp_gaps` | ecg | safety_verdict/shutdown | hr_stale | hr_stale | PASS |
| `process_sigterm_auto_baseline` | process | shutdown | - | emergency zero | PASS |
| `process_sigterm_auto_warmup` | process | shutdown | - | emergency zero | PASS |
| `process_sigterm_auto_hold` | process | shutdown | - | emergency zero | PASS |
| `process_sigterm_auto_cooldown` | process | shutdown | - | emergency zero | PASS |
| `process_sigterm_auto_recovery` | process | shutdown | - | emergency zero | PASS |
| `process_sigterm_manual_ramp_up` | process | operator_stop/shutdown | - | emergency zero | PASS |
| `process_sigterm_manual_at_speed` | process | operator_stop/shutdown | - | emergency zero | PASS |
| `process_sigterm_manual_ramp_down` | process | operator_stop/shutdown | - | emergency zero | PASS |
| `process_tick_raises_auto_warmup` | process | tick_exception | - | silent | PASS |
| `process_tick_raises_auto_hold` | process | tick_exception | - | silent | PASS |
| `process_tick_raises_manual_ramp_up` | process | tick_exception | - | silent | PASS |
| `process_loop_stall_manual_5s` | process | safety_verdict | loop_stall | SLF; loop_stall | PASS |
| `process_loop_stall_auto_hold_5s` | process | safety_verdict | loop_stall | SLF; loop_stall | PASS |
| `process_loop_stall_manual_1s` | process | operator_stop/programme_complete/shutdown | loop_stall | loop_stall | PASS |
| `process_loop_stall_auto_hold_2s` | process | operator_stop/programme_complete/shutdown | loop_stall | loop_stall | PASS |
| `process_wall_clock_jump_forward` | process | programme_complete | - | - | PASS |
| `process_wall_clock_jump_backward` | process | programme_complete | - | - | PASS |
| `operator_double_start_manual` | operator | operator_stop | - | demarrage refuse; deja | PASS |
| `operator_double_start_auto` | operator | shutdown | - | demarrage refuse; deja | PASS |
| `operator_stop_while_idle` | operator | operator_stop | - | operator_estop | PASS |
| `operator_stop_after_refused_start` | operator | refused | - | variateur en defaut (OCF | PASS |
| `operator_fault_reset_while_turning` | operator | safety_verdict | drive_fault | reset refuse | PASS |
| `operator_fault_reset_after_standstill` | operator | safety_verdict | drive_fault | OLF (LFT 17) | PASS |
| `operator_fault_reset_forbidden` | operator | safety_verdict | drive_fault | OCF (LFT 9); non rearmable | PASS |
| `operator_restart_after_finish` | operator | shutdown | - | emergency zero | PASS |
| `operator_fault_reset_while_running` | operator | operator_stop | - | reset refuse | PASS |
| `operator_target_above_nameplate` | operator | operator_stop | - | consigne refusee | PASS |
| `operator_target_in_the_gap` | operator | operator_stop | - | consigne refusee | PASS |
| `operator_target_negative` | operator | operator_stop | - | consigne refusee | PASS |
| `operator_target_absurd` | operator | operator_stop | - | consigne refusee | PASS |

The same families run against the REAL composition root (`build_panel`, HTTP
API, `LocalPanel.run` for SIGTERM and a dying web server) in
`raspberry-pi/tests/test_failure_{rig,drive,ecg,process,operator}.py`. The
five strict xfails they used to carry are fixed (idle console leaving a turning
drive, misaddressed writes never escalated, echo never checked, refused disable
retried silently - `src/training`; corrupted ECG graded good -
`src/ecg_pipeline.py`) and are ordinary passing tests now. Cases only the panel can express: the
web server dying (EXIT_FAILED, shutdown, shaft 0), the dashboard link raising or
unreachable (session unaffected), a real SIGTERM through `LocalPanel.run`, the
HTTP refusals (409/422 with a reason), NaN samples, a transient BITalino
disconnect that reconnects.

## Findings about `raspberry-pi/src`

1. **FIXED - Vasovagal: the controller accelerated before `hr_drop` fired.**
   Now (`src/training/runtime.py`, the vasovagal gate): the setpoint may not
   RISE while the least-squares slope of the last five accepted readings
   (`RuntimeLimits.trend_samples`) is below `RuntimeLimits.falling_trend` =
   -20 bpm/min, or unknown - the controller is refused the increase, a climb
   already under way towards an earlier demand is held, and the controller
   adopts the held speed. The verdict still dominates. `vasovagal_auto_hold`:
   the setpoint stays at 968 motor rpm through the collapse, `hr_drop` at
   t = 920 s; `vasovagal_auto_warmup`: held, `hr_drop` at t = 505 s; both PASS.
   Residual (cohort S07, strict xfail): a +12 rpm step decided on the very
   tick the scripted collapse starts, before the sensor shows any fall (true
   123 bpm, read 122), is still walked out; nothing rises after the first
   measurable fall. Original finding: a collapse of 30 bpm in 20 s in HOLD:
   the heart rate falls 145 -> 121 bpm while the
   setpoint rises 968 -> 1039 motor rpm (+1.4 output rpm, leg-tip g 1.03 -> 1.18)
   for ~16 s, until `hr_drop`'s 25 bpm / 30 s threshold trips (`vasovagal_auto_hold`;
   in WARMUP 600 -> 733 rpm while the rate falls 83 -> 68, `vasovagal_auto_warmup`).
   Nothing gates the control law on a falling heart rate before that rule.
2. **FIXED - Programmed sessions bypassed the anti-nausea limits.** Now every
   non-emergency programme setpoint change (the controller's demand, the
   warm-up, the cooldown) walks `src/training/motion.py` exactly as a manual
   target does; only the safety descents (REDUCE / RAMP_DOWN at the 15 rpm/s
   slew) and the emergency zero are faster. A programme's 0 <-> min_run
   passage additionally waits for the slew budget.
   `test_programmed_sessions_hold_the_anti_nausea_limits` passes (0 violations;
   `auto_jog_150_nominal` measured arm 0.28 output rpm/s over 1 s windows
   within the RFRD slack, g-dot 0.017 g/s). Original finding: the control law steps
   the setpoint by up to 75 motor rpm once per 5 s period and the drive executes
   each step on its own ramp: the measured arm accelerates at up to ~0.54 output
   rpm/s over 1 s windows (limit 0.25), and the g-dot limit is not applied. The
   cooldown descends at 15 rpm/s = 0.30 output rpm/s. `config/motion_limits.json`
   says the limits apply to every non-emergency setpoint change; only manual
   sessions honour them (`test_programmed_sessions_hold_the_anti_nausea_limits`).
3. **The g-dot limit is evaluated at `ARM_RADIUS_M` only.** At 27 rpm the leg tip
   (2.43 m) sees 0.038 g/s while the reference radius sees 0.023 g/s. The
   runtime side is FIXED: `TrainingRuntime(..., limit_radius: Metres | None =
   None)` evaluates the g-rate limit at that radius (refused below the
   reference radius), tested at 2.43 m in `raspberry-pi/tests/test_runtime.py`.
   STILL OPEN until `src/local_panel.py` (`LEG_TIP_RADIUS_M`) and
   `simulation/harness.py` pass the leg-tip radius
   (`test_the_g_rate_limit_holds_at_the_leg_tip_too`, strict xfail).
4. **Real DSP path: false heart rates graded "good" under motion noise.** With the
   synthesiser's modelled artefact (3.5 mV per g), the DSP reports up to 159 bpm
   for a true 90 bpm at only ~0.2 g; `hr_rate` then `hr_drop` (a false
   vasovagal) end the programme at the start of HOLD (`auto_jog_150_dsp`). The
   quality grade does not catch the artefact, and nothing checks a reading
   against the recent trend before the safety rules act on it (a +50..70 bpm
   step within a second is not physiological).
5. **FIXED - `hr_unresponsive` fired in every nominal jog warm-up.** Now it is
   judged against the commanded LOAD (g at the reference radius, carried in
   `SafetyObservation.commanded_g`): the mean load must rise by
   `unresponsive_g_rise` = 0.08 g across the halves of the window and the later
   half must average at least `unresponsive_min_g` = 0.15 g. No nominal
   scenario raises it any more. Original finding (~350 s): below ~7
   output rpm the heart barely responds because g goes as rpm squared, the rule
   reads that as unresponsiveness and REDUCE walks the setpoint 353 -> 117 rpm
   (a speed reversal) before the warm-up resumes.
6. **FIXED - `hr_drop` fired on the programme's own cooldown** (see 9 for the
   fix); `auto_jog_subject_fast_responder` and `auto_jog_noisy_ecg` complete as
   `programme_complete`. Original finding: for a fast-recovering
   subject (and with +/-4 bpm measurement noise): the completed session is
   recorded as a `safety_verdict` end (`auto_jog_subject_fast_responder`,
   `auto_jog_noisy_ecg`). With that noise, `hr_rate` also raises six nuisance
   REDUCE verdicts in HOLD.
7. **The shipped profiles cannot reach their zone.** `max_rpm` 276 (5.5 output
   rpm, 0.05 g at 1.5 m) moves the modelled heart by ~6 bpm; 118-138 bpm is out
   of reach. The runtime correctly saturates and completes; the profiles are
   bench-only as shipped.

Observed, not defects: `SimulatedDrive.close` models the bare-minimum close, so
after every console exit the simulated drive latches SLF via ttO (the real
`ATV320Drive.close` writes the stop sequence). A manual session with a person on
board rises into `hr_rate` FREEZE before 19 output rpm at the motion-limited
ramp (`manual_occupied_ceiling`).

New in the cohort and failure batteries (each a strict xfail with the numbers):

8. **FIXED - Nothing refused or capped a rider under 16.** Now: a programmed
   start carries the rider's age (console form, or the dashboard launch from the
   birth year) and the console refuses an unknown age or one under
   `MIN_RIDER_AGE` (default 18); the dashboard refuses too. The cohort applies
   the same gate (`rider_age_refusal`) and the test now passes. Original finding:
   A launch carried no age (only
   `subjectHrMax`), `hr_max_from_age` accepts 10, and the jog is fitted to the
   child's HRmax and accepted with the adult 1380 rpm ceiling: S01 (10 years,
   HRmax 196) is spun to 20.9 output rpm, 1.19 g at the leg tip
   (`test_a_rider_under_16_is_refused_or_capped`, 5 XFAIL).
9. **FIXED - `hr_drop` fired on subjects who are not fainting.** Now
   (`src/training/safety.py`): both ends of the fall are medians of fresh
   readings - the level the median of the last five (`hr_drop_confirm_samples`:
   three of five must agree), the peak the highest running median of nine
   (`hr_drop_peak_samples`) - so one or two artefacts cannot make a fall or a
   peak; and the threshold is what the load being removed does not explain:
   with `phi = g_now / g_ref` (`g_ref` the highest load in the last
   `hr_drop_load_window` = 120 s) the confirmed level must reach
   `rest + (peak - rest) * phi - (15 + 10 * phi)` bpm and be at least 15 bpm
   below the peak (`hr_drop_rest_margin_bpm`). Unloaded nothing (`phi = 1`,
   HOLD) that is the old 25 bpm rule; fully unloaded (RECOVERY) it is 15 bpm
   below the resting rate. Every non-vasovagal cohort pair now passes
   (0 false `hr_drop`); the vasovagal subject still trips it. Original finding
   A burst of THREE artefacts (S27: 97 -> 71, 73, 72 bpm, true 97) moves a
   median of five for three readings, so the fall must also hold on four
   consecutive fresh readings (`hr_drop_persist_samples`); a real collapse
   keeps deepening and still ends the session inside its 20 s window.
   Original finding (finding 6, far more
   common than the fast-responder case): 10 of 25 eligible non-vasovagal
   subjects end the jog as `safety_verdict` because the programme's own COOLDOWN
   drops a nominal heart 25 bpm in 30 s (e.g. S01 143 -> 118 bpm at t=1301 s),
   two ectopic subjects never leave BASELINE (one artefact reading, 99 -> 71 bpm
   at t=0), two motion-artefact subjects trip `hr_rate` then `hr_drop`. There
   is no outlier rejection and no "load is coming off" context.
10. **FIXED - An idle console left a drive found turning untouched.** Now the
    idle poll that finds the drive OPERATION_ENABLED or turning takes the link
    over, zeroes the reference (run command kept, the drive's own ramp),
    latches `drive_precommanded` QUICK_STOP and ends; `_settle` removes the
    output stage at standstill; START is refused until a named acknowledgement.
    `drive_stuck_enabled_idle_console`: caught on the first poll (t = 0.2 s),
    shaft 900 -> 0, output disabled. Original finding: a drive left
    OPERATION_ENABLED at 900 motor rpm by a crashed process stays at 900 rpm for
    the whole 60 s idle pre-roll (120 s in the panel test) with no verdict: the
    read-only idle poll feeds ttO. Only an operator's START (refused
    `DrivePrecommanded`) zeroes it (`drive_stuck_enabled_idle_console`).
11. **FIXED - `tracking_error` was blind while the setpoint ramped.** Now it is
    judged against an envelope the runtime states every tick
    (`src/training/tracking.py`: between the setpoint and a follower of it at
    the drive's own ACC/dEC read back at arming, derated 3x). Stuck RFRD during
    the climb: caught at t = 67 s (7 s after sticking, was 52 s); stuck at speed
    and frozen status during the stop: t = 207 s (7 s after the STOP, was
    110 s); frozen status in the jog's HOLD: t = 510 s (6 s after freezing, at
    the first correction, was 63 s after the cooldown began). Original
    finding: a manual climb
    or stop at the motion limits ramps for ~100 s: RFRD stuck at 774 rpm while
    the setpoint climbs to 1344 is caught 52 s later; a frozen status during a
    stop 110 s after the STOP; a frozen status in a programme's HOLD 63 s after
    the cooldown starts (`drive_speed_not_following_*`, `drive_status_frozen_*`).
12. **FIXED - Writes the drive acks but misaddresses were never escalated.**
    Now `tracking_error` escalates to GO_SILENT when the LFRD echo has also
    disagreed with the writes for `setpoint_echo_dwell` = 1 s: neither the
    register nor the shaft shows a write landing, so the runtime stops writing
    and the drive's ttO stops the motor. `drive_register_offset_at_speed`:
    `setpoint_unconfirmed` at t = 201.4 s, `tracking_error` GO_SILENT at
    t = 207.2 s, ttO takes over. A misaddressed keepalive at CONSTANT speed is
    indistinguishable from a landed one, so detection waits for the setpoint to
    move (here, the operator's stop). Original finding: register
    offset at 27 rpm: `tracking_error` RAMP_DOWN, "emergency zero
    ACKNOWLEDGED", and the shaft still at 1344 rpm 90 s later with the runtime
    ENDING and not silent; the misaddressed keepalive keeps feeding ttO, so the
    one stop that does not need a write to land (GO_SILENT) never happens
    (`drive_register_offset_at_speed`; panel: 799 rpm for 300 s).
13. **FIXED - The LFRD echo was shown, never acted on.** Now the supervisor's
    `setpoint_unconfirmed` rule (RAMP_DOWN, latched) fires when LFRD read back
    in the same tick disagrees with what was written for 1 s; the descent is
    watched and escalates only if the shaft does not follow it.
    `drive_setpoint_echo_mismatch`: t = 61.4 s (1.4 s), controlled stop, not
    silent. Original finding: echo stuck at 774 while 1344 is
    written: no verdict, no operator message, the session runs to its stop
    (`drive_setpoint_echo_mismatch`; `drive.py` says writes must be verified).
14. **FIXED - A refused stop word was retried forever, silently.** Now after
    `RuntimeLimits.disable_attempts` = 5 consecutive refusals at confirmed
    standstill: a refused SWITCH_ON is followed by SHUTDOWN (transition 8,
    harmless only because the shaft is confirmed stopped) and a latched
    `disable_refused` RAMP_DOWN names the word; if SHUTDOWN is refused too, the
    verdict is GO_SILENT and ttO drops the stage. A drive in FAULT is not
    retried at all (every word but FAULT_RESET is refused by design).
    `drive_refuses_switch_on_at_stop`: 5 refused frames, output disabled,
    verdict at t = 304.8 s. Original finding: SWITCH_ON refused at
    the end of a manual session: ~480 failed frames at 5 Hz until the console
    exits, output stage left OPERATION_ENABLED at 0 rpm, no verdict, nothing on
    the operator's screen (`drive_refuses_switch_on_at_stop`).
15. **The real DSP grades noise "good".** Uniformly random ADC counts (a
    corrupted stream) give 116-146 bpm for a true 70 bpm; losing 3 batches in 4
    gives 47 then 135 bpm for a true 71; both then end the session on a FALSE
    presyncope `hr_drop` (146 -> 115, 135 -> 41) instead of `hr_stale`. Nothing
    checks batch continuity (`ecg_dsp_corrupted`, `ecg_dsp_gaps`). Flat,
    saturated, 50 Hz hum and stopped frames are handled (no rate, `hr_stale`).

FIXED - the drive fault table: `LFT_FAULT_CODES` is now the COMPLETE Schneider
enumeration (66 faults + nOF, from the manufacturer's communication-parameters
file), replacing a provisional table whose numbers were mostly wrong (SLF1 is 5
- which is what the bench saw after a Modbus loss - not 19; OCF is 9, not 15).
`NO_MOTOR` is gone: "nOF" means "no fault", "no motor" was never a code. A code
no table knows (a newer firmware; the simulator uses 251) still reads as
`UNKNOWN` with its number and is never resettable from the console. Every code
is injected in an auto and a manual session above and stops the machine;
`raspberry-pi/tests/test_drive_faults_complete.py` does the same on the real
console for every code. Losing the ECG in RECOVERY lets
the programme complete as `programme_complete` after FREEZE/REDUCE verdicts (the
shaft is already stopped). A frozen status at CONSTANT speed is indistinguishable
from a truthful one; the runtime never removes the run command on it. As soon as
the setpoint moves away from it, the frozen echo and RFRD escalate to GO_SILENT
(finding 11).

16. **FIXED - `hr_rate` read one reading as a rate.** The rate of rise was the
    difference of the two raw endpoints of its 60 s window, so one ectopic or
    motion-spiked reading at either end was the whole rate: 341 nuisance
    REDUCE episodes across the cohort (S20 95 + 83, S27 80 + 79 - the ectopic
    subjects, even in BASELINE with nothing turning - S06 3, S07 1) and 2 in
    `auto_jog_noisy_ecg`. Now it is the least-squares slope of the running
    medians of five fresh readings (`hr_rate_median_samples`), still over a
    minimum 20 s span: isolated outliers leave every median, and so the line,
    where the heart is; a sustained rise beyond 25 bpm/min fires within 20 + 5
    readings. After: 0 in the cohort, 0 in the noisy-ECG scenario; the two
    occupied manual scenarios keep their one genuine episode each (the heart
    really rises during the climb).
