---
name: anheart-strict-python
description: The mandatory typing, error-handling and testing contract for the Anheart Raspberry Pi client under raspberry-pi/. Read this BEFORE writing or editing any Python in this repository, including a one-line change. Covers the domain-unit NewTypes, banned constructs, the Result[T, E] pattern, the untyped-library isolation rule, the clock-injection rule, and the 100%-branch-coverage gate.
---

# Anheart strict Python contract

## Why

The code under `raspberry-pi/` commands a Schneider ATV320 variable-frequency
drive, which spins a motoreducer, which spins a centrifuge **with a person
inside it**. The heart rate measured from an ECG decides how fast that motor
turns.

The bug classes that matter here are not crashes. They are a heart rate passed
where revolutions-per-minute are expected, a motor-shaft speed confused with an
output-shaft speed (a factor of 49.79), a stale measurement regulated on as if
it were fresh, or an unhandled drive error that leaves the motor commanded.
Every one of those is silent at runtime and catastrophic in context.

So: **types are verified at compile time, generic types are banned, and the
safety chain is covered to 100% of branches.** This costs more effort than
idiomatic Python. That is the accepted trade.

## The gate

Nothing merges unless this passes. Run it from `raspberry-pi/`:

    ./scripts/check.sh            # or .\scripts\check.ps1 on Windows

which is:

    ruff check . && ruff format --check .
    basedpyright                                    # strict, zero Any - BLOCKING
    mypy .                                          # second opinion - BLOCKING
    pytest --cov --cov-branch --cov-fail-under=100

All configuration lives in `raspberry-pi/pyproject.toml` (tool sections only;
there is deliberately no `[project]` table). The operator console runs with
`python -m src.local_panel`; `python -m src.main` is the legacy ECG recorder.

## 1. Use domain units, never bare numbers

`src/units.py` defines `NewType` aliases. They cost nothing at runtime and make
the dangerous mix-ups **fail to compile**:

    Bpm, MotorRpm, OutputRpm, RpmPerSecond, GLoad, Seconds, Monotonic,
    UnixMillis, Hertz, Millivolts, AdcCount, Amperes,
    RegisterAddress, RawRegister, StatusWord

    # WRONG - compiles, and one day spins the motor at 150 rpm because
    # someone handed it a heart rate
    def set_speed(rpm: int) -> None: ...

    # RIGHT
    def set_speed(rpm: MotorRpm) -> Result[None, DriveError]: ...

`MotorRpm` and `OutputRpm` are distinct types on purpose: the gearbox ratio is
49.79, so confusing them is a 50x error. Conversions go through the single
helper set in `src/units.py`, which has a round-trip test.

`Monotonic` and `UnixMillis` are distinct too: a monotonic reading is
meaningless as a wall-clock timestamp and vice versa.

## 2. Banned constructs

The tools enforce these. Do not argue with them, fix the code.

| Banned | Use instead |
|---|---|
| `Any`, explicit or implicit | a real type; write a stub if the library lacks one |
| bare `dict` / `list` / `tuple` | `Mapping[K, V]`, `Sequence[X]`, `tuple[X, ...]`, or a `@dataclass(frozen=True, slots=True)` |
| `str` carrying meaning (`quality: str`, `phase: str`) | an `Enum`: `SignalQuality`, `Phase`, `DriveState`, `DriveFault`, `SafetyAction` |
| `Optional[X]` with no explicit `None` branch | handle it, or make it non-optional |
| `# type: ignore` with no error code | `# type: ignore[code]` plus a comment saying why |
| `assert` as control flow | a real check; `assert` vanishes under `-O` |
| `time.monotonic()` / `time.time()` called directly | see rule 4 |

Structures are frozen dataclasses with `slots=True`. Mutable shared state is
the exception, not the default, and every instance of it is commented.

## 3. Errors: `Result[T, E]`, never exceptions, in the motor path

`src/result.py` provides `Ok[T]`, `Err[E]` and
`Result[T, E]: TypeAlias = Ok[T] | Err[E]`. Error types are frozen dataclasses
in a **closed union**. Every `match` ends in `assert_never`:

    match await drive.write_speed(rpm):
        case Ok():
            pass
        case Err(error):
            match error:                          # narrow to Err, THEN match
                case CommTimeout():
                    safety.trip("drive_comm", SafetyAction.GO_SILENT)
                case DriveFaulted():
                    safety.trip("drive_fault", SafetyAction.RAMP_DOWN)
                case _ as unreachable:
                    assert_never(unreachable)     # a new variant fails HERE

**Use the nested form.** Matching variants directly inside `Err(...)` -
`case Err(CommTimeout())` - runs correctly but does **not** narrow the type
argument, so `assert_never` cannot see exhaustiveness and the check silently
buys you nothing. Extract the error with `case Err(error)` and match that.

The last line is the point: adding a variant to `DriveError` breaks every
incomplete `match` at check time, with an error naming the variant you forgot.
**An unhandled drive error cannot reach production.**
`tests/test_typing_contract.py` runs mypy against a deliberately-incomplete
fixture to prove this stays true. Exceptions are still fine in the web layer
and in scripts.

## 4. Never read the clock directly

Every timing decision takes `now` as a parameter or reads an injected `Clock`
(`src/clock.py`: the `Clock` protocol, `RealClock`, `SimClock(speed=60)`).

