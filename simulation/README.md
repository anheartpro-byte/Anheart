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
  tracefile.py                shared schema-2 recordings, legacy JSONL export and CSV
  run.py                      CLI: python -m simulation.run
  live.py                     2D viewer server + live streaming (stdlib only)
  viewer/index.html           Layer 2: the 2D view (single static file)
  scenarios/*.json            the battery (scenarios/_profiles.json: extra profiles)
  faultdrive.py               DriveBackend wrapper: refused command words, echo mismatch, stuck RFRD, frozen status
  faultsource.py              BITalino sample corruption before the REAL DSP (flat, saturated, noise, hum, stops, gaps)
  failures.py                 the failure-injection matrix and its judge
  cohort/generate.py          the seeded 30-subject cohort generator -> cohort/cohort.json
  cohort/battery.py           every subject x {auto jog, auto standard 30 min, manual 27 rpm bench}
  quick.py                    CLI: python -m simulation.quick (instant verdicts + report.html/json)
  SESSION_TESTS.md            acceptance-test mapping, implemented cases and remaining proposals
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
* **reference radius** (`ARM_RADIUS_M`, where the runtime quotes g): **1.5 m**, the value raspberry-pi's tests and console use.
  It does not come from the CAD (the occupant lies down; there is no seat).
  The harness judges the g-rate limit at the larger of this radius and the
  leg-tip radius. The console accepts the measured `LEG_TIP_RADIUS_M` for that
  purpose; when it is unset, it falls back to `ARM_RADIUS_M`.

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
  production path, with waveform processing costs that depend on the host).

The loop runs a 15 s pre-roll (the idle poll also handles a drive found turning), the start
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
* motion limits on the measured arm over 1 s windows, with the register-rounding
  slack defined in `check_motion_limits` (configured limits: 0.25 output rpm/s,
  0.03 g/s);
* after going silent, not one frame, reads included;
* a refused start never moved anything;
* **every exit path**: after teardown the drive produces no torque, the shaft
  reads 0 rpm, and (where a frame could be delivered) LFRD is 0 and the runtime
  commands nothing.

## Running

```sh
simulation/scripts/check.sh                      # the gate: lint, both type checkers, battery, 100% coverage
$PY -m pytest -c simulation/pyproject.toml simulation/tests -q  # full battery; DSP dominates
$PY -m pytest -c simulation/pyproject.toml simulation/tests/test_cohort.py -q  # cohort
$PY -m pytest -c simulation/pyproject.toml simulation/tests/test_failures.py -q  # failures
$PY -m simulation.cohort.generate [--check]      # regenerate (or verify) cohort/cohort.json
$PY -m simulation.run --list                     # the scenarios
$PY -m simulation.run manual_27_rpm --csv        # schema-2 directory + optional CSV in simulation/out/
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
a documented defect (XFAIL). Skipped DSP-dependent cases also permit exit 0:
check the verdict counts before calling the matrix complete.

Measured on 2026-10-05, macOS arm64, Python 3.12.13, from source
`7b1857b` with `--workers 2` (other test jobs were running on the host):

| Command suffix after `$PY -m simulation.quick` | Result | Wall time |
|---|---|---|
| `--failures --workers 2` | 192 PASS, 7 SKIPPED | 87.81 s |
| `--failures --dsp --workers 2` | 199 PASS, no skips or XFAIL | 182.30 s |
| `--cohort --workers 2` | 89 PASS, 1 XFAIL (S07 jog) | 61.99 s |
| `S07 --workers 1` | 2 PASS, 1 XFAIL | 3.83 s |

These are quick-run timings, not the full pytest/coverage gate. The full battery
also checks properties, parsers, lifecycle and recording behavior; budget tens
of minutes and record its actual terminal duration. Obtain current collection
with `$PY -m pytest -c simulation/pyproject.toml simulation/tests --collect-only -q`.
Keep the printed counts and generated `report.json` / `report.html` with the
command, revision, platform and flags; timings are not fixed guarantees.

## Layer 2: the 2D view

```sh
$PY -m simulation.live                            # http://127.0.0.1:8765/
```

* live: `http://127.0.0.1:8765/?live=manual_27_rpm&speed=20` (or pick a scenario
  in the header). Deterministic simulated time shown at N x; add `&clock=sim`
  for the codebase's `SimClock` (real time scaled; keep the speed modest, a
  host pause then becomes a loop stall the runtime reacts to, as it should);
* playback of a current recording: `http://127.0.0.1:8765/?trace=out/<UTC>_<local_ref>`
  through `simulation.live`; the folder contains `manifest.json`, `events.jsonl`,
  `ticks.csv` and any captured ECG blocks;
* legacy playback: the viewer still accepts a schema-1 `.jsonl` via its file
  picker or `?trace=out/<legacy-file>.jsonl`.

`simulation.run` writes schema-2 directories by default; `--csv` adds a CSV named
with the recording's opaque local reference. The explicit legacy JSONL export
remains in `Trace.write_jsonl`. See [the recording format](../docs/enregistrement.md).

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

The harness records what the OPERATOR is told in `RunResult.messages` and trace
events of kind `verdict`, `drive_fault`, `refusal` or `end`: every new verdict with its sentence, every drive
fault with its mnemonic and LFT code, every refused start/target/reset worded by
`src.local_panel.describe_*`, and the shutdown report.

`known_defect` marks a scenario whose expectations describe correct behaviour
that `raspberry-pi/src` does not deliver today: the battery runs it as a strict
xfail (it turns red the day the defect is fixed).

## The battery

The current scenarios and expectations are the JSON documents in
[`scenarios/`](scenarios/), enumerated by `$PY -m simulation.run --list`.
Generate current per-scenario results with `$PY -m simulation.run --all`:
`simulation/out/summary.md` and `summary.json` hold the verdicts for that run.
The source documents and generated results replace the former copied numeric
table, which had diverged from the current geometry and control code.

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
`standard_30_min`, manual 27 rpm bench}: a programme first passes the same
`MIN_RIDER_AGE` gate as the console, then resolves for the rider's HRmax through
`ProfileStore.resolve`, which refuses a zone above 90% of that HRmax.
The battery then checks every invariant on each trace, and for auto runs: never above the
programme ceiling, `hr_drop` must fire for a vasovagal-prone subject and must
NOT fire for anybody else (it is the presyncope rule). Tests:
[`tests/test_cohort.py`](tests/test_cohort.py).

Measured 2026-10-05 with `--cohort --workers 2`: 89 PASS, 1 XFAIL (S07's jog, README finding 1's residual), 0 FAIL. Every
rider under `MIN_RIDER_AGE` (default 18: the 8 subjects aged 10-17) is refused
for both programmes before anything turns (finding 8), and S08's jog is refused
on HRmax (172 < 155 / 0.9). The false `hr_drop` pairs (13 COOLDOWN ends, two
ectopic subjects stuck in BASELINE, two motion-artefact subjects) are gone
(finding 9). Manual bench sessions are identical by design (nobody rides a bench
session): 27.0 rpm, 1.98 g at the leg tip for all 30.

For subject characteristics, read [`cohort/cohort.json`](cohort/cohort.json).
For each subject's peak speed, g, zone fraction, violations and end reason,
regenerate the quick report with `$PY -m simulation.quick --cohort`. The zone
fraction is the share of HOLD ticks with a usable rate inside the zone;
`standard_30_min` never reaches its zone on this model (finding 7).

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
Measured 2026-10-05 with `--failures --dsp --workers 2`: 199 PASS, 0 XFAIL,
0 SKIPPED. Without `--dsp`: 192 PASS and 7 SKIPPED; that is not full DSP coverage. Every one of the drive's 66 fault codes (the complete Schneider LFT
enumeration) plus an unknown code is injected in an auto and a manual session,
and every one ends in a controlled stop with the code named to the operator.

The authoritative case names, categories, expected end reasons, rules,
messages and deadlines are in [`failures.py`](failures.py). The generated
quick `report.json` / `report.html` contains each observed verdict, replacing
the duplicated static result table. [`tests/test_failures.py`](tests/test_failures.py)
also corrupts traces deliberately to verify that the judge detects broken promises.

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

The numbers below retain the existing finding IDs. FIXED describes a software
regression covered by the named scenario/test; it is not hardware or medical
acceptance. Current verdicts come from the commands above.

1. **FIXED, with an onset residual — acceleration during a vasovagal fall.**
   The runtime's trend gate holds increases once the five-reading trend is
   below -20 bpm/min or unknown. `vasovagal_auto_hold` and
   `vasovagal_auto_warmup` exercise the corrected response.
   **S07 AUTO jog remains a strict XFAIL.** Reproduced on 2026-10-05
   (macOS arm64, Python 3.12.13, committed cohort and source `7b1857b`):
   the setpoint walks **887 → 895 motor rpm** over t=679.0–679.8 s.
   At the decision tick the true rate is 133 bpm and the sensor reads 132;
   the four rising ticks have a true rate of 132 bpm. The sensor next drops
   to 128 at t=681 s, with no further increase; `hr_drop` fires at **t=701 s**.
   The invariant still reports four `vasovagal_no_accel` violations.
   Reproduce with `$PY -m simulation.quick S07`; classification lives in
   `cohort/battery.py:KNOWN_VASOVAGAL_ONSET`.
2. **FIXED — programmes bypassed the motion limits.**
   Non-emergency programme changes use `training/motion.py`; safety descents
   and emergency zero retain their own paths.
   `tests/test_limits.py::test_programmed_sessions_hold_the_anti_nausea_limits`
   checks the measured arm with the invariant's register-quantization slack.
3. **FIXED — the g-rate limit ignored the leg tip.**
   The console passes `config.leg_tip_radius`; the harness passes the larger
   of leg-tip and reference radius to `TrainingRuntime(limit_radius=...)`.
   `tests/test_limits.py::test_the_g_rate_limit_holds_at_the_leg_tip_too`
   is an ordinary passing regression. A real installation still has to set
   the measured `LEG_TIP_RADIUS_M`; an unset value falls back to the reference.
4. **FIXED — motion-noise heart rates reached control without an independent veto.**
   `ecg_pipeline.py` accepts a legacy DSP rate only when the typed ECG
   processor grades the same continuous window GOOD and agrees within 5 bpm.
   Otherwise it withholds the rate, allowing `hr_stale` to act.
   `auto_jog_150_dsp` exercises the real waveform path; run it with `--dsp`
   in quick mode. This is a modeled-noise regression, not clinical validation.
5. **FIXED — `hr_unresponsive` fired before load increased enough.**
   The rule now requires sufficient commanded-load rise and level before
   judging the heart-rate response. Nominal jog scenarios exercise it.
6. **FIXED — ordinary cooldown caused false `hr_drop`.**
   `auto_jog_subject_fast_responder`, `auto_jog_noisy_ecg` and the
   non-vasovagal cohort pairs exercise the load-aware fall rule (finding 9).
7. **Model limitation — shipped profiles cannot reach their zone.**
   Their 276 motor-rpm ceiling cannot reach 118–138 bpm on the modeled subject.
   `tests/test_limits.py::test_the_shipped_profiles_cannot_reach_their_zone_and_saturate_safely`
   asserts saturation within the ceiling and completion; the simulation does
   not authorize raising the shipped limits.
8. **FIXED — programmed starts lacked an age gate.**
   `rider_age_refusal` refuses an unknown age or one below `MIN_RIDER_AGE`
   (default 18). The cohort uses the same gate; eight current subjects aged
   10–17 are refused for both programmes. The historical test name
   `test_a_rider_under_16_is_refused_or_capped` remains, but the configured
   minimum also covers ages 16 and 17. Manual bench sessions have no rider.
9. **FIXED — isolated artifacts and unloading looked like presyncope.**
   `training/safety.py` judges confirmed medians of fresh readings against
   the recent commanded load and resting rate. Non-vasovagal cohort pairs
   have no false `hr_drop`; S07 still triggers the rule during its real
   simulated collapse and retains finding 1's onset XFAIL.
10. **FIXED — idle polling left a previously enabled drive turning.**
    The idle runtime zeroes the reference, latches `drive_precommanded` and
    settles the output after measured standstill, without waiting for START.
    Covered by `drive_stuck_enabled_idle_console` and the panel regression
    `test_a_drive_found_turning_at_startup_is_stopped_without_a_start`.
11. **FIXED — tracking ignored ramps and frozen status.**
    The runtime checks a moving envelope derived from commissioned ramps.
    `drive_speed_not_following_ramp`, `drive_status_frozen_during_stop` and
    `drive_status_frozen_auto_hold` assert their detection deadlines.
    Frozen status at unchanged speed is not evidence of a new failure;
    disagreement becomes detectable when the demanded speed changes.
12. **FIXED — acknowledged but misaddressed writes never yielded to ttO.**
    A tracking error with a persistently wrong LFRD echo escalates to
    GO_SILENT. `drive_register_offset_at_speed` checks silence and the
    watchdog stop; the panel has the same failure regression.
13. **FIXED — LFRD echo mismatch was only displayed.**
    `setpoint_unconfirmed` latches RAMP_DOWN after the configured mismatch
    dwell. `drive_setpoint_echo_mismatch` checks the verdict and operator message.
14. **FIXED — refused disable was retried indefinitely.**
    At confirmed standstill, repeated refused stop words produce
    `disable_refused`; if disabling cannot be completed, the runtime goes
    silent. `drive_refuses_switch_on_at_stop` and its panel regression check
    the output state and visible refusal.
15. **FIXED — corrupt or gapped ECG was graded usable.**
    The independent veto and continuity tracking in `ecg_pipeline.py`
    withhold rates from those windows. `ecg_dsp_corrupted` and `ecg_dsp_gaps`
    pass with `--dsp`; `raspberry-pi/tests/test_failure_ecg.py` also checks
    corrupt signals ending on `hr_stale` and gapped samples yielding no false
    usable rate.
16. **FIXED — one reading distorted `hr_rate`.**
    The rule fits the slope of running medians over fresh readings rather
    than using two raw endpoints. The cohort's ectopic/noisy subjects and
    the noisy-ECG scenario exercise that response.

The fault matrix uses `LFT_FAULT_CODES`: all 66 named faults plus an unknown
code in AUTO and MANUAL. `NO_FAULT_STORED` / nOF is not a fault. These cases
verify the simulator and software decisions; they do not verify a physical
drive's commissioned ttO, SLL, braking or STO wiring.
