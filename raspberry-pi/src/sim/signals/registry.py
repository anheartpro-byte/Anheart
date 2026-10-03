"""Which generator renders which simulated channel."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import Final

from src.sensors.base import SensorKind
from src.sim.signals import eda, emg, lux, resp, spo2
from src.sim.signals.base import SignalGenerator

GENERATORS: Final[Mapping[SensorKind, Callable[[int], SignalGenerator]]] = MappingProxyType(
    {
        SensorKind.EDA: eda.make,
        SensorKind.SPO2: spo2.make,
        SensorKind.RESP: resp.make,
        SensorKind.EMG: emg.make,
        SensorKind.LUX: lux.make,
    }
)
"""Every channel except the ECG, which ``src/sim/ecg.py`` renders from the heart itself."""


def generator_for(kind: SensorKind, seed: int = 0) -> SignalGenerator | None:
    """The generator for ``kind``, or ``None`` for the ECG."""
    make = GENERATORS.get(kind)
    return None if make is None else make(seed)
