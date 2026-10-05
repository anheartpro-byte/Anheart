const algorithm = { name: "HMAC", hash: "SHA-256" } as const;
const encoder = new TextEncoder();

function hex(bytes: Uint8Array): string {
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join(
    "",
  );
}

function randomHex(length: number): string {
  return hex(crypto.getRandomValues(new Uint8Array(length)));
}

function unhex(value: string): Uint8Array<ArrayBuffer> {
  return Uint8Array.from(value.match(/.{2}/g) ?? [], (byte) =>
    parseInt(byte, 16),
  );
}

export function apiKeySelector(plain: string): string | null {
  return /^anh1\.([a-f0-9]{32})\.[a-f0-9]{64}$/.exec(plain)?.[1] ?? null;
}

export async function hashApiKey(plain: string): Promise<string> {
  const salt = randomHex(16);
  const key = await crypto.subtle.importKey(
    "raw",
    unhex(salt),
    algorithm,
    false,
    ["sign"],
  );
  const digest = await crypto.subtle.sign("HMAC", key, encoder.encode(plain));
  return `hmac-sha256:1:${salt}:${hex(new Uint8Array(digest))}`;
}

export async function generateApiKey(): Promise<{
  readonly plain: string;
  readonly hashed: string;
  readonly selector: string;
}> {
  const selector = randomHex(16);
  const plain = `anh1.${selector}.${randomHex(32)}`;
  return { plain, hashed: await hashApiKey(plain), selector };
}

export async function verifyApiKey(
  plain: string,
  storedHash: string,
): Promise<boolean> {
  const match = /^hmac-sha256:1:([a-f0-9]{32}):([a-f0-9]{64})$/.exec(
    storedHash,
  );
  const salt = match?.[1];
  const digest = match?.[2];
  if (!salt || !digest || !apiKeySelector(plain)) return false;
  const key = await crypto.subtle.importKey(
    "raw",
    unhex(salt),
    algorithm,
    false,
    ["verify"],
  );
  // Delegate digest comparison to the native constant-time HMAC verifier.
  return crypto.subtle.verify(
    "HMAC",
    key,
    unhex(digest),
    encoder.encode(plain),
  );
}
