from pathlib import Path

from src.local_config import LocalConfig, load_local_config
from src.local_panel import (
    DSP_OUTPUT_RATE,
    ECG_SAMPLE_RATE,
    PROJECT_ROOT,
    RUNTIME_LIMITS,
    load_panel_motion_limits,
)
from src.record.codec import Privacy, document, encode
from src.record.context import ContextSource, Parameters, SessionContext, context_source
from src.record.writer import MANIFEST
from src.result import Ok
from src.training.safety import SafetyLimits
from src.units import Hertz
from tests.test_local_config import SIM_ENV


def configured(**changes: str) -> LocalConfig:
    parsed = load_local_config(
        {
            **SIM_ENV,
            "RECORD_MACHINE_ID": "machine-a",
            "RECORD_ORGANIZATION_ID": "organization-b",
            "ANHEART_SOFTWARE_VERSION": "synthetic-build-1",
            **changes,
        }
    )
    assert isinstance(parsed, Ok)
    return parsed.value


def medical_path(config: LocalConfig) -> Path:
    return (
        config.motion_limits_path
        if config.motion_limits_path.is_absolute()
        else PROJECT_ROOT / config.motion_limits_path
    )


def parameters_for(config: LocalConfig) -> Parameters:
    return Parameters(
        RUNTIME_LIMITS,
        SafetyLimits(
            hard_max_bpm=config.tiers.hard_max_bpm, critical_bpm=config.tiers.critical_bpm
        ),
        load_panel_motion_limits(config),
        Hertz(ECG_SAMPLE_RATE),
        Hertz(DSP_OUTPUT_RATE),
    )


def source_for(config: LocalConfig) -> ContextSource:
    return context_source(config, parameters_for(config), medical_path(config))


def encoded(context: SessionContext) -> str:
    assert isinstance(context.manifest, Ok)
    return encode(document(MANIFEST, context.manifest.value), Privacy())
