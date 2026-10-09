/**
 * ANH-134: tests of the release process.
 *
 * Each test of `scripts/release.sh` builds a throwaway git repository with its
 * own bare `origin` and replaces `gh` with a double that answers the CI state
 * and records what it is asked to create. Nothing here reaches GitHub or the
 * real repository. The last block checks the real files of this repository.
 */
import assert from "node:assert/strict";
import { execFileSync, spawn, spawnSync } from "node:child_process";
import { once } from "node:events";
import {
  appendFileSync,
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
import { setTimeout as pause } from "node:timers/promises";
import { fileURLToPath } from "node:url";
import test from "node:test";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const SCRIPT = join(ROOT, "scripts", "release.sh");
const TEMPLATE = ".github/PULL_REQUEST_TEMPLATE/release.md";
const MARKER = "<!-- release.sh : nouvelles sections sous cette ligne -->";
const DATE = "2026-10-07";
const ALL = ["--pi", "0.1.0", "--cloud", "0.1.0", "--web", "0.1.0", "--pi-validation", "bench", "--date", DATE];
const GATES = ["pi-gate", "simulation-gate", "convex-tests", "web", "audit", "docs"];
const VERCEL = "Vercel Preview Comments\tcompleted\tsuccess\n";

/**
 * What the CI answers for a commit: the six gates, each "completed success"
 * unless `overrides` says otherwise (`null` leaves a gate out), then `extra`.
 */
function ci(overrides = {}, extra = VERCEL) {
  const lines = GATES.filter((gate) => overrides[gate] !== null).map(
    (gate) => `${gate}\t${overrides[gate] ?? "completed\tsuccess"}\n`,
  );
  return lines.join("") + extra;
}
const GREEN = ci();

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

/**
 * What makes a throwaway repository quiet: git starts nothing in it that
 * outlives the command that was run.
 *
 * After a commit, a merge or a fetch, and after a push in the repository that
 * receives it, git runs `git maintenance run --auto --detach`: a process meant
 * to go on working under `objects/` once the command has returned. The cleanup
 * of a test then removed a directory something could still write to, and now
 * and then failed with ENOTEMPTY on `origin.git/objects` (ANH-183). These
 * repositories live for one test: they need no maintenance.
 *
 * Written in the configuration of each repository, not handed through the
 * environment: what GIT_CONFIG_COUNT and the like say does not reach the
 * repository a local push writes to.
 */
const QUIET = `[gc]
\tauto = 0
\tautoDetach = false
[maintenance]
\tauto = false
\tautoDetach = false
[receive]
\tautogc = false
`;

/** @param {string} gitDir the directory of a repository that was just created: `.git`, or a bare one */
function quiet(gitDir) {
  appendFileSync(join(gitDir, "config"), QUIET);
}

/** What the removal of a directory answers while something still writes in it. */
const STILL_WRITTEN = new Set(["ENOTEMPTY", "EBUSY", "EPERM", "EEXIST"]);

/** How many times a throwaway directory is emptied before its removal is given up: 3.3 s in all. */
const REMOVALS = 12;

/**
 * Remove a throwaway directory, whatever is left in it. Nothing should still
 * be writing there (see QUIET). Should something be, a directory that gains an
 * entry while it is being emptied is emptied again, a little later each time,
 * instead of failing the test that used it.
 *
 * The whole removal is done again at each try, here, and not by the
 * `maxRetries` of `rmSync`: depending on the version of Node, that option only
 * repeats the last `rmdir`, which an entry that came late fails every time
 * (seen on the CI: ENOTEMPTY after its ten tries).
 */
async function remove(dir) {
  for (let attempt = 1; ; attempt += 1) {
    try {
      rmSync(dir, { recursive: true, force: true });
      return;
    } catch (error) {
      if (attempt >= REMOVALS || !STILL_WRITTEN.has(error?.code)) throw error;
      await pause(attempt * 50);
    }
  }
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

/**
 * The double of `gh`. It answers the check runs of any commit with
 * `FAKE_GH_CHECKS`, and the commit statuses of a commit with the file named
 * after its SHA in `FAKE_GH_STATUSES` (no file: the commit carries no status).
 * `gh pr list` answers the address of the open pull request, if any.
 */
const FAKE_GH = `#!/bin/sh
printf '%s\\n' "$*" >>"$FAKE_GH_LOG"
if [ "$1" = api ]; then
  case "$2" in
    */check-runs)
      printf '%s' "$FAKE_GH_CHECKS"
      exit "\${FAKE_GH_API_EXIT:-0}"
      ;;
    */status)
      sha="\${2%/status}"
      if [ -f "$FAKE_GH_STATUSES/\${sha##*/}" ]; then cat "$FAKE_GH_STATUSES/\${sha##*/}"; fi
      exit "\${FAKE_GH_STATUS_EXIT:-0}"
      ;;
  esac
  exit 64
fi
if [ "$1 $2" = "pr list" ]; then
  printf '%s' "$FAKE_GH_OPEN_PR"
  exit 0
fi
if [ "$1 $2" = "pr create" ]; then
  if [ "\${FAKE_GH_CREATE_EXIT:-0}" != 0 ]; then
    echo "gh: the pull request could not be created" >&2
    exit "$FAKE_GH_CREATE_EXIT"
  fi
  while [ $# -gt 0 ]; do
    if [ "$1" = --body-file ]; then cat "$2" >"$FAKE_GH_LOG.body"; fi
    shift
  done
  echo "https://github.invalid/anheart/pull/1"
fi
`;

/** The commit status the independent review leaves on the commit it read. */
const REVIEW = "agent-review/R1";

/** History of `develop` before any release: titles as squash merges leave them. */
const HISTORY = [
  ["ANH-10 : régler la rampe du bras (#1)", { "raspberry-pi/src/ramp.py": "ramp = 1\n" }],
  ["ANH-11: add the release register (#2)", { "convex/register.ts": "export {};\n" }],
  ["ANH-12 : pied de page du site (#3)", { "components/Footer.tsx": "// footer\n", "convex/footer.ts": "export {};\n" }],
  ["ANH-13 : guide de démarrage (#4)", { "docs/guide.md": "# Guide\n" }],
  ["chore: bump a dependency", { "app/page.tsx": "// page\n" }],
];

/**
 * A repository as it stands after this ticket: development versions on `main`
 * and `develop`, then `history` squash-merged on `develop`.
 */
function fixture(t, history = HISTORY) {
  const dir = mkdtempSync(join(tmpdir(), "anheart-release-"));
  t.after(() => remove(dir));
  const origin = join(dir, "origin.git");
  const work = join(dir, "work");
  const bin = join(dir, "bin");
  const log = join(dir, "gh.log");
  const statuses = join(dir, "statuses");
  mkdirSync(bin);
  mkdirSync(statuses);
  writeFileSync(join(bin, "gh"), FAKE_GH);
  chmodSync(join(bin, "gh"), 0o755);

  git(dir, "init", "--quiet", "--bare", "--initial-branch=main", origin);
  git(dir, "init", "--quiet", "--initial-branch=main", work);
  quiet(origin);
  quiet(join(work, ".git"));
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
    /**
     * Run release.sh in the work clone. `checks` is what the CI answers;
     * `createExit` makes `gh pr create` fail; `openPr` is the address of a
     * pull request already open for the branch.
     */
    run(args, { checks = GREEN, apiExit = "0", statusExit = "0", createExit = "0", openPr = "", env = {} } = {}) {
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
          FAKE_GH_STATUSES: statuses,
          FAKE_GH_STATUS_EXIT: statusExit,
          FAKE_GH_CREATE_EXIT: createExit,
          FAKE_GH_OPEN_PR: openPr,
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
    /** What merging a ticket PR by squash leaves on `develop`. */
    mergeTicket(subject, files) {
      git(work, "switch", "--quiet", "develop");
      commit(work, subject, files);
      git(work, "push", "--quiet", "origin", "develop");
    },
    /** What merging the preparation PR by squash leaves on `develop`. */
    squashIntoDevelop(branch, subject) {
      git(work, "switch", "--quiet", "develop");
      git(work, "merge", "--quiet", "--squash", branch);
      git(work, "commit", "--quiet", "-m", subject);
      git(work, "push", "--quiet", "origin", "develop");
    },
    /**
     * The commit statuses of a commit, as `name<TAB>state` lines (GitHub keeps
     * the latest of each name). An empty text leaves the commit without any.
     */
    setStatuses(sha, lines) {
      writeFileSync(join(statuses, sha), lines);
    },
    /**
     * What merging the release PR with a merge commit leaves on `main`. The
     * protection of `main` lets it through only when the head of `develop`
     * carries the independent review: `reviewed: false` is a merge made with
     * that rule lifted.
     */
    mergeIntoMain({ reviewed = true } = {}) {
      if (reviewed) fx.setStatuses(git(work, "rev-parse", "develop"), `${REVIEW}\tsuccess\n`);
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
  assertTagsComplete(fx);
  return fx;
}

function sectionOf(text, tag) {
  const start = text.indexOf(`## ${tag} (`);
  assert.notEqual(start, -1, `no section ${tag}`);
  const next = text.indexOf("\n## ", start + 1);
  return text.slice(start, next === -1 ? undefined : next).trim();
}

/** True when a git command that answers by its exit status says yes. */
function gitSays(cwd, ...args) {
  return spawnSync("git", args, { cwd, env: GIT_ENV }).status === 0;
}

const WEB_ROOT_FILES = ["proxy.ts", "next.config.ts", "package.json", "package-lock.json", "postcss.config.mjs", "components.json", "tsconfig.json"];
const TOUCHES = {
  pi: (path) => path.startsWith("raspberry-pi/"),
  cloud: (path) => path.startsWith("convex/"),
  web: (path) => /^(app|components|hooks|i18n|lib|messages|public)\//.test(path) || WEB_ROOT_FILES.includes(path),
};
const versionOf = (tag) => tag.split("-")[1].split(".").map(Number);
const isOlder = (a, b) => {
  const [x, y] = [versionOf(a), versionOf(b)];
  const at = x.findIndex((part, index) => part !== y[index]);
  return at !== -1 && x[at] < y[at];
};

/**
 * The invariant of a release, computed here without release.sh: every ticket
 * of `develop` that a tag contains, that touches the tag's component and that
 * no older tag of the component contains, is cited by the tag message and by
 * the section of CHANGELOG.md at the tagged commit.
 */
function assertTagsComplete(fx) {
  const tags = git(fx.origin, "tag", "-l").split("\n").filter(Boolean);
  assert.notEqual(tags.length, 0, "no tag to check");
  const chain = git(fx.origin, "log", "--first-parent", "--format=%H%x09%s", "refs/heads/develop")
    .split("\n")
    .map((line) => line.split("\t"));
  for (const tag of tags) {
    const component = tag.split("-")[0];
    const older = tags.filter((other) => other.startsWith(`${component}-`) && isOlder(other, tag));
    const message = git(fx.origin, "tag", "-l", "--format=%(contents)", tag).split("\n");
    const section = sectionOf(git(fx.origin, "show", `${tag}:CHANGELOG.md`), tag).split("\n");
    for (const [hash, subject] of chain) {
      const ticket = /^(ANH-\d+) ?: *(.*)$/.exec(subject);
      if (!ticket) continue;
      if (!gitSays(fx.origin, "merge-base", "--is-ancestor", hash, `${tag}^{commit}`)) continue;
      if (older.some((other) => gitSays(fx.origin, "merge-base", "--is-ancestor", hash, `${other}^{commit}`))) continue;
      const files = git(fx.origin, "show", "--name-only", "--format=", hash).split("\n");
      if (!files.some(TOUCHES[component])) continue;
      const line = `- ${ticket[1]} : ${ticket[2]}`;
      assert.ok(message.includes(line), `${tag} is on a commit that contains "${subject}", absent from the tag message`);
      assert.ok(section.includes(line), `${tag} is on a commit that contains "${subject}", absent from its section`);
    }
  }
}

function refuses(result, message) {
  assert.equal(result.status, 1, result.stdout + result.stderr);
  assert.match(result.stderr, message);
}

// ---------------------------------------------------------------------------
// The throwaway repositories themselves (ANH-183): quiet, and removed for good
// ---------------------------------------------------------------------------

test("a throwaway repository starts no maintenance, on either side of a push", (t) => {
  const fx = fixture(t);
  /** Run a git command of the tests with git's own trace of what it starts, on both sides. */
  const traced = (...args) => {
    const done = spawnSync("git", args, { cwd: fx.work, env: { ...GIT_ENV, GIT_TRACE: "1" }, encoding: "utf8" });
    assert.equal(done.status, 0, done.stderr);
    return done.stderr;
  };
  write(fx.work, "docs/note.md", "# Note\n");
  git(fx.work, "add", "-A");
  const started = [
    traced("commit", "--quiet", "-m", "ANH-99 : une note (#99)"),
    traced("push", "--quiet", "origin", "develop"),
    traced("fetch", "--quiet", "--tags", "origin"),
    traced("merge", "--quiet", "--no-ff", "-m", "Merge", "origin/main"),
  ].join("\n");
  // The trace is the one of both repositories: the push was received by origin.
  assert.match(started, /run_command: .*git-receive-pack/);
  assert.doesNotMatch(started, /git[- ](maintenance|gc)\b/);
  // And git reads the settings where they were written, in the two repositories.
  for (const repo of [fx.origin, fx.work]) {
    assert.equal(git(repo, "config", "--local", "--get", "maintenance.auto"), "false");
    assert.equal(git(repo, "config", "--local", "--get", "gc.auto"), "0");
    assert.equal(git(repo, "config", "--local", "--get", "receive.autogc"), "false");
  }
  // release.sh runs its own git commands in the same repositories: nothing of it is left behind either.
  const run = fx.run(["prepare", ...ALL], { env: { GIT_TRACE: "1" } });
  assert.equal(run.status, 0, run.stderr);
  assert.match(run.stderr, /run_command: .*git-receive-pack/);
  assert.doesNotMatch(run.stderr, /git[- ](maintenance|gc)\b/);
});

test("a throwaway directory is removed even when an entry appears while it is being emptied", async () => {
  const dir = mkdtempSync(join(tmpdir(), "anheart-release-"));
  const objects = join(dir, "origin.git", "objects");
  mkdirSync(objects, { recursive: true });
  // What a process left behind would do: go on writing under objects/ for a third of a second.
  const writer = spawn(
    process.execPath,
    [
      "-e",
      `const { writeFileSync } = require("node:fs");
       const until = Date.now() + 300;
       for (let n = 0; Date.now() < until; n += 1) {
         try { writeFileSync(process.argv[1] + "/late-" + n, ""); } catch { break; }
       }`,
      objects,
    ],
    { stdio: "ignore" },
  );
  const over = once(writer, "exit");
  while (!existsSync(join(objects, "late-0")) && writer.exitCode === null) {
    await new Promise((resolve) => setTimeout(resolve, 2));
  }
  assert.ok(existsSync(join(objects, "late-0")), "the writer never wrote");
  await remove(dir);
  await over;
  assert.ok(!existsSync(dir), "the directory is still there");
});

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

test("EX-1 tag refuses a main that was squashed instead of merged", (t) => {
  const fx = fixture(t);
  assert.equal(fx.run(["prepare", ...ALL]).status, 0);
  fx.squashIntoDevelop("release/pi-0.1.0_cloud-0.1.0_web-0.1.0", "Release : pi-0.1.0, cloud-0.1.0, web-0.1.0 (#5)");
  git(fx.work, "switch", "--quiet", "main");
  git(fx.work, "merge", "--quiet", "--squash", "develop");
  git(fx.work, "commit", "--quiet", "-m", "Release (#9)");
  git(fx.work, "push", "--quiet", "origin", "main");
  git(fx.work, "switch", "--quiet", "develop");

  refuses(fx.run(["tag"]), /fusionnée en squash \(elle doit l'être par commit de fusion\)/);
  assert.equal(git(fx.origin, "tag", "-l"), "");
  assert.equal(git(fx.work, "tag", "-l"), "");
});

test("EX-1 tag keeps no local tag when origin refuses them", (t) => {
  const fx = fixture(t);
  assert.equal(fx.run(["prepare", ...ALL]).status, 0);
  fx.squashIntoDevelop("release/pi-0.1.0_cloud-0.1.0_web-0.1.0", "Release : pi-0.1.0, cloud-0.1.0, web-0.1.0 (#5)");
  fx.mergeIntoMain();
  const hook = join(fx.origin, "hooks", "pre-receive");
  writeFileSync(hook, "#!/bin/sh\nexit 1\n");
  chmodSync(hook, 0o755);

  refuses(fx.run(["tag"]), /les tags n'ont pas pu être poussés/);
  assert.equal(git(fx.origin, "tag", "-l"), "");
  assert.equal(git(fx.work, "tag", "-l"), "");

  rmSync(hook);
  assert.equal(fx.run(["tag"]).status, 0);
  assert.equal(git(fx.origin, "tag", "-l"), "cloud-0.1.0\npi-0.1.0\nweb-0.1.0");
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

test("EX-2 no tag lands on a commit that carries a component ticket missing from its section, when develop moves during the release", (t) => {
  const fx = fixture(t);
  const title = "Release : pi-0.1.0, cloud-0.1.0, web-0.1.0";
  const completed = (result) => {
    assert.equal(result.status, 0, result.stderr);
    const create = result.gh.filter((call) => call.startsWith("pr create"));
    assert.equal(create.length, 1);
    const head = /^pr create --base develop --head (release\/pi-0\.1\.0_cloud-0\.1\.0_web-0\.1\.0-changelog-[0-9a-f]+) --title Release : pi-0\.1\.0, cloud-0\.1\.0, web-0\.1\.0 \(changelog complété\) --body-file /.exec(create[0]);
    assert.ok(head, create[0]);
    assert.deepEqual(git(fx.work, "diff", "--name-only", "origin/develop", `origin/${head[1]}`).split("\n"), ["CHANGELOG.md"]);
    return head[1];
  };
  assert.equal(fx.run(["prepare", ...ALL]).status, 0);

  // First interleaving: a ticket is merged after prepare read develop and
  // before the preparation PR is merged.
  fx.mergeTicket("ANH-40 : nouvelle rampe d'arrêt (#40)", { "raspberry-pi/src/stop.py": "stop = 1\n" });
  fx.squashIntoDevelop("release/pi-0.1.0_cloud-0.1.0_web-0.1.0", `${title} (#5)`);
  for (const args of [["pr"], ["pr", "--dry-run"]]) {
    const blocked = fx.run(args);
    refuses(blocked, /contient des PR de ticket que CHANGELOG\.md ne cite pas :\n {2}pi-0\.1\.0 : ANH-40 : nouvelle rampe d'arrêt \(#40\)\n/);
    assert.match(blocked.stderr, /Relancer scripts\/release\.sh prepare avec les mêmes versions/);
    assert.deepEqual(blocked.gh.filter((call) => call.startsWith("pr ")), []);
  }

  // The operator runs prepare again with the same versions (and no date): only
  // the changelog changes, the stale section is rewritten in place.
  const first = completed(fx.run(["prepare", ...ALL.slice(0, -2)]));
  const rewritten = git(fx.work, "show", `origin/${first}:CHANGELOG.md`);
  assert.equal(
    sectionOf(rewritten, "pi-0.1.0"),
    [
      `## pi-0.1.0 (${DATE})`,
      "",
      "Composant : Raspberry Pi. Changements depuis : la première version.",
      "Niveau de validation : `bench`.",
      "",
      "- ANH-10 : régler la rampe du bras (#1)",
      "- ANH-40 : nouvelle rampe d'arrêt (#40)",
    ].join("\n"),
  );
  assert.deepEqual(rewritten.match(/^## \S+/gm), ["## pi-0.1.0", "## cloud-0.1.0", "## web-0.1.0"]);
  assert.doesNotMatch(rewritten, /\n\n\n/);
  fx.squashIntoDevelop(first, `${title} (changelog complété) (#6)`);
  const opened = fx.run(["pr"]);
  assert.equal(opened.status, 0, opened.stderr);
  assert.match(opened.body, /- ANH-40 : nouvelle rampe d'arrêt \(#40\)/);

  // Second interleaving: a ticket is merged after pr and before the release PR
  // is merged, so main receives it without its line.
  fx.mergeTicket("ANH-41 : nouvelle table des versions (#41)", { "convex/versions.ts": "export {};\n" });
  fx.mergeIntoMain();
  for (const args of [["tag"], ["tag", "--dry-run"]]) {
    refuses(fx.run(args), /ne cite pas :\n {2}cloud-0\.1\.0 : ANH-41 : nouvelle table des versions \(#41\)\n/);
    assert.equal(git(fx.origin, "tag", "-l"), "");
    assert.equal(git(fx.work, "tag", "-l"), "");
  }

  // Completing it: prepare again, merge, a new PR to main, merge, then tag.
  const second = completed(fx.run(["prepare", ...ALL]));
  assert.notEqual(second, first);
  fx.squashIntoDevelop(second, `${title} (changelog complété) (#7)`);
  refuses(fx.run(["tag"]), /porte une préparation plus récente/);
  assert.equal(git(fx.origin, "tag", "-l"), "");
  assert.equal(fx.run(["pr"]).status, 0);
  fx.mergeIntoMain();
  const tagged = fx.run(["tag"]);
  assert.equal(tagged.status, 0, tagged.stderr);

  assert.equal(git(fx.origin, "tag", "-l"), "cloud-0.1.0\npi-0.1.0\nweb-0.1.0");
  assertTagsComplete(fx);
  assert.match(git(fx.origin, "tag", "-l", "--format=%(contents)", "pi-0.1.0"), /- ANH-40 : nouvelle rampe d'arrêt \(#40\)/);
  assert.match(git(fx.origin, "tag", "-l", "--format=%(contents)", "cloud-0.1.0"), /- ANH-41 : nouvelle table des versions \(#41\)/);

  // And the next version starts after them.
  fx.mergeTicket("ANH-50 : nouveau palier de vitesse (#50)", { "raspberry-pi/src/tiers.py": "t = 1\n" });
  const next = fx.run(["prepare", "--pi", "0.2.0", "--pi-validation", "bench", "--date", "2026-11-02", "--dry-run"]);
  assert.equal(next.status, 0, next.stderr);
  assert.deepEqual(next.stdout.match(/^- ANH-\d+ .*$/gm).filter((line, index, all) => all.indexOf(line) === index), [
    "- ANH-50 : nouveau palier de vitesse (#50)",
  ]);
});

test("EX-2 the invariant check of these tests does see a tag put on an incomplete section", (t) => {
  const fx = fixture(t);
  assert.equal(fx.run(["prepare", ...ALL]).status, 0);
  fx.mergeTicket("ANH-40 : nouvelle rampe d'arrêt (#40)", { "raspberry-pi/src/stop.py": "stop = 1\n" });
  fx.squashIntoDevelop("release/pi-0.1.0_cloud-0.1.0_web-0.1.0", "Release : pi-0.1.0, cloud-0.1.0, web-0.1.0 (#5)");
  fx.mergeIntoMain();
  // What release.sh refuses to do, done by hand.
  git(fx.work, "tag", "-a", "-m", "pi-0.1.0\n\n- ANH-10 : régler la rampe du bras (#1)", "pi-0.1.0", "origin/main");
  git(fx.work, "push", "--quiet", "origin", "refs/tags/pi-0.1.0");
  assert.throws(() => assertTagsComplete(fx), /pi-0\.1\.0 is on a commit that contains "ANH-40 : nouvelle rampe d'arrêt \(#40\)"/);
});

test("EX-2 prepare run again has nothing to do while the sections cite every ticket", (t) => {
  const fx = fixture(t);
  assert.equal(fx.run(["prepare", ...ALL]).status, 0);
  fx.squashIntoDevelop("release/pi-0.1.0_cloud-0.1.0_web-0.1.0", "Release : pi-0.1.0, cloud-0.1.0, web-0.1.0 (#5)");
  // A ticket that touches no component does not make a section stale.
  fx.mergeTicket("ANH-42 : guide de release (#42)", { "docs/release-guide.md": "# Guide\n" });
  const before = fx.state();
  const again = fx.run(["prepare", ...ALL.slice(0, -2)]);
  refuses(again, /rien à changer/);
  assert.equal(fx.state(), before);
  assert.deepEqual(again.gh.filter((call) => call.startsWith("pr ")), []);
  assert.equal(fx.run(["pr"]).status, 0);
});

for (const [name, checks, message] of [
  ["a failed gate", ci({ "pi-gate": "completed\tfailure" }), /develop n'est pas vert[\s\S]*pi-gate : completed failure/],
  ["a cancelled gate", ci({ "pi-gate": "completed\tcancelled" }), /develop n'est pas vert[\s\S]*pi-gate : completed cancelled/],
  ["a gate still running", ci({ "simulation-gate": "in_progress\t" }), /develop n'est pas vert[\s\S]*simulation-gate : in_progress/],
  ["a failed check that is not a gate", ci({}, "Vercel\tcompleted\tfailure\n"), /develop n'est pas vert[\s\S]*Vercel : completed failure/],
  ["a gate that failed in one run though it passed in another", ci({}, "pi-gate\tcompleted\tfailure\n"), /develop n'est pas vert[\s\S]*pi-gate : completed failure/],
  ["no check at all", "", /aucune vérification CI pour develop/],
  // Seen on the real repository: before the CI starts, a pushed commit carries
  // one successful third-party check and none of the gates.
  ["only a third-party check, the CI never ran", VERCEL, /gates absentes ou non réussies : pi-gate simulation-gate convex-tests web audit docs$/m],
  ["one gate missing", ci({ "simulation-gate": null }), /gates absentes ou non réussies : simulation-gate$/m],
  ["a gate that was skipped", ci({ audit: "completed\tskipped" }), /gates absentes ou non réussies : audit$/m],
]) {
  test(`EX-2 prepare refuses when develop has ${name}`, (t) => {
    const fx = fixture(t);
    const before = fx.state();
    refuses(fx.run(["prepare", ...ALL], { checks }), message);
    assert.equal(fx.state(), before);
  });
}

test("EX-2 a gate skipped by one run of a commit does not hide that another run passed it", (t) => {
  // On a pull request the CI may skip a gate that the changed files cannot
  // affect; on a push every gate runs. The head of develop carries both runs
  // while the release PR is open on it.
  const fx = fixture(t);
  const both = ci({}, `${VERCEL}pi-gate\tcompleted\tskipped\nsimulation-gate\tcompleted\tskipped\n`);
  const result = fx.run(["prepare", ...ALL, "--dry-run"], { checks: both });
  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stdout, /^CI : verte \(9 vérifications terminées, toutes les gates réussies\)$/m);
});

test("EX-2 the gates release.sh requires are the required checks of the CI workflow", () => {
  const required = /^REQUIRED_CHECKS="([^"]+)"$/m.exec(real("scripts/release.sh"));
  assert.ok(required, "REQUIRED_CHECKS not found in scripts/release.sh");
  const names = required[1].split(" ");
  assert.deepEqual(names, GATES);
  const held = /^const REQUIRED = (\[[^\]]+\]);$/m.exec(real("scripts/ci/ci-workflow.test.mjs"));
  assert.ok(held, "REQUIRED not found in scripts/ci/ci-workflow.test.mjs");
  assert.deepEqual(names, JSON.parse(held[1]));
  const workflow = real(".github/workflows/ci.yml");
  for (const name of names) {
    assert.match(workflow, new RegExp(`^ {2}${name}:[ \\t]*$`, "m"), `${name} is not a job of ci.yml`);
  }
});

test("ANH-195 EX-8 the commit status release.sh requires is the one the protection of main and of develop requires", () => {
  const required = /^REQUIRED_STATUSES="([^"]+)"$/m.exec(real("scripts/release.sh"));
  assert.ok(required, "REQUIRED_STATUSES not found in scripts/release.sh");
  assert.deepEqual(required[1].split(" "), [REVIEW]);
  // The record of the two protections, read from GitHub: the six gates and this status.
  assert.match(
    real("docs/deploiement.md"),
    /^\| Vérifications obligatoires \| `pi-gate`, `simulation-gate`, `convex-tests`, `web`, `audit`, `docs` et `agent-review\/R1` \| les sept mêmes \|$/m,
  );
  // And the release documents say that tag requires it, and that no approval stands in for it.
  assert.match(real("docs/release.md"), /`tag`\s+l'exige, sur le commit de `develop` que `main` a reçu/);
  assert.doesNotMatch(real("docs/release.md") + real(TEMPLATE), /deux approbations/i);
});

test("EX-2 prepare refuses when the CI state cannot be read", (t) => {
  const fx = fixture(t);
  refuses(fx.run(["prepare", ...ALL], { apiExit: "1" }), /lecture des vérifications CI impossible/);
});

test("EX-2 pr and tag refuse a branch that is not green", (t) => {
  const fx = fixture(t);
  assert.equal(fx.run(["prepare", ...ALL]).status, 0);
  fx.squashIntoDevelop("release/pi-0.1.0_cloud-0.1.0_web-0.1.0", "Release : pi-0.1.0, cloud-0.1.0, web-0.1.0 (#5)");
  const red = ci({ web: "completed\tfailure" });
  const blocked = fx.run(["pr"], { checks: red });
  refuses(blocked, /develop n'est pas vert/);
  assert.deepEqual(blocked.gh.filter((call) => call.startsWith("pr ")), []);

  fx.mergeIntoMain();
  refuses(fx.run(["tag"], { checks: red }), /main n'est pas vert/);
  // Right after the merge, before the CI of main has registered its gates.
  refuses(fx.run(["tag"], { checks: VERCEL }), /main n'est pas vert[\s\S]*gates absentes ou non réussies/);
  assert.equal(git(fx.origin, "tag", "-l"), "");
});

test("ANH-198 a deployment button run on the head commit is read like any check", (t) => {
  // The jobs of deploy-preview.yml and deploy-production.yml are check runs of
  // the head of the branch they deploy (docs/release.md, section 4).
  const fx = fixture(t);
  assert.equal(fx.run(["prepare", ...ALL]).status, 0);
  fx.squashIntoDevelop("release/pi-0.1.0_cloud-0.1.0_web-0.1.0", "Release : pi-0.1.0, cloud-0.1.0, web-0.1.0 (#5)");
  const preview = ci({}, "Déployer le site (préversion)\tcompleted\tfailure\n");
  refuses(fx.run(["pr"], { checks: preview }), /develop n'est pas vert[\s\S]*Déployer le site \(préversion\) : completed failure/);

  fx.mergeIntoMain();
  /** A run of the production button for the site alone: the simulation, not chosen, is skipped. */
  const button = (/** @type {string} */ site) =>
    ci({}, `Vérifier la demande (production)\tcompleted\tsuccess\nDéployer le site (production)\t${site}\nDéployer la simulation (production)\tcompleted\tskipped\n`);
  // Failed, cancelled, running, or waiting for its approval: tag refuses.
  for (const site of ["completed\tfailure", "completed\tcancelled", "in_progress\t", "waiting\t", "queued\t"]) {
    refuses(fx.run(["tag"], { checks: button(site) }), /main n'est pas vert[\s\S]*Déployer le site \(production\) : /);
  }
  assert.equal(git(fx.origin, "tag", "-l"), "");
  // Succeeded: nothing stands in the way.
  const done = fx.run(["tag", "--dry-run"], { checks: button("completed\tsuccess") });
  assert.equal(done.status, 0, done.stderr);
  assert.match(done.stdout, /^CI : verte \(9 vérifications terminées, toutes les gates réussies\)$/m);
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
    /git push --atomic origin pi-0\.1\.0 cloud-0\.1\.0 web-0\.1\.0/,
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
  ["two validation levels in one", ["--pi", "0.1.0", "--pi-validation", "bench auto_validated"], /niveau de validation inconnu/],
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
// ANH-195 EX-5: a revert carries a ticket title
// ---------------------------------------------------------------------------

const BRANCH = "release/pi-0.1.0_cloud-0.1.0_web-0.1.0";
const TITLE = "Release : pi-0.1.0, cloud-0.1.0, web-0.1.0";
/** The title GitHub proposes for the pull request that reverts the one of ANH-10. */
const REVERT = 'Revert "ANH-10 : régler la rampe du bras"';
const RAMP = "raspberry-pi/src/ramp.py";
const creations = (result) => result.gh.filter((call) => call.startsWith("pr create"));

/** A repository where the preparation of 0.1.0 is merged into `develop`. */
function prepared(t, history = HISTORY) {
  const fx = fixture(t, history);
  const first = fx.run(["prepare", ...ALL]);
  assert.equal(first.status, 0, first.stderr);
  fx.squashIntoDevelop(BRANCH, `${TITLE} (#5)`);
  return fx;
}

test("ANH-195 EX-5 prepare refuses a revert merged under the title GitHub proposes, and names it", (t) => {
  const fx = fixture(t, [...HISTORY, [`${REVERT} (#6)`, { [RAMP]: "ramp = 0\n" }]]);
  const short = git(fx.work, "rev-parse", "--short", "develop");
  const before = fx.state();

  for (const args of [
    ["prepare", ...ALL],
    ["prepare", ...ALL, "--dry-run"],
  ]) {
    const refused = fx.run(args);
    refuses(
      refused,
      new RegExp(
        `un revert sans titre de ticket touche pi \\(Raspberry Pi\\) dans origin/develop :\\n {2}${short} Revert "ANH-10 : régler la rampe du bras" \\(#6\\)\\n`,
      ),
    );
    assert.match(refused.stderr, /Un revert porte un titre de ticket \(ANH-n : \.\.\.\)/);
    assert.deepEqual(
      refused.gh.filter((call) => call.startsWith("pr ")),
      [],
    );
  }
  assert.equal(fx.state(), before);
});

test("ANH-195 EX-5 a revert merged during a release under its default title stops pr, prepare and tag", (t) => {
  const fx = prepared(t);
  // The section of pi-0.1.0 cites ANH-10; this takes its change away.
  fx.mergeTicket(`${REVERT} (#6)`, { [RAMP]: "ramp = 0\n" });

  for (const args of [["pr"], ["pr", "--dry-run"], ["prepare", ...ALL.slice(0, -2)]]) {
    const blocked = fx.run(args);
    refuses(blocked, /un revert sans titre de ticket touche pi \(Raspberry Pi\) dans \S+ :\n {2}[0-9a-f]+ Revert "ANH-10 : régler la rampe du bras" \(#6\)\n/);
    assert.deepEqual(
      blocked.gh.filter((call) => call.startsWith("pr ")),
      [],
    );
  }

  // Merged into main all the same, the release PR being open already: no tag.
  fx.mergeIntoMain();
  for (const args of [["tag"], ["tag", "--dry-run"]]) {
    refuses(fx.run(args), /un revert sans titre de ticket touche pi/);
  }
  assert.equal(git(fx.origin, "tag", "-l"), "");
  assert.equal(git(fx.work, "tag", "-l"), "");
});

for (const [how, restore] of [
  ["the Revert button of GitHub, which quotes the title of the pull request", `Revert "${REVERT}" (#7)`],
  ["git revert, which quotes the title of the merged commit", `Revert "${REVERT} (#6)" (#7)`],
  ["git revert of a revert, which writes Reapply", 'Reapply "ANH-10 : régler la rampe du bras" (#7)'],
]) {
  test(`ANH-195 EX-5 the way out: the revert undone by ${how}, then made again under a ticket title`, (t) => {
    const fx = prepared(t);
    fx.mergeTicket(`${REVERT} (#6)`, { [RAMP]: "ramp = 0\n" });
    refuses(fx.run(["pr", "--dry-run"]), /un revert sans titre de ticket/);

    // Undone under the proposed title: the two cancel out, nothing left untold.
    fx.mergeTicket(restore, { [RAMP]: "ramp = 1\n" });
    const cancelled = fx.run(["pr", "--dry-run"]);
    assert.equal(cancelled.status, 0, cancelled.stderr);

    // Made again as a ticket: a line the section must carry, like any other.
    fx.mergeTicket("ANH-60 : annuler la rampe du bras (#8)", { [RAMP]: "ramp = 0\n" });
    refuses(fx.run(["pr"]), /ne cite pas :\n {2}pi-0\.1\.0 : ANH-60 : annuler la rampe du bras \(#8\)\n/);
    const completed = fx.run(["prepare", ...ALL.slice(0, -2)]);
    assert.equal(completed.status, 0, completed.stderr);
    const head = /--head (\S+) /.exec(creations(completed)[0] ?? "")?.[1] ?? "";
    fx.squashIntoDevelop(head, `${TITLE} (changelog complété) (#9)`);
    assert.equal(fx.run(["pr"]).status, 0);
    fx.mergeIntoMain();
    const tagged = fx.run(["tag"]);

    assert.equal(tagged.status, 0, tagged.stderr);
    assertTagsComplete(fx);
    const message = git(fx.origin, "tag", "-l", "--format=%(contents)", "pi-0.1.0");
    assert.match(message, /- ANH-10 : régler la rampe du bras \(#1\)\n- ANH-60 : annuler la rampe du bras \(#8\)/);
    assert.doesNotMatch(message, /Revert|Reapply/);
  });
}

test("ANH-195 EX-5 a third default revert, which undoes the one that cancelled the first, is refused in its turn", (t) => {
  const fx = fixture(t, [
    ...HISTORY,
    [`${REVERT} (#6)`, { [RAMP]: "ramp = 0\n" }],
    [`Revert "${REVERT}" (#7)`, { [RAMP]: "ramp = 1\n" }],
    [`Revert "Revert "${REVERT}"" (#8)`, { [RAMP]: "ramp = 0\n" }],
  ]);
  const short = git(fx.work, "rev-parse", "--short", "develop");

  const refused = fx.run(["prepare", ...ALL, "--dry-run"]);

  refuses(refused, /un revert sans titre de ticket touche pi/);
  // The third alone: the first two cancelled each other.
  assert.deepEqual(refused.stderr.match(/^ {2}[0-9a-f]+ Re.*$/gm), [`  ${short} Revert "Revert "${REVERT}"" (#8)`]);
});

test("ANH-195 EX-5 a default revert does not hold back a component it does not touch", (t) => {
  const fx = fixture(t, [
    ...HISTORY,
    [`${REVERT} (#6)`, { [RAMP]: "ramp = 0\n" }],
    ['Revert "ANH-13 : guide de démarrage" (#7)', { "docs/guide.md": "# Guide, as before\n" }],
  ]);

  const others = fx.run(["prepare", "--cloud", "0.1.0", "--web", "0.1.0", "--date", DATE, "--dry-run"]);
  const pi = fx.run(["prepare", "--pi", "0.1.0", "--pi-validation", "bench", "--date", DATE, "--dry-run"]);

  assert.equal(others.status, 0, others.stderr);
  refuses(pi, /un revert sans titre de ticket touche pi/);
  // The revert of a page of docs/ touches no component: it is not named.
  assert.doesNotMatch(pi.stderr, /guide de démarrage/);
});

test("ANH-195 EX-5 a revert under a ticket title is a line of the section, next to the ticket it undoes", (t) => {
  const fx = fixture(t, [...HISTORY, ['ANH-60 : Revert "ANH-10 : régler la rampe du bras" (#6)', { [RAMP]: "ramp = 0\n" }]]);

  const result = fx.run(["prepare", "--pi", "0.1.0", "--pi-validation", "bench", "--date", DATE]);

  assert.equal(result.status, 0, result.stderr);
  const changelog = git(fx.work, "show", "origin/release/pi-0.1.0:CHANGELOG.md");
  assert.deepEqual(sectionOf(changelog, "pi-0.1.0").split("\n").slice(-2), [
    "- ANH-10 : régler la rampe du bras (#1)",
    '- ANH-60 : Revert "ANH-10 : régler la rampe du bras" (#6)',
  ]);
});

// ---------------------------------------------------------------------------
// ANH-195 EX-6: a resumed prepare that changes the validation level says so
// ---------------------------------------------------------------------------

const SAME_VERSIONS = ["prepare", "--pi", "0.1.0", "--cloud", "0.1.0", "--web", "0.1.0", "--pi-validation"];

test("ANH-195 EX-6 a resumed prepare that changes the Pi validation level says so in the title and the text of its PR", (t) => {
  const fx = prepared(t);

  // The same versions, another level, and nothing merged since the preparation.
  const changed = fx.run([...SAME_VERSIONS, "auto_validated"]);

  assert.equal(changed.status, 0, changed.stderr);
  assert.equal(creations(changed).length, 1);
  assert.match(
    creations(changed)[0],
    /^pr create --base develop --head release\/\S+-changelog-[0-9a-f]+ --title Release : pi-0\.1\.0, cloud-0\.1\.0, web-0\.1\.0 \(niveau de validation du Pi modifié\) --body-file /,
  );
  assert.match(changed.body, /^\*\*Change le niveau de validation de `pi-0\.1\.0` : `bench` devient `auto_validated`\.\*\* /m);
  assert.match(changed.body, /la ligne « Niveau de validation du Pi justifié » de la check-list de release est à prouver pour ce niveau/);
  // The text no longer claims that ticket PRs were added.
  assert.match(changed.body, /^Réécrit le changelog de la release pi-0\.1\.0, cloud-0\.1\.0, web-0\.1\.0 sans y ajouter de PR de ticket\.$/m);
  assert.doesNotMatch(changed.body, /a reçu des PR de ticket/);
  assert.match(changed.body, /^Niveau de validation : `auto_validated`\.$/m);
});

test("ANH-195 EX-6 a resumed prepare that adds tickets and changes the level says both, and one that keeps the level says neither", (t) => {
  const fx = prepared(t);
  fx.mergeTicket("ANH-40 : nouvelle rampe d'arrêt (#40)", { "raspberry-pi/src/stop.py": "stop = 1\n" });

  const both = fx.run([...SAME_VERSIONS, "occupied_validated", "--dry-run"]);
  const same = fx.run([...SAME_VERSIONS, "bench", "--dry-run"]);

  assert.equal(both.status, 0, both.stderr);
  assert.match(both.stdout, /--title "Release : pi-0\.1\.0, cloud-0\.1\.0, web-0\.1\.0 \(changelog complété, niveau de validation du Pi modifié\)"/);
  assert.match(both.stdout, /Complète le changelog de la release .* : `develop` a reçu des PR de ticket depuis sa préparation\./);
  assert.match(both.stdout, /\*\*Change le niveau de validation de `pi-0\.1\.0` : `bench` devient `occupied_validated`\.\*\*/);
  assert.equal(same.status, 0, same.stderr);
  assert.match(same.stdout, /--title "Release : pi-0\.1\.0, cloud-0\.1\.0, web-0\.1\.0 \(changelog complété\)"/);
  assert.doesNotMatch(same.stdout, /Change le niveau de validation|niveau de validation du Pi modifié/);
});

// ---------------------------------------------------------------------------
// ANH-195 EX-8: recovery after a failed prepare, atomic tags, commit statuses
// ---------------------------------------------------------------------------

/** What a failed prepare must give back: the branch checked out, a clean tree, no release branch. */
function assertCloneAsBefore(fx, before) {
  assert.equal(fx.state(), before);
  assert.equal(git(fx.work, "branch", "--list", "release/*"), "");
}

test(
  "ANH-195 EX-8 a file that cannot be written after the branch switch leaves the clone as it was",
  // The owner of a file is not stopped by its permissions when the owner is root.
  { skip: process.getuid?.() === 0 ? "run as root: a read-only file can still be written" : false },
  (t) => {
    const fx = fixture(t);
    chmodSync(join(fx.work, "package.json"), 0o444);
    const before = fx.state();

    const failed = fx.run(["prepare", ...ALL]);

    refuses(failed, /écriture de package\.json impossible/);
    assert.match(
      failed.stderr,
      /prepare a échoué après avoir créé la branche release\/pi-0\.1\.0_cloud-0\.1\.0_web-0\.1\.0\. Rien n'a été créé sur GitHub, et le clone est rendu tel qu'il était/,
    );
    // The files written before the one that failed are gone with the branch.
    assertCloneAsBefore(fx, before);
    assert.equal(readFileSync(join(fx.work, "raspberry-pi/VERSION"), "utf8"), "pi-0.0.0-dev\n");
    assert.deepEqual(
      failed.gh.filter((call) => call.startsWith("pr ")),
      [],
    );

    // The cause removed, the same command goes through.
    chmodSync(join(fx.work, "package.json"), 0o644);
    const again = fx.run(["prepare", ...ALL]);
    assert.equal(again.status, 0, again.stderr);
    assert.equal(creations(again).length, 1);
  },
);

test("ANH-195 EX-8 a commit refused after the files were written leaves the clone as it was", (t) => {
  const fx = fixture(t);
  const hook = join(fx.work, ".git", "hooks", "pre-commit");
  mkdirSync(dirname(hook), { recursive: true });
  writeFileSync(hook, "#!/bin/sh\nexit 1\n");
  chmodSync(hook, 0o755);
  // From a commit that is not a branch: the clone must come back to it too.
  git(fx.work, "switch", "--quiet", "--detach", "main");
  const before = fx.state();

  const failed = fx.run(["prepare", ...ALL]);

  refuses(failed, /le commit de préparation a été refusé/);
  assertCloneAsBefore(fx, before);
  assert.equal(git(fx.origin, "for-each-ref", "refs/heads/release"), "");
});

test("ANH-195 EX-8 a push refused by origin leaves the clone as it was, and the same command then goes through", (t) => {
  const fx = fixture(t);
  const hook = join(fx.origin, "hooks", "pre-receive");
  writeFileSync(hook, "#!/bin/sh\nexit 1\n");
  chmodSync(hook, 0o755);
  const before = fx.state();

  const failed = fx.run(["prepare", ...ALL]);

  refuses(failed, /la branche release\/pi-0\.1\.0_cloud-0\.1\.0_web-0\.1\.0 n'a pas pu être poussée sur origin/);
  assert.match(failed.stderr, /Rien n'a été créé sur GitHub/);
  assertCloneAsBefore(fx, before);

  rmSync(hook);
  const again = fx.run(["prepare", ...ALL]);
  assert.equal(again.status, 0, again.stderr);
  assert.equal(git(fx.work, "log", "-1", "--format=%s", `origin/${BRANCH}`), TITLE);
});

test("ANH-195 EX-8 a PR that could not be created after the push is created by the same command run again", (t) => {
  const fx = fixture(t);
  const before = fx.state();

  const failed = fx.run(["prepare", ...ALL], { createExit: "1" });

  refuses(failed, /la PR de préparation n'a pas pu être créée/);
  assert.match(
    failed.stderr,
    /la branche release\/pi-0\.1\.0_cloud-0\.1\.0_web-0\.1\.0 est sur origin, mais sa PR n'a pas été créée\. Le clone est rendu tel qu'il était\. Relancer la même commande/,
  );
  // The branch is on origin; the clone is back on develop, without the branch.
  const pushed = git(fx.origin, "rev-parse", `refs/heads/${BRANCH}`);
  assert.equal(git(fx.work, "rev-parse", "--abbrev-ref", "HEAD"), "develop");
  assert.equal(git(fx.work, "status", "--porcelain"), "");
  assert.equal(git(fx.work, "branch", "--list", "release/*"), "");
  assert.notEqual(fx.state(), before);

  // Run again while GitHub still refuses: nothing more is written, and it says what to do.
  const stillFailing = fx.run(["prepare", ...ALL], { createExit: "1" });
  refuses(stillFailing, /la PR de préparation n'a pas pu être créée : la branche release\/\S+ reste sur origin, relancer la même commande/);
  assert.equal(git(fx.origin, "rev-parse", `refs/heads/${BRANCH}`), pushed);
  assert.equal(git(fx.work, "rev-parse", "--abbrev-ref", "HEAD"), "develop");

  // The dry run says what is left to do, and does not do it.
  const dry = fx.run(["prepare", ...ALL, "--dry-run"]);
  assert.equal(dry.status, 0, dry.stderr);
  assert.match(dry.stdout, new RegExp(`^Reprise : la branche ${BRANCH} est déjà sur origin avec ce contenu \\(${pushed}\\)\\. Seule sa PR reste à créer\\.$`, "m"));
  assert.doesNotMatch(dry.stdout, /^git (switch|commit|push -u) /m);
  assert.deepEqual(
    dry.gh.filter((call) => call.startsWith("pr ")),
    [],
  );

  const resumed = fx.state();
  const again = fx.run(["prepare", ...ALL]);
  assert.equal(again.status, 0, again.stderr);
  assert.equal(creations(again).length, 1);
  assert.match(creations(again)[0], new RegExp(`^pr create --base develop --head ${BRANCH} --title ${TITLE} --body-file `));
  assert.match(again.body, /^Prépare la release pi-0\.1\.0, cloud-0\.1\.0, web-0\.1\.0\.$/m);
  // Nothing was written, committed or pushed a second time.
  assert.equal(fx.state(), resumed);
  assert.equal(git(fx.origin, "rev-parse", `refs/heads/${BRANCH}`), pushed);

  // And the release goes on from the branch that was pushed the first time.
  fx.squashIntoDevelop(`origin/${BRANCH}`, `${TITLE} (#5)`);
  assert.equal(fx.run(["pr"]).status, 0);
});

test("ANH-195 EX-8 prepare run again while its PR is open says so and creates nothing", (t) => {
  const fx = fixture(t);
  assert.equal(fx.run(["prepare", ...ALL]).status, 0);
  const before = fx.state();

  const again = fx.run(["prepare", ...ALL], { openPr: "https://github.invalid/anheart/pull/5" });

  assert.equal(again.status, 0, again.stderr);
  assert.match(again.stdout, /^La PR de préparation est déjà ouverte : https:\/\/github\.invalid\/anheart\/pull\/5$/m);
  assert.deepEqual(creations(again), []);
  assert.equal(fx.state(), before);
});

test("ANH-195 EX-8 prepare refuses a branch already on origin that holds another preparation", (t) => {
  const fx = fixture(t);
  assert.equal(fx.run(["prepare", ...ALL]).status, 0);
  // develop moves: the branch pushed above was prepared from another commit.
  fx.mergeTicket("ANH-40 : nouvelle rampe d'arrêt (#40)", { "raspberry-pi/src/stop.py": "stop = 1\n" });
  const before = fx.state();

  for (const args of [
    ["prepare", ...ALL],
    ["prepare", ...ALL, "--dry-run"],
  ]) {
    const refused = fx.run(args);
    refuses(
      refused,
      /la branche release\/pi-0\.1\.0_cloud-0\.1\.0_web-0\.1\.0 existe déjà sur origin avec un autre contenu que celui de cette préparation : fermer sa PR s'il y en a une, la supprimer \(git push origin --delete release\/pi-0\.1\.0_cloud-0\.1\.0_web-0\.1\.0\), puis relancer/,
    );
    assert.deepEqual(
      refused.gh.filter((call) => call.startsWith("pr ")),
      [],
    );
  }
  assert.equal(fx.state(), before);
});

test("ANH-195 EX-8 a branch deleted on origin is not taken for a pushed one, and a branch left in the clone is named", (t) => {
  const fx = fixture(t);
  assert.equal(fx.run(["prepare", ...ALL]).status, 0);
  // The PR is closed and its branch deleted on GitHub; the clone still remembers it.
  git(fx.origin, "update-ref", "-d", `refs/heads/${BRANCH}`);
  git(fx.work, "switch", "--quiet", "develop");
  assert.notEqual(git(fx.work, "for-each-ref", `refs/remotes/origin/${BRANCH}`), "");

  refuses(
    fx.run(["prepare", ...ALL]),
    /la branche release\/pi-0\.1\.0_cloud-0\.1\.0_web-0\.1\.0 existe déjà dans ce clone sans être sur origin : la supprimer \(git branch -D release\/pi-0\.1\.0_cloud-0\.1\.0_web-0\.1\.0\), puis relancer/,
  );
  git(fx.work, "branch", "--quiet", "-D", BRANCH);
  const again = fx.run(["prepare", ...ALL]);

  assert.equal(again.status, 0, again.stderr);
  assert.doesNotMatch(again.stdout, /Reprise/);
  assert.equal(git(fx.origin, "log", "-1", "--format=%s", `refs/heads/${BRANCH}`), TITLE);
});

test("ANH-195 EX-8 origin takes every tag or none", (t) => {
  const fx = prepared(t);
  fx.mergeIntoMain();
  // origin refuses one tag of the three, and would take the two others.
  const hook = join(fx.origin, "hooks", "update");
  writeFileSync(hook, '#!/bin/sh\n[ "$1" != refs/tags/web-0.1.0 ]\n');
  chmodSync(hook, 0o755);

  const refused = fx.run(["tag"]);

  refuses(refused, /les tags n'ont pas pu être poussés : origin n'en a reçu aucun, et aucun n'est conservé en local/);
  assert.equal(git(fx.origin, "tag", "-l"), "");
  assert.equal(git(fx.work, "tag", "-l"), "");

  rmSync(hook);
  const tagged = fx.run(["tag"]);
  assert.equal(tagged.status, 0, tagged.stderr);
  assert.equal(git(fx.origin, "tag", "-l"), "cloud-0.1.0\npi-0.1.0\nweb-0.1.0");
});

for (const [name, lines, message] of [
  ["a failed commit status", "deploy/site\tfailure\n", /statut deploy\/site : failure/],
  ["a commit status in error", "deploy/site\terror\n", /statut deploy\/site : error/],
  ["a pending commit status", "deploy/site\tpending\n", /statut deploy\/site : pending/],
  ["a refused independent review", `${REVIEW}\tfailure\n`, /statut agent-review\/R1 : failure/],
  ["one failed status beside a successful one", `${REVIEW}\tsuccess\ndeploy/site\tfailure\n`, /statut deploy\/site : failure/],
]) {
  test(`ANH-195 EX-8 prepare and pr refuse when the head of develop carries ${name}`, (t) => {
    const fx = fixture(t);
    fx.setStatuses(git(fx.work, "rev-parse", "develop"), lines);
    const before = fx.state();
    const early = fx.run(["prepare", ...ALL]);
    refuses(early, /develop n'est pas vert/);
    assert.match(early.stderr, message);
    assert.equal(fx.state(), before);

    // The same once the preparation is merged: pr reads the new head.
    fx.setStatuses(git(fx.work, "rev-parse", "develop"), "");
    assert.equal(fx.run(["prepare", ...ALL]).status, 0);
    fx.squashIntoDevelop(BRANCH, `${TITLE} (#5)`);
    fx.setStatuses(git(fx.work, "rev-parse", "develop"), lines);
    const late = fx.run(["pr"]);
    refuses(late, /develop n'est pas vert/);
    assert.match(late.stderr, message);
    assert.deepEqual(
      late.gh.filter((call) => call.startsWith("pr ")),
      [],
    );
  });
}

test("ANH-195 EX-8 pr writes in the pull request where the independent review stands on its candidate", (t) => {
  const fx = prepared(t);
  const candidate = git(fx.work, "rev-parse", "develop");

  const unread = fx.run(["pr", "--dry-run"]);
  fx.setStatuses(candidate, `${REVIEW}\tsuccess\ndeploy/site\tsuccess\n`);
  const read = fx.run(["pr"]);

  // Not required to open the pull request: the review is made on what it shows.
  assert.equal(unread.status, 0, unread.stderr);
  assert.match(unread.stdout, /^Avis indépendant : agent-review\/R1 : absent$/m);
  assert.equal(read.status, 0, read.stderr);
  assert.match(
    read.body,
    /^Avis indépendant sur ce commit : à l'ouverture de cette PR, agent-review\/R1 : success\. `main` exige ce statut réussi pour fusionner, et `scripts\/release\.sh tag` le vérifie avant de poser un tag\.$/m,
  );
  assert.ok(read.gh.includes(`api repos/{owner}/{repo}/commits/${candidate}/status --paginate --jq .statuses[] | [.context, .state] | @tsv`));
});

test("ANH-195 EX-8 tag refuses a release whose candidate does not carry the independent review, whatever main carries", (t) => {
  const fx = prepared(t);
  const candidate = git(fx.work, "rev-parse", "develop");
  // Merged with the rule of main lifted: nothing was posted on the candidate.
  fx.mergeIntoMain({ reviewed: false });
  const merge = git(fx.origin, "rev-parse", "refs/heads/main");
  // A status on the merge commit is not a review of what it brought in.
  fx.setStatuses(merge, `${REVIEW}\tsuccess\n`);

  for (const [lines, state] of [
    ["", "absent"],
    ["deploy/site\tsuccess\n", "absent"],
    [`${REVIEW}\tpending\n`, "pending"],
    [`${REVIEW}\tfailure\n`, "failure"],
    [`${REVIEW}\terror\n`, "error"],
  ]) {
    fx.setStatuses(candidate, lines);
    for (const args of [["tag"], ["tag", "--dry-run"]]) {
      const refused = fx.run(args);
      refuses(refused, new RegExp(`le candidat publié \\(${candidate}\\) n'a pas l'avis indépendant requis :\\n {2}- agent-review/R1 : ${state}\\n`));
      assert.match(refused.stderr, /La release ne part pas sans lui/);
    }
  }
  assert.equal(git(fx.origin, "tag", "-l"), "");
  assert.equal(git(fx.work, "tag", "-l"), "");

  fx.setStatuses(candidate, `${REVIEW}\tsuccess\n`);
  const tagged = fx.run(["tag"]);
  assert.equal(tagged.status, 0, tagged.stderr);
  assert.match(tagged.stdout, new RegExp(`^Avis indépendant : agent-review/R1 réussi sur le candidat ${candidate}$`, "m"));
  assert.equal(git(fx.origin, "tag", "-l"), "cloud-0.1.0\npi-0.1.0\nweb-0.1.0");
});

test("ANH-195 EX-8 tag refuses when the merge commit of main carries a status that is not successful", (t) => {
  const fx = prepared(t);
  fx.mergeIntoMain();
  fx.setStatuses(git(fx.origin, "rev-parse", "refs/heads/main"), "deploy/site\tpending\n");

  const refused = fx.run(["tag"]);

  refuses(refused, /main n'est pas vert[\s\S]*statut deploy\/site : pending/);
  assert.equal(git(fx.origin, "tag", "-l"), "");
});

test("ANH-195 EX-8 the three steps refuse when the commit statuses cannot be read", (t) => {
  const fx = fixture(t);
  const unreadable = { statusExit: "1" };
  const before = fx.state();
  refuses(fx.run(["prepare", ...ALL], unreadable), /lecture des statuts de commit impossible pour develop/);
  assert.equal(fx.state(), before);

  assert.equal(fx.run(["prepare", ...ALL]).status, 0);
  fx.squashIntoDevelop(BRANCH, `${TITLE} (#5)`);
  refuses(fx.run(["pr"], unreadable), /lecture des statuts de commit impossible pour develop/);
  fx.mergeIntoMain();
  refuses(fx.run(["tag"], unreadable), /lecture des statuts de commit impossible pour main/);
  assert.equal(git(fx.origin, "tag", "-l"), "");
});

test("ANH-195 two releases in a row go through though main carries a commit develop does not have", (t) => {
  const fx = fixture(t);
  // A pull request merged into main alone, which develop never takes back.
  git(fx.work, "switch", "--quiet", "main");
  commit(fx.work, "ANH-198 : apporter sur main les boutons de déploiement (#90)", { "vercel.json": "{}\n" });
  git(fx.work, "push", "--quiet", "origin", "main");
  git(fx.work, "switch", "--quiet", "develop");

  assert.equal(fx.run(["prepare", ...ALL]).status, 0);
  fx.squashIntoDevelop(BRANCH, `${TITLE} (#5)`);
  assert.equal(fx.run(["pr"]).status, 0);
  fx.mergeIntoMain();
  const first = fx.run(["tag"]);
  assert.equal(first.status, 0, first.stderr);

  fx.mergeTicket("ANH-30 : nouveau palier de vitesse (#30)", { "raspberry-pi/src/tiers.py": "t = 1\n" });
  const next = fx.run(["prepare", "--pi", "0.2.0", "--pi-validation", "bench", "--date", "2026-11-02"]);
  assert.equal(next.status, 0, next.stderr);
  fx.squashIntoDevelop("release/pi-0.2.0", "Release : pi-0.2.0 (#31)");
  const opened = fx.run(["pr"]);
  assert.equal(opened.status, 0, opened.stderr);
  fx.mergeIntoMain();
  const second = fx.run(["tag"]);

  assert.equal(second.status, 0, second.stderr);
  assert.equal(git(fx.origin, "tag", "-l"), "cloud-0.1.0\npi-0.1.0\npi-0.2.0\nweb-0.1.0");
  assertTagsComplete(fx);
  // develop never received the commit of main, nor any merge commit.
  assert.equal(git(fx.origin, "log", "--format=%s", "refs/heads/develop", "--", "vercel.json"), "");
  assert.equal(git(fx.origin, "rev-list", "--merges", "--count", "refs/heads/develop"), "0");
  const message = git(fx.origin, "tag", "-l", "--format=%(contents)", "pi-0.2.0").split("\n");
  assert.deepEqual(
    message.filter((line) => line.startsWith("- ")),
    ["- ANH-30 : nouveau palier de vitesse (#30)"],
  );
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
  assert.equal(template.length, 10);
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
    // ANH-198: merging the release deploys nothing any more, the deployment is a step with its window.
    "Déployer en production (main)",
    "aucune séance n'est en cours",
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
