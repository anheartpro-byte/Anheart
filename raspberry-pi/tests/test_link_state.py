"""The state of the dashboard link an operator reads, from what the link last heard.

:class:`~src.link_state.LinkHealth` alone, with instants given by hand. What the
real link feeds it, and what the console's page is served, is in
``tests/test_link_indicator.py``.

The two rules every test here comes back to:

* the state is what the routes that carry the heartbeat and the session last
  said, for as long as they keep saying it. Only an answer of theirs that took
  what was sent reads ``joignable``; the stop question's answers keep the age
  of the last answer fresh and change nothing else;
* a silence, an error and an answer that is not the dashboard's prove nothing.
  They never read ``joignable``, never unsay a refusal, and show only once
  those routes have said nothing recognisable for
  :data:`~src.link_state.UNREACHABLE_AFTER`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final, cast

import pytest
from hypothesis import given
from hypothesis import strategies as st

from src.cloud_sync import HEARTBEAT_PERIOD
from src.link_state import (
    MAX_DETAIL,
    NO_EXCHANGE,
    NO_KEY,
    NOT_CONFIGURED,
    UNREACHABLE_AFTER,
    LinkHealth,
    LinkState,
    LinkStatus,
    bounded,
    label_of,
)
from src.units import Monotonic, Seconds

BORN: Final[Monotonic] = Monotonic(100.0)
SENTENCE: Final[str] = "serveur incompatible (contrat 1.1 vs 2)"
TIMEOUT: Final[str] = "POST /api/machine/heartbeat: ReadTimeout('')"
NOT_OURS: Final[str] = "HTTP 404 : pas une reponse du tableau de bord"
DELAY: Final[float] = float(UNREACHABLE_AFTER)
JUST_UNDER: Final[float] = DELAY - 0.1


def at(seconds: float) -> Monotonic:
    """An instant, ``seconds`` after the console started."""
    return Monotonic(BORN + seconds)


def state_at(health: LinkHealth, seconds: float) -> LinkState:
    return health.status(at(seconds)).state


def taking(seconds: float = 0.0) -> LinkHealth:
    """A link whose dashboard took something ``seconds`` after the console started."""
    health = LinkHealth(BORN)
    health.answered(at(seconds), contract=True)
    return health


# =========================================================================
# The words, and the delay the pages quote
# =========================================================================


def test_a_console_without_a_key_reads_not_configured_and_says_which_key() -> None:
    assert LinkStatus(LinkState.NOT_CONFIGURED, NO_KEY, None) == NOT_CONFIGURED
    assert "MACHINE_API_KEY" in NOT_CONFIGURED.detail
    assert label_of(NOT_CONFIGURED.state) == "non configure"


def test_every_state_has_a_word_and_a_state_added_without_one_fails_loudly() -> None:
    words = {label_of(state) for state in LinkState}
    assert len(words) == len(LinkState), "two states must never read the same"
    assert all(word.isascii() and word == word.strip() for word in words)
    with pytest.raises(AssertionError):
        label_of(cast("LinkState", "a_state_added_later"))


DOCS: Final[Path] = Path(__file__).resolve().parents[2] / "docs"


@pytest.mark.parametrize(
    ("page", "quoted"),
    [
        ("console-locale.md", "`{}`"),
        ("guides/guide-console-locale.md", "`{}`"),
        ("raspberry-pi.md", "`{}`"),
        ("client/manuel-operateur.md", "« {} »"),
    ],
)
def test_the_pages_that_describe_the_indicator_quote_its_words_exactly(
    page: str, quoted: str
) -> None:
    """EX-4: an operator looks for the word on the screen in the page that explains it."""
    text = (DOCS / page).read_text(encoding="utf-8")
    missing = [label_of(state) for state in LinkState if quoted.format(label_of(state)) not in text]
    assert missing == [], f"{page} does not quote {missing}"


def test_the_delay_is_the_25_seconds_the_pages_quote() -> None:
    """Two heartbeats and a half. The pages say 25 s: the number is theirs and this one's."""
    assert float(UNREACHABLE_AFTER) == 25.0  # the number the pages quote
    assert Seconds(2.5 * HEARTBEAT_PERIOD) == UNREACHABLE_AFTER
    for page in ("console-locale.md", "guides/guide-console-locale.md", "raspberry-pi.md"):
        assert "25 s" in (DOCS / page).read_text(encoding="utf-8"), page
    # And it is the delay applied, to the tenth of a second.
    health = taking()
    health.silent(TIMEOUT, contract=True)
    assert state_at(health, 24.9) is LinkState.REACHABLE
    assert state_at(health, 25.0) is LinkState.UNREACHABLE
    never = LinkHealth(BORN)
    assert state_at(never, 24.9) is LinkState.WAITING
    assert state_at(never, 25.0) is LinkState.UNREACHABLE


