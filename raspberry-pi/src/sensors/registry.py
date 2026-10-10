"""Which processor handles which channel. The one place the six modules are named."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import Final

from src.sensors import ecg, eda, emg, lux, resp, spo2
from src.sensors.base import SensorKind, SensorProcessor, SensorSpec

FACTORIES: Final[Mapping[SensorKind, Callable[[], SensorProcessor]]] = MappingProxyType(
    {
        SensorKind.ECG: ecg.make,
        SensorKind.EDA: eda.make,
        SensorKind.SPO2: spo2.make,
        SensorKind.RESP: resp.make,
        SensorKind.EMG: emg.make,
        SensorKind.LUX: lux.make,
    }
)

SPECS: Final[Mapping[SensorKind, SensorSpec]] = MappingProxyType(
    {
        SensorKind.ECG: ecg.SPEC,
        SensorKind.EDA: eda.SPEC,
        SensorKind.SPO2: spo2.SPEC,
        SensorKind.RESP: resp.SPEC,
        SensorKind.EMG: emg.SPEC,
        SensorKind.LUX: lux.SPEC,
    }
)


def processor_for(kind: SensorKind) -> SensorProcessor:
    """A fresh processor for ``kind``."""
    return FACTORIES[kind]()
