import { httpAction } from "./_generated/server";
import { internal } from "./_generated/api";
import { validateMachineAuth } from "./lib/machineHttpAuth";
import { machineErrorResponse } from "./lib/contract";
import { commitRequest, uploadRequest } from "./lib/recordStorage";
import { limitMachine } from "./lib/rateLimits";

async function requestBody(req: Request): Promise<unknown> {
  try {
    return await req.json();
  } catch {
    return null;
  }
}
function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      "Content-Type": "application/json",
      "Cache-Control": "no-store",
    },
  });
}
/** One reservation per file; ten a second per machine at most, 64 in a row (ANH-166). */
export const uploadUrl = httpAction(async (ctx, req) => {
  const auth = await validateMachineAuth(ctx, req);
  if ("error" in auth) return auth.error;
  const limited = await limitMachine(ctx, "machineUploadUrl", auth.machine._id);
  if (limited) return limited;
  const parsed = uploadRequest.safeParse(await requestBody(req));
  if (!parsed.success)
    return machineErrorResponse(
      400,
      "invalid_request",
      "Invalid file metadata",
    );
  const result = await ctx.runMutation(internal.recordStorage.requestUpload, {
    ...parsed.data,
    machineId: auth.machine._id,
  });
  return typeof result.error === "string"
    ? machineErrorResponse(400, "invalid_request", result.error)
    : json(result);
});
/** One commit per reservation; ten a second per machine, 64 in reserve (ANH-228). */
export const commit = httpAction(async (ctx, req) => {
  const auth = await validateMachineAuth(ctx, req);
  if ("error" in auth) return auth.error;
  const limited = await limitMachine(
    ctx,
    "machineRecordCommit",
    auth.machine._id,
  );
  if (limited) return limited;
  const parsed = commitRequest.safeParse(await requestBody(req));
  if (!parsed.success)
    return machineErrorResponse(
      400,
      "invalid_request",
      "Invalid commit metadata",
    );
  const result = await ctx.runMutation(internal.recordStorage.commit, {
    ...parsed.data,
    machineId: auth.machine._id,
  });
  return typeof result.error === "string"
    ? machineErrorResponse(400, "invalid_request", result.error)
    : json(result);
});
/** Never rate limited: read only; an invalid key already costs as much. */
export const status = httpAction(async (ctx, req) => {
  const auth = await validateMachineAuth(ctx, req);
  if ("error" in auth) return auth.error;
  const sessionId = new URL(req.url).searchParams.get("sessionId");
  if (!sessionId)
    return machineErrorResponse(400, "invalid_request", "Missing sessionId");
  const result = await ctx.runQuery(internal.recordStorage.machineStatus, {
    sessionId,
    machineId: auth.machine._id,
    cursor: new URL(req.url).searchParams.get("cursor"),
  });
  return result === null
    ? machineErrorResponse(404, "session_not_found", "Session not found")
    : json(result);
});

const cors = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "Authorization, Content-Type",
  "Access-Control-Allow-Methods": "GET, OPTIONS",
  "Cache-Control": "no-store",
};
export const options = httpAction(
  async () => new Response(null, { status: 204, headers: cors }),
);
export const download = httpAction(async (ctx, req) => {
  if (!(await ctx.auth.getUserIdentity()))
    return new Response("Unauthorized", { status: 401, headers: cors });
  const fileId = new URL(req.url).searchParams.get("fileId");
  if (!fileId)
    return new Response("Missing fileId", { status: 400, headers: cors });
  const file = await ctx.runQuery(internal.recordStorage.authorizedFile, {
    fileId,
  });
  if (!file) return new Response("Not found", { status: 404, headers: cors });
  const blob = await ctx.storage.get(file.storageId);
  if (!blob) return new Response("Not found", { status: 404, headers: cors });
  return new Response(blob, {
    headers: {
      ...cors,
      "Content-Type": "application/octet-stream",
      "Content-Disposition": `attachment; filename="${file.path.split("/").pop() ?? "record"}"`,
      "X-Content-Type-Options": "nosniff",
    },
  });
});
