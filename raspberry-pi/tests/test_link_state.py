"""The state of the dashboard link an operator reads, from what the link last heard.

:class:`~src.link_state.LinkHealth` alone, with instants given by hand. What the
real link feeds it, and what the console's page is served, is in
``tests/test_link_indicator.py``.

The rule every test here comes back to: the indicator never says more than was
heard. Never ``joignable`` of a dashboard that has answered nothing for
:data:`~src.link_state.UNREACHABLE_AFTER`, never ``injoignable`` on one lost
request, and a refusal the dashboard stated stands until something proves the
contrary.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final, cast

import pytest
from hypothesis import given
from hypothesis import strategies as st

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
TIMEOUT: Final[str] = "GET /api/machine/heartbeat: ReadTimeout('')"
JUST_UNDER: Final[float] = float(UNREACHABLE_AFTER) - 0.1


def at(seconds: float) -> Monotonic:
    """An instant, ``seconds`` after the console started."""
    return Monotonic(BORN + seconds)


def state_at(health: LinkHealth, seconds: float) -> LinkState:
    return health.status(at(seconds)).state


# =========================================================================
# Never configured, and configured but never reached
# =========================================================================


def test_a_console_without_a_key_reads_not_configured_and_says_which_key() -> None:
    assert LinkStatus(LinkState.NOT_CONFIGURED, NO_KEY, None) == NOT_CONFIGURED
    assert "MACHINE_API_KEY" in NOT_CONFIGURED.detail
    assert label_of(NOT_CONFIGURED.state) == "non configure"


def test_a_console_that_has_just_started_waits_before_it_calls_the_dashboard_unreachable() -> None:
    health = LinkHealth(BORN)
    assert health.status(at(0.0)) == LinkStatus(LinkState.WAITING, "", None)
    assert state_at(health, JUST_UNDER) is LinkState.WAITING
    # Nothing was even seen to fail: the reason says no exchange went through.
    assert health.status(at(float(UNREACHABLE_AFTER))) == LinkStatus(
        LinkState.UNREACHABLE, NO_EXCHANGE, None
    )


def test_a_dashboard_never_reached_reads_unreachable_with_the_last_failure_and_no_age() -> None:
    health = LinkHealth(BORN)
    health.silent(TIMEOUT)
    # While it waits, the failure is already said: the operator is not left guessing.
    assert health.status(at(4.0)) == LinkStatus(LinkState.WAITING, TIMEOUT, None)
    health.silent("POST /api/machine/profiles: ConnectError('no route to host')")
    late = health.status(at(float(UNREACHABLE_AFTER)))
    assert late.state is LinkState.UNREACHABLE
    assert late.detail == "POST /api/machine/profiles: ConnectError('no route to host')"
    assert late.last_answer_age is None, "it never answered: no age is invented"


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


# =========================================================================
# Reachable, silent, back again
# =========================================================================


def test_the_first_answer_makes_the_link_reachable_at_once() -> None:
    health = LinkHealth(BORN)
    health.answered(at(1.0), contract=True)
    assert health.status(at(1.0)) == LinkStatus(LinkState.REACHABLE, "", Seconds(0.0))
    assert health.status(at(8.5)).last_answer_age == Seconds(7.5)


def test_one_lost_request_never_changes_what_the_operator_reads() -> None:
    """A heartbeat lost, the next one answered: the state does not move at any instant."""
    health = LinkHealth(BORN)
    health.answered(at(0.0), contract=True)
    health.silent(TIMEOUT)  # the heartbeat of t = 10 s
    seen = {state_at(health, 10.0 + step * 0.5) for step in range(20)}
    assert seen == {LinkState.REACHABLE}
    # The age shown beside it says how old the last answer is meanwhile.
    assert health.status(at(19.5)).last_answer_age == Seconds(19.5)
    health.answered(at(20.0), contract=True)
    assert health.status(at(20.0)) == LinkStatus(LinkState.REACHABLE, "", Seconds(0.0))


def test_a_silent_dashboard_reads_unreachable_after_the_delay_and_not_before() -> None:
    health = LinkHealth(BORN)
    health.answered(at(0.0), contract=True)
    health.silent(TIMEOUT)
    health.silent(TIMEOUT)
    assert state_at(health, JUST_UNDER) is LinkState.REACHABLE
    silent = health.status(at(float(UNREACHABLE_AFTER)))
    assert silent == LinkStatus(LinkState.UNREACHABLE, TIMEOUT, UNREACHABLE_AFTER)
    assert state_at(health, 3600.0) is LinkState.UNREACHABLE


def test_the_delay_is_two_heartbeats_and_a_half() -> None:
    """One heartbeat lost is inside it, two in a row are not; far inside the 90 s offline cutoff."""
    assert Seconds(20.0) < UNREACHABLE_AFTER < Seconds(30.0)


def test_a_dashboard_that_answers_again_is_reachable_at_once() -> None:
    health = LinkHealth(BORN)
    health.answered(at(0.0), contract=True)
    health.silent(TIMEOUT)
    assert state_at(health, 60.0) is LinkState.UNREACHABLE
    health.answered(at(61.0), contract=True)
    assert health.status(at(61.0)) == LinkStatus(LinkState.REACHABLE, "", Seconds(0.0))
    # The failure of before is forgotten with it: a later silence has its own reason.
    assert health.status(at(61.0 + float(UNREACHABLE_AFTER))).detail == NO_EXCHANGE


def test_a_link_that_stops_exchanging_reads_unreachable_even_with_no_failure_seen() -> None:
    """The link's own task waiting on something else: no answer is no answer."""
    health = LinkHealth(BORN)
    health.answered(at(0.0), contract=True)
    assert health.status(at(40.0)) == LinkStatus(LinkState.UNREACHABLE, NO_EXCHANGE, Seconds(40.0))


