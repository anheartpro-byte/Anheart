from __future__ import annotations

from pathlib import Path

import pytest

from src.bitalino_client import ChannelData, SampleBatch
from src.clock import ManualClock
from src.local_panel import RecordedRuntime
from src.presence.simulated import Intrusion
from src.presence.types import Confidence
from src.record import commands
from src.record import session as session_module
from src.record.journal import Journal, Limits, Storage
from src.record.reader import read
from src.record.schema import EventKind
from src.record.session import (
    PRESTART_ENTRIES,
    PRESTART_SAMPLES,
    SessionRecorder,
    SessionRequest,
    operator_alias,
)
from src.result import Err, Ok
from src.training.types import Occupancy, SignalQuality
from src.units import Bpm, Metres, Monotonic, OutputRpm, Seconds, UnixMillis
from tests.record_console_support import recorded_rig, start_bench, stop
from tests.test_failure_rig import (
    BENCH_ENV,
    OPERATOR,
    PROGRAMME_ENV,
    attest,
    make_rig,
    start_programme,
)
from tests.test_panel_presence import camera_of, run
from tests.test_record_session import STAMP, bare_recorder


@pytest.mark.parametrize("attested", [False, True])
async def test_an_unattributed_attestation_refusal_changes_no_runtime_state_or_tick(
    tmp_path: Path, attested: bool
) -> None:
    observed = recorded_rig(tmp_path / "observed")
    baseline = recorded_rig(tmp_path / "baseline")
    if attested:
        for rig in (observed, baseline):
            attest_estop_wiring_result_3 = rig.rig.panel.surface.attest_estop_wiring(OPERATOR)
            assert isinstance(attest_estop_wiring_result_3, Ok)
    supervisor = observed.rig.panel.runtime.supervisor
    before = supervisor.require_estop_confirmed()
    buffered = observed.recorder.prestart_size
    refused = observed.rig.panel.surface.attest_estop_wiring(" ")
    assert isinstance(refused, Err)
    assert supervisor.require_estop_confirmed() == before
    assert observed.recorder.prestart_size == buffered
    for _ in range(5):
        actual = await observed.rig.tick(0.2)
        expected = await baseline.rig.tick(0.2)
        assert actual == expected
    assert supervisor.require_estop_confirmed() == before
    await observed.rig.panel.close()
    await baseline.rig.panel.close()


@pytest.mark.parametrize("rest", [3, 9])
@pytest.mark.parametrize("record_first", [False, True])
async def test_the_window_starts_after_the_last_latch_cleared(
    tmp_path: Path, rest: int, record_first: bool
) -> None:
    recorded = recorded_rig(tmp_path)
    async with recorded.rig.http() as http:
        await recorded.tick(12)
        if record_first:
            await start_bench(recorded, http)
        else:
            await attest(http)
        stopped = await http.post("/api/session/estop", json={"operator": OPERATOR})
        assert stopped.status_code == 200
        await recorded.tick(120)
        assert not recorded.recorder.recording
        cleared = recorded.rig.panel.surface.acknowledge(OPERATOR, estop_released=True)
        assert isinstance(cleared, Ok)
        cleared_at = recorded.clock.monotonic()
        for _ in range(rest * 5):
            await recorded.tick(0.2)
            recorded.recorder.note_batch(raw(recorded.clock))
            recorded.recorder.note_delivery(recorded.clock.monotonic(), recorded.clock.monotonic())
        await start_bench(recorded, http)
    paths = recorded.records()
    assert len(paths) == 1 + int(record_first)
    second = read(paths[-1])
    assert isinstance(second, Ok)
    recording = second.value
    assert (
        min(row.t for row in recording.rows) + recording.manifest.clocks.monotonic_start
        >= cleared_at
    )
    short = any(event.detail.startswith("replay_preamble:") for event in recording.events)
    assert short is (rest < 8)
    assert not any(event.detail == "latch_estop" for event in recording.events)


