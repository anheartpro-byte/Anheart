import { readFileSync } from "node:fs";

/**
 * The secret values of a run: the Clerk keys, and what the harness makes
 * (Convex admin key, webhook secret, tokens, machine key). Kept in memory of
 * the runner only, so that everything it prints is masked and the reporter
 * `secret-scan.ts` can count them in what the run leaves behind.
 */
const STORE = Symbol.for("anheart.e2e.secrets");
const store: Set<string> = ((globalThis as Record<symbol, unknown>)[STORE] ??=
  new Set<string>()) as Set<string>;

export function remember(value: string): string {
  if (value.length >= 8) store.add(value);
  return value;
}

export function knownSecrets(): string[] {
  return [...store];
}

export function redact(text: string): string {
  let out = text;
  for (const value of store) out = out.split(value).join("[masqué]");
  return out;
}

/** The three Clerk values the site and Convex read, nothing else. */
const CLERK_NAMES = [
  "NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY",
  "CLERK_SECRET_KEY",
  "CLERK_JWT_ISSUER_DOMAIN",
] as const;
type ClerkName = (typeof CLERK_NAMES)[number];

export type ClerkKeys = {
  publishableKey: string;
  secretKey: string;
  issuer: string;
  /** Frontend API host of the instance, read from the publishable key. */
  frontendApi: string;
};

/**
 * Reads the named lines of the file `E2E_ENV_FILE` (or the same names from the
 * environment when it is unset). Any other line is skipped before its value
 * is looked at: a deploy key or another secret of that file never reaches
 * memory.
 */
function readClerkValues(): Partial<Record<ClerkName, string>> {
  const file = process.env.E2E_ENV_FILE;
  const found: Partial<Record<ClerkName, string>> = {};
  if (!file) {
    for (const name of CLERK_NAMES) {
      const value = process.env[name]?.trim();
      if (value) found[name] = value;
    }
    return found;
  }
  for (const line of readFileSync(file, "utf8").split(/\r?\n/)) {
    const match = /^\s*(?:export\s+)?([A-Z0-9_]+)\s*=/.exec(line);
    const name = match?.[1] as ClerkName | undefined;
    if (!match || !name || !CLERK_NAMES.includes(name)) continue;
    let value = line.slice(match[0].length).trim();
    const quote = value[0];
    if ((quote === '"' || quote === "'") && value.endsWith(quote)) {
      value = value.slice(1, -1);
    } else {
      value = value.replace(/\s+#.*$/, "");
    }
    if (value) found[name] = value;
  }
  return found;
}

export function loadClerkKeys(): ClerkKeys {
  const values = readClerkValues();
  const source = process.env.E2E_ENV_FILE
    ? "le fichier E2E_ENV_FILE"
    : "l'environnement (E2E_ENV_FILE absent)";
  const publishableKey = values.NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY;
  const secretKey = values.CLERK_SECRET_KEY;
  if (!publishableKey || !secretKey) {
    throw new Error(
      `Clés Clerk absentes de ${source} : NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY et CLERK_SECRET_KEY sont requises.`,
    );
  }
  remember(secretKey);
  // Prefixes built at run time: no key-shaped literal in the repository.
  const devPublishable = ["pk", "test", ""].join("_");
  const devSecret = ["sk", "test", ""].join("_");
  if (!publishableKey.startsWith(devPublishable)) {
    throw new Error(
      "La clé publiable Clerk n'est pas celle d'une instance de développement : refus.",
    );
  }
  if (!secretKey.startsWith(devSecret)) {
    throw new Error(
      "La clé secrète Clerk n'est pas celle d'une instance de développement : refus.",
    );
  }
  const frontendApi = Buffer.from(
    publishableKey.slice(devPublishable.length),
    "base64",
  )
    .toString("utf8")
    .replace(/\$$/, "");
  if (!/^[a-z0-9.-]+\.[a-z]+$/.test(frontendApi)) {
    throw new Error("La clé publiable Clerk ne désigne aucune instance lisible.");
  }
  // The issuer of an instance's tokens is its Frontend API.
  const issuer = (
    values.CLERK_JWT_ISSUER_DOMAIN ?? `https://${frontendApi}`
  ).replace(/\/+$/, "");
  return { publishableKey, secretKey, issuer, frontendApi };
}

/**
 * Never a Convex cloud deployment: a run is refused when any Convex variable
 * of its environment names one (production, or the dev deployment).
 */
export function refuseCloudConvex(): void {
  for (const [name, value] of Object.entries(process.env)) {
    if (/CONVEX/i.test(name) && value && /\.convex\.(cloud|site)\b/i.test(value)) {
      throw new Error(
        `${name} désigne un déploiement Convex dans le cloud : le harnais ne travaille que sur son backend local, refus.`,
      );
    }
  }
}

/** Every address the harness talks to on its own side is on the loopback. */
export function assertLocal(url: string): string {
  if (!["127.0.0.1", "localhost"].includes(new URL(url).hostname)) {
    throw new Error(`Adresse hors de la boucle locale refusée : ${url}`);
  }
  return url;
}

/**
 * The environment for a child process: the runner's own, without any Convex,
 * Clerk or machine variable, plus what the caller adds.
 */
export function childEnv(extra: Record<string, string>): Record<string, string | undefined> {
  const env = Object.fromEntries(
    Object.entries(process.env).filter(
      ([name]) => !/CONVEX|CLERK|MACHINE_API_KEY|^E2E_|^NEXT_PUBLIC_/i.test(name),
    ),
  );
  return { ...env, ...extra };
}
