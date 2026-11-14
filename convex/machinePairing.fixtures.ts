import { api } from "./_generated/api";
import type { Id } from "./_generated/dataModel";
import { CONTRACT_HEADER, CONTRACT_VERSION } from "./lib/contract";
import { as, type Actor, type World } from "./test.setup";

/** A Pi's serial number, as `/proc/device-tree/serial-number` reads on a Pi 4. */
export const PI_SERIAL = "10000000c0ffee42";

/** The machine's registry names this Pi (`machineRegistry.updateIdentity`, as the Anheart admin). */
export async function withPiSerial(
  w: World,
  machineId: Id<"machines"> = w.machine,
  serialPi: string = PI_SERIAL,
): Promise<void> {
  await as(w.t, "admin").mutation(api.machineRegistry.updateIdentity, {
    machineId,
    serialPi,
  });
}

/** A pairing code of the machine, as `actor` asks for it on the site. */
export async function issuedCode(
  w: World,
  actor: Actor = "admin",
  machineId: Id<"machines"> = w.machine,
): Promise<{ code: string; expiresAt: number }> {
  return await as(w.t, actor).action(api.pairingCodes.issuePairingCode, {
    machineId,
  });
}

/** A console's pairing request: the contract header, no key, this body. */
export function pair(
  t: World["t"],
  body: unknown,
  headers: Record<string, string> = { [CONTRACT_HEADER]: CONTRACT_VERSION },
): Promise<Response> {
  return t.fetch("/api/machine/pair", {
    method: "POST",
    headers: { "Content-Type": "application/json", ...headers },
    body: typeof body === "string" ? body : JSON.stringify(body),
  });
}
