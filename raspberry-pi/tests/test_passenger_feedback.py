"""The rider's feedback at the console (ANH-146), its module alone.

* P7: what a feedback is: whole scores from 0 to 10, a comfort of the list, a
  comment of at most 500 characters once trimmed; a refusal names the field,
  never its value;
* P9: the scale and its labels are those of ``contracts/feedback.json`` (the
  console is deployed without it), and the dashboard's minor that takes a
  feedback is told apart from those that would keep it as an event;
* P5 (in memory): what each answer of the link does to a feedback, the wait
  for the session's identifier and for a dashboard of 1.7, the 24 h after
  the end, and the bound of what is held;
* P6: a skipped card sends nothing.

The console's own wiring, its routes and a whole session are
``tests/test_feedback_console.py``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, cast

import pytest

from src.clock import ManualClock
from src.contract import (
    CONTRACT_VERSION,
    FEEDBACK_SINCE,
    ContractVersion,
    ErrorCode,
    takes_feedback,
    version_of,
)
from src.passenger_feedback import (
    COMFORT_LABELS,
    DELIVERY_WORDS,
    DIZZINESS,
    EDIT_WINDOW,
    FIELDS,
    FREE_TEXT_MAX,
    MAX_HELD,
    NAUSEA,
    OPERATOR_ACTOR,
    RETRY_PERIOD,
    SCALE_MAX,
    SCALE_MIN,
    SESSION_NOT_ENDED,
    Comfort,
    Delivery,
    Feedback,
    FeedbackOffer,
    InvalidField,
    NothingToFill,
    PassengerFeedback,
    Score,
    SessionIdReader,
    describe_delivery,
    feedback_body,
    parse_feedback,
    utf16_length,
)
from src.record_uplink import Acked, Answer, Held, Refusal, RouteMissing
from src.result import Err, Ok
from src.training.plan import JsonValue
from src.units import Monotonic, Seconds, UnixMillis

SHARED: Final[Path] = Path(__file__).resolve().parents[2] / "contracts" / "feedback.json"
SENTINEL: Final[str] = "synthetic-private-feedback-sentinel"

VALID: Final[Mapping[str, object]] = {
    "nausea_score": 7,
    "dizziness_score": 9,
    "comfort": "bad",
    "free_text": SENTINEL,
}
FEEDBACK: Final[Feedback] = Feedback(Score(7), Score(9), Comfort.BAD, SENTINEL)
SERVED: Final[ContractVersion] = ContractVersion(CONTRACT_VERSION)


def shared() -> Mapping[str, object]:
    loaded: object = json.loads(SHARED.read_text(encoding="utf-8"))  # pyright: ignore[reportAny]  # narrowed below
    assert isinstance(loaded, dict)
    return cast("Mapping[str, object]", loaded)


def clock() -> ManualClock:
    return ManualClock(Monotonic(10.0), UnixMillis(1_790_000_000_000))


@dataclass
class Link:
    """A dashboard link: what it announced, what it answers, and every body it was sent."""

    server_contract: ContractVersion | None = SERVED
    answers: list[Answer] = field(default_factory=list[Answer])
    sent: list[Mapping[str, JsonValue]] = field(default_factory=list[Mapping[str, JsonValue]])
    during: list[object] = field(default_factory=list[object])
    """Something to do while a request travels (a route acting meanwhile)."""

    async def send_feedback(self, body: Mapping[str, JsonValue]) -> Answer:
        self.sent.append(body)
        for act in self.during:
            if callable(act):
                act()
        self.during.clear()
        return self.answers.pop(0) if self.answers else Acked({"stored": 1})


def reader(session_id: str) -> SessionIdReader:
    """The reader of a session whose identifier is already known."""
    return lambda: session_id


def offered(store: PassengerFeedback) -> FeedbackOffer:
    current = store.view().current
    assert current is not None
    return current


def entered(session_id: str | None = "SESSION-1") -> tuple[ManualClock, PassengerFeedback]:
    """A store whose card is open for a session that ended, its feedback entered."""
    ticking = clock()
    store = PassengerFeedback(ticking)
    store.began()
    store.ended(lambda: session_id)
    assert store.submit(FEEDBACK) == Ok(None)
    return ticking, store


# =========================================================================
# P9: the scale is the shared file's, and the minor of the dashboard
# =========================================================================


def test_p9_the_scales_are_the_shared_file_s() -> None:
    contract = shared()
    scales = cast("Mapping[str, Mapping[str, object]]", contract["scales"])
    for scale, mine in (("nausea", NAUSEA), ("dizziness", DIZZINESS)):
        theirs = scales[scale]
        assert cast("Mapping[str, str]", theirs["title"])["fr"] == mine.title
        assert (theirs["min"], theirs["max"]) == (SCALE_MIN, SCALE_MAX)
        levels = cast("list[Mapping[str, object]]", theirs["levels"])
        assert [level["value"] for level in levels] == list(range(SCALE_MIN, SCALE_MAX + 1))
        assert tuple(cast("Mapping[str, str]", level["label"])["fr"] for level in levels) == (
            mine.labels
        )


def test_p9_the_comforts_the_text_the_window_and_the_actor_are_the_shared_file_s() -> None:
    contract = shared()
    comforts = cast("list[Mapping[str, object]]", contract["comfort"])
    assert [item["value"] for item in comforts] == [comfort.value for comfort in Comfort]
    assert [cast("Mapping[str, str]", item["label"])["fr"] for item in comforts] == [
        COMFORT_LABELS[comfort] for comfort in Comfort
    ]
    assert contract["freeTextMaxLength"] == FREE_TEXT_MAX
    assert float(cast("int", contract["editWindowHours"]) * 3600) == float(EDIT_WINDOW)
    assert OPERATOR_ACTOR in cast("list[str]", contract["submittedBy"])


@pytest.mark.parametrize(
    ("announced", "takes"),
    [
        ("1.6", False),
        ("1.7", True),
        ("1.12", True),
        ("2.0", False),
        ("0.9", False),
        ("", False),
        ("1.x", False),
        (None, False),
    ],
)
def test_p9_only_a_dashboard_of_this_major_and_1_7_or_later_takes_a_feedback(
    announced: str | None, *, takes: bool
) -> None:
    assert takes_feedback(version_of(announced)) is takes
    assert FEEDBACK_SINCE == "1.7"


# =========================================================================
# P7: what a feedback is
# =========================================================================


@pytest.mark.parametrize("score", [0, 10, 5])
def test_p7_whole_scores_of_the_scale_are_accepted(score: int) -> None:
    parsed = parse_feedback({**VALID, "nausea_score": score, "dizziness_score": score})
    assert parsed == Ok(Feedback(Score(score), Score(score), Comfort.BAD, SENTINEL))


@pytest.mark.parametrize("score", [-1, 11, 2.5, 3.0, "3", None, True, [3]])
@pytest.mark.parametrize("field_name", ["nausea_score", "dizziness_score"])
def test_p7_a_score_off_the_scale_is_refused_by_its_field(score: object, field_name: str) -> None:
    assert parse_feedback({**VALID, field_name: score}) == Err(InvalidField(field_name))


@pytest.mark.parametrize("comfort", list(Comfort))
def test_p7_each_comfort_of_the_list_is_accepted(comfort: Comfort) -> None:
    parsed = parse_feedback({**VALID, "comfort": comfort.value})
    assert isinstance(parsed, Ok)
    assert parsed.value.comfort is comfort


@pytest.mark.parametrize("comfort", ["great", "", "Good", 1, None])
def test_p7_a_comfort_off_the_list_is_refused(comfort: object) -> None:
    assert parse_feedback({**VALID, "comfort": comfort}) == Err(InvalidField("comfort"))


def test_p7_500_characters_of_comment_are_accepted_and_501_refused() -> None:
    text = "é" * FREE_TEXT_MAX
    accepted = parse_feedback({**VALID, "free_text": f"  {text}\n"})
    assert isinstance(accepted, Ok)
    assert accepted.value.free_text == text
    assert parse_feedback({**VALID, "free_text": f"{text}x"}) == Err(InvalidField("free_text"))


def test_p7_the_comment_is_measured_as_the_page_and_the_dashboard_measure_it() -> None:
    # Outside the basic plane, one character is two units, as JavaScript counts.
    wide = "\U0001f600" * (FREE_TEXT_MAX // 2)
    assert utf16_length(wide) == FREE_TEXT_MAX
    assert isinstance(parse_feedback({**VALID, "free_text": wide}), Ok)
    assert parse_feedback({**VALID, "free_text": f"{wide}a"}) == Err(InvalidField("free_text"))


@pytest.mark.parametrize("text", ["", "   ", "\n\t ", None])
def test_p7_an_empty_or_blank_comment_is_none(text: object) -> None:
    parsed = parse_feedback({**VALID, "free_text": text})
    assert isinstance(parsed, Ok)
    assert parsed.value.free_text is None
    without = {key: value for key, value in VALID.items() if key != "free_text"}
    assert parse_feedback(without) == Ok(Feedback(Score(7), Score(9), Comfort.BAD, None))


def test_p7_a_comment_keeps_its_lines_and_refuses_other_control_characters() -> None:
    kept = parse_feedback({**VALID, "free_text": "un\tdeux\ntrois\u202f!"})
    assert isinstance(kept, Ok)
    assert kept.value.free_text == "un\tdeux\ntrois\u202f!"
    for control in ("\x00", "\x1b", "\r", "\x7f"):
        assert parse_feedback({**VALID, "free_text": f"a{control}b"}) == Err(
            InvalidField("free_text")
        )
    assert parse_feedback({**VALID, "free_text": 12}) == Err(InvalidField("free_text"))


@pytest.mark.parametrize(
    "body", [[VALID], "feedback", None, {**VALID, "rider": SENTINEL}, {**VALID, "operator": "x"}]
)
def test_p7_a_body_that_is_not_an_object_of_feedback_keys_is_refused(body: object) -> None:
    assert parse_feedback(body) == Err(InvalidField("body"))
    assert {"nausea_score", "dizziness_score", "comfort", "free_text"} == FIELDS


def test_p7_no_refusal_says_what_was_refused() -> None:
    for body in ({**VALID, "free_text": SENTINEL * 40}, {**VALID, "nausea_score": 77}):
        refused = parse_feedback(body)
        assert isinstance(refused, Err)
        assert SENTINEL not in repr(refused)
        assert "77" not in repr(refused)


# =========================================================================
# What the dashboard is sent
# =========================================================================


def test_the_body_is_one_feedback_event_alone_with_the_compact_detail() -> None:
    body = feedback_body("SESSION-1", FEEDBACK, UnixMillis(1_790_000_000_123))
    assert body == {
        "sessionId": "SESSION-1",
        "events": [
            {
                "seq": 0,
                "t": 1_790_000_000_123,
                "kind": "feedback",
                "detail": (
                    f'{{"nauseaScore":7,"dizzinessScore":9,"comfort":"bad","freeText":"{SENTINEL}"}}'
                ),
                "actor": "operator",
            }
        ],
    }
    plain = feedback_body("S", Feedback(Score(0), Score(10), Comfort.GOOD, None), UnixMillis(1))
    event = cast("list[Mapping[str, JsonValue]]", plain["events"])[0]
    assert json.loads(cast("str", event["detail"])) == {
        "nauseaScore": 0,
        "dizzinessScore": 10,
        "comfort": "good",
    }


def test_a_comment_s_own_characters_keep_the_event_under_the_dashboard_s_2000() -> None:
    worst = Feedback(Score(10), Score(10), Comfort.ACCEPTABLE, '"\\é\n' * (FREE_TEXT_MAX // 4))
    event = cast(
        "list[Mapping[str, JsonValue]]", feedback_body("S", worst, UnixMillis(1))["events"]
    )[0]
    detail = cast("str", event["detail"])
    assert utf16_length(detail) <= 2000
    assert json.loads(detail)["freeText"] == worst.free_text


# =========================================================================
# The card, and what the operator does with it
# =========================================================================


def test_nothing_is_shown_before_a_session_with_somebody_on_board_ended() -> None:
    store = PassengerFeedback(clock())
    assert store.view().current is None
    assert store.submit(FEEDBACK) == Err(NothingToFill())
    assert store.skip() == Err(NothingToFill())


def test_the_card_opens_at_the_end_and_a_second_entry_replaces_the_first() -> None:
    ticking = clock()
    store = PassengerFeedback(ticking)
    store.ended(lambda: "SESSION-1")
    current = offered(store)
    assert (current.delivery, current.can_submit, current.code) == (Delivery.TO_FILL, True, None)
    assert current.ended_at == ticking.unix_millis()
    assert store.submit(FEEDBACK) == Ok(None)
    assert offered(store).delivery is Delivery.WAITING_LINK
    assert store.submit(Feedback(Score(1), Score(1), Comfort.GOOD, None)) == Ok(None)
    assert offered(store).can_submit


async def test_p6_a_skipped_card_closes_and_sends_nothing() -> None:
    _, store = entered()
    assert store.skip() == Ok(None)
    assert store.view().current is None
    link = Link()
    await store.step(link)
    assert link.sent == []
    assert store.submit(FEEDBACK) == Err(NothingToFill())


def test_the_next_session_closes_a_card_left_to_fill_and_keeps_one_entered() -> None:
    store = PassengerFeedback(clock())
    store.ended(lambda: "SESSION-1")
    store.began()
    assert store.view().current is None
    _, entered_store = entered()
    entered_store.began()
    assert entered_store.view().current is None


# =========================================================================
# P4, P5: the sending
# =========================================================================


async def test_p4_a_feedback_is_sent_once_and_said_sent() -> None:
    ticking, store = entered()
    link = Link()
    await store.step(link)
    assert link.sent == [feedback_body("SESSION-1", FEEDBACK, ticking.unix_millis())]
    assert offered(store).delivery is Delivery.SENT
    assert not offered(store).can_submit
    await store.step(link)
    assert len(link.sent) == 1
    # Shown until closed; closing a sent one changes nothing of it.
    assert store.skip() == Ok(None)
    assert store.view().current is None


@pytest.mark.parametrize("announced", ["1.6", "2.0"])
async def test_p5_a_dashboard_older_than_1_7_is_sent_nothing(announced: str) -> None:
    _, store = entered()
    link = Link(server_contract=ContractVersion(announced))
    await store.step(link)
    assert link.sent == []
    assert offered(store).delivery is Delivery.SERVER_TOO_OLD
    assert offered(store).can_submit
    # Once it announces 1.7, the feedback goes.
    link.server_contract = ContractVersion("1.7")
    await store.step(link)
    assert len(link.sent) == 1
    assert offered(store).delivery is Delivery.SENT


async def test_p5_no_version_heard_yet_waits_for_the_link() -> None:
    _, store = entered()
    link = Link(server_contract=None)
    await store.step(link)
    assert (link.sent, offered(store).delivery) == ([], Delivery.WAITING_LINK)


async def test_p5_a_lost_link_is_tried_again_after_the_retry_period() -> None:
    ticking, store = entered()
    link = Link(answers=[Held("no route to host")])
    await store.step(link)
    assert offered(store).delivery is Delivery.WAITING_LINK
    await store.step(link)
    assert len(link.sent) == 1, "nothing again before the retry period"
    ticking.advance(Seconds(RETRY_PERIOD))
    await store.step(link)
    assert len(link.sent) == 2
    assert offered(store).delivery is Delivery.SENT


async def test_p5_an_unknown_route_waits_as_a_dashboard_too_old() -> None:
    ticking, store = entered()
    link = Link(answers=[RouteMissing()])
    await store.step(link)
    assert offered(store).delivery is Delivery.SERVER_TOO_OLD
    ticking.advance(Seconds(RETRY_PERIOD))
    await store.step(link)
    assert offered(store).delivery is Delivery.SENT


async def test_p5_the_session_s_identifier_learned_after_the_entry_lets_it_go() -> None:
    known: list[str | None] = [None]
    store = PassengerFeedback(clock())
    store.ended(lambda: known[0])
    assert store.submit(FEEDBACK) == Ok(None)
    link = Link()
    await store.step(link)
    assert (link.sent, offered(store).delivery) == ([], Delivery.WAITING_SESSION)
    known[0] = "SESSION-9"
    await store.step(link)
    assert cast("str", link.sent[0]["sessionId"]) == "SESSION-9"


@pytest.mark.parametrize(
    ("status", "code"),
    [(409, "feedback_window_closed"), (400, "feedback_not_eligible"), (400, None)],
)
async def test_p5_a_refusal_is_said_with_its_code_and_never_sent_again(
    status: int, code: str | None
) -> None:
    _, store = entered()
    error = None if code is None else ErrorCode(code)
    link = Link(answers=[Refusal("refused", final=False, status=status, code=error)])
    await store.step(link)
    current = offered(store)
    assert (current.delivery, current.code, current.can_submit) == (
        Delivery.REFUSED,
        error,
        False,
    )
    assert describe_delivery(current.delivery, current.code).endswith(
        f"({'sans code' if code is None else code})"
    )
    await store.step(link)
    assert len(link.sent) == 1


async def test_p5_given_up_24_hours_after_the_end() -> None:
    ticking, store = entered()
    ticking.advance(Seconds(EDIT_WINDOW))
    assert offered(store).delivery is Delivery.WAITING_LINK, "still within the window"
    ticking.advance(Seconds(0.001))
    current = offered(store)
    assert (current.delivery, current.can_submit) == (Delivery.EXPIRED, False)
    link = Link()
    await store.step(link)
    assert link.sent == []
    assert store.submit(FEEDBACK) == Err(NothingToFill())


def test_a_card_left_to_fill_expires_too() -> None:
    ticking = clock()
    store = PassengerFeedback(ticking)
    store.ended(lambda: "SESSION-1")
    ticking.advance(Seconds(EDIT_WINDOW + 1))
    assert offered(store).delivery is Delivery.EXPIRED


async def test_p5_more_than_the_bound_waiting_gives_up_the_oldest_and_says_so(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = PassengerFeedback(clock())
    for session in range(MAX_HELD):
        store.ended(reader(f"SESSION-{session}"))
        assert store.submit(FEEDBACK) == Ok(None)
    assert store.view().given_up == 0
    store.ended(lambda: "SESSION-LAST")
    assert store.view().given_up == 1
    assert "given up" in caplog.text
    link = Link()
    await store.step(link)
    sent = [cast("str", body["sessionId"]) for body in link.sent]
    assert sent == [f"SESSION-{session}" for session in range(1, MAX_HELD)]


async def test_a_card_left_to_fill_is_not_counted_when_it_is_dropped() -> None:
    store = PassengerFeedback(clock())
    for session in range(MAX_HELD):
        store.ended(reader(f"SESSION-{session}"))
        assert store.submit(FEEDBACK) == Ok(None)
    store.began()
    store.ended(lambda: "SESSION-NEXT")
    assert store.view().given_up == 1
    # Cards left to fill close as the next ones open: nothing of them is held or counted.
    for _ in range(MAX_HELD):
        store.ended(lambda: "SESSION-EMPTY")
    assert store.view().given_up == 1


async def test_a_feedback_entered_again_while_one_travels_goes_at_the_next_step() -> None:
    _, store = entered()
    replacement = Feedback(Score(1), Score(2), Comfort.GOOD, None)
    link = Link(during=[lambda: store.submit(replacement)])
    await store.step(link)
    assert offered(store).delivery is Delivery.WAITING_LINK
    await store.step(link)
    second = cast("list[Mapping[str, JsonValue]]", link.sent[1]["events"])[0]
    assert json.loads(cast("str", second["detail"]))["nauseaScore"] == 1
    assert offered(store).delivery is Delivery.SENT


@pytest.mark.parametrize(
    ("answer", "delivery"),
    [
        (Acked({}), Delivery.SENT),
        (Refusal("no", final=True, status=409, code=None), Delivery.REFUSED),
        (Held("lost"), Delivery.NOT_PROVIDED),
        (RouteMissing(), Delivery.NOT_PROVIDED),
        (
            Refusal("not over", final=False, status=400, code=SESSION_NOT_ENDED),
            Delivery.NOT_PROVIDED,
        ),
    ],
)
async def test_a_card_skipped_while_its_feedback_travels_is_told_what_became_of_it(
    answer: Answer, delivery: Delivery
) -> None:
    ticking = clock()
    store = PassengerFeedback(ticking)
    store.ended(lambda: "SESSION-1")
    assert store.submit(FEEDBACK) == Ok(None)
    link = Link(answers=[answer], during=[store.skip])
    store_entries = store._entries  # pyright: ignore[reportPrivateUsage]  # what the closed card holds
    (entry,) = store_entries
    await store.step(link)
    assert entry.delivery is delivery
    assert entry.feedback is None
    ticking.advance(Seconds(RETRY_PERIOD))
    await store.step(link)
    assert len(link.sent) == 1, "a skipped feedback is never sent again"


def test_every_delivery_has_its_words_and_a_refusal_only_its_code() -> None:
    assert set(DELIVERY_WORDS) == set(Delivery)
    assert describe_delivery(Delivery.TO_FILL, None) == ""
    for delivery in Delivery:
        assert SENTINEL not in describe_delivery(delivery, ErrorCode("feedback_window_closed"))


async def test_an_answer_of_no_known_kind_is_a_programming_error() -> None:
    _, store = entered()
    with pytest.raises(AssertionError):
        await store.step(Link(answers=[cast("Answer", "not an answer")]))


async def test_p5_a_session_not_ended_on_the_dashboard_is_tried_again_and_then_taken() -> None:
    """The dashboard has the end only once the console's points are acknowledged."""
    ticking, store = entered()
    not_over = Refusal("not over", final=False, status=400, code=SESSION_NOT_ENDED)
    link = Link(answers=[not_over])
    await store.step(link)
    current = offered(store)
    assert (current.delivery, current.can_submit, current.code) == (
        Delivery.WAITING_END,
        True,
        None,
    )
    assert "fin de séance" in describe_delivery(current.delivery, current.code)
    await store.step(link)
    assert len(link.sent) == 1, "nothing again before the retry period"
    ticking.advance(Seconds(RETRY_PERIOD))
    await store.step(link)
    assert len(link.sent) == 2
    assert link.sent[1]["sessionId"] == "SESSION-1"
    assert offered(store).delivery is Delivery.SENT
