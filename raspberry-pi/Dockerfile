# AnHeart Raspberry Pi - Docker image of the operator console.
# Works on Raspberry Pi 4/5 (ARM64) and on an ARM64 or x86-64 development host.
#
# One command: the local operator console (python -m src.local_panel), the
# process that drives the ATV320, reads the BITalino, applies the safety
# supervisor and serves the operator page.

# 3.12 (not 3.11): numpy's bundled type stubs require a 3.12+ target, and the
# strict type-check gate runs against this same version.
# The exact release AND its digest: the tag alone moves with every rebuild of
# the official image. docs/pi-image.md lists this version; to move it, change
# both here and there (tests/test_pi_install.py compares them).
FROM python:3.12.15-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258

# Install Bluetooth and build dependencies. The extra libs (BLAS/LAPACK, HDF5,
# freetype) let scipy / biosppy install cleanly on ARM if a source build is needed.
# libusb-1.0-0 is what pyftdi drives the Schneider USB-RS485 cable through when
# MOTOR_PORT is an ftdi:// URL (see src/motor/ftdi_link.py).
RUN apt-get update && apt-get install -y --no-install-recommends \
    bluetooth \
    libusb-1.0-0 \
    bluez \
    libbluetooth-dev \
    libglib2.0-dev \
    build-essential \
    gcc \
    gfortran \
    python3-dev \
    libopenblas-dev \
    liblapack-dev \
    libhdf5-dev \
    libfreetype6-dev \
    pkg-config \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements first for better caching
# The two lock files, not requirements-prod.txt: every package the image
# installs is named there at an exact version, with its hashes.
COPY requirements-build-lock.txt requirements-lock.txt ./

# Headless matplotlib backend (BioSPPy imports matplotlib).
ENV MPLBACKEND=Agg

# Install Python dependencies. pip is the one of the base image, not upgraded.
# --require-hashes refuses any package that is not in the lock files.
# PyBluez-bitalino (pulled in by bitalino) is the only package built from
# source: its build tools are locked too, installed first, and reused
# (--no-build-isolation) so that pip fetches nothing unpinned to build it.
RUN pip install --no-cache-dir --require-hashes -r requirements-build-lock.txt && \
    pip install --no-cache-dir --require-hashes --no-build-isolation -r requirements-lock.txt

# Fail the build early if the on-device DSP stack didn't install correctly.
RUN python -c "from biosppy.signals import ecg; from scipy import signal; print('DSP stack OK')"

# Copy application code, and the shipped presets and motion limits the console
# loads at startup (config/ sits next to src/, see src/training/plan.py).
COPY src/ ./src/
COPY config/ ./config/
# The build's version (src/contract.py): shown on the console's page, announced
# in the heartbeat and written in the manifest of every session record.
COPY VERSION ./VERSION
COPY docker/entrypoint.sh /usr/local/bin/anheart-entrypoint
RUN chmod +x /usr/local/bin/anheart-entrypoint

# Create data directory (profiles.local.json)
RUN mkdir -p /app/data

# Environment variables (override with docker-compose or -e flag)
ENV PYTHONUNBUFFERED=1

# /healthz answers without a token and says nothing about the session.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('UI_PORT', '8080') + '/healthz', timeout=3)"

ENTRYPOINT ["anheart-entrypoint"]
CMD ["python", "-m", "src.local_panel"]
