from dataclasses import replace

import pytest

from src.clock import ManualClock
from src.result import Err, Ok
from src.roster import (
    FitnessExpired,
    NoConsent,
    NotFit,
    PassengerRefusal,
    Rider,
    cleared_to_board,
    describe_passenger_refusal,
    parse_roster,
)
from src.units import Seconds, UnixMillis
from tests.roster_support import RIDER, rider_row


def parsed(field: str, value: object, *, absent: bool = False) -> Rider:
    row: dict[str, object] = dict(rider_row(RIDER))
    if absent:
        row.pop(field)
    else:
        row[field] = value
    result = parse_roster({"riders": [row]})
    assert isinstance(result, Ok)
    return result.value.riders[0]


@pytest.mark.parametrize("field", ["fit", "consent"])
@pytest.mark.parametrize("value", [None, False, 0, 1, "true", [], {}])
def test_only_json_true_grants_fitness_or_consent(field: str, value: object) -> None:
    rider = parsed(field, value)
    assert not (rider.fit if field == "fit" else rider.consent)
    absent = parsed(field, value, absent=True)
    assert not (absent.fit if field == "fit" else absent.consent)


@pytest.mark.parametrize(
    "value", [None, True, -1, 4_133_980_800_001, 1.5, "1700000000000", [], float("inf")]
)
def test_unusable_fitness_expiry_is_unknown(value: object) -> None:
    assert parsed("fitnessValidUntil", value).fitness_valid_until is None
    assert parsed("fitnessValidUntil", value, absent=True).fitness_valid_until is None


@pytest.mark.parametrize("value", [0, 1_700_000_000_000.0, 4_133_980_800_000])
def test_whole_bounded_fitness_expiry_is_kept(value: int | float) -> None:
    assert parsed("fitnessValidUntil", value).fitness_valid_until == int(value)


@pytest.mark.parametrize("value", [None, 1, True, "", "x" * 33, "private note", "0.1.0\n", {}])
def test_unusable_questionnaire_version_is_unknown(value: object) -> None:
    assert parsed("fitnessQuestionnaireVersion", value).questionnaire_version is None
    assert parsed("fitnessQuestionnaireVersion", value, absent=True).questionnaire_version is None


@pytest.mark.parametrize("value", ["0.1.0", "A+build-1", "v" * 32])
def test_safe_bounded_questionnaire_version_is_kept(value: str) -> None:
    assert parsed("fitnessQuestionnaireVersion", value).questionnaire_version == value


def test_a_15_dashboard_roster_fails_closed() -> None:
    row: dict[str, object] = {
        "userId": RIDER.user_id,
        "name": RIDER.name,
        "hrMax": 190,
        "birthYear": 1940,
    }
    result = parse_roster({"riders": [row]})
    assert isinstance(result, Ok)
    rider = result.value.riders[0]
    assert (rider.fit, rider.consent, rider.fitness_valid_until, rider.questionnaire_version) == (
        False,
        False,
        None,
        None,
    )
    assert cleared_to_board(rider, UnixMillis(0)) == Err(NotFit())


@pytest.mark.parametrize(
    ("rider", "refusal"),
    [
        (replace(RIDER, fit=False), NotFit()),
        (replace(RIDER, questionnaire_version=None), NotFit()),
        (replace(RIDER, fitness_valid_until=None), NotFit()),
        (replace(RIDER, fitness_valid_until=UnixMillis(999)), FitnessExpired()),
        (replace(RIDER, consent=False), NoConsent()),
    ],
)
def test_boarding_refusals_are_closed_and_contain_no_personal_values(
    rider: Rider, refusal: PassengerRefusal
) -> None:
    assert cleared_to_board(rider, UnixMillis(1000)) == Err(refusal)
    words = describe_passenger_refusal(refusal)
    assert RIDER.name not in words
    assert str(RIDER.fitness_valid_until) not in words
    assert "0.1.0" not in words


def test_fitness_expires_exactly_at_the_injected_clock_deadline() -> None:
    clock = ManualClock(epoch_millis=UnixMillis(1000))
    rider = replace(RIDER, fitness_valid_until=UnixMillis(2000))
    assert cleared_to_board(rider, clock.unix_millis()) == Ok("0.1.0")
    clock.advance(Seconds(1.0))
    assert cleared_to_board(rider, clock.unix_millis()) == Err(FitnessExpired())
