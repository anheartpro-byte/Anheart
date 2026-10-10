import { existsSync, readdirSync, readFileSync } from "node:fs";
import { join, relative } from "node:path";
import { describe, expect, it } from "vitest";

/**
 * The ECG recording mode is retired from the site.
 *
 * One mode remains: the training sessions the console runs. The dashboard can
 * launch one (`training.launchAutoSession`) and nothing else; a session of the
 * former recording mode is read-only history on its detail page.
 */

const ROOT = join(__dirname, "..");
const SOURCE_DIRECTORIES = ["app", "components", "hooks", "lib", "i18n"];

type Source = { path: string; text: string };

function walk(directory: string, found: string[] = []): string[] {
  for (const entry of readdirSync(directory, { withFileTypes: true })) {
    const full = join(directory, entry.name);
    if (entry.isDirectory()) walk(full, found);
    else if (/\.(ts|tsx|mts|mjs)$/.test(entry.name)) found.push(full);
  }
  return found;
}

const sources: Source[] = SOURCE_DIRECTORIES.filter((directory) =>
  existsSync(join(ROOT, directory)),
)
  .flatMap((directory) => walk(join(ROOT, directory)))
  .map((file) => ({
    path: relative(ROOT, file).split("\\").join("/"),
    text: readFileSync(file, "utf8"),
  }))
  // The site's sources, not its tests: a test of a removal names what it removes.
  .filter((source) => !/\.test\.tsx?$/.test(source.path));

function matches(pattern: RegExp): string[] {
  return sources.flatMap((source) =>
    source.text
      .split("\n")
      .map((line, index) => ({ line, number: index + 1 }))
      .filter(({ line }) => pattern.test(line))
      .map(({ line, number }) => `${source.path}:${number}: ${line.trim()}`),
  );
}

function catalog(locale: string): Record<string, unknown> {
  return JSON.parse(
    readFileSync(join(ROOT, "messages", `${locale}.json`), "utf8"),
  ) as Record<string, unknown>;
}

function leaves(node: Record<string, unknown>, prefix = ""): string[] {
  return Object.entries(node).flatMap(([key, value]) => {
    const path = prefix ? `${prefix}.${key}` : key;
    return value !== null && typeof value === "object"
      ? leaves(value as Record<string, unknown>, path)
      : [path];
  });
}

function resolve(node: Record<string, unknown>, dotted: string): unknown {
  let current: unknown = node;
  for (const part of dotted.split(".")) {
    if (current === null || typeof current !== "object") return undefined;
    current = (current as Record<string, unknown>)[part];
  }
  return current;
}