# =========================================================================
# Configured but never reached
# =========================================================================


def test_a_console_that_has_just_started_waits_before_it_calls_the_dashboard_unreachable() -> None:
    health = LinkHealth(BORN)
    assert health.status(at(0.0)) == LinkStatus(LinkState.WAITING, "", None)
    assert state_at(health, JUST_UNDER) is LinkState.WAITING
    # Nothing was even seen to fail: the reason says nothing sent went through.
    assert health.status(at(DELAY)) == LinkStatus(LinkState.UNREACHABLE, NO_EXCHANGE, None)


def test_a_dashboard_never_reached_reads_unreachable_with_the_last_failure_and_no_age() -> None:
    health = LinkHealth(BORN)
    health.silent(TIMEOUT, contract=True)
    # While it waits, the failure is already said: the operator is not left guessing.
    assert health.status(at(4.0)) == LinkStatus(LinkState.WAITING, TIMEOUT, None)
    health.silent("POST /api/machine/profiles: ConnectError('no route to host')", contract=True)
    late = health.status(at(DELAY))
    assert late.state is LinkState.UNREACHABLE
    assert late.detail == "POST /api/machine/profiles: ConnectError('no route to host')"
    assert late.last_answer_age is None, "it never answered: no age is invented"


# =========================================================================
# B1: only the dashboard's own answer, taking what was sent, reads reachable
# =========================================================================


def test_the_first_answer_that_takes_what_was_sent_reads_reachable_at_once() -> None:
    health = LinkHealth(BORN)
    health.answered(at(1.0), contract=True)
    assert health.status(at(1.0)) == LinkStatus(LinkState.REACHABLE, "", Seconds(0.0))
    assert health.status(at(8.5)).last_answer_age == Seconds(7.5)


def test_an_address_that_answers_what_is_not_the_dashboards_never_reads_reachable() -> None:
    """A 404 with no stable code at every route: waiting, then unreachable, with the reason."""
    health = LinkHealth(BORN)
    seen: set[LinkState] = set()
    for second in range(120):
        health.unrecognised(NOT_OURS, contract=True)
        seen.add(state_at(health, float(second)))
    assert seen == {LinkState.WAITING, LinkState.UNREACHABLE}
    assert health.status(at(1.0)) == LinkStatus(LinkState.WAITING, NOT_OURS, None)
    assert health.status(at(DELAY)) == LinkStatus(LinkState.UNREACHABLE, NOT_OURS, None)


def test_an_answer_that_is_not_the_dashboards_unsays_nothing_and_proves_nothing() -> None:
    """A 426 on every route but one, which answers a 404 with no code: no flapping."""
    health = LinkHealth(BORN)
    seen: set[LinkState] = set()
    for second in range(60):
        now = at(float(second))
        if second % 3 == 0:
            health.contract_refused(now, SENTENCE, contract=True)
        if second % 5 == 0:
            health.unrecognised(NOT_OURS, contract=True)
        seen.add(health.status(now).state)
    assert seen == {LinkState.INCOMPATIBLE}
    # Nor does it keep a state alive: after a proof, alone, it lets the state lapse.
    health = taking()
    for second in range(1, 40):
        health.unrecognised(NOT_OURS, contract=True)
        expected = LinkState.REACHABLE if second < DELAY else LinkState.UNREACHABLE
        assert state_at(health, float(second)) is expected
    assert health.status(at(39.0)).detail == NOT_OURS
    assert health.status(at(39.0)).last_answer_age == Seconds(39.0)


# =========================================================================
# Reachable, silent, back again
# =========================================================================


def test_one_lost_request_never_changes_what_the_operator_reads() -> None:
    """A heartbeat lost, the next one answered: the state does not move at any instant."""
    health = taking()
    health.silent(TIMEOUT, contract=True)  # the heartbeat of t = 10 s
    seen = {state_at(health, 10.0 + step * 0.5) for step in range(20)}
    assert seen == {LinkState.REACHABLE}
    # The age shown beside it says how old the last answer is meanwhile.
    assert health.status(at(19.5)).last_answer_age == Seconds(19.5)
    health.answered(at(20.0), contract=True)
    assert health.status(at(20.0)) == LinkStatus(LinkState.REACHABLE, "", Seconds(0.0))


