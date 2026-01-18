"""Configuration management for AnHeart RPi client."""

import os
from pydantic import BaseModel, Field
from dotenv import load_dotenv

load_dotenv()


class Config(BaseModel):
    """Application configuration."""

    convex_url: str = Field(..., description="Convex deployment URL")
    machine_api_key: str = Field(..., description="Machine API key (64 chars)")
    bitalino_mac: str = Field(..., description="BITalino MAC address")

    sample_rate: int = Field(default=1000, description="Sample rate in Hz")
    batch_interval_ms: int = Field(default=1000, description="Batch interval in ms")
    heartbeat_interval_s: int = Field(default=30, description="Heartbeat interval in seconds")
    log_level: str = Field(default="INFO", description="Logging level")
    buffer_db_path: str = Field(default="buffer.db", description="Offline buffer database path")

    @classmethod
    def from_env(cls) -> "Config":
        """Load configuration from environment variables."""
        convex_url = os.getenv("CONVEX_URL")
        api_key = os.getenv("MACHINE_API_KEY")
        bitalino_mac = os.getenv("BITALINO_MAC")

        if not convex_url:
            raise ValueError("CONVEX_URL environment variable is required")
        if not api_key:
            raise ValueError("MACHINE_API_KEY environment variable is required")
        if not bitalino_mac:
            raise ValueError("BITALINO_MAC environment variable is required")

        return cls(
            convex_url=convex_url,
            machine_api_key=api_key,
            bitalino_mac=bitalino_mac,
            sample_rate=int(os.getenv("SAMPLE_RATE", "1000")),
            batch_interval_ms=int(os.getenv("BATCH_INTERVAL_MS", "1000")),
            heartbeat_interval_s=int(os.getenv("HEARTBEAT_INTERVAL_S", "30")),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
            buffer_db_path=os.getenv("BUFFER_DB_PATH", "buffer.db"),
        )


config: Config | None = None


def get_config() -> Config:
    """Get or create configuration instance."""
    global config
    if config is None:
        config = Config.from_env()
    return config