describe("the site offers one way to create a session", () => {
  it("found the sources it reads", () => {
    expect(sources.length).toBeGreaterThan(40);
    expect(matches(/api\.training\.launchAutoSession/)).not.toEqual([]);
  });

  it("calls no mutation of the sessions module", () => {
    // launchAutoSession is the only creation path; a session ends at the
    // machine, or through training.requestStop.
    // The site calls its mutations through useMutationWithFeedback (ANH-156).
    expect(matches(/useMutation(?:WithFeedback)?\(\s*api\.sessions\./)).toEqual(
      [],
    );
    expect(matches(/\b(createSession|endSession|cancelSession)\b/)).toEqual([]);
  });

  it("has no form that creates a recording session", () => {
    expect(
      existsSync(join(ROOT, "components/modals/SessionFormModal.tsx")),
    ).toBe(false);
    expect(matches(/SessionFormModal/)).toEqual([]);
  });
});

describe("the former ECG block is gone from the live and detail views", () => {
  const retired = [
    "components/charts",
    "components/ECGWaveform.tsx",
    "lib/ecg.ts",
    "lib/ecg",
    "lib/generatePdf.ts",
  ];

  it.each(retired)("%s no longer exists", (path) => {
    expect(existsSync(join(ROOT, path))).toBe(false);
  });

  it("imports none of the retired modules", () => {
    expect(
      matches(
        /components\/charts|ECGWaveform|LiveSensorDisplay|SensorCharts|lib\/ecg|generatePdf|generateSessionPdf/,
      ),
    ).toEqual([]);
  });

  it("reads ECG batches in one place only: the read-only history card", () => {
    const readers = matches(/api\.ecgData\./);
    expect(readers).toHaveLength(1);
    expect(readers[0]).toContain(
      "app/[locale]/dashboard/sessions/[id]/page.tsx",
    );
    expect(readers[0]).toContain("api.ecgData.getSessionDataStats");
    expect(matches(/api\.sessionSummaries\./)).toEqual([]);
  });

  it("gives a recording session no live view", () => {
    const live = sources.find(
      (source) =>
        source.path === "app/[locale]/dashboard/sessions/[id]/live/page.tsx",
    );
    expect(live).toBeDefined();
    expect(live?.text).toContain("if (!isTrainingKind(session.kind))");
    expect(live?.text).toContain('t("sessionLive.legacyRecording")');
  });
});

describe("the machine form no longer configures a recorder", () => {
  const form = sources.find(
    (source) => source.path === "components/modals/MachineFormModal.tsx",
  );

  it("asks for no sampling frequency, channel or batch interval", () => {
    expect(form).toBeDefined();
    expect(form?.text).not.toMatch(
      /sampleRate|batchInterval|channels|config\b/,
    );
  });

  it("shows none of them on the machine page", () => {
    expect(matches(/machine\.config\b/)).toEqual([]);
  });
});

describe("the message catalogs follow", () => {
  const fr = catalog("fr");
  const en = catalog("en");

  it("keeps both locales on the same keys", () => {
    expect(leaves(en).sort()).toEqual(leaves(fr).sort());
  });

  it.each(["ecg", "recording", "sensors"])(
    "has no %s namespace left",
    (namespace) => {
      expect(fr[namespace]).toBeUndefined();
      expect(en[namespace]).toBeUndefined();
    },
  );

  it.each([
    "sessions.create",
    "sessions.end",
    "sessions.form",
    "sessions.channelOptions",
    "reports.download",
    "reports.pdf",
    "machines.sampleRate",
    "machines.channels",
    "machines.batchInterval",
    "sessionLive.endDialogTitle",
    "sessionDetail.ecgRecording",
  ])("has no %s key left, and no source names it", (key) => {
    expect(resolve(fr, key)).toBeUndefined();
    expect(resolve(en, key)).toBeUndefined();
    expect(sources.filter((source) => source.text.includes(`"${key}`))).toEqual(
      [],
    );
  });

  it("defines every key the session and report pages name", () => {
    const pages = [
      "app/[locale]/dashboard/sessions/page.tsx",
      "app/[locale]/dashboard/sessions/[id]/page.tsx",
      "app/[locale]/dashboard/sessions/[id]/live/page.tsx",
      "components/modals/MachineFormModal.tsx",
    ];
    const missing: string[] = [];
    for (const path of pages) {
      const page = sources.find((source) => source.path === path);
      expect(page, path).toBeDefined();
      // These pages all use the root translator: t("namespace.key").
      expect(page?.text).toContain("useTranslations()");
      for (const [, key] of page?.text.matchAll(/\bt\(\s*"([\w.]+)"/g) ?? []) {
        for (const [locale, messages] of [
          ["fr", fr],
          ["en", en],
        ] as const) {
          if (typeof resolve(messages, key) !== "string") {
            missing.push(`${path}: ${locale} ${key}`);
          }
        }
      }
    }
    expect(missing).toEqual([]);
  });

  it("defines every key the reports page names", () => {
    const page = sources.find(
      (source) => source.path === "app/[locale]/dashboard/reports/page.tsx",
    );
    expect(page?.text).toContain('useTranslations("reports")');
    const keys = [...(page?.text.matchAll(/\bt\(\s*"([\w.]+)"/g) ?? [])].map(
      ([, key]) => `reports.${key}`,
    );
    expect(keys.length).toBeGreaterThan(3);
    for (const key of keys) {
      expect(typeof resolve(fr, key), key).toBe("string");
      expect(typeof resolve(en, key), key).toBe("string");
    }
  });
});
