# Phase 3: Session Management

## Overview

This phase implements the complete ECG recording session lifecycle, including real-time data streaming, session summaries, and access control.

## Prerequisites

- Phase 1 completed (schema, auth, users)
- Phase 2 completed (machines, heartbeat)

## Tasks

| Task | Name               | Description                          | Dependencies |
| ---- | ------------------ | ------------------------------------ | ------------ |
| 3.1  | Session CRUD       | Create, manage, and end sessions     | Phase 1, 2   |
| 3.2  | ECG Data Streaming | HTTP endpoint for data ingestion     | 3.1          |
| 3.3  | Session Queries    | Query ECG data with role-based delay | 3.2          |
| 3.4  | Session Summaries  | Post-session analytics generation    | 3.1, 3.2     |

## Completion Checklist

- [ ] Sessions can be created for online machines
- [ ] Machine status updates to "in_session" when active
- [ ] RPi can poll for pending sessions
- [ ] RPi can stream ECG data batches
- [ ] Data is stored in ecg_data table
- [ ] Gestionnaire sees 5-second delayed data
- [ ] Patient sees real-time data
- [ ] Session end triggers summary generation
- [ ] Summary includes heart rate metrics
- [ ] Summary includes downsampled ECG

## Files Modified/Created

```
convex/
├── sessions.ts         # Session CRUD operations
├── ecgData.ts          # ECG data ingestion and queries
├── sessionSummaries.ts # Summary generation
└── http.ts             # Add data streaming endpoints
```