This is not style. It is what makes a 45-minute session testable in 45 seconds,
and the closed-loop tests are the only evidence the control law is safe before
the hardware exists. A grep-based test fails the build on a direct
`time.monotonic()` or `time.time()` call inside `src/` (outside `clock.py`).

## 5. Isolate untyped libraries in exactly one module each

`bitalino`, `biosppy`, `scipy`, `pyftdi`, `pyusb`, pyobjc and parts of `pymodbus` ship no usable types. Zero-`Any`
therefore requires hand-written stubs under `raspberry-pi/stubs/`, covering only
the members actually used.

And each untyped library is imported from **one** module:

| Library | Only module allowed to import it |
|---|---|
| `bitalino` | `src/bitalino_client.py` |
| `biosppy` | `src/signal_processing.py` |
| `scipy` (typed wrappers for every sensor processor) | `src/dsp.py` |
| `pymodbus`, `serial` | `src/motor/atv320.py` |
| `pyftdi`, `usb` (pyusb) | `src/motor/ftdi_link.py` |
| pyobjc: `Foundation`, `IOBluetooth`, `objc` (macOS only, imported lazily) | `src/bitalino_rfcomm_macos.py` |

Everything else sees domain types only. If you need a new member of one of
these libraries, extend its stub. Do not add an `ignore_missing_imports`
override.

## 6. Parse at the hardware boundary, trust types behind it

No type checker knows what a copper wire sends. So exactly one layer converts
Modbus registers and ADC counts into domain types, **validating ranges** and
returning `Err` when out of domain (register outside 0..65535, ADC outside
0..1023, rpm outside the clamps, an incoherent status word).

Past that layer there is no runtime validation; the static types carry it. This
is "parse, don't validate": runtime checks live only where the static world is
structurally blind.

## 7. Coverage: 100% of branches on the safety chain

The authoritative blocking scope is `[tool.coverage.report].include` in
`raspberry-pi/pyproject.toml`, with `fail_under = 100` and branch coverage
enabled. It includes the motor/training chain, console composition and
lifecycle, dashboard link, sensor processing and presence fail-safe. Consult
that list directly rather than maintaining a second module inventory here.

`[tool.anheart].safety_chain` records the full required scope;
`coverage_pending` records the explicit migration debt. Currently
`src/signal_processing.py` is pending, not part of the blocking include list.
It remains safety-critical: **the heart rate that drives the motor comes out
of it**, so a mis-parsed analog column or a mis-classified signal quality can
make the machine accelerate on noise. `tests/test_typing_contract.py` checks
the relationship between these lists; do not weaken them to pass the gate.

`# pragma: no cover` is **not** an accepted waiver inside that scope. An
unreachable branch in the safety chain is a design smell: delete the branch or
test it.

### Coverage is necessary, not sufficient

100% of branches does not prove the controller never exceeds its slew rate.
Property tests (`hypothesis`) are what bound behaviour, and they carry the real
guarantee:

- output always within `{0} union [min_run, max_rpm]`, for any heart-rate sequence
- the change in output never exceeds slew rate x dt, ever
- a safety verdict always dominates the controller's demand
- no `NaN` or `inf` propagates from a missing heart rate or a corrupt frame
- the integrator cannot diverge under prolonged saturation
- unit conversions round-trip exactly

Plus the plant sweep: `K in [0.05, 0.20]` x `tau in [20, 70]` x
`theta in [4, 12]`, asserting no overshoot above the zone ceiling and no
sustained oscillation.

Exit-path regressions live in `tests/test_failure_process.py` (tick exceptions,
loop stalls, SIGTERM and web-server failure) and `tests/test_panel_lifecycle.py`
(cancellation, stop and web failure while an observer is hung). These exercise
the real console with a simulated drive: the reference is zeroed, and shutdown
is checked against the drive's ramp and watchdog. Preserve the distinction
between a commanded stop and a shaft observed stopped.

## 8. Safety invariants that are code, not comments

- The **safety supervisor has precedence over the controller**, always. The
  runner applies the safety verdict first; the controller's demand is used only
  when there is no verdict. This exists because in the vasovagal case the
  correct response is the *opposite* of the control law's: a falling heart rate
  reads as "below zone", and a naive controller would speed up on someone who
  is fainting.
- Fresh measurements only. `treat_batch` **re-emits the previous metrics dict**
  when extraction fails, so "unchanged" does not mean "still true". The
  controller gates on `SignalTreatment.metric_seq(channel)` and ignores a
  repeated sequence number.
- A heart rate is reported only when `quality == "good"`. Never fabricate one.
- `drive.service()` is called **first** in `_tick()`. If the loop stalls, the
  keepalive stops and the drive's own `ttO` timeout ramp-stops the motor. The
  independent watchdog is deliberately outside this process: any watchdog we
  wrote would die with the loop it was watching.
- Never assume the drive's state at startup. If `ETA` reports
  `OPERATION_ENABLED`, a previous crashed process left the motor spinning:
  command zero, disable, latch, and require an operator acknowledgement.
- No automatic fault reset, and no automatic resumption of motion, anywhere.
- Nothing blocks the event loop. All FastAPI handlers are `async def` (asserted
  at startup); blocking calls (`pymodbus`, `subprocess`, `serial.tools`) go
  through `run_in_executor`.

## 9. When something here is wrong

If a rule blocks something genuinely correct, say so and propose the change.
Do not weaken a check locally to get past it. Silencing a checker in the safety
chain needs a comment naming what was verified by hand instead.
