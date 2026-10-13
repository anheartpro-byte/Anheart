// @vitest-environment jsdom
import { screen, waitFor, within } from "@testing-library/react";
import type { FunctionReturnType } from "convex/server";
import { beforeEach, expect, it, vi } from "vitest";
import { api } from "@/convex/_generated/api";
import type { Id } from "@/convex/_generated/dataModel";
import {
  account,
  answer,
  lastArgsAsked,
  mutation,
  renderPage,
} from "@/test-support/pages";
import fr from "@/messages/fr.json";
import { SessionRecordCard } from "./SessionRecordCard";

const calls = vi.hoisted(() => ({
  query: vi.fn(),
  token: vi.fn(),
  download: vi.fn(),
}));
vi.mock("convex/react", async () => ({
  ...(await import("@/test-support/pages")).convexReact,
  useConvex: () => ({ query: calls.query }),
}));
vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({ getToken: calls.token }),
}));
vi.mock("@/lib/session-record", async (original) => ({
  ...(await original<typeof import("@/lib/session-record")>()),
  fetchRecordFile: calls.download,
}));
type Data = NonNullable<FunctionReturnType<typeof api.recordStorage.list>>;
type PresentData = Extract<Data, { deleted: false }>;
const sessionId = "session-1" as Id<"sessions">;
const file: Data["files"][number] = {
  id: "file-1" as Id<"session_record_files">,
  path: "ticks.csv",
  sha256: "a".repeat(64),
  size: 10,
  verified: true,
  rejection: undefined,
};
function data(changes: Partial<PresentData> = {}): PresentData {
  return {
    record: "complete",
    files: [file],
    expectedCount: 1,
    verifiedCount: 1,
    cursor: null,
    viewerFiles: [],
    deleted: false,
    ...changes,
  };
}
beforeEach(() => {
  calls.query.mockReset();
  calls.token.mockReset().mockResolvedValue("synthetic-jwt");
  calls.download.mockReset().mockResolvedValue(new Uint8Array([42]));
  answer(api.users.getCurrentUser, account("gestionnaire"));
  answer(api.recordStorage.list, data());
});
it("does not render a forbidden record or enable export of a partial deposit", async () => {
  answer(api.recordStorage.list, null);
  const view = await renderPage(<SessionRecordCard sessionId={sessionId} />);
  expect(screen.queryByText(fr.sessionRecord.title)).toBeNull();
  answer(api.recordStorage.list, data({ record: "partial" }));
  await view.refresh();
  expect(
    screen
      .getByRole("button", { name: fr.sessionRecord.archive })
      .hasAttribute("disabled"),
  ).toBe(true);
  expect(
    screen.queryByRole("button", { name: fr.sessionRecord.delete }),
  ).toBeNull();
});
it("requires explicit confirmation before an administrator deletes a deposit", async () => {
  answer(api.users.getCurrentUser, account("org_admin"));
  const view = await renderPage(<SessionRecordCard sessionId={sessionId} />);
  await view.user.click(
    screen.getByRole("button", { name: fr.sessionRecord.delete }),
  );
  expect(mutation(api.recordStorage.remove)).not.toHaveBeenCalled();
  await view.user.click(
    within(screen.getByRole("dialog")).getByRole("button", {
      name: fr.sessionRecord.cancel,
    }),
  );
  expect(mutation(api.recordStorage.remove)).not.toHaveBeenCalled();
  await view.user.click(
    screen.getByRole("button", { name: fr.sessionRecord.delete }),
  );
  await view.user.click(
    within(screen.getByRole("dialog")).getByRole("button", {
      name: fr.sessionRecord.delete,
    }),
  );
  expect(mutation(api.recordStorage.remove)).toHaveBeenCalledWith({
    sessionId,
  });
});
it("moves between file pages using server cursors and returns to the first page", async () => {
  answer(api.recordStorage.list, ({ cursor }) =>
    data({ cursor: cursor ? null : "page-2" }),
  );
  const view = await renderPage(<SessionRecordCard sessionId={sessionId} />);
  await view.user.click(screen.getByText("Voir les 1 fichiers"));
  await view.user.click(
    screen.getByRole("button", { name: fr.sessionRecord.next }),
  );
  expect(lastArgsAsked(api.recordStorage.list)).toEqual({
    sessionId,
    cursor: "page-2",
  });
  await view.user.click(
    screen.getByRole("button", { name: fr.sessionRecord.previous }),
  );
  expect(lastArgsAsked(api.recordStorage.list)).toEqual({
    sessionId,
    cursor: null,
  });
});
it("stops archive downloads when authentication is unavailable", async () => {
  calls.query.mockResolvedValue(data());
  calls.token.mockResolvedValue(null);
  const view = await renderPage(<SessionRecordCard sessionId={sessionId} />);
  await view.user.click(
    screen.getByRole("button", { name: fr.sessionRecord.archive }),
  );
  await waitFor(() =>
    expect(screen.getByRole("alert").textContent).toBe(fr.sessionRecord.error),
  );
  expect(calls.download).not.toHaveBeenCalled();
});
it("cancels an in-flight authenticated file download and restores the controls", async () => {
  calls.query.mockResolvedValue(data());
  calls.download.mockImplementation(
    (_file: unknown, _token: string, signal: AbortSignal) =>
      new Promise<Uint8Array>((_resolve, reject) => {
        signal.addEventListener(
          "abort",
          () => reject(new DOMException("Cancelled", "AbortError")),
          { once: true },
        );
      }),
  );
  const view = await renderPage(<SessionRecordCard sessionId={sessionId} />);
  await view.user.click(
    screen.getByRole("button", { name: fr.sessionRecord.archive }),
  );
  await waitFor(() => expect(calls.download).toHaveBeenCalled());
  await view.user.click(
    screen.getByRole("button", { name: fr.sessionRecord.cancel }),
  );
  await waitFor(() => expect(screen.queryByRole("status")).toBeNull());
  expect(screen.queryByRole("alert")).toBeNull();
});
it("rejects a repeated server cursor instead of downloading forever", async () => {
  calls.query.mockResolvedValue(data({ cursor: "loop" }));
  const view = await renderPage(<SessionRecordCard sessionId={sessionId} />);
  await view.user.click(
    screen.getByRole("button", { name: fr.sessionRecord.archive }),
  );
  await waitFor(() =>
    expect(screen.getByRole("alert").textContent).toBe(fr.sessionRecord.error),
  );
  expect(calls.query).toHaveBeenCalledTimes(2);
});
