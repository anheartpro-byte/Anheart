# Phase 4: Frontend Development

## Overview

This phase implements the Next.js web application including dashboard, ECG visualization, and all user interfaces.

## Prerequisites

- Phase 1-3 completed (backend fully functional)
- Next.js + Clerk + Convex already configured

## Tasks

| Task | Name                  | Description                            | Dependencies |
| ---- | --------------------- | -------------------------------------- | ------------ |
| 4.1  | i18n Setup            | Internationalization (French/English)  | None         |
| 4.2  | Dashboard Layout      | Sidebar, header, role-based navigation | 4.1          |
| 4.3  | Machine Management UI | List, create, edit, view machines      | 4.2          |
| 4.4  | Session Management UI | Start, view, end sessions              | 4.2, 4.3     |
| 4.5  | ECG Visualization     | Real-time and historical ECG charts    | 4.4          |
| 4.6  | User Management UI    | List, create patients, view profiles   | 4.2          |

## Libraries to Install

```bash
npm install next-intl lightweight-charts date-fns
npm install -D @types/node
```

## Files Created

```
app/
├── [locale]/
│   ├── layout.tsx
│   ├── page.tsx
│   └── dashboard/
│       ├── layout.tsx
│       ├── page.tsx
│       ├── machines/
│       ├── sessions/
│       └── users/
components/
├── ui/
├── dashboard/
├── ecg/
│   └── ECGWaveform.tsx
└── forms/
messages/
├── en.json
└── fr.json
```
