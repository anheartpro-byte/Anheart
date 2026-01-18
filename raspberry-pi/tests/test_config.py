"""Test configuration loading."""

import os
import pytest
from src.config import Config


def test_config_from_env():
    """Test loading config from environment."""
    os.environ["CONVEX_URL"] = "https://test.convex.cloud"
    os.environ["MACHINE_API_KEY"] = "a" * 64
    os.environ["BITALINO_MAC"] = "AA:BB:CC:DD:EE:FF"

    config = Config.from_env()

    assert config.convex_url == "https://test.convex.cloud"
    assert config.machine_api_key == "a" * 64
    assert config.bitalino_mac == "AA:BB:CC:DD:EE:FF"
    assert config.sample_rate == 1000
    assert config.batch_interval_ms == 1000


def test_config_missing_required():
    """Test error when required vars missing."""
    os.environ.pop("CONVEX_URL", None)

    with pytest.raises(ValueError, match="CONVEX_URL"):
        Config.from_env()
