"""Configuration, parsed once from the environment.

Deliberately a frozen dataclass rather than a pydantic ``BaseModel``. Two
reasons:

1. ``BaseModel`` is incompatible with the strict gate. pydantic's own API
   surface declares ``Any`` (``__init__(**data: Any)`` and friends), so
   inheriting from it trips mypy's ``disallow_any_explicit`` on the class
   statement itself. Silencing that would blind the checker to every real
   ``Any`` leak in the module, which is the opposite of the intent.
2. Config loading *is* the boundary this project handles by parsing: read the
   untyped environment once, validate it, and hand back a value whose types are
   trusted from then on. A dataclass expresses that directly, and freezing it
   means a safety limit cannot be mutated at runtime after the operator
   approved it.

Nothing downstream used pydantic's features (only attribute reads and keyword
construction), so this is a drop-in.

``from_env`` raises ``ValueError`` rather than returning a ``Result``. That is
consistent with the contract: ``Result`` is required in the motor and safety
paths, where a missed error would leave a machine commanded. Startup is not
that path -- no motor is running yet, and ``main.py`` already turns the
exception into an exit code and a readable message.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Final

from dotenv import load_dotenv

load_dotenv()

#: Sample rates the BITalino firmware accepts. Anything else is rejected by the
#: device itself, so catching it here gives a far better message.
VALID_SAMPLE_RATES: Final[frozenset[int]] = frozenset({1, 10, 100, 1000})


def _require(name: str) -> str:
    """Read a mandatory environment variable, or explain what is missing."""
    value = os.getenv(name)
    if not value:
        raise ValueError(f"{name} environment variable is required")
    return value


def _int_env(name: str, default: int) -> int:
    """Read an integer environment variable, naming the offender when it is not one."""
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


@dataclass(frozen=True, slots=True)
class Config:
    """Application configuration. Immutable once loaded."""

    convex_url: str
    machine_api_key: str
    bitalino_mac: str

    sample_rate: int = 1000
    """Acquisition rate in Hz, as requested from the BITalino."""

    output_sample_rate: int = 250
    """Treated/transmitted rate in Hz, after on-device decimation."""

    batch_interval_ms: int = 1000
    heartbeat_interval_s: int = 30
    log_level: str = "INFO"
    buffer_db_path: str = "buffer.db"

    def __post_init__(self) -> None:
        if self.sample_rate not in VALID_SAMPLE_RATES:
            raise ValueError(
                f"SAMPLE_RATE must be one of {sorted(VALID_SAMPLE_RATES)}, got {self.sample_rate}"
            )
        if self.output_sample_rate <= 0:
            raise ValueError(f"OUTPUT_SAMPLE_RATE must be positive, got {self.output_sample_rate}")
        if self.output_sample_rate > self.sample_rate:
            raise ValueError(
                f"OUTPUT_SAMPLE_RATE ({self.output_sample_rate}) cannot exceed "
                f"SAMPLE_RATE ({self.sample_rate}): decimation only ever reduces the rate"
            )
        if self.batch_interval_ms <= 0:
            raise ValueError(f"BATCH_INTERVAL_MS must be positive, got {self.batch_interval_ms}")
        if self.heartbeat_interval_s <= 0:
            raise ValueError(
                f"HEARTBEAT_INTERVAL_S must be positive, got {self.heartbeat_interval_s}"
            )

    @classmethod
    def from_env(cls) -> Config:
        """Load configuration from environment variables."""
        return cls(
            convex_url=_require("CONVEX_URL"),
            machine_api_key=_require("MACHINE_API_KEY"),
            bitalino_mac=_require("BITALINO_MAC"),
            sample_rate=_int_env("SAMPLE_RATE", 1000),
            output_sample_rate=_int_env("OUTPUT_SAMPLE_RATE", 250),
            batch_interval_ms=_int_env("BATCH_INTERVAL_MS", 1000),
            heartbeat_interval_s=_int_env("HEARTBEAT_INTERVAL_S", 30),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
            buffer_db_path=os.getenv("BUFFER_DB_PATH", "buffer.db"),
        )


config: Config | None = None


def get_config() -> Config:
    """Get or create the configuration instance."""
    global config  # noqa: PLW0603  # deliberate process-wide singleton
    if config is None:
        config = Config.from_env()
    return config


def reset_config() -> None:
    """Drop the cached configuration.

    Exists for tests: the module-level singleton otherwise lets the first test
    that builds a Config fix it for every later test in the run.
    """
    global config  # noqa: PLW0603  # deliberate process-wide singleton
    config = None