def test_an_error_of_the_dashboards_own_is_named_as_such_after_the_same_delay() -> None:
    health = LinkHealth(BORN)
    health.answered(at(0.0), contract=True)
    health.errored("HTTP 503")
    assert state_at(health, JUST_UNDER) is LinkState.REACHABLE, "one 503 is not a state"
    broken = health.status(at(float(UNREACHABLE_AFTER)))
    assert broken == LinkStatus(LinkState.SERVER_ERROR, "HTTP 503", UNREACHABLE_AFTER)
    # Whatever came back last names the state: here the network, after the errors.
    health.silent(TIMEOUT)
    assert state_at(health, 30.0) is LinkState.UNREACHABLE
    health.answered(at(31.0), contract=True)
    assert state_at(health, 31.0) is LinkState.REACHABLE


# =========================================================================
# A refused key
# =========================================================================


def test_a_refused_key_is_said_at_once_and_stands_until_an_answer_that_is_not_about_it() -> None:
    health = LinkHealth(BORN)
    health.answered(at(0.0), contract=True)
    health.key_refused(at(10.0), "HTTP 401 (unauthorized)")
    assert health.status(at(10.0)) == LinkStatus(
        LinkState.KEY_REFUSED, "HTTP 401 (unauthorized)", Seconds(0.0)
    )
    # A slow request in between changes nothing of it.
    health.silent(TIMEOUT)
    assert state_at(health, 12.0) is LinkState.KEY_REFUSED
    health.key_refused(at(20.0), "HTTP 401 (unauthorized)")
    assert state_at(health, 20.0) is LinkState.KEY_REFUSED
    # The route that answers under every contract checks the key too: its answer clears it.
    health.answered(at(23.0), contract=False)
    assert state_at(health, 23.0) is LinkState.REACHABLE