def test_a_silent_dashboard_reads_unreachable_after_the_delay_and_not_before() -> None:
    health = taking()
    health.silent(TIMEOUT, contract=True)
    health.silent(TIMEOUT, contract=True)
    assert state_at(health, JUST_UNDER) is LinkState.REACHABLE
    silent = health.status(at(DELAY))
    assert silent == LinkStatus(LinkState.UNREACHABLE, TIMEOUT, UNREACHABLE_AFTER)
    assert state_at(health, 3600.0) is LinkState.UNREACHABLE


def test_a_dashboard_that_takes_again_is_reachable_at_once() -> None:
    health = taking()
    health.silent(TIMEOUT, contract=True)
    assert state_at(health, 60.0) is LinkState.UNREACHABLE
    health.answered(at(61.0), contract=True)
    assert health.status(at(61.0)) == LinkStatus(LinkState.REACHABLE, "", Seconds(0.0))
    # The failure of before is forgotten with it: a later silence has its own reason.
    assert health.status(at(61.0 + DELAY)).detail == NO_EXCHANGE


def test_a_link_that_stops_exchanging_reads_unreachable_even_with_no_failure_seen() -> None:
    """The link's own task waiting on something else: no answer is no answer."""
    health = taking()
    assert health.status(at(40.0)) == LinkStatus(LinkState.UNREACHABLE, NO_EXCHANGE, Seconds(40.0))


def test_an_error_of_the_dashboards_own_is_named_as_such_after_the_same_delay() -> None:
    health = taking()
    health.errored("HTTP 503", contract=True)
    assert state_at(health, JUST_UNDER) is LinkState.REACHABLE, "one 503 is not a state"
    broken = health.status(at(DELAY))
    assert broken == LinkStatus(LinkState.SERVER_ERROR, "HTTP 503", UNREACHABLE_AFTER)
    # Whatever came back last names the state: here the network, after the errors.
    health.silent(TIMEOUT, contract=True)
    assert state_at(health, 30.0) is LinkState.UNREACHABLE
    health.answered(at(31.0), contract=True)
    assert state_at(health, 31.0) is LinkState.REACHABLE


# =========================================================================
# B2: the stop question keeps the last answer fresh, and nothing else
# =========================================================================


def test_a_session_of_which_nothing_arrives_does_not_read_reachable() -> None:
    """Heartbeat and uploads answer 500 then are lost; the stop question is served all along."""
    health = taking()
    seen: list[tuple[int, LinkStatus]] = []
    for second in range(1, 241):
        now = at(float(second))
        if second % 3 == 0:
            health.answered(now, contract=False)  # the stop question
        if second % 5 == 0:
            if second <= 120:
                health.errored("HTTP 500", contract=True)
            else:
                health.silent(TIMEOUT, contract=True)
        seen.append((second, health.status(now)))
    for second, status in seen:
        if second < DELAY:
            assert status.state is LinkState.REACHABLE, second
        else:
            # The dashboard is there (it answers the stop question) and takes nothing.
            assert status.state is LinkState.SERVER_ERROR, second
            assert status.detail == ("HTTP 500" if second < 125 else TIMEOUT), second
        if second >= 3:
            assert status.last_answer_age is not None
            assert status.last_answer_age <= Seconds(2.0), "the stop question keeps it fresh"
    # It takes again: reachable at once.
    health.answered(at(241.0), contract=True)
    assert health.status(at(241.0)) == LinkStatus(LinkState.REACHABLE, "", Seconds(0.0))


def test_the_stop_question_alone_never_makes_the_link_read_reachable() -> None:
    health = LinkHealth(BORN)
    for second in range(0, 90, 3):
        health.answered(at(float(second)), contract=False)
        assert state_at(health, float(second)) is not LinkState.REACHABLE
    # Answered, so the dashboard is there; nothing sent was ever taken.
    assert health.status(at(87.0)) == LinkStatus(LinkState.SERVER_ERROR, NO_EXCHANGE, Seconds(0.0))


