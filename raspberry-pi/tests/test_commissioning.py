"""ANH-115 EX-4: the commissioning of a console (:mod:`src.commissioning`, contract 1.18).

The pre-flight is not changed (its list pinned here, column by column); the commissioning
list is it, three controls raised, three of its own; what is sent, within the route's size
whatever the details; what comes back; and the desk: one at a time, refused up front, dropped
when the console leaves rest, the dashboard's answer kept for the card.
"""

from __future__ import annotations

import asyncio
import json
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Final, cast

import pytest

from src.clock import ManualClock
from src.cloud_sync import CloudError, Document, Refused, Unreachable
from src.commissioning import (
    ATTESTED_UNSET,
    CLOCK_UNSET,
    COMMISSIONING_CONTROLS,
    COMMISSIONING_PATH,
    COMMISSIONING_SINCE,
    FORBIDDEN_CATEGORIES,
    MAX_BODY_BYTES,
    MAX_DETAIL_CHARS,
    MAX_FINDINGS,
    MAX_STATEMENT_CHARS,
    MAX_TEXT_CHARS,
    MIN_DATE_MS,
    NOT_LINKED,
    RAISED,
    RUN_WAIT,
    UNATTESTED,
    UNDER_WAY,
    UNREADABLE_ANSWER,
    CommissioningDesk,
    CommissioningSetup,
    DashboardLink,
    Facts,
    _pause,  # pyright: ignore[reportPrivateUsage]  # the default pause, run once
    attested,
    describe_outcome,
    drive_limits_result,
    dropped,
    never_coasting,
    outcome_of,
    recorded_of,
    report_body,
    sto_ready_result,
    sto_source_result,
)
from src.commissioning_view import Finding, NotRecorded, Recorded, Verdict
from src.contract import ContractVersion, ErrorCode
from src.motor.drive import CommTimeout, DriveError, DriveLimits, DriveState
from src.motor.sto import StoReading, StoSourceKind, StoState
from src.result import Err, Ok, Result
from src.training.plan import JsonValue
from src.training.preflight import (
    CONTROLS,
    ControlResult,
    ControlState,
    PreflightInputs,
    Severity,
)
from src.training.safety import ESTOP_ATTESTATION, EstopAttestation, EstopUnattested
from src.training.types import RunMode
from src.units import Hertz, Monotonic, Seconds, UnixMillis
from tests.test_preflight import (
    _green,  # pyright: ignore[reportPrivateUsage]  # the pre-flight's own fixtures
    _snapshot,  # pyright: ignore[reportPrivateUsage]
)

SHARED: Final[Path] = Path(__file__).resolve().parents[2] / "contracts" / "machine-api.json"
CONTRACT: Final[Mapping[str, Mapping[str, object]]] = cast(
    "Mapping[str, Mapping[str, object]]", json.loads(SHARED.read_text(encoding="utf-8"))
)
SECTION: Final[Mapping[str, object]] = CONTRACT["machine_commissioning"]
LIMITS: Final[DriveLimits] = DriveLimits(
    max_frequency=Hertz(60.0),
    high_speed=Hertz(50.0),
    low_speed=Hertz(0.0),
    acceleration=Seconds(3.0),
    deceleration=Seconds(2.5),
)
CEILING: Final[Hertz] = Hertz(50.0)
ATTESTATION: Final[EstopAttestation] = EstopAttestation(
    operator="user-2",
    statement=ESTOP_ATTESTATION,
    at=Monotonic(900.0),
    wall_clock=UnixMillis(1_799_999_000_000),
)
FACTS: Final[Facts] = Facts(
    sto_source=StoSourceKind.DRIVE_INPUT,
    bitalino_address="20:16:07:18:15:52",
    software_version="pi-0.4.0",
    medical_version="med-2026.1",
    config_hash="a" * 64,
)
RECORDED_ANSWER: Final[Document] = {
    "recordId": "record-1",
    "verdict": "conforming",
    "findings": [],
    "signed": True,
    "server_contract_version": "1.18",
}


def _reading(state: StoState, source: StoSourceKind = StoSourceKind.DRIVE_INPUT) -> StoReading:
    return StoReading(state=state, source=source, last_success_at=Monotonic(999.0), detail="")


