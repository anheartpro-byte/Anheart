from collections import deque

import pytest
from pymodbus.exceptions import ConnectionException
from pymodbus.pdu import ModbusPDU

from src.clock import ManualClock
from src.motor.atv320 import (
    FtdiModbusClient,
    ModbusMaster,
    ObservedModbusClient,
    SerialSettings,
    serial_master,
)
from src.motor.ftdi_link import BufferedFtdiPort
from src.motor.observation import ExchangeKind, ExchangeLog
from src.units import Seconds
from tests.test_atv320 import read_reply
from tests.test_ftdi_link import FakeChip


@pytest.mark.parametrize("complete", [True, False])
def test_sdk_receive_body_retries_preserve_every_chunk(
    monkeypatch: pytest.MonkeyPatch, complete: bool
) -> None:
    clock = ManualClock()
    chip = FakeChip()
    master = FtdiModbusClient(
        SerialSettings(port="COM-NONE", retries=2, timeout=Seconds(0.001)),
        lambda: BufferedFtdiPort(chip, timeout=Seconds(0.001), clock=clock),
    )
    log = ExchangeLog(clock)
    master.observe_exchanges(log)
    typed_master: ModbusMaster = master
    response = read_reply([64])
    assert isinstance(response, ModbusPDU)
    response.slave_id = 1
    wire = master.framer.buildFrame(response)
    chunks = deque((wire[:4], b"", wire[4:] if complete else b"") * 2)

    def read_fragment(_port: BufferedFtdiPort, _size: int) -> bytes:
        clock.advance(Seconds(0.025))
        return chunks.popleft()

    monkeypatch.setattr(BufferedFtdiPort, "read", read_fragment)
    try:
        replies = tuple(master.read_holding_registers(3201, count=1, slave=1) for _ in range(2))
        events = log.entries
        assert [event.kind for event in events] == (
            [ExchangeKind.SEND] + [ExchangeKind.RECEIVE_CHUNK] * 3
        ) * 2
        sends = [event for event in events if event.kind is ExchangeKind.SEND]
        receives = [event for event in events if event.kind is ExchangeKind.RECEIVE_CHUNK]
        assert [event.raw_hex for event in sends] == [wire.hex() for wire in chip.written]
        assert [event.raw_hex for event in receives] == [
            wire[:4].hex(),
            "",
            wire[4:].hex() if complete else "",
        ] * 2
        assert [event.ok for event in receives] == [True, False, complete] * 2
        assert all(event.latency_ms == pytest.approx(25) for event in receives)
        assert all(isinstance(reply, ModbusPDU) is complete for reply in replies)
        assert len(chip.written) == 2
    finally:
        typed_master.close()


@pytest.mark.parametrize("observe", [True, False])
def test_sdk_send_recv_exception_behavior_is_unchanged(observe: bool) -> None:
    client = ObservedModbusClient(port="COM-NONE")
    log = ExchangeLog(ManualClock())
    if observe:
        client.observe_exchanges(log)
    with pytest.raises(ConnectionException, match="COM-NONE"):
        client.send(b"request")
    with pytest.raises(ConnectionException, match="COM-NONE"):
        client.recv(1)
    assert len(log.entries) == (2 if observe else 0)
    assert all(not event.ok and event.raw_hex is None for event in log.entries)


def test_sdk_partial_send_records_only_accepted_bytes() -> None:
    clock = ManualClock()
    chip = FakeChip()
    chip.accept = 2
    port = BufferedFtdiPort(chip, timeout=Seconds(0.001), clock=clock)
    master = FtdiModbusClient(SerialSettings(port="COM-NONE"), lambda: port)
    log = ExchangeLog(clock)
    master.observe_exchanges(log)
    typed_master: ModbusMaster = master
    assert master.connect()
    try:
        assert master.send(b"abcd") == 2
        assert log.entries[0].raw_hex == "6162"
        assert not log.entries[0].ok
    finally:
        typed_master.close()


@pytest.mark.parametrize("port", ["COM-NONE", "ftdi://ftdi:232:TEST/1"])
def test_serial_factory_can_opt_in_before_any_transport_is_opened(port: str) -> None:
    clock = ManualClock()
    log = ExchangeLog(clock)
    client = serial_master(SerialSettings(port=port), clock, exchange_log=log)
    assert isinstance(client, ObservedModbusClient)
    assert client.socket is None
    assert log.entries == ()
