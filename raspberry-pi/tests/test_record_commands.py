"""The command vocabulary a record names its inputs in: written and read back exactly."""

from __future__ import annotations

from typing import cast

import pytest

from src.record import commands
from src.record.schema import EventKind
from src.result import Err, Ok
from src.units import MotorRpm, OutputRpm

EVERY_COMMAND: tuple[tuple[commands.Command, str], ...] = (
    (commands.ConfirmEstopWiring(), "confirm_estop_wiring"),
    (commands.StartProgramme(), "start_programme"),
    (commands.StartManual(MotorRpm(1380)), "start_manual ceiling_motor_rpm=1380"),
    (commands.SetManualTarget(OutputRpm(27.0)), "manual_target output_rpm=27.0"),
    (
        commands.SetManualTarget(OutputRpm(0.1 + 0.2)),
        "manual_target output_rpm=0.30000000000000004",
    ),
    (commands.Stop(), "stop"),
    (commands.EmergencyStop(), "estop"),
    (commands.Acknowledge(estop_released=True), "acknowledge estop_released=true"),
    (commands.Acknowledge(estop_released=False), "acknowledge estop_released=false"),
    (commands.FaultReset(), "fault_reset"),
    (commands.Shutdown(), "shutdown"),
    (commands.Attendant(present=True), "attendant present=true"),
    (commands.Attendant(present=False), "attendant present=false"),
)


@pytest.mark.parametrize(("command", "detail"), EVERY_COMMAND)
def test_ex1_every_command_is_written_as_pinned_and_read_back_identical(
    command: commands.Command, detail: str
) -> None:
    assert commands.encode(command) == detail
    parsed = commands.parse(detail)
    assert isinstance(parsed, Ok)
    assert parsed.value == command


@pytest.mark.parametrize(
    "detail",
    [
        "",
        "manual session started",
        "target 27.0 output rpm: accepted -> 1344 motor rpm",
        "STOP",
        "stop now",
        "confirm_estop_wiring twice",
        "start_manual",
        "start_manual ceiling_motor_rpm=1380 extra=1",
        "start_manual ceiling=1380",
        "start_manual ceiling_motor_rpm",
        "start_manual ceiling_motor_rpm=fast",
        "start_manual ceiling_motor_rpm=1380.5",
        "manual_target output_rpm=nan",
        "manual_target output_rpm=inf",
        "manual_target output_rpm=",
        "manual_target rpm=27",
        "acknowledge",
        "acknowledge estop_released=yes",
        "acknowledge released=true",
        "attendant present=1",
        "attendant here=true",
        "start_programme profile=1",
        "estop hard",
        "fault_reset now",
        "shutdown -h",
    ],
)
def test_ex1_anything_else_is_refused_with_a_reason(detail: str) -> None:
    parsed = commands.parse(detail)
    assert isinstance(parsed, Err)
    assert parsed.error


@pytest.mark.parametrize(
    "detail",
    [
        "PassengerSentinel went on board",
        "start_manual ceiling_motor_rpm=PassengerSentinel",
        "manual_target output_rpm=PassengerSentinel",
        "acknowledge estop_released=PassengerSentinel",
        "attendant PassengerSentinel=true",
    ],
)
def test_ex1_a_refusal_never_quotes_what_the_record_said(detail: str) -> None:
    parsed = commands.parse(detail)
    assert isinstance(parsed, Err)
    assert "PassengerSentinel" not in parsed.error


def test_ex1_a_command_is_recorded_under_the_kind_that_says_where_it_came_from() -> None:
    assert commands.kind_of(commands.Stop()) is EventKind.OPERATOR_ACTION
    assert commands.kind_of(commands.Stop(), remote=True) is EventKind.REMOTE_COMMAND
    assert commands.kind_of(commands.Acknowledge(estop_released=True)) is EventKind.VERDICT_ACK
    assert commands.kind_of(commands.Acknowledge(estop_released=True), remote=True) is (
        EventKind.VERDICT_ACK
    )
    assert {
        commands.kind_of(command, remote=remote)
        for command, _ in EVERY_COMMAND
        for remote in (False, True)
    } == commands.INPUT_KINDS


def test_a_value_that_is_not_a_command_fails_loudly() -> None:
    """The exhaustive match's last arm, reached only by an ill-typed caller."""
    with pytest.raises(AssertionError):
        commands.encode(cast("commands.Command", "not-a-command"))
