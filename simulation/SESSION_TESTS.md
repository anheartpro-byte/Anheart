# Session acceptance tests (proposal)

Proposed acceptance tests per session type, what each one proves, and where it is
implemented today. "sim" = `simulation/` (the real `raspberry-pi/src` runtime,
drive simulator and physiology on the deterministic clock); "panel" = the real
composition root `src.local_panel.build_panel` driven through its HTTP API in
`raspberry-pi/tests/test_failure_*.py` / `test_local_panel_e2e.py` /
`test_cloud_sync.py`. **XFAIL** means implemented as a strict xfail: the test
states correct behaviour that `raspberry-pi/src` does not deliver today.

Every test, whatever its type, also asserts the common exit contract: after the
session (and a teardown window with nobody ticking) the shaft reads 0 rpm, the
drive produces no torque, LFRD is 0 wherever a frame could still be written, no
number is NaN, and no exception escaped.

## 1. AUTO (heart-rate driven programme)

| # | test | what it proves | status |
|---|---|---|---|
| A1 | Nominal jog 145-155 bpm, 30 min, nominal subject | the control law reaches and holds the zone (>= 50 % of HOLD) and completes | sim `auto_jog_150_nominal` |
| A2 | Every cohort subject x jog, eligible only if zone <= 90 % HRmax | the real gate (`ProfileStore.resolve` with the rider's HRmax) refuses exactly the riders it must; every trace holds every invariant | sim `test_cohort.py` (29 accepted / 1 refused; 10 XFAIL, see README) |
| A3 | Every cohort subject x shipped `standard_30_min` | shipped profile saturates at its 276 rpm ceiling, never exceeds it, completes | sim `test_cohort.py` |
| A4 | Rider under 16 | refused, or capped below the adult ceiling | sim `test_a_rider_under_16_is_refused_or_capped` **XFAIL** |
| A5 | Vasovagal collapse in WARMUP / HOLD | the setpoint never rises while the heart rate collapses; `hr_drop` ends the session | sim `vasovagal_auto_*`, cohort S04/S07 **XFAIL** |
| A6 | Non-responder | saturates at the ceiling, never above it (windup) | sim `auto_jog_subject_nonresponder`, cohort S18 |
| A7 | Fast / slow responder, drift, unfit, fit | no overshoot above the hard max, completes | sim `auto_jog_subject_*` (fast responder **XFAIL**) |
| A8 | Cooldown of a nominal subject | a heart rate falling as the load comes off is not presyncope (no `hr_drop`) | sim cohort (10 subjects **XFAIL**) |
| A9 | Heart rate above hard max / critical | REDUCE / QUICK_STOP, end `safety_verdict` | sim `hr_above_hard_max`, `hr_critical`, `hr_spike_artifact` |
| A10 | ECG lost at every phase (disconnect, silent stop, electrodes off, connect failure) | `hr_stale` FREEZE -> REDUCE -> RAMP_DOWN; nothing moves without a first rate | sim `ecg_*` (8 cases), panel `test_failure_ecg.py` |
| A11 | Real DSP: flat, saturated, 50 Hz hum, frames stopped | no false rate graded usable; the rate goes stale | sim `ecg_dsp_*`, panel |
| A12 | Real DSP: corrupted samples, lost batches | no false rate graded usable | sim `ecg_dsp_corrupted`, `ecg_dsp_gaps` **XFAIL**; panel corrupted **XFAIL** |
| A13 | Drive comm loss at every phase | `comms_lost` GO_SILENT, not one frame after, the drive's ttO (SLF) stops it | sim `drive_comm_timeout_auto_*` (5), panel |
| A14 | Every drive fault in HOLD (USF OPF OCF OLF SCF SLF nOF InF ObF unknown) | `drive_fault`, mnemonic + LFT code shown, never auto-reset | sim `drive_fault_*_auto` (10), panel |
| A15 | SIGTERM at every phase | emergency zero, link closed, end `shutdown` | sim `process_sigterm_auto_*` (5), panel `LocalPanel.run` |
| A16 | Tick raises in WARMUP / HOLD | fail closed: silent, end `tick_exception`, ttO backstop | sim `process_tick_raises_auto_*` |
| A17 | Loop stall 2 s / 5 s in HOLD | FREEZE under ttO; over ttO GO_SILENT and SLF | sim `process_loop_stall_auto_*` |
| A18 | Wall clock +/- 1 h | nothing changes (monotonic time rules) | sim `process_wall_clock_jump_*`, panel |
| A19 | Frozen drive status in HOLD | caught within seconds of the cooldown asking for less speed | sim `drive_status_frozen_auto_hold` **XFAIL** (63 s late) |
| A20 | Attendant absent | FREEZE then RAMP_DOWN | sim `attendant_absent_auto` |
| A21 | Anti-nausea limits in a programme | measured arm within 0.25 rpm/s and 0.03 g/s | not asserted (README finding 2: programmes bypass them) - proposed as XFAIL |
| A22 | Occupied programme through the panel with `max_rpm` above the 1.2 g occupied ceiling | refused before anything turns | panel `test_cloud_sync.py` |

## 2. MANUAL (operator target, at the machine only)

| # | test | what it proves | status |
|---|---|---|---|
| M1 | Bench 0 -> 27 rpm -> hold -> STOP | ramps inside the motion limits (0.25 rpm/s, 0.03 g/s), reaches 27, stops, REPOS | sim `manual_27_rpm`, cohort x30, panel e2e |
| M2 | Ladder / retarget mid-ramp | the setpoint turns round at the limits | sim `manual_ladder`, `manual_retarget_mid_ramp` |
| M3 | Target out of range: 32 rpm, 0.5 rpm (gap), -5, 1000, infinite | refused with "consigne refusee", the speed held | sim `operator_target_*`, `manual_32_rpm_refused`, panel |
| M4 | Occupied ceiling (1.2 g resultant) | 27 refused, 19 accepted | sim `manual_occupied_ceiling`, panel (refused by config until M6) |
| M5 | E-stop at speed / during a ramp, then a target | reference zeroed at once, nothing resumes, acknowledge returns to REPOS | sim `estop_*`, panel e2e |
| M6 | Comm loss climbing / cruising / stopping | GO_SILENT, ttO stops it | sim `drive_comm_timeout_manual_*` (3), panel |
| M7 | Every drive fault at speed | `drive_fault` RAMP_DOWN, mnemonic shown | sim `drive_fault_*_manual` (10), panel |
| M8 | Drive found enabled and turning when the console starts | zeroed, disabled, latched **without waiting for START** | sim `drive_stuck_enabled_idle_console` **XFAIL**, panel **XFAIL**; with START: sim/panel PASS |
| M9 | Drive refuses ENABLE_OPERATION / SWITCH_ON at start | start refused with the reason, nothing energised | sim `drive_refuses_*_at_start`, panel |
| M10 | Drive refuses SWITCH_ON at the stop | the operator is told; the output stage does not stay enabled silently | sim `drive_refuses_switch_on_at_stop` **XFAIL**, panel **XFAIL** |
| M11 | LFRD echo stops following | a verdict, the operator told | sim `drive_setpoint_echo_mismatch` **XFAIL**, panel **XFAIL** |
| M12 | Writes acked but misaddressed at speed | the stop handed to ttO (GO_SILENT) when writes do not land | sim `drive_register_offset_at_speed` **XFAIL**, panel **XFAIL** |
| M13 | Measured speed stops following during a climb | `tracking_error` within seconds | sim `drive_speed_not_following_ramp` **XFAIL** (52 s late); at speed: panel PASS |
| M14 | Frozen status, then STOP | caught during the descent; run command kept | sim `drive_status_frozen_during_stop` **XFAIL** (late); panel PASS (run command kept) |
| M15 | Tick raises / loop stall 1.4 s / 5 s / SIGTERM at each stage | fail closed / FREEZE / GO_SILENT+SLF / shutdown | sim `process_*_manual*`, panel |
| M16 | Double START, STOP while idle, E-STOP after the end | refused with words / harmless | sim `operator_*`, panel |
| M17 | FAULT RESET while running / while turning / after standstill + acknowledge | refused, refused, accepted (and nothing moves) | sim `operator_fault_reset_*` |
| M18 | Restart after a finished session | a new session armed from scratch, nothing resumed | sim `operator_restart_after_finish` |
| M19 | Manual with a person on board, heart rate critical | QUICK_STOP | sim `hr_critical_manual_occupied` |

## 3. REMOTE (dashboard launch and stop)

| # | test | what it proves | status |
|---|---|---|---|
| R1 | Dashboard launch runs; dashboard stop ends it | the launch goes through the surface's gates, the stop reaches the runtime once | panel `test_cloud_sync.py` |
| R2 | Launch for a rider whose HRmax the zone exceeds | refused, reported back as a failed session with the reason | panel `test_a_preset_too_hard_for_the_rider_is_refused`; cohort A2 exercises the same `resolve` |
| R3 | Launch of an unknown preset / above the occupied ceiling / tiers mismatch / programmes disabled / occupied disabled | refused before anything turns, reason sent back | panel `test_cloud_sync.py` |
| R4 | Remote end mid-HOLD (auto) and at 27 rpm (manual) | ends as `operator_stop`, cooldown ramp | sim `stop_remote_auto`, `stop_remote_manual` |
| R5 | Dashboard unreachable / its step raising during a session | the session is unaffected, the error logged | panel `test_failure_process.py`, `test_cloud_sync.py` |
| R6 | Remote launch while a local session runs | refused ("busy"), no poll while busy | panel `test_cloud_sync.py` |
| R7 | Remote launch for a rider under 16 | refused or capped (the launch carries no age today) | proposed; the cohort's A4 **XFAIL** is the same defect |
| R8 | Remote stop while the drive link is dead | the stop is still honoured by ttO; the dashboard learns the real end reason | proposed |
| R9 | Launch lost in transit (no confirmation) | the machine does not start a session nobody confirmed; timeout reported | panel `test_a_launch_nobody_answers_times_out` |

## 4. How to run them

```sh
export PYTHONPATH=.:raspberry-pi; PY=raspberry-pi/.venv/bin/python
$PY -m simulation.quick --all --dsp           # everything in sim, with the real DSP, report.html
$PY -m pytest simulation/tests -q              # the sim battery (gate: simulation/scripts/check.sh)
cd raspberry-pi && .venv/bin/pytest tests/test_failure_*.py tests/test_local_panel_e2e.py tests/test_cloud_sync.py -q
```