# =========================================================================
# The pre-flight, unchanged, and the list made of it
# =========================================================================

PREFLIGHT: Final[tuple[tuple[str, str, str | None, float], ...]] = (
    ("drive_link", "block", "block", 10.0),
    ("drive_state", "block", "block", 10.0),
    ("estop", "block", "block", 0.0),
    ("verdict", "block", "block", 0.0),
    ("disk", "block", "block", 10.0),
    ("clock", "block", "warn", 10.0),
    ("contract", "block", "block", 30.0),
    ("medical", "block", None, 0.0),
    ("incidents", "block", "warn", 30.0),
    ("drive_interventions", "block", "warn", 30.0),
    ("bitalino", "block", "warn", 10.0),
    ("ecg", "block", None, 44.0),
    ("temperature", "warn", "warn", 15.0),
    ("cloud", "warn", "warn", 30.0),
    ("records", "warn", "warn", 15.0),
    ("software", "warn", "warn", 0.0),
)
"""The pre-flight as develop holds it (ANH-142, ANH-145, ANH-149): a commissioning changes
none of its columns, its bench rules nor its nobody-on-board lock."""


def test_the_pre_flight_itself_is_not_changed() -> None:
    assert (
        tuple(
            (
                c.id,
                c.on_board.value,
                None if c.bench is None else c.bench.value,
                float(c.max_duration),
            )
            for c in CONTROLS
        )
        == PREFLIGHT
    )


def test_the_commissioning_list_is_the_pre_flight_three_controls_raised() -> None:
    assert {"clock", "bitalino", "medical"} == RAISED
    assert [c.id for c in COMMISSIONING_CONTROLS] == [c.id for c in CONTROLS]
    for control, original in zip(COMMISSIONING_CONTROLS, CONTROLS, strict=True):
        if control.id in RAISED:
            assert control == replace(original, bench=Severity.BLOCK)
        else:
            assert control is original


def test_a_commissioning_carries_every_control_the_dashboard_requires() -> None:
    sent = [c.id for c in COMMISSIONING_CONTROLS] + ["sto_source", "sto_ready", "drive_limits"]
    required = cast("list[str]", SECTION["required_controls"])
    assert set(required) <= set(sent)
    assert len(sent) <= cast("int", SECTION["max_controls"])
    pattern = re.compile(rf"[a-z][a-z0-9_]{{0,{cast('int', SECTION['max_control_id_chars']) - 1}}}")
    assert all(pattern.fullmatch(control_id) for control_id in sent)


def test_the_required_controls_are_exactly_the_red_ones_of_a_commissioning() -> None:
    """``required_controls`` of the shared file is exactly what a commissioning holds red when
    it does not hold: the pre-flight's controls red at its bench, :data:`RAISED` included,
    and the three of its own. Each is sent once, so the dashboard finds each one."""
    required = cast("list[str]", SECTION["required_controls"])
    own = ("sto_source", "sto_ready", "drive_limits")
    red = [c.id for c in COMMISSIONING_CONTROLS if c.bench is Severity.BLOCK]
    assert sorted(required) == sorted([*red, *own])
    assert len(set(required)) == len(required)
    sent = [c.id for c in COMMISSIONING_CONTROLS] + list(own)
    assert all(sent.count(control_id) == 1 for control_id in required)
    assert set(required) >= RAISED


def test_the_bounds_are_the_shared_files() -> None:
    assert ContractVersion(cast("str", SECTION["since"])) == COMMISSIONING_SINCE
    assert f"POST {COMMISSIONING_PATH}" == SECTION["route"]
    assert SECTION["max_detail_chars"] == MAX_DETAIL_CHARS
    assert SECTION["max_statement_chars"] == MAX_STATEMENT_CHARS
    assert SECTION["max_text_chars"] == MAX_TEXT_CHARS
    assert SECTION["min_date_ms"] == MIN_DATE_MS
    sizes = cast("Mapping[str, int]", CONTRACT["max_body_bytes"]["routes"])
    assert sizes[f"POST {COMMISSIONING_PATH}"] == MAX_BODY_BYTES
    assert "commissioning_refused" in CONTRACT["error_codes"]
    assert [member.value for member in Verdict] == SECTION["verdicts"]