async def test_the_surface_latch_and_the_runtime_estop_keep_their_own_instants(
    tmp_path: Path,
) -> None:
    recorded = recorded_rig(tmp_path)
    panel, clock = recorded.rig.panel, recorded.clock
    async with recorded.rig.http() as http:
        await recorded.tick(12)
        await start_bench(recorded, http)
        clock.advance(Seconds(0.0123456))
        receipt = panel.surface.submit_estop(operator=OPERATOR, reason="operator pressed stop")
        clock.advance(Seconds(0.2))
        forwarded = clock.monotonic()
        snapshot = await panel.control_step()
        assert snapshot.safety_action.name == "QUICK_STOP"
        recorded.drain()
    recording = recorded.recording()
    origin = recording.manifest.clocks.monotonic_start
    latch = [event for event in recording.events if event.detail == "latch_estop"]
    estop = [event for event in recording.events if event.detail == "estop"]
    assert len(latch) == len(estop) == 1
    assert latch[0].t == receipt.at - origin
    assert estop[0].t == forwarded - origin
    assert latch[0].t < estop[0].t
    assert latch[0].actor == estop[0].actor == operator_alias(OPERATOR)
    assert latch[0].kind is EventKind.OPERATOR_ACTION


async def test_a_programme_refused_after_submission_never_arms_without_a_recorder(
    tmp_path: Path,
) -> None:
    rig, _ = make_rig(tmp_path, env=PROGRAMME_ENV)
    assert rig.panel.recorder is None
    async with rig.http() as http:
        await start_programme(http)
        rig.panel.runtime.request_estop("stop after programme submission")
        await rig.tick(0.2)
    assert rig.refusals()
    assert rig.panel.runtime.state.value == "ending"
    assert not rig.panel.runtime.output_enabled
    await rig.panel.close()
    stopped = await rig.left_stopped()
    assert stopped == ""


async def test_each_new_presence_ping_is_written_once_at_its_unrounded_observation(
    tmp_path: Path,
) -> None:
    recorded = recorded_rig(tmp_path)
    panel, clock = recorded.rig.panel, recorded.clock
    attest_estop_wiring_result = panel.surface.attest_estop_wiring(OPERATOR)
    assert isinstance(attest_estop_wiring_result, Ok)
    clock.advance(Seconds(0.2))
    await panel.control_step()
    clock.advance(Seconds(0.0123456))
    before = panel.surface.note_presence(OPERATOR)
    submit_start_manual_result = panel.surface.submit_start_manual(
        occupancy=Occupancy.BENCH, operator=OPERATOR
    )
    assert isinstance(submit_start_manual_result, Ok)
    clock.advance(Seconds(0.2))
    await panel.control_step()
    clock.advance(Seconds(0.0234567))
    after = panel.surface.note_presence(OPERATOR)
    for _ in range(3):
        clock.advance(Seconds(0.2))
        await panel.control_step()
    recorded.drain()
    recording = recorded.recording()
    origin = recording.manifest.clocks.monotonic_start
    pings = [event for event in recording.events if event.detail == "attendant_ping"]
    assert [event.t for event in pings] == [before - origin, after - origin]
    assert all(event.actor == "system" for event in pings)


def raw(clock: ManualClock, count: int = 200, *, ecg: bool = True) -> SampleBatch:
    return SampleBatch(
        timestamp=UnixMillis(clock.unix_millis() - count),
        channels=(ChannelData("ECG" if ecg else "EDA", (500.0,) * count),),
    )


@pytest.mark.parametrize(
    ("blocks", "lag_ms", "pending", "cut", "gap", "short"),
    [
        (40, 50, False, False, False, False),
        (40, 0, True, False, False, False),
        (39, 50, True, False, False, True),
        (60, 0, True, True, False, True),
        (60, 0, True, False, True, True),
    ],
)
def test_preamble_measures_the_latest_contiguous_acquired_span_before_the_origin(
    tmp_path: Path, blocks: int, lag_ms: int, *, pending: bool, cut: bool, gap: bool, short: bool
) -> None:
    recorder, journal, clock = bare_recorder(tmp_path / "recorded")
    rig, _ = make_rig(tmp_path)
    runtime = rig.panel.runtime
    recorder.observe(clock.monotonic(), runtime.snapshot(), runtime)
    clock.advance(Seconds(1))
    for index in range(blocks):
        if index == 30:
            if cut:
                recorder.discard_preroll()
            if gap:
                clock.advance(Seconds(0.2))
        clock.advance(Seconds(0.2))
        batch = raw(clock)
        recorder.note_batch(
            SampleBatch(timestamp=UnixMillis(batch.timestamp - lag_ms), channels=batch.channels)
        )
        if not pending or index < blocks - 1:
            recorder.note_delivery(clock.monotonic(), clock.monotonic())
        recorder.observe(clock.monotonic(), runtime.snapshot(), runtime)
    clock.advance(Seconds(0.2))
    recorder.begin(SessionRequest(occupancy=Occupancy.BENCH, operator=OPERATOR))
    recorder.note_delivery(clock.monotonic(), clock.monotonic())
    journal.drain()
    path = journal.status(clock.monotonic()).path
    assert path is not None
    loaded = read(path)
    assert isinstance(loaded, Ok)
    warnings = [
        event for event in loaded.value.events if event.detail.startswith("replay_preamble:")
    ]
    assert bool(warnings) is short
    if not short:
        assert sum(block.header.n_samples for block in loaded.value.raw) == 8000


