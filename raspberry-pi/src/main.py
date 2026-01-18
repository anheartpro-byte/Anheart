"""Main entry point for AnHeart Raspberry Pi client."""

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path

from . import __version__
from .config import get_config
from .session_manager import SessionManager

_manager: SessionManager | None = None


def setup_logging(level: str) -> None:
    """Configure logging."""
    log_level = getattr(logging, level.upper(), logging.INFO)

    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    logging.getLogger("bleak").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="AnHeart ECG Monitor - Raspberry Pi Client"
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"anheart-rpi {__version__}",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to .env configuration file",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )
    return parser.parse_args()


def handle_shutdown(signum, frame):
    """Handle shutdown signals."""
    global _manager
    logger = logging.getLogger(__name__)
    logger.info(f"Received signal {signum}, shutting down...")

    if _manager:
        asyncio.create_task(_manager.stop())


async def main_async(config) -> None:
    """Async main function."""
    global _manager
    logger = logging.getLogger(__name__)

    _manager = SessionManager(config)

    try:
        await _manager.start()
        await _manager.run()

    except Exception as e:
        logger.error(f"Fatal error: {e}")
        raise
    finally:
        await _manager.stop()


def main() -> int:
    """Main entry point."""
    args = parse_args()

    if args.config:
        from dotenv import load_dotenv
        load_dotenv(args.config)

    try:
        config = get_config()
    except ValueError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1

    log_level = "DEBUG" if args.debug else config.log_level
    setup_logging(log_level)

    logger = logging.getLogger(__name__)
    logger.info(f"Starting AnHeart RPi Client v{__version__}")
    logger.info(f"Convex URL: {config.convex_url}")
    logger.info(f"BITalino MAC: {config.bitalino_mac}")

    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)

    try:
        asyncio.run(main_async(config))
        return 0
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        return 0
    except Exception as e:
        logger.exception(f"Unexpected error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