# =========================================================================
# The controls of its own
# =========================================================================


@pytest.mark.parametrize(
    ("source", "state"),
    [
        (StoSourceKind.DRIVE_INPUT, ControlState.OK),
        (StoSourceKind.BRIDGED_DECLARED, ControlState.BLOCK),
        (StoSourceKind.SIM_READY, ControlState.WARN),
        (StoSourceKind.SIM_OPEN, ControlState.WARN),
        (StoSourceKind.NONE, ControlState.WARN),
    ],
)
def test_the_sto_source_is_green_only_on_the_drive_s_input(
    source: StoSourceKind, state: ControlState
) -> None:
    result = sto_source_result(source)
    assert (result.id, result.state) == ("sto_source", state)
    if state is not ControlState.OK:
        assert "enregistree non conforme" in result.detail


def test_the_sto_is_ready_bridged_at_the_bench_or_red_with_its_words() -> None:
    assert sto_ready_result(_reading(StoState.READY)).state is ControlState.OK
    bridged = sto_ready_result(_reading(StoState.BRIDGED, StoSourceKind.BRIDGED_DECLARED))
    assert (bridged.state, bridged.detail) == (
        ControlState.OK,
        "STO ponte declare : depart au banc seulement",
    )
    opened = sto_ready_result(_reading(StoState.OPEN))
    assert (opened.state, opened.detail) == (
        ControlState.BLOCK,
        "chaine STO ouverte : retablir la chaine de securite",
    )


def test_the_drive_s_parameters_are_judged_as_every_arming_judges_them() -> None:
    green = drive_limits_result(Ok(LIMITS), CEILING)
    assert (green.state, green.detail) == (ControlState.OK, f"relus : {LIMITS.describe()}")
    above = drive_limits_result(Ok(LIMITS), Hertz(45.0))
    assert above.state is ControlState.BLOCK
    assert above.detail.endswith(f"(lus : {LIMITS.describe()})")
    unread = drive_limits_result(Err(CommTimeout(after=Seconds(0.5))), CEILING)
    assert unread.state is ControlState.BLOCK
    assert unread.detail.startswith("parametres du variateur illisibles : ")


# =========================================================================
# What is sent
# =========================================================================


def _result(control_id: str, state: ControlState, detail: str) -> ControlResult:
    return ControlResult(control_id, control_id, state, detail)


def _body(
    controls: tuple[ControlResult, ...],
    *,
    facts: Facts = FACTS,
    limits: DriveLimits | None = LIMITS,
    attestation: EstopAttestation = ATTESTATION,
) -> Mapping[str, JsonValue]:
    return report_body(
        ran_at=UnixMillis(1_800_000_000_000),
        operator="user-1",
        attestation=attestation,
        controls=controls,
        limits=limits,
        facts=facts,
    )


def test_the_report_is_the_contract_s_fields_in_its_names() -> None:
    body = _body((_result("estop", ControlState.OK, "attestation donnee"),))
    assert body == {
        "ranAt": 1_800_000_000_000,
        "operator": "user-1",
        "attestation": {
            "operator": "user-2",
            "statement": ESTOP_ATTESTATION,
            "at": 1_799_999_000_000,
        },
        "stoSource": "drive_input",
        "controls": [{"id": "estop", "state": "ok", "detail": "attestation donnee"}],
        "drive": {"tfrHz": 60.0, "hspHz": 50.0, "lspHz": 0.0, "accS": 3.0, "decS": 2.5},
        "bitalinoAddress": "20:16:07:18:15:52",
        "softwareVersion": "pi-0.4.0",
        "medicalParametersVersion": "med-2026.1",
        "configHash": "a" * 64,
    }
    assert set(body) == {
        "ranAt",
        "operator",
        "attestation",
        "stoSource",
        "controls",
        "drive",
        "bitalinoAddress",
        "softwareVersion",
        "medicalParametersVersion",
        "configHash",
    }


