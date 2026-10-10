"""What the local console reports about its links, beyond the telemetry snapshot.

:class:`~src.training.types.TelemetrySnapshot` is the session's picture: heart
rate, speeds, drive state, verdict. The console needs a few more facts that are
not the session's to own, and an operator at a bench needs them first when
something does not work:

* how the drive link is doing while idle (reads, failures, the last latency,
  the last error), from :class:`~src.training.runtime.IdleLink`;
* how the BITalino link is doing (connected, acquiring, the decoder's
  :class:`~src.bitalino_client.LinkStats`, what the DSP bridge has treated);
* whether motion is enabled at all in this build, and the geometry every g on
  the screen was computed with;
* which build this console is (``raspberry-pi/VERSION``), and how its link with
  the dashboard is doing (:mod:`src.link_state`).

These are plain frozen records. The composition root
(:mod:`src.local_panel`) builds one on request through :class:`PanelSource`;
the web layer only renders it. Kept out of both so neither imports the other.
"""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from enum import Enum, unique
from typing import Protocol

from src.bitalino_client import LinkStats
from src.contract import SoftwareVersion
from src.ecg_pipeline import EcgBridgeStats
from src.link_state import LinkStatus
from src.training.runtime import IdleLink, RiseHold
from src.units import BpmPerMinute, GearRatio, Metres, Monotonic, MotorRpm

# The two kinds of link the records below report. They are defined here, below
# the configuration that chooses them (``src.local_config`` imports the web
# layer, which imports this module), and ``src.local_config`` gives them back
# under its own name.


@unique
class MotorBackend(Enum):
    """Which drive the console commands."""

    SIM = "sim"
    """``SimulatedDrive``: a full dry run, nothing opened."""

    SERIAL = "serial"
    """The ATV320 over Modbus RTU, through ``src/motor/atv320.py``."""


@unique
class EcgSource(Enum):
    """Where the ECG comes from."""

    SIM = "sim"
    """``SimulatedBitalinoClient`` fed by the physiology plant."""

    SERIAL = "serial"
    """A serial device (``/dev/rfcomm0`` on the Pi, ``COM4`` on Windows)."""

    RFCOMM = "rfcomm"
    """macOS IOBluetooth RFCOMM, address ``rfcomm:XX-XX-XX-XX-XX-XX``."""


@dataclass(frozen=True, slots=True)
class EcgLinkStatus:
    """The BITalino side: the transport, the decoder counters, the DSP bridge."""

    source: EcgSource
    address: str | None
    """``None`` for the simulator."""

    connected: bool
    acquiring: bool

    connect_attempts: int
    """Connection attempts made by the console since it started."""

    last_error: str | None
    """Why the last connection or start attempt failed, in words, or ``None``."""

    link: LinkStats | None
    """The real client's decoder counters; ``None`` for the simulator, which has none."""

    bridge: EcgBridgeStats


@dataclass(frozen=True, slots=True, kw_only=True)
class PanelStatus:
    """Everything the console's link panel shows, at one instant."""

    at: Monotonic
    motion_enabled: bool
    """``False`` in the read-only milestone: every motion route answers 403."""

    programs_enabled: bool
    """Whether programmed sessions may start. ``False`` until milestone M5."""

    motor_backend: MotorBackend
    drive_link: str | None
    """The serial link, described in one line; ``None`` for the simulated drive."""

    drive: IdleLink
    ecg: EcgLinkStatus
    heart_rate_trend: BpmPerMinute | None

    manual_rise_hold: RiseHold | None
    """Why the heart rate holds a manual rise now; ``None`` when it does not.

    From :meth:`~src.training.runtime.TrainingRuntime.manual_rise_hold`, so
    the page can say it before a target is typed.
    """

    radius: Metres
    ratio: GearRatio
    motor_max_rpm: MotorRpm

    software_version: SoftwareVersion
    """What this build calls itself: ``raspberry-pi/VERSION``, read once at startup.

    The same value the heartbeat announces and every session record's
    manifest is stamped with.
    """

    dashboard: LinkStatus
    """The link with the dashboard, as the operator reads it now."""


class PanelSource(Protocol):
    """Anything that can report a :class:`PanelStatus` now. Synchronous, no I/O."""

    @abstractmethod
    def panel_status(self) -> PanelStatus:
        """The link panel, now."""
