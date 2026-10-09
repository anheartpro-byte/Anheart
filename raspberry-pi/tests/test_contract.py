"""The versioned machine contract, on the console's side (ANH-133).

Two things are established here:

* the constants of :mod:`src.contract` are the values of the shared file
  ``contracts/machine-api.json`` at the repository root (EX-1). The console is
  deployed without that file, so it cannot read it at run time; this test is
  what keeps the two from drifting;
* the decision that guards a remote launch, :func:`~src.contract.server_refusal`,
  refuses everything except a well-formed version of this console's major
  (EX-4), for any value a dashboard could send.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Final, cast

import pytest
from hypothesis import given
from hypothesis import strategies as st

from src import contract
from src.contract import (
    CONTRACT_HEADER,
    CONTRACT_UNSUPPORTED,
    CONTRACT_VERSION,
    MAJORS_SHOWN,
    MAX_REFUSAL_SENTENCE,
    SERVER_VERSION_FIELD,
    UNKNOWN_SOFTWARE_VERSION,
    VERSION_PATH,
    ContractVersion,
    error_code_of,
    incompatible_server,
    major_of,
    majors_of,
    read_software_version,
    server_refusal,
    unsupported_refusal,
    version_of,
)

REPOSITORY: Final[Path] = Path(__file__).resolve().parents[2]
SHARED: Final[Path] = REPOSITORY / "contracts" / "machine-api.json"


def shared() -> dict[str, object]:
    """The shared contract file, as the dashboard's code reads it."""
    loaded: object = json.loads(SHARED.read_text(encoding="utf-8"))  # pyright: ignore[reportAny]  # narrowed below
    assert isinstance(loaded, dict)
    return cast("dict[str, object]", loaded)


# =========================================================================
# EX-1: one contract, defined in contracts/machine-api.json
# =========================================================================


def test_ex1_the_contract_version_is_the_shared_file_s() -> None:
    assert shared()["contract_version"] == CONTRACT_VERSION
    assert version_of(CONTRACT_VERSION) == CONTRACT_VERSION


def test_ex1_the_header_and_the_poll_field_are_the_shared_file_s() -> None:
    assert shared()["header"] == CONTRACT_HEADER
    assert shared()["server_version_field"] == SERVER_VERSION_FIELD


def test_ex1_the_code_of_a_refused_contract_is_one_of_the_shared_codes() -> None:
    codes = shared()["error_codes"]
    assert isinstance(codes, dict)
    assert CONTRACT_UNSUPPORTED in codes
    for code in cast("dict[str, object]", codes):
        assert error_code_of(code) == code  # every shared code is one this console can log


# =========================================================================
# Reading versions and codes out of untrusted values
# =========================================================================


@pytest.mark.parametrize(
    ("value", "major"),
    [("1.0", "1"), ("1.12", "1"), ("12.3", "12"), ("0.9", "0"), ("9999.9999", "9999")],
)
def test_a_well_formed_version_is_read_with_its_major(value: str, major: str) -> None:
    version = version_of(value)
    assert version == value
    assert version is not None
    assert major_of(version) == major


@pytest.mark.parametrize(
    "value",
    [
        None,
        1,
        1.0,
        True,
        b"1.0",
        ["1.0"],
        {"version": "1.0"},
        "",
        "1",
        "1.",
        ".0",
        "1.0.0",
        "v1.0",
        "01.0",
        "1.00",
        "1,0",
        "-1.0",
        " 1.0",
        "1.0\n",
        "12345.0",
        "1.0<script>",
        "1.\u0663",  # a digit, but not an ASCII one
        "\u0661.0",
    ],
)
def test_anything_else_is_not_a_version(value: object) -> None:
    assert version_of(value) is None


@pytest.mark.parametrize("value", ["session_not_found", "contract_unsupported", "a", "a1_b2"])
def test_a_stable_code_is_read(value: str) -> None:
    assert error_code_of(value) == value


@pytest.mark.parametrize(
    "value",
    [None, 3, "", "Session not found", "Invalid API key", "9lives", "_x", "a-b", "a" * 65, "a\nb"],
)
def test_a_sentence_is_not_a_stable_code(value: object) -> None:
    assert error_code_of(value) is None


