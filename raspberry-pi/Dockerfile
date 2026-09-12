# AnHeart Raspberry Pi Client - Docker Image
# Works on Raspberry Pi 3/4/5 (ARM64)

# 3.12 (not 3.11): numpy's bundled type stubs require a 3.12+ target, and the
# strict type-check gate runs against this same version.
FROM python:3.12-slim-bookworm

# Install Bluetooth and build dependencies. The extra libs (BLAS/LAPACK, HDF5,
# freetype) let scipy / biosppy install cleanly on ARM if a source build is needed.
RUN apt-get update && apt-get install -y --no-install-recommends \
    bluetooth \
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
COPY requirements-prod.txt .

# Headless matplotlib backend (BioSPPy imports matplotlib).
ENV MPLBACKEND=Agg

# Install Python dependencies
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements-prod.txt

# Fail the build early if the on-device DSP stack didn't install correctly.
RUN python -c "from biosppy.signals import ecg; from scipy import signal; print('DSP stack OK')"

# Copy application code
COPY src/ ./src/

# Create data directory
RUN mkdir -p /app/data

# Environment variables (override with docker-compose or -e flag)
ENV PYTHONUNBUFFERED=1
ENV LOG_LEVEL=INFO

# Run the application
CMD ["python", "-m", "src.main"]