def test_a_dashboard_that_goes_silent_after_refusing_the_key_reads_unreachable() -> None:
    """What was last said is not repeated for ever over a dashboard that answers nothing."""
    health = LinkHealth(BORN)
    health.key_refused(at(0.0), "HTTP 403")
    health.silent(TIMEOUT)
    assert state_at(health, JUST_UNDER) is LinkState.KEY_REFUSED
    assert state_at(health, float(UNREACHABLE_AFTER)) is LinkState.UNREACHABLE
    # It answers again, and still refuses: that is what is read again.
    health.key_refused(at(50.0), "HTTP 403")
    assert state_at(health, 50.0) is LinkState.KEY_REFUSED


# =========================================================================
# Another contract
# =========================================================================


def test_a_refused_contract_is_said_at_once_and_the_status_route_never_clears_it() -> None:
    """A session under a 426: the stop question is answered every 3 s and proves nothing."""
    health = LinkHealth(BORN)
    health.answered(at(0.0), contract=True)
    health.contract_refused(at(10.0), SENTENCE)
    assert health.status(at(10.0)) == LinkStatus(LinkState.INCOMPATIBLE, SENTENCE, Seconds(0.0))
    seen: set[LinkState] = set()
    for step in range(1, 40):
        health.answered(at(10.0 + 3.0 * step), contract=False)
        seen.add(state_at(health, 10.0 + 3.0 * step))
        seen.add(state_at(health, 11.5 + 3.0 * step))
    assert seen == {LinkState.INCOMPATIBLE}, "it does not flap while the session goes on"
    # An answer of a route the dashboard serves only to this console's major does.
    health.answered(at(200.0), contract=True)
    assert state_at(health, 200.0) is LinkState.REACHABLE


def test_a_contract_read_in_a_poll_answer_is_cleared_by_a_poll_answer_only() -> None:
    """An older dashboard takes everything this console sends: its successes prove nothing."""
    health = LinkHealth(BORN)
    health.answered(at(0.0), contract=True)
    health.announced("serveur incompatible (contrat 1.1 vs inconnu)")
    assert health.status(at(0.0)).state is LinkState.INCOMPATIBLE
    assert health.status(at(0.0)).detail == "serveur incompatible (contrat 1.1 vs inconnu)"
    health.answered(at(10.0), contract=True)  # a heartbeat it accepted
    assert state_at(health, 10.0) is LinkState.INCOMPATIBLE
    health.answered(at(12.0), contract=True)
    health.announced(None)  # a poll answer of this console's major
    assert state_at(health, 12.0) is LinkState.REACHABLE


def test_the_dashboards_own_refusal_is_the_reason_shown_before_the_consoles_reading() -> None:
    health = LinkHealth(BORN)
    health.answered(at(0.0), contract=True)
    health.announced("serveur incompatible (contrat 1.1 vs 3.0)")
    health.contract_refused(at(1.0), SENTENCE)
    assert health.status(at(1.0)).detail == SENTENCE
    health.answered(at(2.0), contract=True)
    # The 426 is over; what the last poll answer announced still stands.
    assert health.status(at(2.0)) == LinkStatus(
        LinkState.INCOMPATIBLE, "serveur incompatible (contrat 1.1 vs 3.0)", Seconds(0.0)
    )


def test_a_refused_key_is_read_before_a_refused_contract_and_each_is_cleared_by_its_own_proof() -> (
    None
):
    """Both at once: one word, always the same, never one then the other at each exchange."""
    health = LinkHealth(BORN)
    health.contract_refused(at(0.0), SENTENCE)
    health.key_refused(at(1.0), "HTTP 401 (unauthorized)")
    health.contract_refused(at(2.0), SENTENCE)
    assert state_at(health, 2.0) is LinkState.KEY_REFUSED
    # The key is accepted again (the status route says so): the contract is still refused.
    health.answered(at(3.0), contract=False)
    assert state_at(health, 3.0) is LinkState.INCOMPATIBLE
    health.answered(at(4.0), contract=True)
    assert state_at(health, 4.0) is LinkState.REACHABLE


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
    health = LinkHealth(BORN)
    wall = "serveur incompatible (contrat 1.1 vs " + ", ".join(["9999"] * 500) + ")\n<script>"
    health.contract_refused(at(0.0), wall)
    assert len(health.status(at(0.0)).detail) == MAX_DETAIL
    health.answered(at(1.0), contract=True)
    health.announced(wall)
    assert len(health.status(at(1.0)).detail) == MAX_DETAIL
    health.announced(None)
    health.key_refused(at(2.0), wall)
    assert len(health.status(at(2.0)).detail) == MAX_DETAIL
    health.answered(at(3.0), contract=True)
    health.errored(wall)
    assert len(health.status(at(3.0 + float(UNREACHABLE_AFTER))).detail) == MAX_DETAIL
    health.silent(wall)
    assert len(health.status(at(3.0 + float(UNREACHABLE_AFTER))).detail) == MAX_DETAIL
    assert "\n" not in health.status(at(3.0 + float(UNREACHABLE_AFTER))).detail


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