@pytest.mark.parametrize(
    ("value", "majors"),
    [
        (["1"], ("1",)),
        (["1", "2"], ("1", "2")),
        (["1", 2, None, "x", "02", "3"], ("1", "3")),
        # Each major once, in the order first listed.
        (["2", "1", "2", "2", "1", "3"], ("2", "1", "3")),
        ([], ()),
        (None, ()),
        ("1", ()),
        ({"1": True}, ()),
    ],
)
def test_the_majors_a_server_serves_are_read_and_the_rest_left_out(
    value: object, majors: tuple[str, ...]
) -> None:
    assert majors_of(value) == majors


# =========================================================================
# EX-4: what may be armed, and the sentence for what may not
# =========================================================================


def test_the_sentence_names_both_versions() -> None:
    assert incompatible_server("2.0") == f"serveur incompatible (contrat {CONTRACT_VERSION} vs 2.0)"


@pytest.mark.parametrize("announced", ["1.0", "1.1", "1.9999"])
def test_ex4_a_server_of_this_major_is_not_refused(announced: str) -> None:
    assert server_refusal(announced) is None


@pytest.mark.parametrize("announced", ["2.0", "0.9", "10.0", "11.0"])
def test_ex4_a_server_of_another_major_is_refused_by_name(announced: str) -> None:
    assert (
        server_refusal(announced)
        == f"serveur incompatible (contrat {CONTRACT_VERSION} vs {announced})"
    )


@pytest.mark.parametrize("announced", [None, "", "1", 1, 1.0, True, "v1.0", ["1.0"], "1.0.0"])
def test_ex4_a_server_that_names_no_readable_version_is_refused(announced: object) -> None:
    assert (
        server_refusal(announced) == f"serveur incompatible (contrat {CONTRACT_VERSION} vs inconnu)"
    )


@given(
    st.one_of(
        st.none(),
        st.booleans(),
        st.integers(),
        st.floats(allow_nan=True),
        st.text(),
        st.from_regex(r"[0-9]{1,5}\.[0-9]{1,5}", fullmatch=True),
        st.lists(st.text(), max_size=3),
    )
)
def test_ex4_only_a_well_formed_version_of_this_major_is_ever_accepted(announced: object) -> None:
    """Over anything a dashboard could send: accepted if and only if it is ``1.<minor>``."""
    accepted = server_refusal(announced) is None
    well_formed = isinstance(announced, str) and (
        re.fullmatch(r"1\.(0|[1-9][0-9]{0,3})", announced) is not None
    )
    assert accepted is well_formed