def test_texts_are_as_the_contract_takes_them() -> None:
    hidden = (
        "\x07\x7f\x85\u202e\u202a\u2068"
        "\u2028\u2029\u200b\u200d\u2060\ufeff"
        "\u200e\u200f\u061c\xad\u2061\u2062"
        "\u2063\u2064\u180e\ufff9\U000e0001\U000e0041"
    )
    body = _body(
        (
            _result("estop", ControlState.OK, f"  a{hidden}b  "),
            _result("disk", ControlState.OK, " "),
        ),
        facts=replace(FACTS, bitalino_address="  ", medical_version="", config_hash=None),
        limits=None,
    )
    controls = cast("list[dict[str, str]]", body["controls"])
    assert controls[0]["detail"] == f"a{' ' * len(hidden)}b"
    assert controls[1]["detail"] == "-"
    assert body["bitalinoAddress"] is None
    assert body["medicalParametersVersion"] is None
    assert body["drive"] is None
    assert set(cast("list[str]", SECTION["forbidden_categories"])) == FORBIDDEN_CATEGORIES
    for char in hidden:
        assert unicodedata.category(char) in FORBIDDEN_CATEGORIES
    # Text beyond ASCII that hides nothing is kept as it is, a grapheme joiner and a Hangul
    # filler included: they reorder nothing.
    kept = "Pi a 41 °C, attesté à l'écran \u034f\u3164"
    sent = _body((_result("estop", ControlState.OK, kept),))
    assert cast("list[dict[str, str]]", sent["controls"])[0]["detail"] == kept


def test_a_report_keeps_whole_details_when_they_fit() -> None:
    controls = tuple(_result(f"c{n}", ControlState.OK, "x" * 400) for n in range(19))
    body = _body(controls)
    details = [c["detail"] for c in cast("list[dict[str, str]]", body["controls"])]
    assert details == ["x" * MAX_DETAIL_CHARS] * 19
    assert len(json.dumps(body)) <= MAX_BODY_BYTES


def test_the_largest_report_the_contract_allows_still_fits_the_route() -> None:
    """32 controls of 300 characters, each beyond ASCII or escaped, every text at its bound."""
    widest = '"\\é€😀'
    detail = (widest * 100)[:MAX_DETAIL_CHARS]
    controls = tuple(
        _result(f"c{n:02d}{'_' * 35}", ControlState.BLOCK, detail)
        for n in range(cast("int", SECTION["max_controls"]))
    )
    wide = "é" * MAX_TEXT_CHARS
    body = report_body(
        ran_at=UnixMillis(8_640_000_000_000_000),
        operator=wide,
        attestation=replace(
            ATTESTATION,
            operator=wide,
            statement="€" * MAX_STATEMENT_CHARS,
            wall_clock=UnixMillis(0),
        ),
        controls=controls,
        limits=LIMITS,
        facts=Facts(
            sto_source=StoSourceKind.BRIDGED_DECLARED,
            bitalino_address=wide,
            software_version=wide,
            medical_version=wide,
            config_hash="f" * 64,
        ),
    )
    assert len(json.dumps(body)) <= MAX_BODY_BYTES
    sent = cast("list[dict[str, str]]", body["controls"])
    assert len(sent) == cast("int", SECTION["max_controls"])
    # Shortened alike, never emptied: each still says its first characters.
    assert len({len(c["detail"]) for c in sent}) == 1
    assert 0 < len(sent[0]["detail"]) < MAX_DETAIL_CHARS
    assert cast("dict[str, str]", body["attestation"])["statement"] == "€" * MAX_STATEMENT_CHARS