def test_once_the_stop_question_falls_silent_too_the_last_failure_names_the_state() -> None:
    health = taking()
    health.errored("HTTP 500", contract=True)
    health.answered(at(30.0), contract=False)
    assert state_at(health, 40.0) is LinkState.SERVER_ERROR
    health.silent(TIMEOUT, contract=True)
    assert state_at(health, 54.9) is LinkState.SERVER_ERROR, "still answered 25 s ago"
    assert health.status(at(55.0)) == LinkStatus(LinkState.UNREACHABLE, TIMEOUT, Seconds(25.0))


def test_what_the_stop_question_fails_at_is_not_the_state_of_the_link() -> None:
    """Its own timeouts, errors and odd answers are not about what the console sends."""
    health = taking()
    health.silent("GET /api/machine/training/status: ReadTimeout('')", contract=False)
    health.errored("HTTP 500", contract=False)
    health.unrecognised(NOT_OURS, contract=False)
    health.key_refused(at(5.0), "HTTP 401 (unauthorized)", contract=False)
    health.contract_refused(at(6.0), SENTENCE, contract=False)
    assert health.status(at(6.0)) == LinkStatus(LinkState.REACHABLE, "", Seconds(0.0))
    # And they leave no reason behind for a later silence of the other routes.
    assert health.status(at(DELAY + 10.0)).detail == NO_EXCHANGE


# =========================================================================
# A refused key, another contract
# =========================================================================


def test_a_refused_key_is_said_at_once_and_stands_until_something_sent_is_taken() -> None:
    health = taking()
    health.key_refused(at(10.0), "HTTP 401 (unauthorized)", contract=True)
    assert health.status(at(10.0)) == LinkStatus(
        LinkState.KEY_REFUSED, "HTTP 401 (unauthorized)", Seconds(0.0)
    )
    # A slow request in between changes nothing of it, nor does the stop question.
    health.silent(TIMEOUT, contract=True)
    health.answered(at(12.0), contract=False)
    assert state_at(health, 12.0) is LinkState.KEY_REFUSED
    health.key_refused(at(20.0), "HTTP 401 (unauthorized)", contract=True)
    assert state_at(health, 20.0) is LinkState.KEY_REFUSED
    health.answered(at(23.0), contract=True)
    assert state_at(health, 23.0) is LinkState.REACHABLE


def test_a_426_after_a_refused_key_reads_incompatible_the_key_was_taken() -> None:
    """The dashboard checks the key before the contract: a 426 means the key passed."""
    health = LinkHealth(BORN)
    health.key_refused(at(0.0), "HTTP 401 (unauthorized)", contract=True)
    assert state_at(health, 0.0) is LinkState.KEY_REFUSED
    seen: set[LinkState] = set()
    for second in range(3, 63, 3):
        health.contract_refused(at(float(second)), SENTENCE, contract=True)
        seen.add(state_at(health, float(second)))
    assert seen == {LinkState.INCOMPATIBLE}
    # And the other way round: the latest statement of those routes is the one read.
    health.key_refused(at(63.0), "HTTP 403", contract=True)
    assert health.status(at(63.0)) == LinkStatus(LinkState.KEY_REFUSED, "HTTP 403", Seconds(0.0))


def test_a_dashboard_that_goes_silent_after_a_refusal_no_longer_reads_as_that_refusal() -> None:
    """What was last said is not repeated for ever over a dashboard that answers nothing."""
    for refuse in ("key", "contract"):
        health = LinkHealth(BORN)
        if refuse == "key":
            health.key_refused(at(0.0), "HTTP 403", contract=True)
            stated = LinkState.KEY_REFUSED
        else:
            health.contract_refused(at(0.0), SENTENCE, contract=True)
            stated = LinkState.INCOMPATIBLE
        health.silent(TIMEOUT, contract=True)
        assert state_at(health, JUST_UNDER) is stated
        assert state_at(health, DELAY) is LinkState.UNREACHABLE
        # It answers again, and still refuses: that is what is read again.
        health.key_refused(at(50.0), "HTTP 403", contract=True)
        assert state_at(health, 50.0) is LinkState.KEY_REFUSED


