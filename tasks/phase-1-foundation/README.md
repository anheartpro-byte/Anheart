# Phase 1: Foundation & Database Schema

## Overview

This phase establishes the core database schema and authentication system. All other phases depend on this foundation.

## Prerequisites

- Convex project configured (`npx convex dev` works)
- Clerk project configured with JWT template for Convex
- Environment variables set in `.env.local`

## Tasks

| Task | Name            | Description                                   | Dependencies |
| ---- | --------------- | --------------------------------------------- | ------------ |
| 1.1  | Database Schema | Define all tables with validators and indexes | None         |
| 1.2  | Auth Helpers    | Create authentication utility functions       | 1.1          |
| 1.3  | User Management | User CRUD operations with role-based access   | 1.1, 1.2     |

## Completion Checklist

- [ ] All 6 tables created in Convex dashboard
- [ ] Schema compiles without errors
- [ ] Auth helpers properly validate roles
- [ ] User sync with Clerk works
- [ ] Role-based access control enforced
- [ ] Gestionnaire can create patients
- [ ] Admin can manage all users

## Files Modified/Created

```
convex/
├── schema.ts           # Database schema
├── lib/
│   └── auth.ts         # Authentication helpers
└── users.ts            # User management functions
```
