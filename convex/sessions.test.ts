/// <reference types="vite/client" />
import { describe, expect, it } from "vitest";
import { api } from "./_generated/api";
import { now, trainingFixture } from "./trainingPrivacy.fixtures";

const modules = import.meta.glob([
  "./**/*.ts",
  "!./**/*.test.ts",
  "!./**/*.fixtures.ts",
]);

describe("ANH-71 completed session read", () => {
  it.each([
    { subject: "rider", ownsCompleted: true },
    { subject: "launcher", ownsCompleted: false },
  ] as const)(
    "returns only owned completed sessions for $subject",
    async ({ subject, ownsCompleted }) => {
      // Given a completed session and a newer active session for the same rider.
      const f = await trainingFixture(modules);
      await f.t.run(async (ctx) => {
        await ctx.db.patch(f.sessionId, { status: "completed", endedAt: now });
        await ctx.db.insert("sessions", {
          machineId: f.machineId,
          userId: f.rider,
          status: "active",
          startedAt: now + 1,
          channels: ["ECG"],
          kind: "auto",
        });
      });
      // When the authenticated user calls the registered empty-argument query.
      const sessions = await f.t
        .withIdentity({ subject })
        .query(api.sessions.getCompletedSessionsForUser, {});
      // Then the active session and another user's records are excluded.
      expect(sessions.map((session) => session._id)).toEqual(
        ownsCompleted ? [f.sessionId] : [],
      );
    },
  );

  it("requires authentication even when no completed session exists", async () => {
    // Given an unauthenticated client and an active-only session.
    const f = await trainingFixture(modules);
    // When / Then the real registered query rejects the read.
    await expect(
      f.t.query(api.sessions.getCompletedSessionsForUser, {}),
    ).rejects.toThrow();
  });
});
