"""Git deployment entrypoint for the hosted, simulation-only FastAPI wrapper.

Load the Pi sources and the hosted BITalino refusal stub from the checkout,
just as build.sh places them beside app.py in the standalone package.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "raspberry-pi"))
sys.path.insert(0, str(Path(__file__).resolve().parent / "deploy/simulation-vercel"))

from app import app

__all__ = ["app"]
