# Phase 6: Raspberry Pi Python Project

## Overview

This phase implements the Python application for Raspberry Pi that connects to BITalino sensors and streams data to the Convex backend.

## Prerequisites

- Phase 2 (Machine API) completed
- Phase 3 (Session API, Data streaming) completed
- Raspberry Pi 4/5 with Python 3.9+
- BITalino Core BT device

## Tasks

| Task | Name             | Description                          | Dependencies  |
| ---- | ---------------- | ------------------------------------ | ------------- |
| 6.1  | Project Setup    | Create Python project structure      | None          |
| 6.2  | BITalino Client  | BLE connection and data reading      | 6.1           |
| 6.3  | Convex Client    | HTTP API communication               | 6.1           |
| 6.4  | Offline Buffer   | SQLite-based data buffering          | 6.1           |
| 6.5  | Session Manager  | Main state machine and orchestration | 6.2, 6.3, 6.4 |
| 6.6  | Main Application | Entry point and systemd service      | All above     |

## Directory Structure

```
raspberry-pi/
├── src/
│   ├── __init__.py
│   ├── main.py              # Entry point
│   ├── config.py            # Configuration
│   ├── bitalino_client.py   # BITalino BLE
│   ├── convex_client.py     # HTTP client
│   ├── data_buffer.py       # Offline storage
│   └── session_manager.py   # State machine
├── tests/
│   ├── __init__.py
│   ├── test_bitalino.py
│   ├── test_convex_client.py
│   └── test_buffer.py
├── scripts/
│   ├── install.sh
│   └── anheart.service
├── requirements.txt
├── .env.example
└── README.md
```
