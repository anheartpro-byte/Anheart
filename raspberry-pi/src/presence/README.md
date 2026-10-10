# Presence fail-safe (camera)

A camera will watch the machine. This package is everything except the vision
model: the typed observation a detector publishes, the rules that turn it into
stops and start refusals, a scriptable simulated camera, and the adapter that
applies decisions through the runtime's existing API. Nothing in
`src/training/` knows this package exists.

| Module | Role |
|---|---|
| `types.py` | `PresenceObservation`, `PresenceSource` protocol, `MotionState`, `MachineContext`, `Confidence` (parsed at the boundary) |
| `monitor.py` | `PresenceMonitor`: pure, clock-injected rules; `PresenceLimits`; the closed decision union `Clear \| StartBlocked \| RampDown \| EmergencyStop` |
| `simulated.py` | `SimulatedCamera`: intrusion, flicker, capsule/posture change, degraded/failed image, freeze, dropout |
| `adapter.py` | `PresenceGuard` (reads source, judges, applies once), `PresenceAcknowledger` (the console's acknowledger), `motion_state`, `session_occupancy` |

## Two kinds of answer

* **Motion possible** (`TURNING`, `ARMED`, or `UNKNOWN`): a rule that fires
  produces a `SafetyVerdict` (`QUICK_STOP` or `RAMP_DOWN`) and **every verdict
  latches**. The latch works like the supervisor's floor: a more severe verdict
  replaces a less severe one, an equal one keeps the first, and only a named
  acknowledgement lowers it. Nothing resumes on its own. A person stepping back
  out does not restart anything.
* **At rest** (no session, output off, shaft *measured* stopped): nothing
  latches. The rules become a **start gate** that refuses, in French, while the
  evidence says a start would be unsafe. A presence latch still standing is
  itself a refusal (`presence_latched`). Not latching at rest is deliberate:
  people are meant to be at the arm at rest (boarding, harness checks). A latch
  there would demand an acknowledgement at every boarding, and alarm fatigue is
  how the acknowledgement that matters gets clicked through.

`ARMED` counts as moving because an armed machine moves at the next click.
`UNKNOWN` (no fresh drive observation) counts as moving because unknown is
never "stopped".

## Rules

Dwell runs from the capture time of the first frame of an episode, clamped to
`now`. An episode ends only after `release_after` (0.2 s) of contrary healthy
evidence. While contrary evidence is arriving the episode does not fire, so a
single false-positive frame never stops the machine, while a detection that
flickers keeps its accumulated dwell.

### While motion is possible (latched verdicts)

| Rule id | Condition | Dwell | Action |
|---|---|---|---|
| `presence_intrusion` | person in zone, conf >= 0.50 (held down to 0.30) | 0.10 s; **none** if conf >= 0.80, or distance <= 0.5 m at conf >= 0.50 | QUICK_STOP |
| `presence_bench_occupied` | BENCH session, capsule seen OCCUPIED | 0.2 s | QUICK_STOP |
| `presence_camera_lost` | no fresh HEALTHY frame for > 0.5 s, or detector reports FAILED | none past the limit | RAMP_DOWN |
| `presence_zone_uncertain` | detection conf >= 0.30 persisting | 1.0 s | RAMP_DOWN |
| `presence_rider_absent` | OCCUPIED session, capsule seen EMPTY | 1.0 s | RAMP_DOWN |
| `presence_capsule_unknown` | OCCUPIED session, capsule UNKNOWN | 3.0 s | RAMP_DOWN |
| `presence_rider_unbuckled` | OCCUPIED session, harness seen unfastened | 0.5 s | RAMP_DOWN |
| `presence_limb_outside` | OCCUPIED session, a limb outside the capsule | 0.3 s | RAMP_DOWN |

### At rest (start gate refusals, not latched)

| Rule id | Refuses when |
|---|---|
| `presence_latched` | any presence verdict is still latched |
| `presence_camera_lost` | no fresh healthy frame (> 0.5 s), or FAILED |
| `presence_zone_not_clear` | the zone has not been seen clear on healthy frames for 2.0 s |
| `presence_rider_absent` | OCCUPIED declared, capsule seen EMPTY |
| `presence_capsule_unknown` | OCCUPIED declared, capsule UNKNOWN |
| `presence_rider_unbuckled` / `presence_limb_outside` | OCCUPIED declared, posture says so |
| `presence_bench_occupied` | BENCH declared, capsule seen OCCUPIED |

A BENCH start with the capsule UNKNOWN is allowed: the motor may be uncoupled,
with no capsule in view, and UNKNOWN does not contradict "nobody on board".
Tighten this in `_occupancy_refusals` if the camera always sees the seat.

### Why these numbers

* **Confirmation window, 100 ms.** This is two frames at 20 fps, and at least
  one full frame interval at the 15 fps minimum. It rejects a single-frame false
  positive (a reflection, a shadow), which would otherwise stop the machine
  often enough to train operators to distrust the camera. The cost against the
  stop itself is under 3%: the drive decelerates on its 3-4 s commissioned
  ramp. A person walking at 1.5 m/s covers 15 cm in 100 ms. Anything the
  detector is sure of (>= 0.80), or that is already close (<= 0.5 m), skips the
  window. The window can only make a stop later by 100 ms; it can never prevent
  one, because a person who stays is confirmed and a flicker keeps the episode.
* **Stale limit, 0.5 s.** 7 frames at 15 fps and more than three times the
  150 ms latency budget, so it means a camera that has stopped, not jitter. It
  triggers RAMP_DOWN, not QUICK_STOP: a machine that cannot see its zone must
  not keep turning, but a camera fault is not an emergency.
* **Clear before start, 2.0 s.** A person walking round the machine is briefly
  hidden by the arm. A start granted in that gap would be a start with somebody
  in the zone.
* **Dwell tolerance, 1 us.** Frame timestamps are sums of binary floats. Without
  slack, a 100 ms window at 20 fps could need a third frame. The slack only ever
  fires a rule earlier.

## The fail-safe asymmetries

* Only a frame whose `frame_seq` advanced is evidence. A frozen camera repeats
  its frame and says "clear" about it forever; its unchanged sequence number
  makes that visible as staleness. This works like `HeartRateSample.seq`.
* A detection counts in **any** frame, degraded or not. An *absence* of
  detection counts only in a **HEALTHY** frame. So a degraded image can stop the
  machine but can never clear the zone, refresh freshness, or say anything
  about the capsule.
* A frame stamped in the future is clamped to `now`, so it cannot keep the
  camera looking fresh.
* A frame captured earlier than the one that opened an episode moves the
  episode start back, which only ever fires sooner.

## Camera requirements

| Requirement | Value | Why |
|---|---|---|
| Frame rate | **>= 15 fps**, 20 fps recommended | two frames inside the 100 ms window at 20 fps |
| Capture-to-observation latency | **<= 150 ms** (p99) | dwells run from capture time; 150 ms + 100 ms keeps the intrusion stop under 0.3 s from first sight |
| `frame_seq` | strictly increasing **across detector restarts** (for example seeded from monotonic nanoseconds) | a restarted counter is treated as no evidence, so it reads as a lost camera until it overtakes the old value |
| `at` | capture instant on **this Pi's monotonic clock** | ages are computed against the monitor's `now`. A detector on another host must convert. |
| `health` | the detector's own image check (exposure, obstruction, blur) | "nobody seen" in a bad image must not read as "clear" |
| Coverage | the whole swept envelope plus a margin, and the seat | the zone is whatever the detector calls the zone; commission it with a person walking the boundary |

"Stale" means: no frame whose `frame_seq` advanced **and** whose `health` is
HEALTHY has been captured within `stale_after` (0.5 s) of `now`. It also covers
the case where no such frame has ever arrived since the monitor was built.

## How a real camera plugs in

1. A **detector process**, separate from the console, owns the camera and the
   vision library (for example OpenCV plus a person detector). Per contract
   rule 5 the library is imported in exactly one module of that process, behind
   a stub. The console never imports it, so a vision-library crash or a GIL-heavy
   inference cannot stall the loop that carries the drive keepalive.
2. It publishes one record per analysed frame over a local channel (a Unix
   datagram socket or a shared-memory slot), fields as in `PresenceObservation`,
   stamped with `CLOCK_MONOTONIC` at capture.
3. In the console, **one** reader module (`src/presence/detector_link.py`, to be
   written) implements `PresenceSource.latest()`. A background thread fills a
   single slot from the socket, and `latest()` reads the slot, so it never blocks.
   That module is the parse boundary (contract rule 6): it validates ranges
   (`parse_confidence`, a non-negative `frame_seq`, a known `health` string) and
   drops a malformed record rather than inventing one. A dropped record simply
   ages the camera toward `presence_camera_lost`.
4. `SimulatedCamera` stays the source for tests and `MOTOR_BACKEND=sim`.

## Wiring into the console

The integration lives in [`src/local_panel.py`](../local_panel.py):

- `build_presence` selects no guard for `PRESENCE_SOURCE=none` (the default),
  or a `SimulatedCamera` for `sim_empty` / `sim_occupied`.
- `build_panel` passes the guard to `LocalPanel` and wraps the runtime in
  `PresenceAcknowledger` so an accepted acknowledgement clears both latches.
- `LocalPanel.run` schedules `presence_step` every `PRESENCE_PERIOD` (0.05 s,
  20 Hz), independently of the control tick.
- `_start_manual` and `_start_programme` call `_presence_refusal` before
  starting; it asks the guard's `start_gate` about the declared occupancy.

[`tests/test_panel_presence.py`](../../tests/test_panel_presence.py) exercises
this wiring through the real composition root. The adapter's rules and
acknowledgement behavior are covered in
[`tests/test_presence_adapter.py`](../../tests/test_presence_adapter.py).
Only simulated camera sources are implemented; the real detector link described
above remains to be written.

## What the existing supervisor covers, and what it does not

Everything above works through the unchanged API:

* `trip_from_thread(rule, action, detail)` accepts any rule id and **always
  latches** into the supervisor floor, and the floor is cleared only by the
  named `acknowledge`. So the supervisor's latching already matches the presence
  latch, and the session log and console carry the `presence_*` rule id and its
  French detail.
* The runtime already refuses every start while the supervisor has a standing
  verdict (`SafetyStanding`), so a drained presence trip also blocks starts.

Three gaps, each worked around here and each worth a small supervisor change:

1. **No synchronous latch that is not the mushroom.** The only synchronous
   entry is `latch_estop`, via `request_estop`. It is recorded as
   `operator_estop` ("operator emergency stop: camera presence: ...") and demands
   `estop_released=True` on acknowledgement, although no mushroom was pressed.
   `trip_from_thread` names the rule correctly but takes effect only at the
   next tick (up to 200 ms). The adapter does both: `request_estop` for the
   immediate zero, then the trip for the rule id. **Recommended:** a
   `SafetySupervisor.latch_now(rule, action, detail)` (synchronous, non-blocking
   and published in one attribute write like `latch_estop`, but not setting the
   estop slot), and a `TrainingRuntime.request_quick_stop(rule, detail)` that
   zeroes the reference exactly as `request_estop` does. The operator would then
   not be asked about a mushroom that nobody touched.
2. **`ALL_RULES` is closed and presence ids are not in it.** That is harmless
   (thread trips bypass the trackers), but dashboards that enumerate
   `ALL_RULES` will not list `presence_*`. **Recommended:** export this
   package's rule ids (`MOTION_RULES` and `START_RULES`, each id once) next to
   it wherever rule ids are catalogued.
3. **Two latches, one acknowledgement.** The presence monitor keeps its own
   latch (the start gate needs it, and it outlives a supervisor ack that the
   runtime refused). `PresenceAcknowledger` clears both together, and clears
   the presence latch only when the runtime accepted, or had nothing latched.
   A supervisor-side `acknowledge` hook for external latches would remove the
   wrapper, but it is not required.