def test_a_refused_contract_is_said_at_once_and_the_stop_question_never_clears_it() -> None:
    """A session under a 426: the stop question is answered every 3 s and proves nothing."""
    health = taking()
    health.contract_refused(at(10.0), SENTENCE, contract=True)
    assert health.status(at(10.0)) == LinkStatus(LinkState.INCOMPATIBLE, SENTENCE, Seconds(0.0))
    seen: set[LinkState] = set()
    for step in range(1, 40):
        now = 10.0 + 3.0 * step
        health.answered(at(now), contract=False)
        if step % 3 == 0:
            health.contract_refused(at(now), SENTENCE, contract=True)  # the heartbeat
        seen.add(state_at(health, now))
        seen.add(state_at(health, now + 1.5))
    assert seen == {LinkState.INCOMPATIBLE}, "it does not flap while the session goes on"
    # An answer that takes what was sent, on a route under the contract, does clear it.
    health.answered(at(200.0), contract=True)
    assert state_at(health, 200.0) is LinkState.REACHABLE


def test_a_contract_read_in_a_poll_answer_is_cleared_by_a_poll_answer_only() -> None:
    """An older dashboard takes everything this console sends: its successes prove nothing."""
    health = taking()
    health.announced("serveur incompatible (contrat 1.1 vs inconnu)")
    assert health.status(at(0.0)).state is LinkState.INCOMPATIBLE
    assert health.status(at(0.0)).detail == "serveur incompatible (contrat 1.1 vs inconnu)"
    health.answered(at(10.0), contract=True)  # a heartbeat it accepted
    assert state_at(health, 10.0) is LinkState.INCOMPATIBLE
    health.answered(at(12.0), contract=True)
    health.announced(None)  # a poll answer of this console's major
    assert state_at(health, 12.0) is LinkState.REACHABLE


def test_the_dashboards_own_refusal_is_the_reason_shown_before_the_consoles_reading() -> None:
    health = taking()
    health.announced("serveur incompatible (contrat 1.1 vs 3.0)")
    health.contract_refused(at(1.0), SENTENCE, contract=True)
    assert health.status(at(1.0)).detail == SENTENCE
    health.answered(at(2.0), contract=True)
    # The 426 is over; what the last poll answer announced still stands.
    assert health.status(at(2.0)) == LinkStatus(
        LinkState.INCOMPATIBLE, "serveur incompatible (contrat 1.1 vs 3.0)", Seconds(0.0)
    )


# =========================================================================
# What the page is given is text, and bounded
# =========================================================================


def test_a_reason_is_one_bounded_line_whatever_was_sent() -> None:
    assert bounded("  HTTP 401\n(unauthorized)\t ") == "HTTP 401 (unauthorized)"
    separator = chr(0x2028)  # a line separator: unprintable, and no space either
    assert bounded(f"a\x00b\x1b[31mc{separator}d") == "a b [31mc d"
    # Half a character, as a reply cut in the middle of one can leave: it must not raise.
    assert bounded(f"a{chr(0xD800)}b") == "a b"
    assert bounded("") == ""
    exact = "x" * MAX_DETAIL
    assert bounded(exact) == exact
    cut = bounded("x" * (MAX_DETAIL + 1))
    assert len(cut) == MAX_DETAIL
    assert cut.endswith("...")


def test_every_reason_kept_is_bounded_at_the_door() -> None:
    wall = "serveur incompatible (contrat 1.1 vs " + ", ".join(["9999"] * 500) + ")\n<script>"
    late = at(3.0 + DELAY)
    health = LinkHealth(BORN)
    health.contract_refused(at(0.0), wall, contract=True)
    assert len(health.status(at(0.0)).detail) == MAX_DETAIL
    health.answered(at(1.0), contract=True)
    health.announced(wall)
    assert len(health.status(at(1.0)).detail) == MAX_DETAIL
    health.announced(None)
    health.key_refused(at(2.0), wall, contract=True)
    assert len(health.status(at(2.0)).detail) == MAX_DETAIL
    health.answered(at(3.0), contract=True)
    for fail in (health.errored, health.silent, health.unrecognised):
        fail(wall, contract=True)
        assert len(health.status(late).detail) == MAX_DETAIL
        assert "\n" not in health.status(late).detail


@given(st.text(max_size=600))
def test_any_text_comes_out_as_one_printable_line_of_bounded_length(text: str) -> None:
    line = bounded(text)
    assert len(line) <= MAX_DETAIL
    assert line.isprintable()
    assert line == line.strip()
    assert bounded(line) == line


# =========================================================================
# Whatever the link hears, in whatever order
# =========================================================================

