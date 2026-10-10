/**
 * The versioned contract between a machine (the Raspberry Pi console) and this
 * backend: one version number, one request header, one list of stable error
 * codes. All of it is read from `contracts/machine-api.json` at the repository
 * root, the same file the Pi's tests pin `raspberry-pi/src/contract.py` to, so
 * the two sides cannot drift apart unnoticed.
 *
 * A version is `major.minor`. The major is what both sides must share: a
 * machine announcing a major this backend does not serve is refused outright
 * rather than half understood. A newer minor is accepted, because within one
 * major a machine only relies on what every minor of it provides.
 */
import { ConvexError } from "convex/values";
import machineApi from "../../contracts/machine-api.json";

export const CONTRACT_VERSION: string = machineApi.contract_version;

/** The request header every machine request carries: `X-Anheart-Contract: 1.0`. */
export const CONTRACT_HEADER: string = machineApi.header;

/** A stable error code of the machine API, as listed in the shared file. */
export type MachineErrorCode = keyof typeof machineApi.error_codes;

export const MACHINE_ERROR_CODES = Object.keys(
  machineApi.error_codes,
) as MachineErrorCode[];

const VERSION = /^(0|[1-9]\d{0,3})\.(0|[1-9]\d{0,3})$/;

/** The major of a `major.minor` version, or null when the text is not one. */
export function contractMajor(version: string | null): string | null {
  const match = version === null ? null : VERSION.exec(version);
  return match ? match[1] : null;
}

/** The majors this backend serves. One today; a transition may list two. */
export const SUPPORTED_MAJORS: readonly string[] = [
  CONTRACT_VERSION.split(".")[0],
];

/** The body of every refusal of a machine route: a stable code, then words. */
export function machineErrorResponse(
  status: number,
  code: MachineErrorCode,
  message: string,
  extra: Record<string, unknown> = {},
): Response {
  return Response.json({ error: code, message, ...extra }, { status });
}

/**
 * The contract version a request announces, when its major is served; null
 * otherwise. An absent or unreadable header is refused like an unknown major.
 */
export function servedContract(req: Request): string | null {
  const announced = req.headers.get(CONTRACT_HEADER);
  const major = contractMajor(announced);
  return major !== null && SUPPORTED_MAJORS.includes(major) ? announced : null;
}

/** The 426 answered to a request whose contract is not served. */
export function contractUnsupported(): Response {
  return machineErrorResponse(
    426,
    "contract_unsupported",
    `Unsupported machine contract: send ${CONTRACT_HEADER} with a served major`,
    { supported: SUPPORTED_MAJORS },
  );
}

type CodedError = { code: MachineErrorCode; message: string };

/** An error a machine route answers with its own stable code. */
export function machineError(code: MachineErrorCode, message: string) {
  return new ConvexError<CodedError>({ code, message });
}

function isCoded(data: unknown): data is CodedError {
  if (typeof data !== "object" || data === null) return false;
  const { code, message } = data as Record<string, unknown>;
  return (
    typeof message === "string" &&
    MACHINE_ERROR_CODES.some((known) => known === code)
  );
}

/** The code and words for anything thrown while a machine route was handled. */
export function refusalOf(error: unknown): CodedError {
  if (error instanceof ConvexError && isCoded(error.data)) return error.data;
  return {
    code: "request_failed",
    message: error instanceof Error ? error.message : "Unknown error",
  };
}

const SOFTWARE_VERSION = /^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$/;

/** A machine's software version (a git tag such as `pi-0.4.2`), if well formed. */
export function softwareVersionOf(raw: unknown): string | undefined {
  return typeof raw === "string" && SOFTWARE_VERSION.test(raw)
    ? raw
    : undefined;
}