@pytest.mark.parametrize("controls", [1, 8, 16, 24, 32])
def test_a_report_of_any_size_fits_with_the_longest_detail_that_does(controls: int) -> None:
    sent = tuple(_result(f"c{n}", ControlState.WARN, "é" * 300) for n in range(controls))
    body = _body(sent)
    size = len(json.dumps(body))
    assert size <= MAX_BODY_BYTES
    detail = cast("list[dict[str, str]]", body["controls"])[0]["detail"]
    if len(detail) < MAX_DETAIL_CHARS:
        # The step before would not have fitted: the details are cut no more than needed.
        steps = [MAX_DETAIL_CHARS]
        while steps[-1] > 1:
            steps.append(steps[-1] // 2)
        before = steps[steps.index(len(detail)) - 1]
        assert size + controls * (before - len(detail)) * 6 > MAX_BODY_BYTES


# =========================================================================
# What comes back
# =========================================================================


def test_a_recorded_answer_is_read_field_by_field() -> None:
    finding = {"id": "drive_hsp", "expected": "50.0 Hz", "read": "50.1 Hz"}
    answer = {**RECORDED_ANSWER, "verdict": "nonconforming", "findings": [finding], "signed": False}
    assert recorded_of(answer) == Recorded(
        "record-1", Verdict.NONCONFORMING, (Finding("drive_hsp", "50.0 Hz", "50.1 Hz"),), False
    )
    assert recorded_of(RECORDED_ANSWER) == Recorded("record-1", Verdict.CONFORMING, (), True)


@pytest.mark.parametrize(
    "changes",
    [
        {"recordId": None},
        {"recordId": ""},
        {"verdict": "conforme"},
        {"findings": {"id": "x"}},
        {"findings": ["x"]},
        {"findings": [{"id": "x", "expected": "y", "read": 3}]},
        {"findings": [{"id": "x", "expected": "y", "read": "z"}] * (MAX_FINDINGS + 1)},
        {"signed": "yes"},
    ],
)
def test_an_answer_this_console_cannot_read_is_none(changes: dict[str, object]) -> None:
    assert recorded_of({**RECORDED_ANSWER, **changes}) is None


@pytest.mark.parametrize(
    ("sent", "outcome"),
    [
        (Ok(RECORDED_ANSWER), Recorded("record-1", Verdict.CONFORMING, (), True)),
        (Ok({"recordId": 1}), NotRecorded(UNREADABLE_ANSWER)),
        (
            Err(Refused(409, "Aucune fiche variateur", ErrorCode("commissioning_refused"))),
            NotRecorded("refusee par le tableau de bord : Aucune fiche variateur"),
        ),
        (
            Err(Refused(400, "Expected the commissioning report", ErrorCode("invalid_request"))),
            NotRecorded("refusee par le tableau de bord (400) : Expected the commissioning report"),
        ),
        (
            Err(Unreachable("POST /api/machine/commissioning: ConnectError")),
            NotRecorded(
                "tableau de bord injoignable : POST /api/machine/commissioning: ConnectError"
            ),
        ),
    ],
)
def test_what_the_dashboard_made_of_it_is_said_in_the_card_s_words(
    sent: Result[Document, CloudError], outcome: Recorded | NotRecorded
) -> None:
    assert outcome_of(sent) == outcome


def test_the_log_line_names_the_verdict_and_the_findings_or_why_not() -> None:
    finding = Finding("sto_bridged", "drive_input", "bridged_declared")
    assert describe_outcome(Recorded("r", Verdict.NONCONFORMING, (finding,), False)) == (
        "recorded nonconforming (sto_bridged)"
    )
    assert describe_outcome(Recorded("r", Verdict.CONFORMING, (), True)) == (
        "recorded conforming (aucun ecart)"
    )
    assert describe_outcome(NotRecorded("injoignable")) == "not recorded (injoignable)"


def test_the_attestation_is_the_runtime_s_or_none() -> None:
    assert attested(Ok(ATTESTATION)) is ATTESTATION
    assert attested(Err(EstopUnattested(ESTOP_ATTESTATION))) is None
    assert never_coasting() is None
    assert dropped("x") == "mise en service interrompue, rien n'est envoye : x"


# =========================================================================
# The desk
# =========================================================================


class Monitor:
    """A pre-flight whose run lasts ``polls`` pauses, and whose inputs a test changes."""

    def __init__(self, inputs: PreflightInputs, polls: int = 2) -> None:
        self.current = inputs
        self.polls = polls
        self.left = 0
        self.reruns = 0

    @property
    def running(self) -> bool:
        return self.left > 0

    def rerun(self) -> None:
        self.reruns += 1
        self.left = self.polls

    def inputs(self) -> PreflightInputs:
        return self.current


class Rig:
    """A desk on a manual clock, its drive, its link and its pre-flight run by the test."""

    def __init__(
        self,
        *,
        polls: int = 2,
        announced: str | None = "1.18",
        wall: UnixMillis = UnixMillis(1_800_000_000_000),
    ) -> None:
        self.clock = ManualClock(Monotonic(1000.0), wall)
        self.monitor = Monitor(_green(), polls)
        self.attestation: EstopAttestation | None = ATTESTATION
        self.coasting: str | None = None
        self.read: Result[DriveLimits, DriveError] = Ok(LIMITS)
        self.sent: list[Mapping[str, JsonValue]] = []
        self.answer: Result[Document, CloudError] = Ok(RECORDED_ANSWER)
        self.on_pause: list[PreflightInputs] = []
        self.on_read: PreflightInputs | None = None
        self.announced = None if announced is None else ContractVersion(announced)
        self.desk = CommissioningDesk(
            CommissioningSetup(
                clock=self.clock,
                monitor=self.monitor,
                attestation=lambda: self.attestation,
                sto=lambda: _reading(StoState.READY),
                read_limits=self._read_limits,
                ceiling=CEILING,
                facts=FACTS,
                coasting=lambda: self.coasting,
                pause=self._pause,
            )
        )

    def bind(self) -> Rig:
        self.desk.bind(DashboardLink(post=self._post, announced=lambda: self.announced))
        return self

    async def _pause(self, delay: Seconds) -> None:
        await asyncio.sleep(0)
        self.clock.advance(delay)
        self.monitor.left -= 1
        if self.on_pause:
            self.monitor.current = self.on_pause.pop(0)

    async def _read_limits(self) -> Result[DriveLimits, DriveError]:
        if self.on_read is not None:
            self.monitor.current = self.on_read
        return self.read

    async def _post(self, body: Mapping[str, JsonValue]) -> Result[Document, CloudError]:
        self.sent.append(body)
        return self.answer


def test_a_desk_not_linked_yet_or_to_an_earlier_dashboard_starts_none() -> None:
    rig = Rig()
    assert rig.desk.view().refusal == NOT_LINKED
    assert asyncio.run(rig.desk.run("user-1")) == Err(NOT_LINKED)
    rig.bind()
    for announced in (None, "1.17", "2.18"):
        rig.announced = None if announced is None else ContractVersion(announced)
        assert rig.desk.view().refusal == NOT_LINKED
    assert rig.monitor.reruns == 0


def test_no_attestation_or_no_rest_or_a_coast_starts_none() -> None:
    rig = Rig().bind()
    rig.attestation = None
    assert rig.desk.view().refusal == UNATTESTED
    rig.attestation = ATTESTATION
    rig.monitor.current = _green(snapshot=_snapshot(mode=RunMode.MANUEL))
    refusal = rig.desk.view().refusal
    assert refusal is not None
    assert refusal.startswith("console pas au repos : ")
    rig.monitor.current = _green()
    rig.coasting = "roue libre en cours"
    assert rig.desk.view().refusal == "console pas au repos : roue libre en cours"
    assert asyncio.run(rig.desk.run("user-1")) == Err("console pas au repos : roue libre en cours")
    assert rig.monitor.reruns == 0
    assert rig.sent == []


def test_a_clock_not_set_or_an_attestation_given_before_it_was_starts_none() -> None:
    rig = Rig(wall=UnixMillis(0)).bind()
    assert rig.desk.view().refusal == CLOCK_UNSET
    rig = Rig().bind()
    rig.attestation = replace(ATTESTATION, wall_clock=UnixMillis(MIN_DATE_MS - 1))
    assert rig.desk.view().refusal == ATTESTED_UNSET
    assert asyncio.run(rig.desk.run("user-1")) == Err(ATTESTED_UNSET)
    rig.attestation = replace(ATTESTATION, wall_clock=MIN_DATE_MS)
    assert rig.desk.view().refusal is None
    assert rig.sent == []


def test_a_commissioning_reruns_the_pre_flight_reads_the_drive_judges_and_sends() -> None:
    rig = Rig().bind()
    assert rig.desk.view().refusal is None
    done = asyncio.run(rig.desk.run("user-1"))
    assert isinstance(done, Ok)
    last = done.value
    assert rig.monitor.reruns == 1
    assert rig.clock.monotonic() == Monotonic(1001.0)
    assert last.outcome == Recorded("record-1", Verdict.CONFORMING, (), True)
    assert last.operator == "user-1"
    assert last.at == rig.clock.unix_millis()
    ids = [c.id for c in last.controls]
    assert ids == [c.id for c in COMMISSIONING_CONTROLS] + [
        "sto_source",
        "sto_ready",
        "drive_limits",
    ]
    (body,) = rig.sent
    assert body["ranAt"] == last.at
    assert body["operator"] == "user-1"
    assert cast("dict[str, object]", body["attestation"])["operator"] == "user-2"
    assert [c["id"] for c in cast("list[dict[str, str]]", body["controls"])] == ids
    # The clock, the BITalino and the medical parameters are judged red where the bench warns.
    states = {c.id: c.state for c in last.controls}
    assert states["ecg"] is ControlState.OK
    assert rig.desk.view().last == last
    assert rig.desk.view().running is False


def test_a_red_commissioning_is_sent_too() -> None:
    rig = Rig().bind()
    rig.monitor.current = _green(bitalino_connected=False)
    rig.read = Err(CommTimeout(after=Seconds(0.5)))
    rig.answer = Err(Unreachable("POST /api/machine/commissioning: ConnectError"))
    done = asyncio.run(rig.desk.run("user-1"))
    assert isinstance(done, Ok)
    states = {c.id: c.state for c in done.value.controls}
    assert states["bitalino"] is ControlState.BLOCK
    assert states["drive_limits"] is ControlState.BLOCK
    (body,) = rig.sent
    assert body["drive"] is None
    assert done.value.outcome == NotRecorded(
        "tableau de bord injoignable : POST /api/machine/commissioning: ConnectError"
    )


def test_one_that_leaves_rest_while_it_waits_or_while_the_drive_is_read_sends_nothing() -> None:
    rig = Rig(polls=3).bind()
    rig.on_pause = [_green(), _green(snapshot=_snapshot(mode=RunMode.MANUEL))]
    done = asyncio.run(rig.desk.run("user-1"))
    assert isinstance(done, Err)
    assert done.error.startswith("mise en service interrompue, rien n'est envoye : ")
    assert rig.sent == []
    assert rig.desk.view().last is None
    rig.monitor.current = _green()
    rig.on_read = _green(snapshot=_snapshot(drive_state=DriveState.OPERATION_ENABLED))
    done = asyncio.run(rig.desk.run("user-1"))
    assert isinstance(done, Err)
    assert rig.sent == []
    rig.monitor.current = _green()
    rig.on_read = None
    rig.on_pause = []
    rig.coasting = None
    assert isinstance(asyncio.run(rig.desk.run("user-1")), Ok)


def test_a_run_that_does_not_settle_is_judged_when_its_wait_is_over() -> None:
    rig = Rig(polls=10_000).bind()
    done = asyncio.run(rig.desk.run("user-1"))
    assert isinstance(done, Ok)
    assert rig.clock.monotonic() == Monotonic(1000.0 + RUN_WAIT)
    assert len(rig.sent) == 1


def test_one_at_a_time() -> None:
    rig = Rig(polls=4).bind()
    seen: list[object] = []

    async def both() -> None:
        first = asyncio.create_task(rig.desk.run("user-1"))
        await asyncio.sleep(0)
        seen.append(rig.desk.view().running)
        seen.append(rig.desk.view().refusal)
        seen.append(await rig.desk.run("user-2"))
        seen.append(await first)

    asyncio.run(both())
    assert seen[:3] == [True, UNDER_WAY, Err(UNDER_WAY)]
    assert isinstance(seen[3], Ok)
    assert rig.desk.view().running is False
    assert len(rig.sent) == 1


def test_an_impossible_value_raises_rather_than_passing() -> None:
    impossible = object()
    with pytest.raises(AssertionError):
        sto_source_result(cast("StoSourceKind", impossible))
    with pytest.raises(AssertionError):
        drive_limits_result(cast("Result[DriveLimits, DriveError]", impossible), CEILING)
    with pytest.raises(AssertionError):
        outcome_of(cast("Result[Document, CloudError]", impossible))
    with pytest.raises(AssertionError):
        outcome_of(Err(cast("CloudError", impossible)))
    with pytest.raises(AssertionError):
        describe_outcome(cast("Recorded", impossible))


def test_the_default_pause_waits_on_the_loop() -> None:
    asyncio.run(_pause(Seconds(0.0)))