async def test_idle_window_stays_bounded_for_two_hours_and_expires_old_inputs(
    tmp_path: Path,
) -> None:
    recorded = recorded_rig(tmp_path)
    recorder, clock = recorded.recorder, recorded.clock
    panel = recorded.rig.panel
    attest_estop_wiring_result_2 = panel.surface.attest_estop_wiring(OPERATOR)
    assert isinstance(attest_estop_wiring_result_2, Ok)
    steady: set[tuple[int, int]] = set()
    for tick in range(36_000):
        clock.advance(Seconds(0.2))
        panel.surface.note_presence(OPERATOR)
        recorder.note_batch(raw(clock))
        recorder.note_delivery(clock.monotonic(), clock.monotonic())
        await panel.control_step()
        recorded.drain()
        entries, samples = recorder.prestart_size
        assert entries <= PRESTART_ENTRIES
        assert samples <= PRESTART_SAMPLES
        if tick > 300:
            steady.add((entries, samples))
    assert len(steady) <= 4
    assert recorded.records() == []
    submit_start_manual_result_2 = panel.surface.submit_start_manual(
        occupancy=Occupancy.BENCH, operator=OPERATOR
    )
    assert isinstance(submit_start_manual_result_2, Ok)
    clock.advance(Seconds(0.2))
    await panel.control_step()
    recorded.drain()
    recording = recorded.recording()
    assert recording.rows[0].t >= -12
    assert recording.rows[-2].t == 0
    assert recording.manifest.clocks.monotonic_start == pytest.approx(7210)
    attested = [event for event in recording.events if event.detail == "confirm_estop_wiring"]
    assert len(attested) == 1
    assert attested[0].t == pytest.approx(recording.rows[0].t)
    assert all(event.t >= recording.rows[0].t - 1e-9 for event in recording.events)


async def test_an_idle_recorder_bug_costs_the_next_record_and_never_the_tick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded = recorded_rig(tmp_path)

    def broken(_self: SessionRecorder, _held: object) -> None:
        raise RuntimeError("injected idle buffer failure")

    with monkeypatch.context() as patch:
        patch.setattr(SessionRecorder, "_remember", broken)
        await recorded.rig.tick(0.2)
    assert recorded.recorder.is_degraded()
    async with recorded.rig.http() as http:
        await start_bench(recorded, http)
        await recorded.tick(3)
    assert recorded.rig.panel.runtime.state.value == "running"
    assert recorded.degraded()
    assert any(
        event.detail.startswith("record_degraded: dropped=")
        for event in recorded.recording().events
    )


def test_window_counts_a_pending_batch_in_its_entry_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(session_module, "PRESTART_ENTRIES", 2)
    recorder, journal, clock = bare_recorder(tmp_path)
    clock.advance(Seconds(0.2))
    recorder.note_batch(raw(clock))
    recorder.note_command(commands.Stop())
    recorder.note_command(commands.EmergencyStop())
    assert recorder.prestart_size == (2, 200)
    recorder.note_delivery(clock.monotonic(), clock.monotonic())
    assert recorder.prestart_size == (2, 200)
    recorder.begin(SessionRequest(occupancy=Occupancy.BENCH, operator=OPERATOR))
    journal.drain()
    assert journal.status(clock.monotonic()).dropped == 1


