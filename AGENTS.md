# AGENTS.md - AnHeart ECG Monitoring System

## Project Overview

Real-time biomedical monitoring application using BITalino sensors connected to Raspberry Pi devices.
Streams ECG/biosensor data to a Next.js web app via Convex with role-based access control via Clerk.

**Tech Stack:** Next.js 16 + React 19 + Convex + Clerk + Tailwind CSS 4 + TypeScript

**Additional Rules:** See `.cursor/rules/convex_rules.mdc` for detailed Convex patterns and examples.

**Development Tasks:** See `tasks/README.md` for the complete development plan with acceptance criteria.

---

## Build/Lint/Test Commands

```bash
npm run dev              # Development (frontend + backend concurrently)
npm run dev:frontend     # Next.js only
npm run dev:backend      # Convex only
npm run build            # Build Next.js for production
npm run start            # Start production server
npm run lint             # ESLint check

# Convex-specific
npx convex dev           # Start Convex dev server
npx convex deploy        # Deploy to production
```

---

## Code Style Guidelines

### TypeScript/JavaScript

- **Strict mode enabled** - No implicit any, strict null checks
- Use `"use client"` directive for client components
- Use `"use node"` at top of Convex actions using Node.js modules
- Prefer `const` over `let`, never use `var`
- Use async/await over raw Promises

### Imports Order

```typescript
// 1. React/Next.js  2. Third-party  3. Convex generated  4. Local
import { useState } from "react";
import { useQuery } from "convex/react";
import { api } from "@/convex/_generated/api";
import ConvexClientProvider from "@/components/ConvexClientProvider";
```

### Naming Conventions

| Type             | Convention           | Example              |
| ---------------- | -------------------- | -------------------- |
| Components       | PascalCase           | `ECGWaveform.tsx`    |
| Functions        | camelCase            | `getUserSessions`    |
| Convex Tables    | snake_case           | `ecg_sessions`       |
| Convex Indexes   | by_field_name        | `by_user_and_status` |
| Types/Interfaces | PascalCase           | `SessionData`        |
| Constants        | SCREAMING_SNAKE_CASE | `MAX_SAMPLE_RATE`    |

---

## Convex Guidelines (CRITICAL)

### Key Rules

1. **Always include `args` and `returns` validators** for ALL functions
2. **Return `v.null()`** if function returns nothing (not `void`)
3. **Use `internalQuery/Mutation/Action`** for private functions
4. **Never use `filter()`** - use `withIndex()` instead
5. **Use `v.id("tableName")`** for document IDs, not `v.string()`
6. **Include index fields in name**: `by_user_and_status` for `["userId", "status"]`

### Function Syntax

```typescript
import { query, mutation } from "./_generated/server";
import { v } from "convex/values";

export const getSession = query({
  args: { sessionId: v.id("sessions") },
  returns: v.union(
    v.object({
      /* ... */
    }),
    v.null(),
  ),
  handler: async (ctx, args) => {
    return await ctx.db.get(args.sessionId);
  },
});
```

### HTTP Endpoints (for Raspberry Pi)

```typescript
// convex/http.ts
import { httpRouter } from "convex/server";
import { httpAction } from "./_generated/server";

const http = httpRouter();
http.route({
  path: "/api/machine/data",
  method: "POST",
  handler: httpAction(async (ctx, req) => {
    // Validate API key, process data
    return new Response(JSON.stringify({ success: true }), { status: 200 });
  }),
});
export default http;
```

---

## React/Next.js Patterns

```typescript
"use client";
import { useQuery, useMutation } from "convex/react";
import { Authenticated, Unauthenticated } from "convex/react";
import { api } from "@/convex/_generated/api";

export function SessionList() {
  const sessions = useQuery(api.sessions.list, { userId: "..." });
  if (sessions === undefined) return <Loading />;
  // ...
}
```

---

## User Roles (Clerk + Convex)

| Role         | Permissions                                             |
| ------------ | ------------------------------------------------------- |
| admin        | Full access to all users, machines, sessions            |
| gestionnaire | Manage multiple machines, create users, view their data |
| user         | View own session data only (read-only)                  |

---

## Project Structure

```
/
├── app/                    # Next.js App Router
├── components/             # React components (ECGWaveform, etc.)
├── convex/                 # Convex backend
│   ├── schema.ts          # Database schema
│   ├── http.ts            # HTTP endpoints for RPi
│   ├── sessions.ts        # Session CRUD
│   ├── machines.ts        # Machine management
│   └── users.ts           # User management
└── raspberry-pi/          # Python project for RPi
```

---

## Raspberry Pi Integration

1. RPi connects to BITalino via Bluetooth
2. Collects ECG samples (1000 Hz), batches data
3. Sends to Convex HTTP endpoint with API key: `Authorization: Bearer <api_key>`
4. Web app subscribes to real-time updates via Convex queries

---

## Environment Variables

```bash
# .env.local (Next.js)
NEXT_PUBLIC_CONVEX_URL=https://xxx.convex.cloud
NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY=pk_xxx
CLERK_SECRET_KEY=sk_xxx

# Convex Dashboard
CLERK_JWT_ISSUER_DOMAIN=https://xxx.clerk.accounts.dev

# Raspberry Pi
CONVEX_API_URL=https://xxx.convex.cloud
MACHINE_API_KEY=xxx
```

---

## Common Gotchas

1. **Convex functions auto-reload** - no need to restart dev server
2. **Use `v.optional()`** for nullable fields, not `| null`
3. **Indexes queried in order** - if `["a", "b"]`, query `a` first
4. **Actions cannot access `ctx.db`** - use `ctx.runQuery/runMutation`
5. **Client components need `"use client"`** directive at top
