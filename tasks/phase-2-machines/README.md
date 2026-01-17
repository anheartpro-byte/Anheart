# Phase 2: Machine Management

## Overview

This phase implements Raspberry Pi machine registration, API key management, and the heartbeat monitoring system.

## Prerequisites

- Phase 1 completed (schema, auth, users)
- Understanding of HTTP endpoints in Convex

## Tasks

| Task | Name               | Description                            | Dependencies |
| ---- | ------------------ | -------------------------------------- | ------------ |
| 2.1  | Machine CRUD       | Create, read, update, delete machines  | Phase 1      |
| 2.2  | API Key Management | Generate and validate machine API keys | 2.1          |
| 2.3  | Heartbeat System   | HTTP endpoint and offline detection    | 2.1, 2.2     |

## Completion Checklist

- [ ] Machines can be created with auto-generated API key
- [ ] API key shown only once on creation
- [ ] API key can be regenerated
- [ ] Machines list respects role-based access
- [ ] Heartbeat endpoint validates API key
- [ ] Heartbeat updates machine status to online
- [ ] Cron job marks stale machines as offline
- [ ] Machine config (sample rate, channels) can be updated

## Files Modified/Created

```
convex/
├── machines.ts         # Machine CRUD operations
├── http.ts             # HTTP endpoints for RPi
├── crons.ts            # Scheduled jobs
└── lib/
    └── crypto.ts       # API key hashing utilities
```
