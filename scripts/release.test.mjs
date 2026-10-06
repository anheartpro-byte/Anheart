/**
 * ANH-134: tests of the release process.
 *
 * Each test of `scripts/release.sh` builds a throwaway git repository with its
 * own bare `origin` and replaces `gh` with a double that answers the CI state
 * and records what it is asked to create. Nothing here reaches GitHub or the
 * real repository. The last block checks the real files of this repository.
 */
import assert from "node:assert/strict";
import { execFileSync, spawnSync } from "node:child_process";
import {
  chmodSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const SCRIPT = join(ROOT, "scripts", "release.sh");
const TEMPLATE = ".github/PULL_REQUEST_TEMPLATE/release.md";
const MARKER = "<!-- release.sh : nouvelles sections sous cette ligne -->";
const DATE = "2026-10-07";
const ALL = ["--pi", "0.1.0", "--cloud", "0.1.0", "--web", "0.1.0", "--pi-validation", "bench", "--date", DATE];
const GREEN = "pi-gate\tcompleted\tsuccess\nweb\tcompleted\tsuccess\ndocs\tcompleted\tskipped\n";

const real = (path) => readFileSync(join(ROOT, path), "utf8");

const GIT_ENV = {
  ...process.env,
  GIT_CONFIG_GLOBAL: "/dev/null",
  GIT_CONFIG_NOSYSTEM: "1",
  GIT_TERMINAL_PROMPT: "0",
  GIT_AUTHOR_NAME: "Synthetic Releaser",
  GIT_AUTHOR_EMAIL: "release@example.invalid",
  GIT_COMMITTER_NAME: "Synthetic Releaser",
  GIT_COMMITTER_EMAIL: "release@example.invalid",
};

function git(cwd, ...args) {
  return execFileSync("git", args, { cwd, env: GIT_ENV, encoding: "utf8" }).trim();
}

function write(repo, path, content) {
  mkdirSync(dirname(join(repo, path)), { recursive: true });
  writeFileSync(join(repo, path), content);
}

function commit(repo, subject, files) {
  for (const [path, content] of Object.entries(files)) write(repo, path, content);
  git(repo, "add", "-A");
  git(repo, "commit", "--quiet", "-m", subject);
}

const FAKE_GH = `#!/bin/sh
printf '%s\\n' "$*" >>"$FAKE_GH_LOG"
if [ "$1" = api ]; then
  printf '%s' "$FAKE_GH_CHECKS"
  exit "\${FAKE_GH_API_EXIT:-0}"
fi
if [ "$1 $2" = "pr create" ]; then
  while [ $# -gt 0 ]; do
    if [ "$1" = --body-file ]; then cat "$2" >"$FAKE_GH_LOG.body"; fi
    shift
  done
  echo "https://github.invalid/anheart/pull/1"
fi
`;

/** History of `develop` before any release: titles as squash merges leave them. */
const HISTORY = [
  ["ANH-10 : régler la rampe du bras (#1)", { "raspberry-pi/src/ramp.py": "ramp = 1\n" }],
  ["ANH-11: add the release register (#2)", { "convex/register.ts": "export {};\n" }],
  ["ANH-12 : pied de page du site (#3)", { "components/Footer.tsx": "// footer\n", "convex/footer.ts": "export {};\n" }],
  ["ANH-13 : guide de démarrage (#4)", { "docs/guide.md": "# Guide\n" }],
  ["chore: bump a dependency", { "app/page.tsx": "// page\n" }],
  ['Revert "ANH-10 : régler la rampe du bras (#1)"', { "raspberry-pi/src/revert.py": "x = 1\n" }],
];

/**
 * A repository as it stands after this ticket: development versions on `main`
 * and `develop`, then `history` squash-merged on `develop`.
 */
function fixture(t, history = HISTORY) {
  const dir = mkdtempSync(join(tmpdir(), "anheart-release-"));
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  const origin = join(dir, "origin.git");
  const work = join(dir, "work");
  const bin = join(dir, "bin");
  const log = join(dir, "gh.log");
  mkdirSync(bin);
  writeFileSync(join(bin, "gh"), FAKE_GH);
  chmodSync(join(bin, "gh"), 0o755);

  git(dir, "init", "--quiet", "--bare", "--initial-branch=main", origin);
  git(dir, "init", "--quiet", "--initial-branch=main", work);
  git(work, "remote", "add", "origin", origin);
  const manifest = { name: "fixture", version: "0.0.0-dev", private: true };
  commit(work, "socle", {
    "raspberry-pi/VERSION": "pi-0.0.0-dev\n",
    "convex/VERSION": "cloud-0.0.0-dev\n",
    "convex/cloudVersion.ts": real("convex/cloudVersion.ts").replace(/"cloud-[^"]*"/, '"cloud-0.0.0-dev"'),
    "package.json": `${JSON.stringify(manifest, null, 2)}\n`,
    "package-lock.json": `${JSON.stringify(
      {
        name: "fixture",
        version: "0.0.0-dev",
        lockfileVersion: 3,
        requires: true,
        packages: {
          "": { name: "fixture", version: "0.0.0-dev", dependencies: { left: "^0.1.0" } },
          "node_modules/left": { version: "0.1.0" },
        },
      },
      null,
      2,
    )}\n`,
    "CHANGELOG.md": `${real("CHANGELOG.md").split(MARKER)[0]}${MARKER}\n`,
    [TEMPLATE]: real(TEMPLATE),
  });
  git(work, "push", "--quiet", "origin", "main");
  git(work, "switch", "--quiet", "-c", "develop");
  for (const [subject, files] of history) commit(work, subject, files);
  git(work, "push", "--quiet", "-u", "origin", "develop");
  // A first fetch records origin's default branch; do it before any snapshot.
  git(work, "fetch", "--quiet", "--tags", "origin");

  const fx = {
    dir,
    origin,
    work,
    /** Run release.sh in the work clone. `checks` is what the CI answers. */
    run(args, { checks = GREEN, apiExit = "0", env = {} } = {}) {
      rmSync(log, { force: true });
      rmSync(`${log}.body`, { force: true });
      const result = spawnSync("bash", [SCRIPT, ...args], {
        cwd: work,
        encoding: "utf8",
        env: {
          ...GIT_ENV,
          PATH: `${bin}:${process.env.PATH}`,
          FAKE_GH_LOG: log,
          FAKE_GH_CHECKS: checks,
          FAKE_GH_API_EXIT: apiExit,
          ...env,
        },
      });
      return {
        status: result.status,
        stdout: result.stdout,
        stderr: result.stderr,
        gh: existsSync(log) ? readFileSync(log, "utf8").trim().split("\n") : [],
        body: existsSync(`${log}.body`) ? readFileSync(`${log}.body`, "utf8") : "",
      };
    },
    /** Everything a run could change: both repositories' refs and the work tree. */
    state() {
      return [
        git(origin, "for-each-ref"),
        git(work, "for-each-ref"),
        git(work, "status", "--porcelain"),
        git(work, "rev-parse", "--abbrev-ref", "HEAD"),
      ].join("\n--\n");
    },
    /** What merging the preparation PR by squash leaves on `develop`. */
    squashIntoDevelop(branch, subject) {
      git(work, "switch", "--quiet", "develop");
      git(work, "merge", "--quiet", "--squash", branch);
      git(work, "commit", "--quiet", "-m", subject);
      git(work, "push", "--quiet", "origin", "develop");
    },
    /** What merging the release PR with a merge commit leaves on `main`. */
    mergeIntoMain() {
      git(work, "switch", "--quiet", "main");
      git(work, "merge", "--quiet", "--no-ff", "-m", "Merge pull request #9 from develop", "develop");
      git(work, "push", "--quiet", "origin", "main");
      git(work, "switch", "--quiet", "develop");
    },
  };
  return fx;
}

/** A repository where 0.1.0 of the three components is released and tagged. */
function released(t) {
  const fx = fixture(t);
  const branch = "release/pi-0.1.0_cloud-0.1.0_web-0.1.0";
  assert.equal(fx.run(["prepare", ...ALL]).status, 0);
  fx.squashIntoDevelop(branch, "Release : pi-0.1.0, cloud-0.1.0, web-0.1.0 (#5)");
  assert.equal(fx.run(["pr"]).status, 0);
  fx.mergeIntoMain();
  assert.equal(fx.run(["tag"]).status, 0);
  return fx;
}

function sectionOf(text, tag) {
  const start = text.indexOf(`## ${tag} (`);
  assert.notEqual(start, -1, `no section ${tag}`);
  const next = text.indexOf("\n## ", start + 1);
  return text.slice(start, next === -1 ? undefined : next).trim();
}

function refuses(result, message) {
  assert.equal(result.status, 1, result.stdout + result.stderr);
  assert.match(result.stderr, message);
}

// ---------------------------------------------------------------------------
// EX-1: the script writes the version files and puts the tags on main
// ---------------------------------------------------------------------------

test("EX-1 prepare writes the three version files, the Convex constant and the lock file", (t) => {
  const fx = fixture(t);
  const result = fx.run(["prepare", ...ALL]);
  assert.equal(result.status, 0, result.stderr);

  const branch = "origin/release/pi-0.1.0_cloud-0.1.0_web-0.1.0";
  const show = (path) => git(fx.work, "show", `${branch}:${path}`);
  assert.equal(show("raspberry-pi/VERSION"), "pi-0.1.0");
  assert.equal(show("convex/VERSION"), "cloud-0.1.0");
  assert.match(show("convex/cloudVersion.ts"), /^export const CLOUD_VERSION = "cloud-0\.1\.0";$/m);
  assert.equal(JSON.parse(show("package.json")).version, "0.1.0");
  const lock = JSON.parse(show("package-lock.json"));
  assert.equal(lock.version, "0.1.0");
  assert.equal(lock.packages[""].version, "0.1.0");
  assert.equal(lock.packages["node_modules/left"].version, "0.1.0");

  assert.equal(git(fx.work, "log", "-1", "--format=%s", branch), "Release : pi-0.1.0, cloud-0.1.0, web-0.1.0");
  assert.deepEqual(
    git(fx.work, "diff", "--name-only", "origin/develop", branch).split("\n").sort(),
    ["CHANGELOG.md", "convex/VERSION", "convex/cloudVersion.ts", "package-lock.json", "package.json", "raspberry-pi/VERSION"],
  );
});

test("EX-1 prepare leaves the files of a component that is not released untouched", (t) => {
  const fx = fixture(t);
  assert.equal(fx.run(["prepare", "--web", "0.1.0", "--date", DATE]).status, 0);
  assert.deepEqual(
    git(fx.work, "diff", "--name-only", "origin/develop", "origin/release/web-0.1.0").split("\n").sort(),
    ["CHANGELOG.md", "package-lock.json", "package.json"],
  );
});

test("EX-1 the Convex constant in this repository is the file prepare writes", (t) => {
  const fx = fixture(t);
  assert.equal(fx.run(["prepare", "--cloud", "0.1.0", "--date", DATE]).status, 0);
  const written = git(fx.work, "show", "origin/release/cloud-0.1.0:convex/cloudVersion.ts");
  const committed = real("convex/cloudVersion.ts").trim();
  assert.equal(written.replace('"cloud-0.1.0"', "V"), committed.replace(/"cloud-[^"]*"/, "V"));
});

test("EX-1 tag puts annotated tags on the commit of main, and only there", (t) => {
  const fx = released(t);
  const main = git(fx.origin, "rev-parse", "refs/heads/main");
  for (const tag of ["pi-0.1.0", "cloud-0.1.0", "web-0.1.0"]) {
    assert.equal(git(fx.origin, "cat-file", "-t", `refs/tags/${tag}`), "tag", `${tag} is not annotated`);
    assert.equal(git(fx.origin, "rev-parse", `refs/tags/${tag}^{commit}`), main);
  }
  assert.notEqual(main, git(fx.origin, "rev-parse", "refs/heads/develop"));
  const message = git(fx.origin, "tag", "-l", "--format=%(contents)", "pi-0.1.0");
  assert.match(message, /^pi-0\.1\.0\n\nComposant : Raspberry Pi\./);
  assert.match(message, /Niveau de validation : `bench`\./);
  assert.match(message, /- ANH-10 : régler la rampe du bras \(#1\)/);
});

test("EX-1 tag refuses while main does not carry the release", (t) => {
  const fx = fixture(t);
  assert.equal(fx.run(["prepare", ...ALL]).status, 0);
  fx.squashIntoDevelop("release/pi-0.1.0_cloud-0.1.0_web-0.1.0", "Release : pi-0.1.0, cloud-0.1.0, web-0.1.0 (#5)");
  refuses(fx.run(["tag"]), /aucune version de origin\/main n'attend son tag/);
  assert.equal(git(fx.origin, "tag", "-l"), "");
});

test("EX-1 tag has nothing to do once every version has its tag", (t) => {
  const fx = released(t);
  refuses(fx.run(["tag"]), /rien à faire/);
});

// ---------------------------------------------------------------------------
// EX-2: changelog from the titles, develop green, the PR develop -> main
// ---------------------------------------------------------------------------

test("EX-2 the changelog files each ticket title under the components it touches", (t) => {
  const fx = fixture(t);
  const result = fx.run(["prepare", ...ALL]);
  assert.equal(result.status, 0, result.stderr);
  const changelog = git(fx.work, "show", "origin/release/pi-0.1.0_cloud-0.1.0_web-0.1.0:CHANGELOG.md");

  assert.equal(
    sectionOf(changelog, "pi-0.1.0"),
    [
      `## pi-0.1.0 (${DATE})`,
      "",
      "Composant : Raspberry Pi. Changements depuis : la première version.",
      "Niveau de validation : `bench`.",
      "",
      "- ANH-10 : régler la rampe du bras (#1)",
    ].join("\n"),
  );
  assert.equal(
    sectionOf(changelog, "cloud-0.1.0"),
    [
      `## cloud-0.1.0 (${DATE})`,
      "",
      "Composant : Convex. Changements depuis : la première version.",
      "",
      "- ANH-11 : add the release register (#2)",
      "- ANH-12 : pied de page du site (#3)",
    ].join("\n"),
  );
  assert.equal(
    sectionOf(changelog, "web-0.1.0"),
    [
      `## web-0.1.0 (${DATE})`,
      "",
      "Composant : Site. Changements depuis : la première version.",
      "",
      "- ANH-12 : pied de page du site (#3)",
    ].join("\n"),
  );
  // Neither the docs-only ticket, nor the untitled commit, nor the revert.
  assert.doesNotMatch(changelog, /ANH-13|chore|Revert/);
  // The preamble and its marker survive, sections come right under the marker.
  assert.ok(changelog.startsWith(real("CHANGELOG.md").split(MARKER)[0]));
  assert.match(changelog, new RegExp(`${MARKER}\n\n## pi-0\\.1\\.0 `));
});

test("EX-2 a later release lists only what merged since the component's last tag, above the older sections", (t) => {
  const fx = released(t);
  commit(fx.work, "ANH-30 : nouveau palier de vitesse (#30)", { "raspberry-pi/src/tiers.py": "t = 1\n" });
  git(fx.work, "push", "--quiet", "origin", "develop");

  const result = fx.run(["prepare", "--pi", "0.2.0", "--pi-validation", "auto_validated", "--web", "0.1.1", "--date", "2026-11-02"]);
  assert.equal(result.status, 0, result.stderr);
  const changelog = git(fx.work, "show", "origin/release/pi-0.2.0_web-0.1.1:CHANGELOG.md");
  assert.equal(
    sectionOf(changelog, "pi-0.2.0"),
    [
      "## pi-0.2.0 (2026-11-02)",
      "",
      "Composant : Raspberry Pi. Changements depuis : `pi-0.1.0`.",
      "Niveau de validation : `auto_validated`.",
      "",
      "- ANH-30 : nouveau palier de vitesse (#30)",
    ].join("\n"),
  );
  assert.match(sectionOf(changelog, "web-0.1.1"), /- Aucune PR de ticket ne touche ce composant depuis `web-0\.1\.0`\./);
  assert.ok(changelog.indexOf("## pi-0.2.0") < changelog.indexOf("## pi-0.1.0"));
  assert.equal(git(fx.work, "show", "origin/release/pi-0.2.0_web-0.1.1:convex/VERSION"), "cloud-0.1.0");
});

for (const [name, checks, message] of [
  ["a failed check", "pi-gate\tcompleted\tfailure\nweb\tcompleted\tsuccess\n", /develop n'est pas vert[\s\S]*pi-gate : completed failure/],
  ["a cancelled check", "pi-gate\tcompleted\tcancelled\nweb\tcompleted\tsuccess\n", /develop n'est pas vert/],
  ["a check still running", "pi-gate\tin_progress\t\nweb\tcompleted\tsuccess\n", /develop n'est pas vert[\s\S]*pi-gate : in_progress/],
  ["no check at all", "", /aucune vérification CI pour develop/],
  ["only skipped checks", "pi-gate\tcompleted\tskipped\n", /aucune vérification CI réussie/],
]) {
  test(`EX-2 prepare refuses when develop has ${name}`, (t) => {
    const fx = fixture(t);
    const before = fx.state();
    refuses(fx.run(["prepare", ...ALL], { checks }), message);
    assert.equal(fx.state(), before);
  });
}

test("EX-2 prepare refuses when the CI state cannot be read", (t) => {
  const fx = fixture(t);
  refuses(fx.run(["prepare", ...ALL], { apiExit: "1" }), /lecture des vérifications CI impossible/);
});

test("EX-2 pr and tag refuse a branch that is not green", (t) => {
  const fx = fixture(t);
  assert.equal(fx.run(["prepare", ...ALL]).status, 0);
  fx.squashIntoDevelop("release/pi-0.1.0_cloud-0.1.0_web-0.1.0", "Release : pi-0.1.0, cloud-0.1.0, web-0.1.0 (#5)");
  const red = "web\tcompleted\tfailure\n";
  const blocked = fx.run(["pr"], { checks: red });
  refuses(blocked, /develop n'est pas vert/);
  assert.deepEqual(blocked.gh.filter((call) => call.startsWith("pr ")), []);

  fx.mergeIntoMain();
  refuses(fx.run(["tag"], { checks: red }), /main n'est pas vert/);
  assert.equal(git(fx.origin, "tag", "-l"), "");
});

test("EX-2 pr opens develop -> main with the release template filled", (t) => {
  const fx = fixture(t);
  assert.equal(fx.run(["prepare", ...ALL]).status, 0);
  fx.squashIntoDevelop("release/pi-0.1.0_cloud-0.1.0_web-0.1.0", "Release : pi-0.1.0, cloud-0.1.0, web-0.1.0 (#5)");
  const candidate = git(fx.work, "rev-parse", "origin/develop");

  const result = fx.run(["pr"]);
  assert.equal(result.status, 0, result.stderr);
  const create = result.gh.filter((call) => call.startsWith("pr create"));
  assert.equal(create.length, 1);
  assert.match(create[0], /^pr create --base main --head develop --title Release : pi-0\.1\.0, cloud-0\.1\.0, web-0\.1\.0 --body-file /);

  assert.match(result.body, /\| Raspberry Pi \| `pi-0\.1\.0` \| `bench` \|/);
  assert.match(result.body, /\| Convex \| `cloud-0\.1\.0` \| sans objet \|/);
  assert.match(result.body, /\| Site \| `web-0\.1\.0` \| sans objet \|/);
  assert.ok(result.body.includes(candidate), "the candidate SHA is in the body");
  assert.match(result.body, /- ANH-10 : régler la rampe du bras \(#1\)/);
  assert.doesNotMatch(result.body, /<!-- release\.sh/);
  assert.deepEqual(checklist(result.body), checklist(real(TEMPLATE)));
});

test("EX-2 pr refuses when no version of develop awaits its tag", (t) => {
  const fx = fixture(t);
  refuses(fx.run(["pr"]), /rien à faire/);
});

test("EX-2 a dry run of each step writes nothing, pushes nothing, creates no PR and no tag", (t) => {
  const fx = fixture(t);
  let before = fx.state();
  const prepare = fx.run(["prepare", ...ALL, "--dry-run"]);
  assert.equal(prepare.status, 0, prepare.stderr);
  assert.equal(fx.state(), before);
  assert.ok(prepare.gh.every((call) => call.startsWith("api ")), prepare.gh.join("\n"));
  // The dry run says everything: files, sections, the two PRs, the tags.
  for (const expected of [
    /simulation, rien n'est écrit, poussé ni créé/,
    /raspberry-pi\/VERSION : pi-0\.0\.0-dev -> pi-0\.1\.0/,
    /convex\/VERSION : cloud-0\.0\.0-dev -> cloud-0\.1\.0/,
    /convex\/cloudVersion\.ts : CLOUD_VERSION = "cloud-0\.1\.0"/,
    /package\.json, package-lock\.json : version 0\.0\.0-dev -> 0\.1\.0/,
    /## cloud-0\.1\.0 \(2026-10-07\)/,
    /gh pr create --base develop --head release\/pi-0\.1\.0_cloud-0\.1\.0_web-0\.1\.0 --title "Release : pi-0\.1\.0, cloud-0\.1\.0, web-0\.1\.0"/,
    /gh pr create --base main --head develop --title "Release : pi-0\.1\.0, cloud-0\.1\.0, web-0\.1\.0"/,
    /\*\*Gates vertes\.\*\*/,
    /git tag -a pi-0\.1\.0 <commit de main>/,
    /git push origin pi-0\.1\.0 cloud-0\.1\.0 web-0\.1\.0/,
  ]) {
    assert.match(prepare.stdout, expected);
  }

  assert.equal(fx.run(["prepare", ...ALL]).status, 0);
  fx.squashIntoDevelop("release/pi-0.1.0_cloud-0.1.0_web-0.1.0", "Release : pi-0.1.0, cloud-0.1.0, web-0.1.0 (#5)");
  before = fx.state();
  const pr = fx.run(["pr", "--dry-run"]);
  assert.equal(pr.status, 0, pr.stderr);
  assert.equal(fx.state(), before);
  assert.ok(pr.gh.every((call) => call.startsWith("api ")));
  assert.match(pr.stdout, /gh pr create --base main --head develop/);

  fx.mergeIntoMain();
  before = fx.state();
  const tag = fx.run(["tag", "--dry-run"]);
  assert.equal(tag.status, 0, tag.stderr);
  assert.equal(fx.state(), before);
  assert.match(tag.stdout, new RegExp(`git tag -a web-0\\.1\\.0 ${git(fx.work, "rev-parse", "origin/main")}`));
  assert.match(tag.stdout, /\{"component":"pi","version":"pi-0\.1\.0","validationLevel":"bench","releasedAt":\d{13}\}/);
  assert.match(tag.stdout, /\{"component":"web","version":"web-0\.1\.0","releasedAt":\d{13}\}/);
});

for (const [name, args, message] of [
  ["no version at all", ["--date", DATE], /indiquer au moins une version/],
  ["a version that is not X.Y.Z", ["--web", "0.1"], /version invalide pour web : '0\.1'/],
  ["a version with a suffix", ["--web", "0.1.0-rc1"], /version invalide pour web/],
  ["a version with a leading zero", ["--cloud", "01.0.0"], /version invalide pour cloud/],
  ["a version carrying its prefix", ["--cloud", "cloud-0.1.0"], /version invalide pour cloud/],
  ["a date that is not a date", ["--web", "0.1.0", "--date", "7 octobre"], /date invalide/],
  ["an unknown option", ["--web", "0.1.0", "--force"], /option inconnue : --force/],
]) {
  test(`EX-2 prepare refuses ${name}`, (t) => {
    const fx = fixture(t);
    const before = fx.state();
    refuses(fx.run(["prepare", ...args]), message);
    assert.equal(fx.state(), before);
  });
}

test("EX-2 prepare refuses a version that is not above the released one, or already tagged", (t) => {
  const fx = released(t);
  refuses(fx.run(["prepare", "--web", "0.1.0", "--date", DATE]), /web-0\.1\.0 n'est pas supérieure à la version courante web-0\.1\.0/);
  refuses(fx.run(["prepare", "--web", "0.0.9", "--date", DATE]), /n'est pas supérieure/);
  git(fx.work, "tag", "-a", "-m", "stray", "web-0.2.0", "origin/main");
  refuses(fx.run(["prepare", "--web", "0.2.0", "--date", DATE]), /le tag web-0\.2\.0 existe déjà/);
  assert.equal(fx.run(["prepare", "--web", "0.10.0", "--date", DATE, "--dry-run"]).status, 0);
});

test("EX-2 prepare refuses a new release while the previous one has no tag", (t) => {
  const fx = fixture(t);
  assert.equal(fx.run(["prepare", ...ALL]).status, 0);
  fx.squashIntoDevelop("release/pi-0.1.0_cloud-0.1.0_web-0.1.0", "Release : pi-0.1.0, cloud-0.1.0, web-0.1.0 (#5)");
  refuses(
    fx.run(["prepare", "--pi", "0.2.0", "--pi-validation", "bench", "--date", DATE]),
    /pi-0\.1\.0 n'a pas de tag : terminer la release précédente/,
  );
});

test("EX-2 prepare refuses to start over a modified work tree", (t) => {
  const fx = fixture(t);
  write(fx.work, "raspberry-pi/VERSION", "pi-9.9.9\n");
  const result = fx.run(["prepare", ...ALL]);
  refuses(result, /l'arbre de travail a des modifications/);
  assert.equal(git(fx.origin, "for-each-ref", "refs/heads/release"), "");
  assert.deepEqual(result.gh.filter((call) => call.startsWith("pr ")), []);
});

test("EX-2 the branch names can only be overridden in a dry run", (t) => {
  const fx = fixture(t);
  const before = fx.state();
  refuses(fx.run(["prepare", ...ALL], { env: { RELEASE_DEVELOP: "main" } }), /ne servent qu'avec --dry-run/);
  refuses(fx.run(["tag"], { env: { RELEASE_MAIN: "develop" } }), /ne servent qu'avec --dry-run/);
  assert.equal(fx.state(), before);
  assert.equal(fx.run(["prepare", ...ALL, "--dry-run"], { env: { RELEASE_DEVELOP: "develop" } }).status, 0);
});

// ---------------------------------------------------------------------------
// EX-4: the validation level of a Pi version is recorded in the changelog
// ---------------------------------------------------------------------------

for (const level of ["bench", "auto_validated", "occupied_validated"]) {
  test(`EX-4 the changelog records the validation level ${level} of a Pi version`, (t) => {
    const fx = fixture(t);
    const result = fx.run(["prepare", "--pi", "0.1.0", "--pi-validation", level, "--date", DATE, "--dry-run"]);
    assert.equal(result.status, 0, result.stderr);
    assert.match(result.stdout, new RegExp(`^Niveau de validation : \`${level}\`\\.$`, "m"));
  });
}

for (const [name, args, message] of [
  ["a Pi version without a validation level", ["--pi", "0.1.0"], /une version du Pi exige --pi-validation/],
  ["an unknown validation level", ["--pi", "0.1.0", "--pi-validation", "validated"], /niveau de validation inconnu : 'validated'/],
  ["a validation level without a Pi version", ["--web", "0.1.0", "--pi-validation", "bench"], /--pi-validation ne s'applique qu'avec --pi/],
]) {
  test(`EX-4 prepare refuses ${name}`, (t) => {
    const fx = fixture(t);
    const before = fx.state();
    refuses(fx.run(["prepare", ...args, "--date", DATE]), message);
    assert.equal(fx.state(), before);
  });
}

test("EX-4 pr refuses a Pi section whose validation level was removed", (t) => {
  const fx = fixture(t);
  assert.equal(fx.run(["prepare", ...ALL]).status, 0);
  const branch = "release/pi-0.1.0_cloud-0.1.0_web-0.1.0";
  const changelog = readFileSync(join(fx.work, "CHANGELOG.md"), "utf8");
  commit(fx.work, "retire le niveau", { "CHANGELOG.md": changelog.replace("Niveau de validation : `bench`.\n", "") });
  git(fx.work, "push", "--quiet", "origin", branch);
  fx.squashIntoDevelop(branch, "Release : pi-0.1.0, cloud-0.1.0, web-0.1.0 (#5)");
  refuses(fx.run(["pr"]), /section pi-0\.1\.0 sans niveau de validation reconnu/);
});

// ---------------------------------------------------------------------------
// The real files of this repository
// ---------------------------------------------------------------------------

/** The check-list items of a Markdown text, each on one line. */
function checklist(text) {
  const items = [];
  let open = false;
  for (const line of text.split("\n")) {
    if (line.startsWith("- [ ] ")) {
      items.push(line.slice(6).trim());
      open = true;
    } else if (open && /^ {2}\S/.test(line)) {
      items[items.length - 1] += ` ${line.trim()}`;
    } else {
      open = false;
    }
  }
  return items;
}

test("EX-1 each component of this repository carries a prefixed semantic version", () => {
  const version = /^\d+\.\d+\.\d+(-dev)?$/;
  assert.match(real("raspberry-pi/VERSION"), /^pi-\d+\.\d+\.\d+(-dev)?\n$/);
  assert.match(real("convex/VERSION"), /^cloud-\d+\.\d+\.\d+(-dev)?\n$/);
  assert.match(JSON.parse(real("package.json")).version, version);
});

test("EX-1 the Convex constant and the lock file agree with the version files", () => {
  const cloud = real("convex/VERSION").trim();
  assert.ok(real("convex/cloudVersion.ts").includes(`export const CLOUD_VERSION = "${cloud}";`));
  const web = JSON.parse(real("package.json")).version;
  const lock = JSON.parse(real("package-lock.json"));
  assert.equal(lock.version, web);
  assert.equal(lock.packages[""].version, web);
});

test("EX-1 the site footer shows the version built from package.json", () => {
  const config = real("next.config.ts");
  assert.match(config, /import packageJson from "\.\/package\.json";/);
  assert.match(config, /NEXT_PUBLIC_WEB_VERSION: `web-\$\{packageJson\.version\}`/);
  assert.match(real("lib/version.ts"), /export const WEB_VERSION: string =\s+process\.env\.NEXT_PUBLIC_WEB_VERSION \?\?/);
  const footer = real("components/landing/Footer.tsx");
  assert.match(footer, /import \{ WEB_VERSION \} from "@\/lib\/version";/);
  assert.match(footer, />\{WEB_VERSION\}</);
});

test("EX-2 CHANGELOG.md keeps the line release.sh inserts under", () => {
  assert.equal(real("CHANGELOG.md").split("\n").filter((line) => line === MARKER).length, 1);
});

test("EX-3 the release PR template carries the check-list of docs/release.md, word for word", () => {
  const template = checklist(real(TEMPLATE));
  assert.deepEqual(template, checklist(real("docs/release.md")));
  assert.equal(template.length, 9);
  for (const required of [
    "Gates vertes",
    "Docs à jour",
    "docs/menaces.md",
    "Matrice de compatibilité",
    "simulation.quick --all --dsp",
    "simulation/scenarios/real/",
    "[MED]",
    "PROGRAMS_ENABLED",
    "OCCUPANCY_OCCUPIED_ENABLED",
    "Niveau de validation du Pi",
  ]) {
    assert.ok(template.some((item) => item.includes(required)), `no check-list item about ${required}`);
  }
});

test("EX-3 the release PR template keeps the two places release.sh fills", () => {
  const lines = real(TEMPLATE).split("\n");
  for (const slot of ["<!-- release.sh : versions -->", "<!-- release.sh : changelog -->"]) {
    assert.equal(lines.filter((line) => line === slot).length, 1, slot);
  }
});

test("EX-5 docs/release.md states which machine receives which version, and what will apply it", () => {
  const doc = real("docs/release.md");
  assert.match(doc, /\| séances programmées \(`auto`, M5\) \| `auto_validated` ou `occupied_validated` \|/);
  assert.match(doc, /\| personne à bord \(`occupied`, M6\) \| `occupied_validated` seulement \|/);
  for (const enforcer of ["releaseAllowedOnMachine", "ANH-147", "ANH-116"]) assert.ok(doc.includes(enforcer), enforcer);
});

test("EX-6 docs/roadmap.md and the docs index point to docs/release.md", () => {
  assert.match(real("docs/roadmap.md"), /\]\(release\.md\)/);
  assert.match(real("docs/README.md"), /\[release\.md\]\(release\.md\)/);
});
