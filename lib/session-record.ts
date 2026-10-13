import { z } from "zod";

const manifestSchema = z.object({
  record_id: z.string(),
  kind: z.string(),
  end_reason: z.string().nullable(),
  geometry: z
    .record(z.string(), z.union([z.number(), z.boolean()]))
    .nullable()
    .optional(),
  profile: z
    .object({
      zone_low_bpm: z.number(),
      zone_high_bpm: z.number(),
      hard_max_bpm: z.number(),
      critical_bpm: z.number(),
    })
    .nullable(),
  end_observation: z
    .object({
      drive_state: z.string().nullable().optional(),
      shaft_motor_rpm: z.number().nullable().optional(),
      energised: z.boolean().nullable().optional(),
    })
    .nullable()
    .optional(),
});
const eventSchema = z.object({
  t: z.number(),
  kind: z.string(),
  detail: z.string(),
  actor: z.string().optional(),
});

export function parseRecordCsv(text: string): string[][] {
  const rows: string[][] = [];
  let row: string[] = [];
  let field = "";
  let quoted = false;
  for (let i = 0; i < text.length; i++) {
    const char = text[i];
    if (char === '"') {
      if (quoted && text[i + 1] === '"') {
        field += '"';
        i++;
      } else quoted = !quoted;
    } else if (char === "," && !quoted) {
      row.push(field);
      field = "";
    } else if ((char === "\n" || char === "\r") && !quoted) {
      if (char === "\r" && text[i + 1] === "\n") i++;
      row.push(field);
      rows.push(row);
      row = [];
      field = "";
    } else field += char;
  }
  if (quoted) throw Error("Truncated CSV field");
  if (field.length || row.length) {
    row.push(field);
    rows.push(row);
  }
  return rows;
}
function scalar(value: string): string | number | boolean | null {
  if (value === "") return null;
  if (["true", "false", "True", "False"].includes(value))
    return value === "true" || value === "True";
  if (
    /^-?(?:\d+\.?\d*|\.\d+)(?:e[+-]?\d+)?$/i.test(value) &&
    Number.isFinite(Number(value))
  )
    return Number(value);
  return value;
}
export function recordTrace(
  manifestText: string,
  ticksText: string,
  eventsText: string,
): string {
  const raw: unknown = JSON.parse(manifestText);
  const manifest = manifestSchema.parse(raw);
  const [header, ...rows] = parseRecordCsv(ticksText);
  if (
    !header ||
    !header.includes("t") ||
    new Set(header).size !== header.length
  )
    throw Error("Invalid tick header");
  const lines: object[] = [
    {
      type: "meta",
      schema: 2,
      scenario: manifest.record_id,
      kind: manifest.kind,
      ...(manifest.geometry ?? { gear_ratio: "?" }),
      ...(manifest.profile ?? {}),
    },
  ];
  for (const values of rows) {
    if (values.length === 1 && values[0] === "") continue;
    if (values.length !== header.length) throw Error("Truncated CSV row");
    const row = Object.fromEntries(
      header.map((name, i) => [name, scalar(values[i])]),
    );
    if (typeof row.t !== "number") throw Error("Invalid tick time");
    if (row.t > 0) lines.push({ ...row, type: "row" });
  }
  for (const line of eventsText.split("\n")) {
    if (!line.trim()) continue;
    const rawEvent: unknown = JSON.parse(line);
    lines.push({ ...eventSchema.parse(rawEvent), type: "event" });
  }
  lines.push({
    type: "final",
    end_reason: manifest.end_reason,
    shaft_motor_rpm: null,
    energised: null,
    ...manifest.end_observation,
    sim_state: manifest.end_observation?.drive_state ?? "inconnu",
  });
  if (!manifest.geometry)
    lines.push({
      type: "warning",
      file: "manifest.json",
      code: "missing_viewer_geometry",
    });
  return lines.map((line) => JSON.stringify(line) + "\n").join("");
}

export function recordArchive(
  files: ReadonlyArray<{ path: string; bytes: Uint8Array }>,
): Blob {
  const encoder = new TextEncoder();
  const parts: BlobPart[] = [];
  const write = (
    header: Uint8Array,
    offset: number,
    length: number,
    text: string,
  ) => {
    const bytes = encoder.encode(text);
    if (bytes.length > length) throw Error("Archive field too long");
    header.set(bytes, offset);
  };
  for (const file of files) {
    if (
      !/^(?:[A-Za-z0-9_-]+\/)*[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)+$/.test(
        file.path,
      )
    )
      throw Error("Invalid archive path");
    const header = new Uint8Array(512);
    const split = file.path.length > 100 ? file.path.lastIndexOf("/") : -1;
    write(header, 0, 100, file.path.slice(split + 1));
    if (split >= 0) write(header, 345, 155, file.path.slice(0, split));
    write(header, 100, 8, "0000600\0");
    write(header, 108, 8, "0000000\0");
    write(header, 116, 8, "0000000\0");
    write(
      header,
      124,
      12,
      file.bytes.length.toString(8).padStart(11, "0") + "\0",
    );
    write(header, 136, 12, "00000000000\0");
    header.fill(32, 148, 156);
    header[156] = 48;
    write(header, 257, 6, "ustar\0");
    write(header, 263, 2, "00");
    write(
      header,
      148,
      8,
      header
        .reduce((total, byte) => total + byte, 0)
        .toString(8)
        .padStart(6, "0") + "\0 ",
    );
    parts.push(
      header,
      new Uint8Array(file.bytes),
      new Uint8Array((512 - (file.bytes.length % 512)) % 512),
    );
  }
  parts.push(new Uint8Array(1024));
  return new Blob(parts, { type: "application/x-tar" });
}

export async function fetchRecordFile(
  file: { id: string; sha256: string; size: number },
  token: string,
  signal: AbortSignal,
): Promise<Uint8Array> {
  const cloud = process.env.NEXT_PUBLIC_CONVEX_URL;
  if (!cloud) throw Error("Convex URL missing");
  const url = new URL(cloud);
  if (!url.hostname.endsWith(".convex.cloud"))
    throw Error("Unsupported Convex file endpoint");
  url.hostname = url.hostname.replace(/\.convex\.cloud$/, ".convex.site");
  url.pathname = "/api/records/download";
  url.searchParams.set("fileId", file.id);
  const response = await fetch(url, {
    headers: { Authorization: `Bearer ${token}` },
    signal,
    cache: "no-store",
  });
  if (!response.ok) throw Error(`Download refused (${response.status})`);
  const bytes = await response.arrayBuffer();
  const digest = Array.from(
    new Uint8Array(await crypto.subtle.digest("SHA-256", bytes)),
    (byte) => byte.toString(16).padStart(2, "0"),
  ).join("");
  if (bytes.byteLength !== file.size || digest !== file.sha256)
    throw Error("Downloaded checksum mismatch");
  return new Uint8Array(bytes);
}
