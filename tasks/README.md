# AnHeart Development Tasks

## Project Summary

| Item                   | Value                                        |
| ---------------------- | -------------------------------------------- |
| **Hardware**           | BITalino Core BT + Raspberry Pi 4/5          |
| **Sample Rate**        | 1000 Hz                                      |
| **Data Batching**      | Every 1 second (~1000 samples)               |
| **Live View Delay**    | 5 seconds (for gestionnaire)                 |
| **Offline Handling**   | Buffer locally on RPi, sync when reconnected |
| **Languages**          | French + English (i18n)                      |
| **PDF Reports**        | Required                                     |
| **Machine Monitoring** | Heartbeat every 30 seconds                   |

---

## Phase Overview

| Phase | Name         | Description                                         | Dependencies  |
| ----- | ------------ | --------------------------------------------------- | ------------- |
| 1     | Foundation   | Database schema, authentication, role management    | None          |
| 2     | Machines     | Machine CRUD, API keys, heartbeat system            | Phase 1       |
| 3     | Sessions     | Session lifecycle, ECG streaming, summaries         | Phase 1, 2    |
| 4     | Frontend     | Dashboard, UI components, ECG visualization         | Phase 1, 2, 3 |
| 5     | Reports      | PDF generation, file storage                        | Phase 3       |
| 6     | Raspberry Pi | Python client, BITalino integration, offline buffer | Phase 2, 3    |
| 7     | Integration  | End-to-end testing, performance testing             | All phases    |

---

## Task Structure

Each phase folder contains:

- `README.md` - Phase overview and task list
- `task-X.X-name.md` - Individual task with acceptance criteria

---

## User Roles

| Role             | Permissions                                                |
| ---------------- | ---------------------------------------------------------- |
| **admin**        | Full access to all users, machines, sessions               |
| **gestionnaire** | Manage multiple machines, create patients, view their data |
| **technician**   | Operate machines independently, start/stop sessions        |
| **user**         | View own session data only (read-only patient)             |

---

## Tech Stack

### Web Application

- **Framework**: Next.js 16 + React 19
- **Backend**: Convex (real-time database + serverless functions)
- **Auth**: Clerk (authentication + user management)
- **Styling**: Tailwind CSS 4
- **Language**: TypeScript
- **i18n**: next-intl
- **Charts**: lightweight-charts (TradingView)
- **PDF**: @react-pdf/renderer

### Raspberry Pi

- **Language**: Python 3.9+
- **BLE Library**: bleak
- **HTTP Client**: httpx
- **Data Validation**: pydantic
- **Local Storage**: SQLite

---

## Getting Started

1. Read `AGENTS.md` for code style guidelines
2. Read `.cursor/rules/convex_rules.mdc` for Convex patterns
3. Start with Phase 1 tasks in order
4. Each task has clear acceptance criteria - all must pass before moving on
5. Run tests after each task completion
