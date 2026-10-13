import { describe, expect, it } from "vitest";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { execFileSync } from "node:child_process";
import { parseRecordCsv, recordArchive, recordTrace } from "./session-record";

describe("verified record export", () => {
  it("reads quoted CSV fields without changing text or inventing missing numbers", () => {
    expect(
      parseRecordCsv('t,note,value\r\n1,"a,b and ""quote""",\r\n'),
    ).toEqual([
      ["t", "note", "value"],
      ["1", 'a,b and "quote"', ""],
    ]);
    expect(() => parseRecordCsv('t,note\n1,"truncated')).toThrow();
    const trace = recordTrace(
      JSON.stringify({
        record_id: "qa",
        kind: "manual",
        end_reason: "operator_stop",
        profile: null,
      }),
      't,measured_motor_rpm,output_rpm,hr_live,note\n-1,0,0,,before\n1,10,0.2,,"<script>text</script>"\n',
      '{"t":1,"kind":"end","detail":"done","actor":"system"}\n',
    );
    const rows: unknown[] = trace
      .trim()
      .split("\n")
      .map((line) => JSON.parse(line));
    expect(rows).toContainEqual({
      type: "row",
      t: 1,
      measured_motor_rpm: 10,
      output_rpm: 0.2,
      hr_live: null,
      note: "<script>text</script>",
    });
    expect(rows).toContainEqual({
      type: "warning",
      file: "manifest.json",
      code: "missing_viewer_geometry",
    });
    expect(rows).not.toContainEqual(
      expect.objectContaining({ type: "row", t: -1 }),
    );
    expect(() => recordTrace('{"bad":true}', "t\n1\n", "")).toThrow();
    const flags = recordTrace(
      JSON.stringify({
        record_id: "qa",
        kind: "manual",
        end_reason: null,
        profile: null,
      }),
      "t,output_enabled,silent\n1,False,True\n",
      "",
    );
    expect(flags).toContain('"output_enabled":false,"silent":true');
  });
  it("produces a standard tar archive with byte-exact contents and confined paths", async () => {
    const bytes = new TextEncoder().encode("t,value\n1,42\n");
    const archive = recordArchive([
      { path: "ticks.csv", bytes },
      { path: "ecg_raw/000001.bin.gz", bytes: new Uint8Array([0, 255, 128]) },
    ]);
    const input = Buffer.from(await archive.arrayBuffer());
    expect(execFileSync("tar", ["-xOf", "-", "ticks.csv"], { input })).toEqual(
      Buffer.from(bytes),
    );
    expect(
      execFileSync("tar", ["-xOf", "-", "ecg_raw/000001.bin.gz"], { input }),
    ).toEqual(Buffer.from([0, 255, 128]));
    expect(() => recordArchive([{ path: "../outside", bytes }])).toThrow();
  });
  it("keeps the static viewer script and style authorized by their exact CSP hashes", () => {
    const viewer = readFileSync(
      new URL("../public/viewer/index.html", import.meta.url),
      "utf8",
    );
    for (const tag of ["script", "style"]) {
      const content = new RegExp(`<${tag}>([\\s\\S]*?)</${tag}>`).exec(
        viewer,
      )?.[1];
      expect(content).toBeDefined();
      const hash = createHash("sha256")
        .update(content ?? "")
        .digest("base64");
      expect(viewer).toContain(`${tag}-src 'sha256-${hash}'`);
    }
  });
});
