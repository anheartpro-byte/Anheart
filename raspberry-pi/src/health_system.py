from __future__ import annotations

import math
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Final, NewType

from src.units import Percent, Seconds

Celsius = NewType("Celsius", float)
Mebibytes = NewType("Mebibytes", float)
CpuLoad = NewType("CpuLoad", float)
MEBIBYTE: Final[int] = 1024 * 1024
MEMORY_FIELD_PARTS: Final[int] = 2


@dataclass(frozen=True, slots=True)
class SystemHealth:
    cpu_temp_c: Celsius | None = None
    cpu_load_1m: CpuLoad | None = None
    mem_used_pct: Percent | None = None
    disk_free_mb: Mebibytes | None = None
    uptime_s: Seconds | None = None


def finite_file(path: Path) -> float | None:
    try:
        value = float(path.read_text(encoding="ascii").split()[0])
    except (OSError, ValueError, IndexError, UnicodeError):
        return None
    return value if math.isfinite(value) else None


def memory_used(path: Path) -> Percent | None:
    try:
        fields = {
            pieces[0].rstrip(":"): int(pieces[1])
            for line in path.read_text(encoding="ascii").splitlines()
            if len(pieces := line.split()) >= MEMORY_FIELD_PARTS
        }
        total = fields["MemTotal"]
        available = fields["MemAvailable"]
    except (OSError, ValueError, KeyError, UnicodeError):
        return None
    if total <= 0 or available < 0 or available > total:
        return None
    return Percent(100 * (total - available) / total)


def cpu_temperature(root: Path = Path("/")) -> Celsius | None:
    """The Pi's SoC temperature, from the kernel's thermal zone; ``None`` where there is none."""
    millidegrees = finite_file(root / "sys/class/thermal/thermal_zone0/temp")
    return None if millidegrees is None else Celsius(millidegrees / 1000)


def system_health(data: Path | None, root: Path = Path("/")) -> SystemHealth:
    uptime = finite_file(root / "proc/uptime")
    load: CpuLoad | None = None
    try:
        first, _, _ = os.getloadavg()
        if math.isfinite(first) and first >= 0:
            load = CpuLoad(first)
    except OSError:
        load = None
    free: Mebibytes | None = None
    if data is not None:
        try:
            free = Mebibytes(shutil.disk_usage(data).free / MEBIBYTE)
        except OSError:
            free = None
    return SystemHealth(
        cpu_temp_c=cpu_temperature(root),
        cpu_load_1m=load,
        mem_used_pct=memory_used(root / "proc/meminfo"),
        disk_free_mb=free,
        uptime_s=None if uptime is None or uptime < 0 else Seconds(uptime),
    )