@pytest.mark.parametrize("sample_limit", [200, 400])
def test_window_overflow_refuses_the_newest_and_counts_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sample_limit: int
) -> None:
    monkeypatch.setattr(session_module, "PRESTART_SAMPLES", sample_limit)
    recorder, journal, clock = bare_recorder(tmp_path)
    clock.advance(Seconds(1))
    recorder.note_batch(raw(clock, sample_limit))
    recorder.note_delivery(clock.monotonic(), clock.monotonic())
    recorder.note_batch(raw(clock, 201))
    assert recorder.prestart_size == (1, sample_limit)
    for _ in range(PRESTART_ENTRIES + 10):
        recorder.note_command(commands.Stop())
    assert recorder.prestart_size == (PRESTART_ENTRIES, sample_limit)
    recorder.begin(SessionRequest(occupancy=Occupancy.BENCH, operator=OPERATOR))
    journal.drain()
    assert journal.status(clock.monotonic()).dropped == 12
    assert recorder.prestart_size == (0, 0)
    clock.advance(Seconds(2))
    journal.drain()
    path = journal.status(clock.monotonic()).path
    assert path is not None
    loaded = read(path)
    assert isinstance(loaded, Ok)
    assert any(
        event.detail.startswith("record_degraded: dropped=12") for event in loaded.value.events
    )


def test_an_oversized_batch_during_a_session_is_counted_and_never_queued(tmp_path: Path) -> None:
    recorder, journal, clock = bare_recorder(tmp_path)
    recorder.begin(SessionRequest(occupancy=Occupancy.BENCH, operator=OPERATOR))
    recorder.note_batch(raw(clock, PRESTART_SAMPLES + 1))
    assert journal.status(clock.monotonic()).dropped == 1
    assert recorder.prestart_size == (0, 0)


def test_discarding_idle_history_never_discards_a_live_pending_block(tmp_path: Path) -> None:
    recorder, journal, clock = bare_recorder(tmp_path)
    recorder.begin(SessionRequest(occupancy=Occupancy.BENCH, operator=OPERATOR))
    clock.advance(Seconds(0.2))
    recorder.note_batch(raw(clock))
    recorder.discard_preroll()
    assert recorder.recording
    assert recorder.prestart_size == (1, 200)
    recorder.note_delivery(clock.monotonic(), clock.monotonic())
    journal.drain()
    path = journal.status(clock.monotonic()).path
    assert path is not None
    loaded = read(path)
    assert isinstance(loaded, Ok)
    assert len(loaded.value.raw) == 1
    assert loaded.value.raw[0].header.runtime_delivery is not None


async def test_two_close_sessions_never_share_a_tick_batch_or_frame(tmp_path: Path) -> None:
    recorded = recorded_rig(tmp_path)
    async with recorded.rig.http() as http:
        await recorded.tick(12)
        await start_bench(recorded, http)
        await stop(recorded, http, 5)
        first = recorded.recording()
        await recorded.tick(2)
        await start_bench(recorded, http)
        await stop(recorded, http, 5)
    paths = recorded.records()
    assert len(paths) == 2
    second = read(paths[1])
    assert isinstance(second, Ok)
    one, two = first.manifest.clocks.monotonic_start, second.value.manifest.clocks.monotonic_start
    assert max(row.t + one for row in first.rows) < min(row.t + two for row in second.value.rows)
    assert max(frame.t + one for frame in first.frames) < min(
        frame.t + two for frame in second.value.frames
    )
    assert max(block.header.t_first + one for block in first.raw) < min(
        block.header.t_first + two for block in second.value.raw
    )
    assert any(event.detail.startswith("replay_preamble:") for event in second.value.events)
    assert second.value.rows[0].state == "finished"
    assert all(row.phase == "done" for row in second.value.rows if row.t <= 0)


