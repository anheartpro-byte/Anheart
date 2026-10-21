import pytest

from simulation.modbus import InlineExecutor
from simulation.replay_modbus import TapeMaster, parse_native
from simulation.tests.test_modbus import frame
from src.clock import ManualClock
from src.motor.atv320 import ATV320Drive, SerialSettings
from src.motor.drive import CommTimeout, ControlWord, RegisterMap
from src.motor.observation import ExchangeKind
from src.result import Err, Ok
from src.units import Monotonic, Seconds


@pytest.mark.parametrize(("count", "slave"), [(2, 248), (1, 1)])
def test_a_read_requires_the_configured_slave_and_one_register(count: int, slave: int) -> None:
    parsed = parse_native((frame(),))
    assert isinstance(parsed, Ok)
    master = TapeMaster(parsed.value, ManualClock(), Monotonic(0), slave=248)
    master.read_holding_registers(3201, count=count, slave=slave)
    mismatch = master.mismatch
    assert mismatch is not None
    assert mismatch.t == Seconds(0)
    assert mismatch.requested == f"modbus_read slave {slave} count {count}"
    assert mismatch.recorded == "modbus_read slave 248 count 1"
    assert master.pending is not None
    master.read_holding_registers(3201, count=count, slave=slave)
    assert master.mismatch == mismatch


def test_a_write_requires_the_configured_slave() -> None:
    parsed = parse_native((frame(ExchangeKind.WRITE, register=8501, value=6),))
    assert isinstance(parsed, Ok)
    master = TapeMaster(parsed.value, ManualClock(), Monotonic(0), slave=248)
    master.write_register(8501, 6, slave=1)
    assert master.mismatch is not None
    assert master.mismatch.requested == "modbus_write slave 1 count 1"
    assert master.pending is not None


@pytest.mark.parametrize("writing", [False, True])
async def test_legacy_native_failure_without_error_uses_no_response(writing: bool) -> None:
    recorded = frame(
        ExchangeKind.WRITE if writing else ExchangeKind.READ,
        register=8501 if writing else 3201,
        value=6 if writing else None,
        ok=False,
        latency_ms=25,
    )
    parsed = parse_native((recorded,))
    assert isinstance(parsed, Ok)
    clock = ManualClock()
    master = TapeMaster(parsed.value, clock, Monotonic(0), slave=248)
    driver = ATV320Drive(
        clock,
        master,
        SerialSettings(port="replay", slave_address=248),
        RegisterMap(),
        executor_factory=InlineExecutor,
        wait=master.wait,
    )
    result = (
        await driver.write_command(ControlWord.SHUTDOWN)
        if writing
        else await driver.read_register(RegisterMap().eta)
    )
    assert isinstance(result, Err)
    assert isinstance(result.error, CommTimeout)
    assert result.error.after == Seconds(0.025)
    assert clock.monotonic() == Monotonic(0.025)
    assert master.mismatch is None
    assert master.pending is None
    await driver.close()
