import { execFileSync } from "node:child_process";
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

/**
 * No reference to the retired ECG recording mode is left in the repository.
 *
 * The recorder's entry point and its two helper modules on the Pi, the machine
 * routes it called, and the mutation that created its sessions are gone. This
 * test reads every text file of the repository and fails on any of their
 * names, outside the short list of places that are allowed to keep them.
 *
 * Each layer has its own, more precise test of the removal (they are in the
 * list below). This one is the net under all three.
 */

const ROOT = join(__dirname, "..");

/** What must not be named any more, and what each name was. */
const RETIRED: ReadonlyArray<{ name: string; pattern: RegExp; was: string }> = [
  {
    name: "src.main",
    pattern: /\bsrc[./]main\b/,
    was: "the recorder's entry point on the Pi",
  },
  {
    name: "convex_client",
    pattern: /convex_client/,
    was: "the recorder's HTTP client on the Pi",
  },
  {
    name: "data_buffer",
    pattern: /data_buffer/,
    was: "the recorder's offline buffer on the Pi",
  },
  {
    name: "/api/machine/session",
    pattern: /\/api\/machine\/session/,
    was: "the four session routes of the recording mode",
  },
  {
    name: "/api/machine/data",
    pattern: /\/api\/machine\/data/,
    was: "the ECG batch route of the recording mode",
  },
  {
    name: "createSession",
    pattern: /\bcreateSession\b/,
    was: "the mutation that created a recording session",
  },
];

/** Where a retired name may still appear, and why. Paths from the repository root. */
const ALLOWED: ReadonlyArray<{ path: string; why: string }> = [
  {
    path: "convex/migrations/",
    why: "the data migration of the retirement",
  },
  {
    path: "docs/suivi-tickets.md",
    why: "documented history: ticket titles, quoted as written",
  },
  {
    path: "docs/reviews/",
    why: "documented history: dated review reports",
  },
  {
    path: "lib/legacyModeReferences.test.ts",
    why: "this test",
  },
  {
    path: "lib/legacyRecordingRetired.test.ts",
    why: "the site's test of the removal",
  },
  {
    path: "convex/legacyRecordingRetired.test.ts",
    why: "the backend's test of the removal",
  },
  {
    path: "raspberry-pi/tests/test_legacy_recorder_retired.py",
    why: "the Pi's test of the removal",
  },
];

const TEXT_FILE =
  /\.(py|pyi|ts|tsx|mts|mjs|cjs|js|jsx|json|md|yml|yaml|toml|sh|ps1|service|txt|example|html|css|cfg|ini|desktop|tsv)$|(^|\/)(Dockerfile|\.dockerignore|\.gitignore)$/;

const SKIPPED_DIRECTORIES = new Set([
  ".git",
  "node_modules",
  ".next",
  "out",
  "build",
  "coverage",
  ".venv",
  "venv",
  ".venv-cad",
  "__pycache__",
  ".pytest_cache",
  ".mypy_cache",
  ".ruff_cache",
  ".hypothesis",
  ".vercel",
  ".omo",
  "worktrees",
  "data",
]);

/** The tracked files; without git (an exported tree), every file under the root. */
function repositoryFiles(): string[] {
  try {
    const listed = execFileSync("git", ["ls-files", "-z"], {
      cwd: ROOT,
      encoding: "utf8",
      maxBuffer: 64 * 1024 * 1024,
      stdio: ["ignore", "pipe", "ignore"],
    });
    const files = listed.split("\0").filter((file) => file.length > 0);
    if (files.length > 0) return files;
  } catch {
    // Not a git checkout: fall through to the directory walk.
  }
  return walk("");
}

function walk(relative: string, found: string[] = []): string[] {
  for (const entry of readdirSync(join(ROOT, relative), {
    withFileTypes: true,
  })) {
    const path = relative ? `${relative}/${entry.name}` : entry.name;
    if (entry.isSymbolicLink()) continue;
    if (entry.isDirectory()) {
      if (!SKIPPED_DIRECTORIES.has(entry.name)) walk(path, found);
    } else {
      found.push(path);
    }
  }
  return found;
}

function isAllowed(path: string): boolean {
  return ALLOWED.some((allowed) =>
    allowed.path.endsWith("/")
      ? path.startsWith(allowed.path)
      : path === allowed.path,
  );
}

function read(path: string): string | null {
  try {
    return readFileSync(join(ROOT, path), "utf8");
  } catch {
    return null; // listed by git but absent from the working tree
  }
}

const files = repositoryFiles().filter((path) => TEXT_FILE.test(path));

function references(): string[] {
  const hits: string[] = [];
  for (const path of files) {
    if (isAllowed(path)) continue;
    const text = read(path);
    if (text === null) continue;
    text.split("\n").forEach((line, index) => {
      for (const retired of RETIRED) {
        if (retired.pattern.test(line)) {
          hits.push(
            `${path}:${index + 1}: ${retired.name} (${retired.was}): ${line.trim().slice(0, 160)}`,
          );
        }
      }
    });
  }
  return hits;
}

describe("the retired ECG recording mode is named nowhere", () => {
  it("reads the whole repository: the Pi, the backend, the site and the docs", () => {
    for (const expected of [
      "raspberry-pi/src/local_panel.py",
      "raspberry-pi/Dockerfile",
      "raspberry-pi/scripts/anheart.service",
      "convex/http.ts",
      "app/[locale]/dashboard/sessions/page.tsx",
      "docs/convex.md",
      "README.md",
      "raspberry-pi/docker-compose.yml",
    ]) {
      expect(files, expected).toContain(expected);
    }
    expect(files.length).toBeGreaterThan(400);
  });

  it("recognises every retired name, and nothing that merely looks like one", () => {
    const named = (line: string) =>
      RETIRED.filter((retired) => retired.pattern.test(line)).map(
        (retired) => retired.name,
      );
    expect(named("CMD python -m src.main")).toEqual(["src.main"]);
    expect(named("`src/main.py` (the recorder)")).toEqual(["src.main"]);
    expect(named("from .convex_client import ConvexClient")).toEqual([
      "convex_client",
    ]);
    expect(named('"src/data_buffer.py",')).toEqual(["data_buffer"]);
    expect(named('path: "/api/machine/session/poll",')).toEqual([
      "/api/machine/session",
    ]);
    expect(named('"/api/machine/data"')).toEqual(["/api/machine/data"]);
    expect(named("useMutation(api.sessions.createSession)")).toEqual([
      "createSession",
    ]);
    for (const kept of [
      "python -m src.local_panel",
      "from src.motor.drive import DriveBackend",
      'path: "/api/machine/training/poll",',
      "api.training.launchAutoSession",
      "internal.training.registerLocalSession",
      "retirer-lancien-mode-enregistrement-ecg-srcmain-routes-sessiondata",
    ]) {
      expect(named(kept), kept).toEqual([]);
    }
  });

  it("keeps the allowed places few, and real", () => {
    expect(ALLOWED).toHaveLength(7);
    for (const allowed of ALLOWED) {
      expect(
        files.some((path) =>
          allowed.path.endsWith("/")
            ? path.startsWith(allowed.path)
            : path === allowed.path,
        ),
        `${allowed.path} (${allowed.why})`,
      ).toBe(true);
    }
  });

  it("finds no reference outside the allowed places", () => {
    expect(references()).toEqual([]);
  });
});