async def test_failed_runtime_start_cuts_the_window_and_preserves_the_refusal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded = recorded_rig(tmp_path)
    panel = recorded.rig.panel
    async with recorded.rig.http() as http:
        await attest(http)
        await recorded.tick(12)
        submit_start_manual_result_3 = panel.surface.submit_start_manual(
            occupancy=Occupancy.BENCH, operator=OPERATOR
        )
        assert isinstance(submit_start_manual_result_3, Ok)

        def no_room(_self: Journal) -> Storage:
            return Storage(0, recorded.clock.monotonic())

        with monkeypatch.context() as patch:
            patch.setattr(Journal, "storage", property(no_room))
            await recorded.tick(0.2)
        refusal_at = recorded.clock.monotonic()
        assert recorded.rig.refusals()
        assert not recorded.records()
        await recorded.tick(3)
        await start_bench(recorded, http)
    recording = recorded.recording()
    origin = recording.manifest.clocks.monotonic_start
    assert min(row.t + origin for row in recording.rows) >= refusal_at - 1e-9
    starts = [event for event in recording.events if event.detail.startswith("start_manual ")]
    assert len(starts) == 1
    assert any(event.kind is EventKind.REFUSAL for event in recording.events)
    assert any(event.detail.startswith("replay_preamble:") for event in recording.events)


def test_pending_raw_block_keeps_both_unrounded_delivery_instants(tmp_path: Path) -> None:
    recorder, journal, clock = bare_recorder(tmp_path)
    recorder.begin(SessionRequest(occupancy=Occupancy.BENCH, operator=OPERATOR))
    clock.advance(Seconds(0.21111))
    received = clock.monotonic()
    recorder.note_batch(raw(clock))
    assert recorder.prestart_size == (1, 200)
    clock.advance(Seconds(0.02222))
    observed = clock.monotonic()
    clock.advance(Seconds(0.03333))
    delivered = clock.monotonic()
    recorder.note_delivery(observed, delivered)
    recorder.note_delivery(observed, delivered)
    journal.drain()
    path = journal.status(clock.monotonic()).path
    assert path is not None
    loaded = read(path)
    assert isinstance(loaded, Ok)
    assert len(loaded.value.raw) == 1
    header = loaded.value.raw[0].header
    assert header.t_received == received - 10
    assert header.runtime_delivery is not None
    assert header.runtime_delivery.t_observed == observed - 10
    assert header.runtime_delivery.t == delivered - 10


def test_raw_without_an_ecg_reading_omits_delivery_and_a_queue_refusal_keeps_its_sequence_gap(
    tmp_path: Path,
) -> None:
    clock = ManualClock(Monotonic(10), UnixMillis(1_700_000_000_000))
    journal = Journal(tmp_path / "records", clock, limits=Limits(entries=1))
    recorder = SessionRecorder(
        clock=clock, journal=journal, stamp=STAMP, radius=Metres(1.5), leg_tip=Metres(1.5)
    )
    recorder.begin(SessionRequest(occupancy=Occupancy.BENCH, operator=OPERATOR))
    clock.advance(Seconds(0.2))
    recorder.note_batch(raw(clock, ecg=False))
    recorder.note_batch(raw(clock, ecg=False))
    journal.drain()
    clock.advance(Seconds(0.2))
    recorder.note_batch(raw(clock, ecg=False))
    journal.drain()
    path = journal.status(clock.monotonic()).path
    assert path is not None
    loaded = read(path)
    assert isinstance(loaded, Ok)
    assert [block.header.seq for block in loaded.value.raw] == [1]
    assert loaded.value.raw[0].header.runtime_delivery is None
    assert loaded.value.raw[0].header.t_received is not None


async def test_done_seals_frames_until_the_pending_ecg_delivery_resolves(tmp_path: Path) -> None:
    recorded = recorded_rig(tmp_path)
    recorder, clock = recorded.recorder, recorded.clock
    runtime = recorded.rig.panel.runtime
    recorder.begin(SessionRequest(occupancy=Occupancy.BENCH, operator=OPERATOR))
    clock.advance(Seconds(0.2))
    recorder.note_batch(raw(clock))
    ended = clock.monotonic()
    recorder.observe(ended, runtime.snapshot(), runtime)
    assert not recorder.recording
    recorded.drain()
    before = recorded.recording()
    assert before.manifest.ended_at is None
    clock.advance(Seconds(0.05))
    await runtime.tick(clock.monotonic())
    recorder.note_command(commands.Stop())
    recorded.drain()
    assert recorded.recording().frames == before.frames
    recorder.note_delivery(ended, clock.monotonic())
    recorded.drain()
    resolved = recorded.recording()
    assert resolved.manifest.ended_at is not None
    assert resolved.frames == before.frames
    assert len(resolved.raw) == 1
    assert resolved.raw[0].header.runtime_delivery is not None
    assert not any(event.detail.startswith("replay_incomplete:") for event in resolved.events)
    assert all(
        event.t <= ended - resolved.manifest.clocks.monotonic_start + 1e-9
        for event in resolved.events
    )