HEARD: Final[st.SearchStrategy[tuple[str, float]]] = st.tuples(
    st.sampled_from(
        ["answer", "status_answer", "key", "contract", "foreign", "served", "silent", "error"]
    ),
    st.floats(min_value=0.0, max_value=40.0, allow_nan=False),
)


def tell(health: LinkHealth, what: str, now: Monotonic) -> bool:
    """Tell ``health`` one thing the link heard. Whether the dashboard answered usably."""
    answered = what not in ("silent", "error", "foreign", "served")
    if what == "answer":
        health.answered(now, contract=True)
    elif what == "status_answer":
        health.answered(now, contract=False)
    elif what == "key":
        health.key_refused(now, "HTTP 401 (unauthorized)")
    elif what == "contract":
        health.contract_refused(now, SENTENCE)
    elif what == "foreign":
        health.announced(SENTENCE)
    elif what == "served":
        health.announced(None)
    elif what == "silent":
        health.silent(TIMEOUT)
    else:
        health.errored("HTTP 503")
    return answered


@given(st.lists(HEARD, max_size=40), st.floats(min_value=0.0, max_value=60.0, allow_nan=False))
def test_the_indicator_never_says_more_than_was_heard(
    heard: list[tuple[str, float]], later: float
) -> None:
    """For any history: never reachable over a silence, never unreachable over a fresh answer."""
    health = LinkHealth(BORN)
    now = BORN
    last_answer: Monotonic | None = None
    for what, gap in heard:
        now = Monotonic(now + gap)
        if tell(health, what, now):
            last_answer = now
    read_at = Monotonic(now + later)
    status = health.status(read_at)
    quiet = read_at - (BORN if last_answer is None else last_answer)
    stale = quiet >= UNREACHABLE_AFTER
    assert (status.state in (LinkState.UNREACHABLE, LinkState.SERVER_ERROR)) == stale
    if last_answer is None:
        assert status.last_answer_age is None
        assert status.state is not LinkState.REACHABLE
    else:
        assert status.last_answer_age == Seconds(read_at - last_answer)
    assert status.state is not LinkState.NOT_CONFIGURED
    assert len(status.detail) <= MAX_DETAIL
    assert label_of(status.state)
    # Reading is not an event: asked twice, the same answer.
    assert health.status(read_at) == status


@given(
    st.lists(HEARD, max_size=20),
    st.sampled_from(["silent", "error"]),
    st.floats(min_value=0.0, max_value=JUST_UNDER),
)
def test_one_exchange_that_fails_never_changes_the_reading_by_itself(
    heard: list[tuple[str, float]], failure: str, after: float
) -> None:
    """No flapping: whatever came before, a single slow or failed request moves nothing."""
    health = LinkHealth(BORN)
    now = BORN
    for what, gap in heard:
        now = Monotonic(now + gap)
        tell(health, what, now)
    health.answered(now, contract=False)
    read_at = Monotonic(now + after)
    before = health.status(read_at)
    tell(health, failure, read_at)
    assert health.status(read_at).state is before.state
