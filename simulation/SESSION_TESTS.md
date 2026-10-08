# Session acceptance-test map

Acceptance checks per session type, their software assertions and implementation.
Rows explicitly marked proposed remain unimplemented here. "sim" = `simulation/` (the real `raspberry-pi/src` runtime,
drive simulator and physiology on the deterministic clock); "panel" = the real
composition root `src.local_panel.build_panel` driven through its HTTP API in
`raspberry-pi/tests/test_failure_*.py` / `test_local_panel_e2e.py` /
`test_cloud_sync.py`. **XFAIL** means implemented as a strict xfail: the test
states correct behaviour that `raspberry-pi/src` does not deliver today.

The scenario/failure/cohort trace judges check the common exit contract after a
teardown window: shaft at 0 rpm, no torque, LFRD at 0 wherever a frame could be delivered,
and finite numbers. Dedicated tests also exercise expected exceptions and
refused starts; those are not evidence that every individual unit test runs a
complete session. These are software checks, not physical acceptance.

Current matrix/cohort results and exact --dsp semantics are in the
[simulation README](README.md#instant-results-python--m-simulationquick).
PASS rows below refer to the 2026-10-05 reconciliation; rerun the named checks
for a new revision. The remaining cohort XFAIL is S07 AUTO jog; proposals are
not counted as passing tests.

## 1. AUTO (heart-rate driven programme)

| # | test | what it proves | status |
|---|---|---|---|
| A1 | Nominal jog 145-155 bpm, 30 min, nominal subject | the control law reaches and holds the zone (>= 50 % of HOLD) and completes | sim `auto_jog_150_nominal` |
| A2 | Every cohort subject x jog | the age gate and ProfileStore.resolve refuse underage riders or zones above 90% HRmax; eligible traces are judged | sim test_cohort.py; eight riders below18 plus S08 refused; only S07 jog remains XFAIL |
| A3 | Every cohort subject x shipped standard_30_min | eligible nominal subjects saturate within276 motor rpm and complete; underage subjects are refused; a vasovagal event ends on safety | sim test_cohort.py |
| A4 | Rider below MIN_RIDER_AGE (default18), or unknown age | programmed start refused before motion | sim test_the_real_gate_refuses_exactly_the_riders_the_brief_says and test_a_rider_under_16_is_refused_or_capped; panel age-gate tests |
| A5 | Vasovagal collapse in WARMUP / HOLD | trend gate prevents increases after a measurable fall; hr_drop ends the session | sim vasovagal_auto_* pass; S07 jog onset remains XFAIL (README finding1); S04 refused by age gate |
| A6 | Non-responder | saturates at the ceiling, never above it (windup) | sim `auto_jog_subject_nonresponder`, cohort S18 |
| A7 | Fast / slow responder, drift, unfit, fit | nominal programme completes under the hard-max tier | sim auto_jog_subject_*; fast-responder case is an ordinary regression |
| A8 | Cooldown of a nominal subject | falling rate as load comes off is not classified as presyncope | sim non-vasovagal cohort pairs; ordinary regressions |
| A9 | Heart rate above hard max / critical | RAMP_DOWN after hard-max dwell / immediate QUICK_STOP at critical, end safety_verdict | sim hr_above_hard_max, hr_critical, hr_spike_artifact |
| A10 | ECG lost at every phase (disconnect, silent stop, electrodes off, connect failure) | hr_stale escalates; nothing moves without a first rate; RECOVERY may complete with the shaft already stopped | sim ecg_*; panel test_failure_ecg.py |
| A11 | Real DSP: flat, saturated, 50 Hz hum, frames stopped | no false rate graded usable; the rate goes stale | sim `ecg_dsp_*`, panel |
| A12 | Real DSP: corrupted samples, lost batches | independent veto and continuity checks withhold unusable heart rates | sim ecg_dsp_corrupted, ecg_dsp_gaps PASS with --dsp; panel corrupt/gapped regressions pass |
| A13 | Drive comm loss at every phase | `comms_lost` GO_SILENT, not one frame after, the drive's ttO (SLF) stops it | sim `drive_comm_timeout_auto_*` (5), panel |
| A14 | Every named drive fault and an unknown code in HOLD | drive_fault, mnemonic + LFT code shown, no auto-reset | sim drive_fault_*_auto covers66 named faults plus unknown; nOF is not a fault |
| A15 | SIGTERM at every phase | emergency zero, link closed, end `shutdown` | sim `process_sigterm_auto_*` (5), panel `LocalPanel.run` |
| A16 | Tick raises in WARMUP / HOLD | fail closed: silent, end `tick_exception`, ttO backstop | sim `process_tick_raises_auto_*` |
| A17 | Loop stall 2 s / 5 s in HOLD | FREEZE under ttO, and the programme then ends by itself (`programme_complete`); over ttO GO_SILENT and SLF | sim `process_loop_stall_auto_*` |
| A18 | Wall clock +/- 1 h | nothing changes (monotonic time rules) | sim `process_wall_clock_jump_*`, panel |
| A19 | Frozen drive status in HOLD | tracking/echo discrepancy is detected when the demanded speed changes, within the case deadline | sim drive_status_frozen_auto_hold PASS |
| A20 | Attendant absent | FREEZE then RAMP_DOWN | sim `attendant_absent_auto` |
| A21 | Anti-nausea limits in a programme and at the leg tip | motion limits hold on the measured arm, with the checker's register-quantization slack | sim test_programmed_sessions_hold_the_anti_nausea_limits and test_the_g_rate_limit_holds_at_the_leg_tip_too PASS |
| A22 | Occupied programme through the panel with `max_rpm` above the 1.2 g occupied ceiling | refused before anything turns | panel `test_cloud_sync.py` |
| A23 | The programme's own cooldown under a latched FREEZE: shipped 30-min programme, loop stall 1.4 s in HOLD (`loop_stall`), no stop asked for | the setpoint is held to the end of HOLD, walks down from the entry into COOLDOWN with the setpoints of the nominal run, and the session ends at its planned duration as `programme_complete`; no `session_overrun`, no standstill latched (ANH-189) | sim `auto_cooldown_under_latched_freeze`; `tests/test_cooldown_under_freeze.py`; panel `test_cooldown_freeze_console.py` |
| A24 | An ending opened late: shipped 30-min programme, operator STOP at 1600 s, in the programme's own recovery with the arm at rest | the STOP re-opens a whole 300 s recovery, which ends at 1900 s; `session_overrun` stays silent past the programme's 1830 s, the console is back at rest with nothing to acknowledge, and a START at 1930 s is accepted (ANH-185) | sim `stop_operator_auto_late`; runtime `tests/test_runtime_ending_alerts.py` (STOP, e-stop and electrodes off, late); panel `test_session_overrun_console.py` |
| A25 | Heart rate above hard max for 3 s, then the electrodes come off (fresh readings with no rate) for 90 s, in HOLD | the dwell is not restarted: RAMP_DOWN `hr_hard_max` 5 s after the first reading above the limit, judged on the last usable rate; `hr_stale` never becomes the standing verdict; end safety_verdict (ANH-213) | sim `hr_above_hard_max_then_signal_lost`; unit `raspberry-pi/tests/test_safety.py`, `test_runtime_level_rules.py` |

## 2. MANUAL (operator target, at the machine only)

| # | test | what it proves | status |
|---|---|---|---|
| M1 | Bench 0 -> 27 rpm -> hold -> STOP | ramps inside the motion limits (0.25 rpm/s, 0.03 g/s), reaches 27, stops, REPOS | sim `manual_27_rpm`, cohort x30, panel e2e |
| M2 | Ladder / retarget mid-ramp | the setpoint turns round at the limits | sim `manual_ladder`, `manual_retarget_mid_ramp` |
| M3 | Target out of range: 32 rpm, 0.5 rpm (gap), -5, 1000, infinite | refused with "consigne refusee", the speed held | sim `operator_target_*`, `manual_32_rpm_refused`, panel |
| M4 | Occupied ceiling (1.2 g resultant) | 27 refused, 19 accepted | sim `manual_occupied_ceiling`, panel (refused by config until M6) |
| M5 | E-stop at speed / during a ramp, then a target | reference zeroed at once, nothing resumes, acknowledge returns to REPOS | sim `estop_*`, panel e2e |
| M6 | Comm loss climbing / cruising / stopping | GO_SILENT, ttO stops it | sim `drive_comm_timeout_manual_*` (3), panel |
| M7 | Every named drive fault and an unknown code at speed | drive_fault RAMP_DOWN, mnemonic shown | sim drive_fault_*_manual covers66 named faults plus unknown; panel |
| M8 | Drive found enabled and turning at startup | zeroed and latched without waiting for START; output disabled after standstill | sim drive_stuck_enabled_idle_console PASS; panel test_a_drive_found_turning_at_startup_is_stopped_without_a_start PASS |
| M9 | Drive refuses ENABLE_OPERATION / SWITCH_ON at start | start refused with the reason, nothing energised | sim `drive_refuses_*_at_start`, panel |
| M10 | Drive refuses SWITCH_ON at stop | bounded retries, visible disable_refused verdict, output disabled or silence/watchdog fallback | sim drive_refuses_switch_on_at_stop PASS; panel refused-disable regression PASS |
| M11 | LFRD echo stops following | setpoint_unconfirmed verdict and operator message | sim drive_setpoint_echo_mismatch PASS; panel echo regression PASS |
| M12 | Writes acked but misaddressed at speed | GO_SILENT hands stopping to ttO once tracking and echo discrepancies show writes do not land | sim drive_register_offset_at_speed PASS; panel misaddressed-write regression PASS |
| M13 | Measured speed stops following during a climb | tracking_error within the case deadline | sim drive_speed_not_following_ramp PASS; panel tracking regression PASS |
| M14 | Frozen status, then STOP | tracking/echo discrepancy detected during descent; no unverified torque removal | sim drive_status_frozen_during_stop PASS; panel frozen-status regression PASS |
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
| R4b | STOP under a FREEZE: operator STOP 12 s into a lost heart rate (auto, `hr_stale`), remote end under a latched `loop_stall` at 27 rpm (manual) | the setpoint walks down from the next tick under the FREEZE and reaches zero; ends as `operator_stop`, no standstill latched (ANH-175) | sim `stop_operator_auto_under_freeze`, `stop_remote_manual_under_latched_freeze`; `tests/test_stop_under_freeze.py` |
| R5 | Dashboard unreachable / its step raising during a session | the session is unaffected, the error logged | panel `test_failure_process.py`, `test_cloud_sync.py` |
| R6 | Remote launch while a local session runs | refused ("busy"), no poll while busy | panel `test_cloud_sync.py` |
| R7 | Remote launch below MIN_RIDER_AGE (default18), or without age | refused before anything turns; subjectAge travels with the launch | panel test_a_launch_for_a_child_is_refused and test_a_launch_without_the_rider_s_age_is_refused PASS |
| R8 | Remote stop while the drive link is dead | the stop is still honoured by ttO; the dashboard learns the real end reason | proposed |
| R9 | Launch lost in transit (no confirmation) | the machine does not start a session nobody confirmed; timeout reported | panel `test_a_launch_nobody_answers_times_out` |

## 4. How to run them

```sh
export PYTHONPATH=.:raspberry-pi; PY=raspberry-pi/.venv/bin/python
$PY -m simulation.quick --all --dsp           # everything in sim, with the real DSP, report.html
$PY -m pytest -c simulation/pyproject.toml simulation/tests -q              # the sim battery (gate: simulation/scripts/check.sh)
cd raspberry-pi && .venv/bin/pytest tests/test_failure_*.py tests/test_local_panel_e2e.py tests/test_cloud_sync.py -q
```