TAKEN: Final[frozenset[str]] = frozenset({"answer"})
STATED: Final[frozenset[str]] = frozenset({"answer", "key", "contract"})
RECOGNISED: Final[frozenset[str]] = frozenset({*STATED, "stop_answer", "stop_key"})
HEARD: Final[st.SearchStrategy[tuple[str, float]]] = st.tuples(
    st.sampled_from(
        [
            "answer",
            "key",
            "contract",
            "stop_answer",
            "stop_key",
            "stop_silent",
            "foreign",
            "served",
            "silent",
            "error",
            "odd",
        ]
    ),
    st.floats(min_value=0.0, max_value=40.0, allow_nan=False),
)


def tell(health: LinkHealth, what: str, now: Monotonic) -> None:
    """Tell ``health`` one thing the link heard."""
    if what == "answer":
        health.answered(now, contract=True)
    elif what == "key":
        health.key_refused(now, "HTTP 401 (unauthorized)", contract=True)
    elif what == "contract":
        health.contract_refused(now, SENTENCE, contract=True)
    elif what == "stop_answer":
        health.answered(now, contract=False)
    elif what == "stop_key":
        health.key_refused(now, "HTTP 401 (unauthorized)", contract=False)
    elif what == "stop_silent":
        health.silent(TIMEOUT, contract=False)
    elif what == "foreign":
        health.announced(SENTENCE)
    elif what == "served":
        health.announced(None)
    elif what == "silent":
        health.silent(TIMEOUT, contract=True)
    elif what == "error":
        health.errored("HTTP 503", contract=True)
    else:
        health.unrecognised(NOT_OURS, contract=True)


@given(st.lists(HEARD, max_size=40), st.floats(min_value=0.0, max_value=60.0, allow_nan=False))
def test_the_indicator_never_says_more_than_was_heard(
    heard: list[tuple[str, float]], later: float
) -> None:
    """For any history: reachable only on something taken, by those routes, less than 25 s ago."""
    health = LinkHealth(BORN)
    now = BORN
    last_stated: tuple[Monotonic, str] | None = None
    last_recognised: Monotonic | None = None
    foreign = False
    for what, gap in heard:
        now = Monotonic(now + gap)
        tell(health, what, now)
        if what in STATED:
            last_stated = (now, what)
        if what in RECOGNISED:
            last_recognised = now
        if what in ("foreign", "served"):
            foreign = what == "foreign"
    read_at = Monotonic(now + later)
    status = health.status(read_at)

    fresh = (read_at - (BORN if last_stated is None else last_stated[0])) < UNREACHABLE_AFTER
    if status.state is LinkState.REACHABLE:
        assert last_stated is not None
        assert fresh
        assert last_stated[1] in TAKEN, "the last word of those routes took what was sent"
        assert not foreign
    if fresh and last_stated is not None:
        expected = {
            "answer": LinkState.INCOMPATIBLE if foreign else LinkState.REACHABLE,
            "key": LinkState.KEY_REFUSED,
            "contract": LinkState.INCOMPATIBLE,
        }[last_stated[1]]
        assert status.state is expected
    if not fresh:
        assert status.state in (LinkState.UNREACHABLE, LinkState.SERVER_ERROR)
    if last_recognised is None:
        assert status.last_answer_age is None
    else:
        assert status.last_answer_age == Seconds(read_at - last_recognised)
    assert status.state is not LinkState.NOT_CONFIGURED
    assert len(status.detail) <= MAX_DETAIL
    assert label_of(status.state)
    # Reading is not an event: asked twice, the same answer.
    assert health.status(read_at) == status


@given(
    st.lists(HEARD, max_size=20),
    st.sampled_from(["silent", "error", "odd", "stop_answer", "stop_silent", "stop_key"]),
    st.floats(min_value=0.0, max_value=JUST_UNDER),
)
def test_no_failure_and_no_answer_to_the_stop_question_changes_the_reading_by_itself(
    heard: list[tuple[str, float]], event: str, after: float
) -> None:
    """No flapping: whatever came before, one failed request moves nothing, and neither
    does anything the stop question brings back."""
    health = LinkHealth(BORN)
    now = BORN
    for what, gap in heard:
        now = Monotonic(now + gap)
        tell(health, what, now)
    health.answered(now, contract=True)
    read_at = Monotonic(now + after)
    before = health.status(read_at)
    tell(health, event, read_at)
    assert health.status(read_at).state is before.state