@pytest.mark.parametrize("boundary", ["tick", "begin", "finish", "discard"])
def test_unresolved_ecg_is_closed_at_the_next_boundary_and_named(
    tmp_path: Path, boundary: str
) -> None:
    recorded = recorded_rig(tmp_path)
    recorder, clock = recorded.recorder, recorded.clock
    runtime = recorded.rig.panel.runtime
    recorder.begin(SessionRequest(occupancy=Occupancy.BENCH, operator=OPERATOR))
    clock.advance(Seconds(0.2))
    recorder.note_batch(raw(clock))
    recorder.observe(clock.monotonic(), runtime.snapshot(), runtime)
    recorded.drain()
    first = recorded.only_record()
    clock.advance(Seconds(0.2))
    if boundary == "tick":
        recorder.observe(clock.monotonic(), runtime.snapshot(), runtime)
    elif boundary == "begin":
        recorder.begin(SessionRequest(occupancy=Occupancy.BENCH, operator=OPERATOR))
    elif boundary == "discard":
        recorder.discard_preroll()
    else:
        recorder.finish(clock.monotonic(), runtime, "exit")
    recorded.drain()
    loaded = read(first)
    assert isinstance(loaded, Ok)
    assert loaded.value.manifest.ended_at is not None
    assert len(loaded.value.raw) == 1
    assert loaded.value.raw[0].header.runtime_delivery is None
    assert any(
        event.detail == "replay_incomplete: pending_ecg_delivery" for event in loaded.value.events
    )


async def test_the_reading_envelope_dates_the_actual_runtime_call(tmp_path: Path) -> None:
    recorded = recorded_rig(tmp_path)
    recorder, clock = recorded.recorder, recorded.clock
    runtime = recorded.rig.panel.runtime
    envelope = RecordedRuntime(runtime, recorder, clock)
    recorder.begin(SessionRequest(occupancy=Occupancy.BENCH, operator=OPERATOR))
    clock.advance(Seconds(0.21111))
    recorder.note_batch(raw(clock))
    clock.advance(Seconds(0.02222))
    observed = clock.monotonic()
    clock.advance(Seconds(0.03333))
    delivered = clock.monotonic()
    envelope.observe_ecg(observed, 1, SignalQuality.GOOD, Bpm(72))
    recorded.drain()
    block = recorded.recording().raw[0]
    assert block.header.runtime_delivery is not None
    assert block.header.runtime_delivery.t == delivered - 10
    assert block.header.runtime_delivery.t_observed == observed - 10
    sample = (await runtime.tick(clock.monotonic())).heart_rate
    assert sample is not None
    assert sample.at == observed
    assert sample.bpm == 72


async def test_camera_verdicts_and_estops_are_written_as_system_commands(tmp_path: Path) -> None:
    recorded = recorded_rig(tmp_path, env={**BENCH_ENV, "PRESENCE_SOURCE": "sim_empty"})
    panel, clock = recorded.rig.panel, recorded.clock
    await run(panel, clock, 3)
    recorded.drain()
    async with recorded.rig.http() as http:
        await start_bench(recorded, http)
        submit_manual_target_result = panel.surface.submit_manual_target(
            output_rpm=OutputRpm(3), operator=OPERATOR
        )
        assert isinstance(submit_manual_target_result, Ok)
        await run(panel, clock, 15)
        recorded.drain()
        camera_of(panel).schedule(Intrusion(start=clock.monotonic(), confidence=Confidence(0.95)))
        await run(panel, clock, 20)
        recorded.drain()
        panel.surface.acknowledge(OPERATOR, estop_released=True)
        recorded.drain()
    inputs = [event for event in recorded.recording().events if event.kind in commands.INPUT_KINDS]
    assert all(isinstance(commands.parse(event.detail), Ok) for event in inputs)
    camera = [event for event in inputs if event.detail.startswith("presence_trip ")]
    assert len(camera) == 1
    assert camera[0].actor == "system"
    emergency = [event for event in inputs if event.detail == "estop"]
    assert len(emergency) == 1
    assert emergency[0].actor == "system"
    assert emergency[0].t <= camera[0].t