def test_ex4_the_decision_follows_this_console_s_own_major(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The acceptance case seen from a console of contract 2.0 facing a 1.x dashboard."""
    monkeypatch.setattr(contract, "CONTRACT_VERSION", ContractVersion("2.0"))
    assert server_refusal("1.4") == "serveur incompatible (contrat 2.0 vs 1.4)"
    assert server_refusal("2.1") is None
    assert unsupported_refusal(("1",)) == "serveur incompatible (contrat 2.0 vs 1)"


@pytest.mark.parametrize(
    ("supported", "theirs"),
    [(("2",), "2"), (("2", "3"), "2, 3"), ((), "inconnu")],
)
def test_ex3_a_refused_contract_is_worded_with_what_the_server_serves(
    supported: tuple[str, ...], theirs: str
) -> None:
    assert (
        unsupported_refusal(supported)
        == f"serveur incompatible (contrat {CONTRACT_VERSION} vs {theirs})"
    )


def test_what_is_kept_of_a_list_of_majors_is_bounded_whatever_its_length() -> None:
    """A major is at most four digits: ten thousand of them exist, however long the list."""
    listed = [str(major) for major in range(10_000)] * 30 + ["10000", "99999", "x"] * 1000
    kept = majors_of(listed)
    assert len(listed) == 303_000
    assert kept == tuple(str(major) for major in range(10_000))


@pytest.mark.parametrize(
    ("count", "theirs"),
    [
        (1, "2"),
        (4, "2, 3, 4, 5"),
        (5, "2, 3, 4, 5 et 1 autre"),
        (6, "2, 3, 4, 5 et 2 autres"),
        (600, "2, 3, 4, 5 et 596 autres"),
        (9998, "2, 3, 4, 5 et 9994 autres"),
    ],
)
def test_a_426_that_lists_many_majors_names_the_first_few_and_counts_the_others(
    count: int, theirs: str
) -> None:
    """The sentence goes to the operator, the record and the log: it stays a sentence."""
    assert MAJORS_SHOWN == 4
    supported = tuple(str(major) for major in range(2, 2 + count))
    sentence = unsupported_refusal(supported)
    assert sentence == f"serveur incompatible (contrat {CONTRACT_VERSION} vs {theirs})"
    assert len(sentence) <= MAX_REFUSAL_SENTENCE


@given(
    st.one_of(
        st.lists(st.one_of(st.integers(0, 12_000).map(str), st.text(max_size=6), st.none())),
        # Long lists of majors that are all well formed: what the bound is for.
        st.lists(st.integers(0, 9999).map(str), min_size=20, max_size=300),
    )
)
def test_no_list_a_server_sends_makes_the_sentence_longer_than_its_bound(
    listed: list[str | None],
) -> None:
    sentence = unsupported_refusal(majors_of(listed))
    assert len(sentence) <= MAX_REFUSAL_SENTENCE
    # The part every reader of the sentence relies on is as it always was.
    assert sentence.startswith(f"serveur incompatible (contrat {CONTRACT_VERSION} vs ")
    assert sentence.endswith(")")
    assert sentence.isprintable()


@given(st.one_of(st.none(), st.integers(), st.text(max_size=40)))
def test_no_version_a_server_announces_makes_the_sentence_longer_than_its_bound(
    announced: object,
) -> None:
    sentence = server_refusal(announced)
    assert sentence is None or len(sentence) <= MAX_REFUSAL_SENTENCE


def test_the_bound_holds_for_the_longest_contract_version_this_console_could_speak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(contract, "CONTRACT_VERSION", ContractVersion("9999.9999"))
    worst = unsupported_refusal(tuple(str(major) for major in range(9999, -1, -1)))
    assert (
        worst == "serveur incompatible (contrat 9999.9999 vs 9999, 9998, 9997, 9996 et 9996 autres)"
    )
    assert len(worst) <= MAX_REFUSAL_SENTENCE


# =========================================================================
# EX-2: the software version, from the VERSION file
# =========================================================================


def test_ex2_the_shipped_version_file_is_readable_and_well_formed() -> None:
    assert VERSION_PATH == REPOSITORY / "raspberry-pi" / "VERSION"
    version = read_software_version()
    assert version != UNKNOWN_SOFTWARE_VERSION
    assert version == VERSION_PATH.read_text(encoding="utf-8").strip()
    assert version.startswith("pi-")


@pytest.mark.parametrize("text", ["pi-0.4.2", "pi-0.4.2\n", "  pi-1.0.0-rc.1+build.7 \n"])
def test_ex2_a_version_file_is_read_without_its_whitespace(tmp_path: Path, text: str) -> None:
    path = tmp_path / "VERSION"
    path.write_text(text, encoding="utf-8")
    assert read_software_version(path) == text.strip()


@pytest.mark.parametrize(
    "text", ["", "\n", "pi 0.4.2", "pi-0.4.2\npi-0.4.3", "<b>pi</b>", "-pi", "p" * 65]
)
def test_ex2_a_malformed_version_file_is_reported_as_unknown(
    tmp_path: Path, text: str, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "VERSION"
    path.write_text(text, encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="src.contract"):
        assert read_software_version(path) == UNKNOWN_SOFTWARE_VERSION
    assert "malformed" in caplog.text


def test_ex2_a_missing_version_file_is_reported_as_unknown(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="src.contract"):
        assert read_software_version(tmp_path / "absent") == UNKNOWN_SOFTWARE_VERSION
    assert "unreadable" in caplog.text


def test_ex2_a_version_file_that_is_not_text_is_reported_as_unknown(tmp_path: Path) -> None:
    path = tmp_path / "VERSION"
    path.write_bytes(b"\xff\xfe\x00pi")
    assert read_software_version(path) == UNKNOWN_SOFTWARE_VERSION
    assert read_software_version(tmp_path) == UNKNOWN_SOFTWARE_VERSION  # a directory
