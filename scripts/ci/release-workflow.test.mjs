// What must stay true of .github/workflows/release.yml, the release pipeline:
// one button on `main` that tests everything, asks CodeQL for no open alert,
// puts the release tags, deploys Convex, then the site and the hosted
// simulation, and says how a machine is updated (ANH-219; docs/release.md).
//
// Run by the `docs` job of ci.yml with the other CI test files, on every run
// and without any install: the workflow is read as text, and the scripts of
// its steps are run as the runner would run them, with doubles in place of
// `gh`, `curl`, `npx` and `scripts/release.sh`, in throwaway repositories.
// Nothing here reaches GitHub, Convex or Vercel, and nothing is tagged. The
// pipeline itself cannot be tried from a test: it exists as a button only
// once on `main`, and what it does there is a release. These tests hold what
// a later edit could break without any job turning red.

import assert from "node:assert/strict";
import { execFileSync, spawnSync } from "node:child_process";
import { appendFileSync, chmodSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { decide } from "./gates-for-changes.mjs";

const root = fileURLToPath(new URL("../../", import.meta.url));
const WORKFLOWS = ".github/workflows";

/** @param {string} path relative to the repository root */
const read = (path) => readFileSync(join(root, path), "utf8");
/** A workflow without its comment lines: what GitHub acts on. @param {string} file */
const workflow = (file) =>
  read(`${WORKFLOWS}/${file}`)
    .split("\n")
    .filter((line) => !line.trimStart().startsWith("#"))
    .join("\n");

const RELEASE = workflow("release.yml");
const PRODUCTION = workflow("deploy-production.yml");
const CI = workflow("ci.yml");
const CODEQL = workflow("codeql.yml");
const INSTALL = workflow("pi-install.yml");

/**
 * The lines nested under a key, trimmed.
 * @param {string} text @param {string} key the key, with its indentation
 */
const block = (text, key) => {
  const indent = /^ */.exec(key)?.[0].length ?? 0;
  const nested = new RegExp(`^${key}:[ \\t]*\\n((?: {${indent + 2}}.*\\n)+)`, "m").exec(text)?.[1] ?? "";
  return nested.split("\n").filter(Boolean).map((line) => line.trim());
};

/** The keys written directly under a key. @param {string} text @param {string} key the key, with its indentation */
const keysUnder = (text, key) => {
  const indent = /^ */.exec(key)?.[0].length ?? 0;
  const nested = new RegExp(`^${key}:[ \\t]*\\n((?: {${indent + 2}}.*\\n)+)`, "m").exec(text)?.[1] ?? "";
  return [...nested.matchAll(new RegExp(`^ {${indent + 2}}([A-Za-z_-]+):`, "gm"))].map((match) => match[1]);
};

/**
 * Each job's own lines, by job id.
 * @param {string} text a workflow @returns {Map<string, string>}
 */
const jobsOf = (text) => {
  const body = text.slice(text.indexOf("\njobs:\n") + "\njobs:\n".length);
  const starts = [...body.matchAll(/^ {2}([a-z][a-z0-9-]*):[ \t]*$/gm)];
  return new Map(
    starts.map((start, index) => [start[1] ?? "", body.slice(start.index, starts[index + 1]?.index ?? body.length)]),
  );
};

/** @param {string} job the lines of one job @param {string} key a key of the job itself, not of one of its steps */
const own = (job, key) => new RegExp(`^ {4}${key}:[ \\t]*(.*)$`, "m").exec(job)?.[1];

/** The steps of a job, each as its own lines. @param {string} job */
const stepsOf = (job) => job.split(/^ {6}- /m).slice(1);
/** @param {string} step */
const labelOf = (step) => /^name: (.*)\n/.exec(step)?.[1] ?? `uses ${/^uses: ([^@\s]+)@/.exec(step)?.[1]}`;
/** @param {string} job @param {string} name */
const stepNamed = (job, name) => {
  const step = stepsOf(job).find((text) => labelOf(text) === name);
  assert.ok(step, `no step "${name}"`);
  return step;
};
/** The shell text of a step, as the runner would be handed it. @param {string} job @param {string} name */
const script = (job, name) => {
  const step = stepNamed(job, name);
  const written = /^ {8}run: \|\n((?: {10}.*\n|\n)+)/m.exec(step)?.[1];
  const line = /^ {8}run: (?!\|)(.+)\n/m.exec(step)?.[1];
  assert.ok(written ?? line, `the step "${name}" runs nothing`);
  return written ? written.replaceAll(/^ {10}/gm, "").replace(/\n+$/, "\n") : `${line}\n`;
};
/** What a step is handed through `env:`, as written. @param {string} step */
const handed = (step) => block(step, "        env");

/** What `env:` gives every script of a workflow. @param {string} text @returns {Record<string, string>} */
const shared = (text) =>
  Object.fromEntries(
    block(text, "env").map((line) => {
      const [, key = "", value = ""] = /^([A-Z_]+): '?([^']*)'?$/.exec(line) ?? [];
      return [key, value];
    }),
  );

const JOBS = jobsOf(RELEASE);
/** @param {string} id */
const job = (id) => {
  const text = JOBS.get(id);
  assert.ok(text, `release.yml has no job "${id}"`);
  return text;
};
const ENV = shared(RELEASE);

/** The jobs of the pipeline, in the order of the file. */
const ORDER = ["request", "gates", "install", "codeql", "alerts", "dry-run", "tags", "convex", "vercel-site", "site-answers", "vercel-simulation", "simulation-answers", "machines"];
/** The jobs of stages 3 to 6 of a real run, in the order they must run: each can write, or follows one that did. */
const REAL = ["tags", "convex", "vercel-site", "site-answers", "vercel-simulation", "simulation-answers", "machines"];
/** The jobs that call another workflow of the repository, and which. */
const CALLS = {
  gates: "ci.yml",
  install: "pi-install.yml",
  codeql: "codeql.yml",
  "vercel-site": "deploy-production.yml",
  "vercel-simulation": "deploy-production.yml",
};
const ONLY_REAL = "${{ needs.request.outputs.mode == 'real' }}";
const ONLY_DRY = "${{ needs.request.outputs.mode == 'dry' }}";

const GUARD = "Refuser toute autre branche";
const MODE = "Lire le mode demandé";
const APPROVAL = "Exiger la règle d'approbation de l'environnement production";
const REMIND = "Rappeler ce que fait cette exécution";
const ALERTS = "Exiger l'analyse de ce commit et aucune alerte ouverte";
const STILL_HEAD = "Refuser un commit qui n'est plus la tête de la branche";
const STATE = "Lire les versions et les tags de ce commit";
const WOULD_TAG = "Dire quels tags scripts/release.sh poserait";
const WOULD_DEPLOY = "Dire ce qui serait déployé, et ce qui serait dit des machines";
const TAG = "Poser les tags par scripts/release.sh";
const TAGGED = "Exiger un tag de release sur ce commit";
const KEY = "Exiger une clé qui vise le déploiement de production attendu";
const DEPENDENCIES = "Installer les dépendances du dépôt";
const DEPLOY_CONVEX = "Déployer Convex en production";
const SERVED = "Vérifier la version que sert le déploiement";
const REFUSAL = "Vérifier que l'API machine répond, sans clé";
const RECORD = "Dire ce qui est déployé, et ce qui reste à enregistrer à la main";
const SITE = "Vérifier que le site sert la version de ce commit";
const SIMULATION = "Vérifier que la simulation sert la page de ce commit";
const MACHINES = "Dire la version du Pi et comment mettre une machine à jour à la main";

// ---------------------------------------------------------------------------
// Doubles, and how a script is run
// ---------------------------------------------------------------------------

/** Stand-ins with a space in them: no secret looks like these, and a script that printed one would show. */
const TOKEN = "jeton de test";
const SECRET = "partie secrete de la cle";
const SHA = "0123456789abcdef0123456789abcdef01234567";

/** The name a double of `gh` or of `curl` keeps an answer under: the address asked for, letters and digits only. */
const answerName = (/** @type {string} */ address) => address.replaceAll(/[^A-Za-z0-9]/g, "_");

/**
 * The doubles. `gh` answers an endpoint with the JSON files kept under its name, one per page, each
 * read through the real `jq` with the filter the script gave: the filters of the workflow are run,
 * not assumed. `curl` answers an address with the file kept under its name and the status next to
 * it. `npx` and `sleep` answer as told. Each writes down how it was called, and with which secret.
 */
const DOUBLES = {
  gh: `#!/usr/bin/env bash
printf '%s|%s\\n' "$*" "\${GH_TOKEN:-none}" >> "$DOUBLE_CALLS"
endpoint=""; filter="."
while (( $# > 0 )); do
  case "$1" in
    api | --paginate) ;;
    --jq) filter="$2"; shift ;;
    *) endpoint="$1" ;;
  esac
  shift
done
name="$(printf '%s' "$endpoint" | tr -c 'A-Za-z0-9' '_')"
found=0
for page in "$DOUBLE_ANSWERS/$name".*.json; do
  [[ -f "$page" ]] || continue
  found=1
  jq -r "$filter" "$page" || exit 5
done
if (( found == 0 )); then echo "gh: HTTP 404" >&2; exit 1; fi
`,
  curl: `#!/usr/bin/env bash
printf '%s|%s|%s\\n' "$*" "\${CONVEX_DEPLOY_KEY:-none}" "\${GH_TOKEN:-none}" >> "$DOUBLE_CALLS"
out=/dev/null; fail=0; code=0; address=""
while (( $# > 0 )); do
  case "$1" in
    --output) out="$2"; shift ;;
    --write-out) code=1; shift ;;
    --header | --max-time) shift ;;
    --fail) fail=1 ;;
    --*) ;;
    *) address="$1" ;;
  esac
  shift
done
name="$(printf '%s' "$address" | tr -c 'A-Za-z0-9' '_')"
status=000
if [[ -f "$DOUBLE_ANSWERS/$name" ]]; then
  status="$(cat "$DOUBLE_ANSWERS/$name.status" 2>/dev/null || echo 200)"
  cp "$DOUBLE_ANSWERS/$name" "$out"
fi
if (( code == 1 )); then printf '%s' "$status"; fi
if [[ "$status" == 000 ]]; then exit 7; fi
if (( fail == 1 )) && [[ "$status" != 2* ]]; then exit 22; fi
exit 0
`,
  npx: `#!/usr/bin/env bash
printf '%s|%s\\n' "$*" "\${CONVEX_DEPLOY_KEY:-none}" >> "$DOUBLE_CALLS"
printf '%s\\n' "\${DOUBLE_NPX_ANSWER:-}"
exit "\${DOUBLE_NPX_STATUS:-0}"
`,
  sleep: `#!/usr/bin/env bash
printf 'sleep %s|\\n' "$*" >> "$DOUBLE_CALLS"
`,
};

const GIT_ENV = {
  GIT_CONFIG_GLOBAL: "/dev/null",
  GIT_CONFIG_NOSYSTEM: "1",
  GIT_TERMINAL_PROMPT: "0",
  GIT_AUTHOR_NAME: "Synthetic Releaser",
  GIT_AUTHOR_EMAIL: "release@example.invalid",
  GIT_COMMITTER_NAME: "Synthetic Releaser",
  GIT_COMMITTER_EMAIL: "release@example.invalid",
};

/**
 * Runs a script the way a GitHub runner runs a `run:` step with `shell: bash`, the doubles first on the path.
 * @param {string} text the script @param {Record<string, string>} env
 * @param {{ cwd?: string, answers?: Record<string, string>, statuses?: Record<string, string> }} [given]
 *   `answers`: what `gh` and `curl` answer, by endpoint (suffixed `#1`, `#2` for the pages of `gh`) or by address
 */
const run = (text, env, { cwd = root, answers = {}, statuses = {} } = {}) => {
  const directory = mkdtempSync(join(tmpdir(), "anheart-release-wf-"));
  const bin = join(directory, "bin");
  const kept = join(directory, "answers");
  mkdirSync(bin);
  mkdirSync(kept);
  for (const [name, body] of Object.entries(DOUBLES)) {
    writeFileSync(join(bin, name), body);
    chmodSync(join(bin, name), 0o755);
  }
  for (const [address, body] of Object.entries(answers)) {
    const [endpoint, page] = address.split("#");
    writeFileSync(join(kept, page ? `${answerName(endpoint ?? "")}.${page}.json` : answerName(address)), body);
  }
  for (const [address, status] of Object.entries(statuses)) writeFileSync(join(kept, `${answerName(address)}.status`), status);
  const files = { calls: join(directory, "calls"), output: join(directory, "output"), summary: join(directory, "summary") };
  for (const file of Object.values(files)) writeFileSync(file, "");
  const result = spawnSync("bash", ["--noprofile", "--norc", "-e", "-o", "pipefail", "-c", text], {
    cwd,
    encoding: "utf8",
    env: {
      PATH: `${bin}:${process.env.PATH ?? ""}`,
      HOME: directory,
      RUNNER_TEMP: directory,
      DOUBLE_CALLS: files.calls,
      DOUBLE_ANSWERS: kept,
      GITHUB_OUTPUT: files.output,
      GITHUB_STEP_SUMMARY: files.summary,
      GITHUB_REPOSITORY: "anheart/depot",
      GITHUB_SHA: SHA,
      GITHUB_RUN_ID: "77",
      ...GIT_ENV,
      ...ENV,
      ...env,
    },
  });
  const calls = readFileSync(files.calls, "utf8").split("\n").filter(Boolean);
  const [output, summary] = [readFileSync(files.output, "utf8"), readFileSync(files.summary, "utf8")];
  rmSync(directory, { recursive: true, force: true });
  return { status: result.status, said: `${result.stdout}${result.stderr}`, calls, output, summary };
};

/** @param {string} cwd @param {string[]} args */
const git = (cwd, ...args) => execFileSync("git", args, { cwd, env: { ...process.env, ...GIT_ENV }, encoding: "utf8" }).trim();

/** The message `scripts/release.sh tag` gives the tag of a version of the Pi: the section of its changelog. */
const PI_MESSAGE = "pi-0.1.0\n\nComposant : Raspberry Pi. Changements depuis : la première version.\nNiveau de validation : `bench`.\n\n- ANH-10 : régler la rampe du bras (#1)\n";

/**
 * A throwaway repository with its `origin`, as a release leaves `main`: the three version files, the
 * contract and the viewer page of this repository. Quiet (docs/release.md, section 7): git starts
 * nothing in it that outlives the command that was run.
 * @param {import("node:test").TestContext} context
 * @param {{ pi?: string, cloud?: string, web?: string }} [versions]
 */
const repository = (context, { pi = "pi-0.1.0", cloud = "cloud-0.1.0", web = "0.1.0" } = {}) => {
  const directory = mkdtempSync(join(tmpdir(), "anheart-release-repo-"));
  context.after(() => rmSync(directory, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 }));
  const [origin, work] = [join(directory, "origin.git"), join(directory, "work")];
  git(directory, "init", "--quiet", "--bare", "--initial-branch=main", origin);
  git(directory, "init", "--quiet", "--initial-branch=main", work);
  for (const gitDir of [origin, join(work, ".git")]) {
    appendFileSync(join(gitDir, "config"), "[gc]\n\tauto = 0\n\tautoDetach = false\n[maintenance]\n\tauto = false\n\tautoDetach = false\n[receive]\n\tautogc = false\n");
  }
  git(work, "remote", "add", "origin", origin);
  const files = {
    "raspberry-pi/VERSION": `${pi}\n`,
    "convex/VERSION": `${cloud}\n`,
    "package.json": `${JSON.stringify({ name: "fixture", version: web, private: true }, null, 2)}\n`,
    "contracts/machine-api.json": read("contracts/machine-api.json"),
    "simulation/viewer/index.html": read("simulation/viewer/index.html"),
  };
  const commit = (/** @type {string} */ subject, /** @type {Record<string, string>} */ written) => {
    for (const [path, content] of Object.entries(written)) {
      mkdirSync(dirname(join(work, path)), { recursive: true });
      writeFileSync(join(work, path), content);
    }
    git(work, "add", "-A");
    git(work, "commit", "--quiet", "-m", subject);
    git(work, "push", "--quiet", "origin", "main");
    return git(work, "rev-parse", "HEAD");
  };
  const head = commit("Merge pull request #9 from develop", files);
  return {
    work,
    origin,
    head,
    commit,
    /** An annotated tag, on origin only: what a release that already ran leaves. @param {string} name @param {string} at @param {string} [message] */
    tag(name, at, message = `${name}\n\nComposant : Site.\n`) {
      git(work, "tag", "-a", "-m", message, name, at);
      git(work, "push", "--quiet", "origin", `refs/tags/${name}`);
      git(work, "tag", "-d", name);
    },
  };
};

// ---------------------------------------------------------------------------
// EX-1: a button on main, a dry run by default, a real run that asks three times
// ---------------------------------------------------------------------------

test("EX-1 the pipeline starts by its button and from nothing else", () => {
  assert.equal(RELEASE.split("\n")[0], "name: Release en production (main)");
  // `on:` is written once, as a block, and holds one event.
  assert.deepEqual(RELEASE.match(/^on:.*$/gm), ["on:"]);
  assert.deepEqual(keysUnder(RELEASE, "on"), ["workflow_dispatch"]);
  assert.doesNotMatch(RELEASE, /pull_request|workflow_run|workflow_call|repository_dispatch|deployment_status|schedule|\bpush:|\bcreate:|\brelease:/);
  // No other workflow can start it either.
  const all = readdirSync(join(root, WORKFLOWS)).filter((file) => /\.ya?ml$/.test(file));
  assert.ok(all.includes("release.yml") && all.includes("ci.yml"), "the workflows were not listed");
  for (const file of all) assert.doesNotMatch(workflow(file), /uses:.*release\.yml/, file);
  // The form: a dry run unless the box is unticked, and a word to type.
  assert.deepEqual(keysUnder(RELEASE, "    inputs"), ["dry_run", "confirmation"]);
  assert.deepEqual(block(RELEASE, "      dry_run").slice(1), ["required: true", "type: boolean", "default: true"]);
  assert.match(block(RELEASE, "      dry_run")[0] ?? "", /^description: "À blanc : .* Décocher pour une release réelle\."$/);
  assert.deepEqual(block(RELEASE, "      confirmation").slice(1), ["required: false", "type: string"]);
  assert.match(block(RELEASE, "      confirmation")[0] ?? "", /Pour confirmer, écrire le mot : production"$/);
  // The jobs are the ones this file knows about.
  assert.deepEqual([...JOBS.keys()], ORDER);
});

test("EX-1 the pipeline refuses every ref but main, before anything else runs", () => {
  const request = job("request");
  assert.equal(ENV.DEPLOY_BRANCH, "main");
  // The guard is the first step of the first job, is handed the ref the run started on, and waits for nothing.
  assert.equal(ORDER[0], "request");
  assert.deepEqual(stepsOf(request).map(labelOf), [GUARD, MODE, APPROVAL, REMIND]);
  assert.deepEqual(handed(stepNamed(request, GUARD)), ["REF: ${{ github.ref }}"]);
  for (const key of ["needs", "if", "environment"]) assert.equal(own(request, key), undefined, key);
  assert.doesNotMatch(request, /secrets\./);
  // Every other job waits for it, directly or through the jobs it waits for.
  for (const id of ORDER.slice(1)) assert.ok(before(id).has("request"), `${id} can start without the request being checked`);

  const guard = script(request, GUARD);
  const accepted = run(guard, { REF: "refs/heads/main" });
  assert.equal(accepted.status, 0, accepted.said);
  for (const ref of ["refs/heads/develop", "refs/heads/feature/main", "refs/heads/main-2", "refs/heads/x/main", "refs/tags/main", "refs/tags/pi-0.1.0", "refs/pull/41/merge", "main", ""]) {
    const refused = run(guard, { REF: ref });
    assert.equal(refused.status, 1, `"${ref}" was accepted`);
    assert.match(refused.said, /^::error title=Mauvaise branche::Rien n'a été tagué ni déployé\. Cette pipeline ne s'exécute que sur la branche main /m);
  }
});

test("EX-1 a real run needs the box unticked and the word typed: anything else is a dry run or a refusal", () => {
  const request = job("request");
  assert.equal(ENV.CONFIRMATION_WORD, "production");
  assert.deepEqual(handed(stepNamed(request, MODE)), ["DRY_RUN: ${{ inputs.dry_run }}", "CONFIRMATION: ${{ inputs.confirmation }}"]);
  assert.match(stepNamed(request, MODE), /^ {8}id: mode$/m);
  assert.deepEqual(block(request, "    outputs"), ["mode: ${{ steps.mode.outputs.mode }}"]);
  const mode = script(request, MODE);
  const asked = (/** @type {string} */ box, /** @type {string} */ typed) => run(mode, { DRY_RUN: box, CONFIRMATION: typed });

  // The box is ticked: a dry run, whatever was typed.
  for (const typed of ["", "production", "oui", "PRODUCTION"]) {
    const dry = asked("true", typed);
    assert.equal(dry.status, 0, dry.said);
    assert.equal(dry.output, "mode=dry\n", `"${typed}"`);
  }
  assert.match(asked("true", "production").said, /^::notice title=À blanc::La case « à blanc » est cochée : le mot de confirmation est ignoré/m);
  // Unticked with the word: the one way to a real run.
  const real = asked("false", "production");
  assert.equal(real.status, 0, real.said);
  assert.equal(real.output, "mode=real\n");
  // Unticked without the exact word: refused, and no mode is handed to the jobs that wait for one.
  for (const typed of ["", "Production", "PRODUCTION", "production ", " production", "prod", "oui", "main", "true", "release", "production\nproduction"]) {
    const refused = asked("false", typed);
    assert.equal(refused.status, 1, `"${typed}" was accepted`);
    assert.equal(refused.output, "", `"${typed}"`);
    assert.match(refused.said, /^::error title=Confirmation refusée::Rien n'a été tagué ni déployé\. /m);
  }
  // A box that reads neither true nor false is no run at all, word typed or not.
  for (const box of ["", "False", "FALSE", "0", "no", "null", "false ", "true\nfalse"]) {
    const refused = asked(box, "production");
    assert.equal(refused.status, 1, `"${box}" was read as a mode`);
    assert.equal(refused.output, "");
    assert.match(refused.said, /^::error title=Mode illisible::Rien n'a été tagué ni déployé\. /m);
  }
  // `real` is written by that step and by no other line of the workflow but the conditions that read it.
  const written = RELEASE.split("\n").filter((line) => /\breal\b/.test(line) && !line.includes(ONLY_REAL));
  assert.deepEqual(written.map((line) => line.trim()), ["mode=real", 'if [[ "$MODE" == real ]]; then']);
});

/** What GitHub answers for an environment: one created without any rule, one set as section 5.5 of docs/deploiement.md asks, and three in between. */
const ENVIRONMENT = {
  unset: JSON.stringify({ name: "Production", protection_rules: [], deployment_branch_policy: null }),
  set: JSON.stringify({
    name: "production",
    protection_rules: [
      { id: 1, type: "required_reviewers", prevent_self_review: false, reviewers: [{ type: "User", reviewer: { login: "responsable" } }] },
      { id: 2, type: "branch_policy" },
    ],
    deployment_branch_policy: { protected_branches: false, custom_branch_policies: true },
  }),
  unreviewed: JSON.stringify({ protection_rules: [{ id: 2, type: "branch_policy" }], deployment_branch_policy: { protected_branches: false, custom_branch_policies: true } }),
  emptyReview: JSON.stringify({ protection_rules: [{ id: 1, type: "required_reviewers", reviewers: [] }], deployment_branch_policy: { protected_branches: true, custom_branch_policies: false } }),
  anyBranch: JSON.stringify({ protection_rules: [{ id: 1, type: "required_reviewers", reviewers: [{ type: "User" }] }], deployment_branch_policy: null }),
};
const ENVIRONMENT_ENDPOINT = "repos/anheart/depot/environments/production#1";

test("EX-1 a real run is refused while the environment production has no approval rule, and a dry run says so", () => {
  const request = job("request");
  const step = stepNamed(request, APPROVAL);
  assert.deepEqual(handed(step), ["GH_TOKEN: ${{ github.token }}", "MODE: ${{ steps.mode.outputs.mode }}"]);
  assert.deepEqual(block(request, "    permissions"), ["actions: read"]);
  const approval = script(request, APPROVAL);
  const judged = (/** @type {string} */ mode, /** @type {string | undefined} */ answer) =>
    run(approval, { GH_TOKEN: TOKEN, MODE: mode }, { answers: answer === undefined ? {} : { [ENVIRONMENT_ENDPOINT]: answer } });

  for (const mode of ["real", "dry"]) {
    const set = judged(mode, ENVIRONMENT.set);
    assert.equal(set.status, 0, set.said);
    assert.doesNotMatch(set.said, /::(error|warning)/);
    assert.equal(set.calls.length, 1);
    assert.ok(set.calls[0]?.startsWith("api repos/anheart/depot/environments/production --jq "), set.calls[0]);
    assert.ok(set.calls[0]?.endsWith(`|${TOKEN}`));
  }
  for (const [answer, said] of /** @type {[string | undefined, RegExp][]} */ ([
    [ENVIRONMENT.unset, /personne n'y est exigé pour approuver/],
    [ENVIRONMENT.unreviewed, /personne n'y est exigé pour approuver/],
    [ENVIRONMENT.emptyReview, /personne n'y est exigé pour approuver/],
    [ENVIRONMENT.anyBranch, /il n'est restreint à aucune branche/],
    [undefined, /il est introuvable ou illisible/],
  ])) {
    const refused = judged("real", answer);
    assert.equal(refused.status, 1, `a real run went on: ${answer}`);
    assert.match(refused.said, /^::error title=Environnement non protégé::Rien n'a été tagué ni déployé\. /m);
    assert.match(refused.said, said);
    const warned = judged("dry", answer);
    assert.equal(warned.status, 0, warned.said);
    assert.match(warned.said, /^::warning title=Environnement non protégé::Une release réelle serait refusée : /m);
    assert.match(warned.said, said);
  }
  // A mode this step does not know is not a dry run.
  assert.equal(judged("", ENVIRONMENT.unset).status, 1);
});

test("EX-1 the top of the run says, before any approval, what this run will do", () => {
  const request = job("request");
  assert.deepEqual(handed(stepNamed(request, REMIND)), ["MODE: ${{ steps.mode.outputs.mode }}"]);
  const real = run(script(request, REMIND), { MODE: "real" });
  assert.equal(real.status, 0, real.said);
  assert.match(real.summary, /^### Release RÉELLE : à lire avant d'approuver\n\n- Branche `main`, commit `0123456789abcdef0123456789abcdef01234567`\.\n/);
  assert.match(real.summary, /pose les tags, déploie Convex, puis le site, puis la simulation, en production\. Chacune de ces quatre écritures attend une approbation\./);
  assert.match(real.summary, /^- Jamais pendant une séance/m);
  assert.match(real.summary, /^- Convex est déployé avant toute console\. .* si elle la change, mettre les consoles à jour d'abord, avant d'approuver le déploiement de Convex \(docs\/deploiement\.md, section 3\.4\)\.$/m);
  const dry = run(script(request, REMIND), { MODE: "dry" });
  assert.match(dry.summary, /^### Release à blanc\n/);
  assert.match(dry.summary, /rien n'est tagué, rien n'est déployé, aucune machine n'est touchée\./);
  // Anything but `real` is described as a dry run.
  assert.match(run(script(request, REMIND), { MODE: "" }).summary, /^### Release à blanc\n/);
});

// ---------------------------------------------------------------------------
// The order of the stages
// ---------------------------------------------------------------------------

/** The jobs a job waits for, as written. @param {string} id @returns {string[]} */
const needs = (id) => {
  const written = own(job(id), "needs");
  if (written === undefined) return [];
  return written.startsWith("[") ? written.slice(1, -1).split(", ") : [written];
};
/** Every job that must have ended before a job starts. @param {string} id @returns {Set<string>} */
function before(id) {
  const all = new Set();
  const walk = (/** @type {string} */ from) => {
    for (const waited of needs(from)) {
      if (all.has(waited)) continue;
      all.add(waited);
      walk(waited);
    }
  };
  walk(id);
  return all;
}

test("EX-8 the stages run in the order asked for, each waiting for the one before, and a failure stops what follows", () => {
  assert.deepEqual(
    Object.fromEntries(ORDER.map((id) => [id, needs(id)])),
    {
      request: [],
      gates: ["request"],
      install: ["request"],
      codeql: ["gates", "install"],
      alerts: ["codeql"],
      "dry-run": ["request", "alerts"],
      tags: ["request", "alerts"],
      convex: ["request", "tags"],
      "vercel-site": ["request", "convex"],
      "site-answers": ["request", "vercel-site"],
      "vercel-simulation": ["request", "site-answers"],
      "simulation-answers": ["request", "vercel-simulation"],
      machines: ["request", "simulation-answers"],
    },
  );
  // Tests, then CodeQL, then the tags, then Convex, then the site, then the simulation, then the machines.
  const chain = ["gates", "codeql", "alerts", "tags", "convex", "vercel-site", "site-answers", "vercel-simulation", "simulation-answers", "machines"];
  chain.forEach((id, index) => {
    for (const earlier of chain.slice(0, index)) assert.ok(before(id).has(earlier), `${id} can start before ${earlier} has ended`);
  });
  for (const id of ["codeql", "alerts", "dry-run", ...REAL]) assert.ok(before(id).has("install"), `${id} does not wait for the install of the Pi`);
  // The dry run waits for the two stages that ran for real, and nothing waits for it.
  assert.ok(before("dry-run").has("gates") && before("dry-run").has("alerts"));
  for (const id of ORDER) assert.ok(!needs(id).includes("dry-run"), `${id} waits for the dry run`);
  // A job starts only if every job it waits for succeeded: no status function lifts that default,
  // and no step is allowed to fail without failing its job.
  assert.doesNotMatch(RELEASE, /always\(\)|cancelled\(\)|failure\(\)|success\(\)|continue-on-error/);
  // The only conditions are the mode, on the jobs of stages 3 to 6, and the state of the tags, on the two steps that call the release script.
  const conditions = RELEASE.split("\n").filter((line) => /^\s*if:/.test(line)).map((line) => line.trim());
  assert.deepEqual([...new Set(conditions)].sort(), [`if: ${ONLY_DRY}`, `if: ${ONLY_REAL}`, "if: ${{ steps.state.outputs.state == 'pending' }}"].sort());
  assert.equal(conditions.filter((line) => line === `if: ${ONLY_REAL}`).length, REAL.length);
  // Each stage has its number in its name, in that order.
  assert.deepEqual(
    ORDER.map((id) => own(job(id), "name")),
    [
      "0. Vérifier la demande (release)",
      "1. Tout tester (gates de la CI)",
      "1. Tout tester (installation du Pi)",
      "2. CodeQL (analyse de ce commit)",
      "2. CodeQL (aucune alerte ouverte)",
      "3 à 6. À blanc (rien n'est écrit)",
      "3. Tags de release",
      "4. Convex en production",
      "5. Vercel (site)",
      "5. Vercel (le site répond)",
      "5. Vercel (simulation)",
      "5. Vercel (la simulation répond)",
      "6. Machines (aucune mise à jour à distance)",
    ],
  );
  // Two releases never run at once, and a second one waits: one in progress is never cancelled.
  assert.deepEqual(block(RELEASE, "concurrency"), ["group: release-production", "cancel-in-progress: false"]);
  assert.equal(RELEASE.split("concurrency:").length - 1, 1);
});

// ---------------------------------------------------------------------------
// EX-2: everything is tested, by the workflows that already test it
// ---------------------------------------------------------------------------

test("EX-2 stage 1 runs the jobs of ci.yml and of pi-install.yml themselves, with no gate skipped", () => {
  for (const [id, file] of Object.entries(CALLS)) {
    assert.equal(own(job(id), "uses"), `./${WORKFLOWS}/${file}`, id);
    // A job that calls a workflow has no step of its own: nothing is copied here.
    assert.deepEqual(stepsOf(job(id)), [], id);
    assert.equal(own(job(id), "runs-on"), undefined, id);
  }
  assert.deepEqual(
    [...JOBS.keys()].filter((id) => own(job(id), "uses") !== undefined),
    Object.keys(CALLS),
  );
  // The three workflows of stages 1 and 2 are called as they are: no input, no secret.
  for (const id of ["gates", "install", "codeql"]) {
    assert.deepEqual(keysUnder(JOBS.get(id) ?? "", `  ${id}`), ["name", "needs", "permissions", "uses"], id);
  }
  for (const [file, text] of [["ci.yml", CI], ["pi-install.yml", INSTALL], ["codeql.yml", CODEQL]]) {
    assert.match(text, /^ {2}workflow_call:$/m, `${file} cannot be called`);
    assert.doesNotMatch(text, /^ {2}workflow_call:\n {4}/m, `${file} asks its caller for something`);
  }
  // A called workflow sees the event of its caller, the button: the path rule skips a gate on a pull
  // request only, so every gate of ci.yml runs, and the synthetic report runs with its `--dsp` scenarios.
  assert.deepEqual(keysUnder(RELEASE, "on"), ["workflow_dispatch"]);
  const forced = decide("workflow_dispatch", ["docs/release.md"]);
  assert.deepEqual([forced.python, forced.node], [true, true]);
  const gated = [...jobsOf(CI)].filter(([, text]) => /^ {4}if:/m.test(text));
  assert.ok(gated.length >= 7, "the conditions of ci.yml were not read");
  for (const [id, text] of gated) {
    if (id === "quality-report") continue;
    assert.match(own(text, "if") ?? "", /\(github\.event_name != 'pull_request' \|\| /, `${id} could be skipped on a manual run`);
  }
  assert.match(jobsOf(CI).get("simulation-report") ?? "", /"\$EVENT_NAME" == schedule \|\| "\$EVENT_NAME" == workflow_dispatch/);
  // Everything: the six required checks, and the install of the Pi, which is no job of ci.yml.
  for (const gate of ["pi-gate", "simulation-gate", "convex-tests", "web", "audit", "docs"]) assert.ok(jobsOf(CI).has(gate), gate);
  assert.deepEqual([...jobsOf(INSTALL).keys()], ["pi-install"]);
  // Left out on purpose, and said so in docs/release.md: the nightly endurance runs on the nightly event alone.
  assert.match(jobsOf(CI).get("pi-tests") ?? "", /^ {8}if: \$\{\{ github\.event_name == 'schedule' && matrix\.endurance \}\}$/m);
  assert.match(read("docs/release.md"), /l'endurance nocturne/);
});

// ---------------------------------------------------------------------------
// EX-3: CodeQL on this commit, and no open alert for main
// ---------------------------------------------------------------------------

const CODE_SCANNING = "repos/anheart/depot/code-scanning";
const ANALYSES = `${CODE_SCANNING}/analyses?ref=refs/heads/main&per_page=100#1`;
const alertsOf = (/** @type {string} */ state, page = 1) => `${CODE_SCANNING}/alerts?ref=refs/heads/main&state=${state}&per_page=100#${page}`;
/** @param {string} sha @param {string[]} languages */
const analysed = (sha, languages) =>
  JSON.stringify(languages.map((language) => ({ commit_sha: sha, ref: "refs/heads/main", category: `/language:${language}`, tool: { name: "CodeQL" } })));
/** An alert as GitHub lists it, with what must never reach a public log. @param {number} number */
const alert = (number) => ({ number, state: "open", rule: { id: "py/regle-qui-ne-doit-pas-sortir" }, most_recent_instance: { location: { path: "chemin/qui/ne/doit/pas/sortir.py" } } });
const LANGUAGES = ["python", "javascript-typescript", "actions"];
/** @param {Record<string, string>} answers @param {string} left the one that is not answered */
const without = (answers, left) => Object.fromEntries(Object.entries(answers).filter(([address]) => address !== left));

test("EX-3 CodeQL analyses the commit, then a single open alert for main stops the run", () => {
  const codeql = job("codeql");
  const alerts = job("alerts");
  assert.deepEqual(stepsOf(alerts).map(labelOf), [ALERTS]);
  assert.deepEqual(handed(stepNamed(alerts, ALERTS)), ["GH_TOKEN: ${{ github.token }}"]);
  // The languages the gate waits for are the ones codeql.yml analyses.
  assert.match(CODEQL, /^ {8}language: \[python, javascript-typescript, actions\]$/m);
  assert.equal(ENV.CODEQL_LANGUAGES, LANGUAGES.join(" "));
  assert.match(codeql, /^ {4}uses: \.\/\.github\/workflows\/codeql\.yml$/m);

  const gate = script(alerts, ALERTS);
  const judged = (/** @type {Record<string, string>} */ answers) => run(gate, { GH_TOKEN: TOKEN }, { answers });
  const clean = { [ANALYSES]: analysed(SHA, LANGUAGES), [alertsOf("open")]: "[]", [alertsOf("dismissed")]: JSON.stringify([alert(3), alert(4)]) };

  // Nothing open: the run goes on. A dismissed alert is not open: it is counted and shown, and does not stop the run.
  const passed = judged(clean);
  assert.equal(passed.status, 0, passed.said);
  assert.equal(
    passed.summary,
    [
      "### 2. CodeQL : aucune alerte ouverte sur `main`",
      "",
      `- Commit analysé : \`${SHA}\` (python javascript-typescript actions).`,
      "- Alertes ouvertes : 0.",
      "- Alertes classées après relecture, qui ne bloquent pas : 2. Elles se lisent dans l'onglet Security, pas ici.",
      "",
    ].join("\n"),
  );
  assert.equal(passed.calls.length, 3);
  for (const call of passed.calls) assert.ok(call.endsWith(`|${TOKEN}`), call);
  assert.ok(passed.calls[1]?.startsWith("api --paginate repos/anheart/depot/code-scanning/alerts?ref=refs/heads/main&state=open&per_page=100 "));

  // One alert open, among none or after a full page of them: refused, with their number and nothing of what they say.
  for (const [answers, count] of /** @type {[Record<string, string>, number][]} */ ([
    [{ ...clean, [alertsOf("open")]: JSON.stringify([alert(7)]) }, 1],
    [{ ...clean, [alertsOf("open")]: JSON.stringify(Array.from({ length: 100 }, (_, index) => alert(index + 1))), [alertsOf("open", 2)]: JSON.stringify([alert(101)]) }, 101],
  ])) {
    const refused = judged(answers);
    assert.equal(refused.status, 1, `${count} open alert(s) let the run go on`);
    assert.ok(refused.said.includes(`::error title=Alertes ouvertes::Aucun tag n'est posé et rien n'est déployé. CodeQL a ${count} alerte(s) ouverte(s) sur main. `), refused.said);
    assert.equal(refused.summary, "");
    // What an alert says is readable by those who can write to the repository only: the log of a run is public.
    assert.doesNotMatch(refused.said + passed.said + passed.summary, /regle-qui-ne-doit-pas-sortir|chemin\/qui\/ne\/doit\/pas\/sortir/);
  }
  // The alerts of main are only worth reading if this commit was analysed, in each language.
  for (const [answer, missing] of /** @type {[string, string][]} */ ([
    [analysed(SHA, ["python", "javascript-typescript"]), "actions"],
    [analysed("f".repeat(40), LANGUAGES), "python javascript-typescript actions"],
    ["[]", "python javascript-typescript actions"],
    [JSON.stringify([{ commit_sha: SHA, category: "/language:python", tool: { name: "Autre outil" } }, ...JSON.parse(analysed(SHA, ["javascript-typescript", "actions"]))]), "python"],
  ])) {
    const refused = judged({ ...clean, [ANALYSES]: answer });
    assert.equal(refused.status, 1, `went on without the analysis of ${missing}`);
    assert.ok(refused.said.includes(`::error title=Analyse absente::Aucun tag n'est posé et rien n'est déployé. CodeQL n'a pas publié l'analyse du commit ${SHA} pour : ${missing}.`), refused.said);
  }
  // What cannot be read is not taken for an empty list.
  for (const answers of [without(clean, ANALYSES), { ...clean, [ANALYSES]: '{"message":"Not Found"}' }]) {
    const refused = judged(answers);
    assert.equal(refused.status, 1);
    assert.match(refused.said, /^::error title=Analyses illisibles::Aucun tag n'est posé et rien n'est déployé\. /m);
  }
  for (const state of ["open", "dismissed"]) {
    for (const answers of [without(clean, alertsOf(state)), { ...clean, [alertsOf(state)]: '{"message":"Not Found"}' }]) {
      const refused = judged(answers);
      assert.equal(refused.status, 1, state);
      assert.match(refused.said, /^::error title=Alertes illisibles::Aucun tag n'est posé et rien n'est déployé\. /m, state);
    }
  }
});

// ---------------------------------------------------------------------------
// Dry run: stages 3 to 6 say what they would do, and can write nothing
// ---------------------------------------------------------------------------

/**
 * The commands of a script: its lines, without those that only print. A line that prints may read a
 * file into what it prints (`$(< file)`) and run nothing else.
 * @param {string} text
 */
const commands = (text) => {
  // Between single quotes nothing is run: only the lines that print through double quotes are read.
  const printed = text.split("\n").filter((line) => /^\s*echo /.test(line) && !/^\s*echo '[^']*'$/.test(line));
  for (const line of printed) assert.doesNotMatch(line, /\$\((?!<)|(?<!\\)`/, `a line that prints runs a command: ${line.trim()}`);
  return text
    .split("\n")
    .filter((line) => !/^\s*echo /.test(line))
    .join("\n");
};

/** Every command of the pipeline, job after job: what its steps run, without what they only print. */
const everyCommand = () =>
  ORDER.flatMap((id) =>
    stepsOf(job(id))
      .filter((step) => /^ {8}run:/m.test(step))
      .map((step) => commands(script(job(id), labelOf(step)))),
  ).join("\n");

/** What a script would need to write somewhere: to the repository, to a deployment, to a machine. */
const WRITES = /\bgit (push|tag|commit|merge|reset|update-ref)\b|\bgh (pr|release|workflow|api .*(-X|--method|-f|--field))\b|\bnpx\b|\bnpm\b|\bvercel\b|\bconvex (deploy|run|import|env)\b|\bcurl\b|\bssh\b|\bscp\b|\brsync\b/;

test("EX-1 on a dry run no job of stages 3 to 6 starts, and the job that speaks for them cannot write", () => {
  // Each job of stages 3 to 6 carries the one condition, and reads the mode from the job that worked it out.
  for (const id of REAL) {
    assert.equal(own(job(id), "if"), ONLY_REAL, `${id} can start on a dry run`);
    assert.ok(needs(id).includes("request"), `${id} does not read the mode from the job that works it out`);
  }
  for (const id of ORDER.filter((other) => !REAL.includes(other) && other !== "dry-run")) {
    assert.equal(own(job(id), "if"), undefined, `${id}: stages 1 and 2 run on a dry run as on a real one`);
  }
  // Whatever can write is in those jobs and nowhere else: the environment, a secret, a permission to write
  // (the analysis of CodeQL apart, which publishes its results on a dry run too), a deployment.
  for (const id of ORDER.filter((other) => !REAL.includes(other))) {
    const text = job(id);
    assert.equal(own(text, "environment"), undefined, `${id} goes through an environment`);
    assert.doesNotMatch(text, /secrets\.|secrets:/, `${id} reads a secret`);
    assert.doesNotMatch(text, /contents: write|uses: .*deploy-production\.yml/, id);
  }

  const dry = job("dry-run");
  assert.equal(own(dry, "if"), ONLY_DRY);
  assert.deepEqual(block(dry, "    permissions"), ["contents: read", "checks: read", "statuses: read"]);
  assert.deepEqual(stepsOf(dry).map(labelOf), ["uses actions/checkout", STILL_HEAD, STATE, WOULD_TAG, WOULD_DEPLOY]);
  // It takes the head of main as the real stages do, leaves no token in the clone, and reads what they read.
  assert.deepEqual(stepNamed(dry, "uses actions/checkout").trimEnd().split("\n").slice(1), ["        with:", "          persist-credentials: false", "          ref: refs/heads/main", "          fetch-depth: 0"]);
  assert.equal(script(dry, STATE), script(job("tags"), STATE), "the dry run does not read the tags as the real stage does");
  // The token of the run, which can only read here, goes to the one step that asks GitHub for the checks of the commit.
  assert.deepEqual(stepsOf(dry).filter((step) => step.includes("github.token")).map(labelOf), [WOULD_TAG]);
  assert.deepEqual(handed(stepNamed(dry, WOULD_TAG)), ["GH_TOKEN: ${{ github.token }}"]);
  // No command of this job writes: the release script is asked for its own dry run, and nothing else is called.
  for (const step of stepsOf(dry).filter((written) => /^ {8}run:/m.test(written))) {
    const text = script(dry, labelOf(step));
    if (labelOf(step) === WOULD_TAG) {
      assert.equal(text, 'export GH_REPO="$GITHUB_REPOSITORY"\nscripts/release.sh tag --dry-run --expect-commit "$GITHUB_SHA" --pipeline-run "$GITHUB_RUN_ID"\n');
    } else {
      assert.doesNotMatch(commands(text), WRITES, `${labelOf(step)} can write`);
      assert.doesNotMatch(commands(text), /release\.sh/, labelOf(step));
    }
  }
});

/** A double of scripts/release.sh, in a throwaway directory: it writes down how it was called and what git was told. */
const releaseDouble = (/** @type {import("node:test").TestContext} */ context) => {
  const directory = mkdtempSync(join(tmpdir(), "anheart-release-double-"));
  context.after(() => rmSync(directory, { recursive: true, force: true }));
  mkdirSync(join(directory, "scripts"));
  writeFileSync(
    join(directory, "scripts", "release.sh"),
    [
      "#!/usr/bin/env bash",
      'printf \'%s|%s|%s|%s <%s>|%s\\n\' "$*" "${GH_TOKEN:-none}" "${GH_REPO:-none}" "${GIT_COMMITTER_NAME:-none}" "${GIT_COMMITTER_EMAIL:-none}" "$(git config --get http.https://github.com/.extraheader || echo none)" >> "$DOUBLE_CALLS"',
      'exit "${DOUBLE_RELEASE_STATUS:-0}"',
      "",
    ].join("\n"),
  );
  chmodSync(join(directory, "scripts", "release.sh"), 0o755);
  return directory;
};

test("EX-1 the dry run asks the release script for its own dry run, on the commit that was tested, and says what would follow", (context) => {
  const dry = job("dry-run");
  const called = run(script(dry, WOULD_TAG), { GH_TOKEN: TOKEN }, { cwd: releaseDouble(context) });
  assert.equal(called.status, 0, called.said);
  // The script is told the commit and the run; it is handed no way to push: no tagger, no credential for git.
  assert.deepEqual(called.calls, [`tag --dry-run --expect-commit ${SHA} --pipeline-run 77|${TOKEN}|anheart/depot|Synthetic Releaser <release@example.invalid>|none`]);
  // What the script refuses, the dry run fails on: a real run would be refused the same way.
  assert.equal(run(script(dry, WOULD_TAG), { GH_TOKEN: TOKEN, DOUBLE_RELEASE_STATUS: "1" }, { cwd: releaseDouble(context) }).status, 1);

  const fx = repository(context);
  const said = run(script(dry, WOULD_DEPLOY), { GITHUB_SHA: fx.head }, { cwd: fx.work });
  assert.equal(said.status, 0, said.said);
  assert.deepEqual(said.calls, []);
  assert.equal(
    said.summary,
    [
      "### 4 à 6. Ce qu'une release réelle ferait ensuite",
      "",
      "Rien de ce qui suit n'a été fait.",
      "",
      "- **4. Convex** : `npx convex deploy` vers le déploiement de production `clean-giraffe-153`, avec la clé de l'environnement GitHub `production`. Version attendue ensuite : `cloud-0.1.0`.",
      "- **5. Vercel** : le site (projet `anheart`), vérifié à https://www.gauratechnologies.com, puis la simulation hébergée (projet `anheart-simulation`), vérifiée à https://anheart-simulation.vercel.app.",
      "- **6. Machines** : aucune action. La version du Pi de ce commit est `pi-0.1.0` ; une console se met à jour à la main (docs/deploiement.md, section 7.6).",
      "- Une release réelle demande une approbation avant chacune de ses quatre écritures : les tags, Convex, le site, la simulation.",
      "",
    ].join("\n"),
  );
});

// ---------------------------------------------------------------------------
// EX-4: the tags, by scripts/release.sh, never twice
// ---------------------------------------------------------------------------

test("EX-4 a run acts on the head of main only: started late or run again after main moved, it is refused", (context) => {
  const fx = repository(context);
  // One text for every job that takes the head of the branch, here and behind the deployment buttons.
  const reference = script(jobsOf(PRODUCTION).get("site") ?? "", STILL_HEAD);
  for (const [id, unchanged] of /** @type {[string, string][]} */ ([
    ["dry-run", "Rien ne serait tagué ni déployé"],
    ["tags", "Aucun tag n'a été posé"],
    ["convex", "Convex n'a pas été déployé"],
  ])) {
    const text = job(id);
    assert.equal(script(text, STILL_HEAD), reference, id);
    assert.deepEqual(block(text, "    env"), [`NOT_DEPLOYED: ${unchanged}`], id);
    // Right after the checkout, which took the head of the branch as it is now, and before anything else.
    const steps = stepsOf(text).map(labelOf);
    assert.equal(steps.indexOf(STILL_HEAD), steps.indexOf("uses actions/checkout") + 1, id);
    assert.match(stepNamed(text, "uses actions/checkout"), /^ {10}ref: refs\/heads\/main$/m, id);
    assert.doesNotMatch(stepNamed(text, STILL_HEAD), /env:|secrets\./, id);
    const current = run(reference, { NOT_DEPLOYED: unchanged, GITHUB_SHA: fx.head }, { cwd: fx.work });
    assert.equal(current.status, 0, `${id}: ${current.said}`);
    for (const started of [SHA, fx.head.slice(0, 12), ""]) {
      const stale = run(reference, { NOT_DEPLOYED: unchanged, GITHUB_SHA: started }, { cwd: fx.work });
      assert.equal(stale.status, 1, `${id}: a run started on "${started}" went on`);
      assert.ok(stale.said.includes(`::error title=Exécution périmée::${unchanged}. Cette exécution a été lancée sur le commit ${started}, et la tête de main est maintenant ${fx.head}.`), stale.said);
    }
  }
});

test("EX-4 the tags are put when a version waits for one, found when they are on this commit, and refused otherwise", (context) => {
  const tags = job("tags");
  assert.deepEqual(stepsOf(tags).map(labelOf), ["uses actions/checkout", STILL_HEAD, STATE, TAG, TAGGED]);
  assert.match(stepNamed(tags, STATE), /^ {8}id: state$/m);
  assert.doesNotMatch(stepNamed(tags, STATE), /secrets\.|github\.token/);
  const state = script(tags, STATE);
  const stateOf = (/** @type {ReturnType<typeof repository>} */ fx, sha = fx.head) =>
    run(state, { HEADING: "3. Tags de release", NOT_DEPLOYED: "Aucun tag n'a été posé", GITHUB_SHA: sha }, { cwd: fx.work });

  // A release that was just merged: its three versions wait for their tags.
  const fresh = repository(context);
  const pending = stateOf(fresh);
  assert.equal(pending.status, 0, pending.said);
  assert.equal(pending.output, "state=pending\n");
  assert.equal(
    pending.summary,
    ["### 3. Tags de release", "", `Commit \`${fresh.head}\`.`, "", "- `pi-0.1.0` : attend son tag, à poser sur ce commit.", "- `cloud-0.1.0` : attend son tag, à poser sur ce commit.", "- `web-0.1.0` : attend son tag, à poser sur ce commit.", ""].join("\n"),
  );
  // The same run again once the tags are on origin, on this commit: nothing to put, and the run goes on.
  for (const name of ["pi-0.1.0", "cloud-0.1.0", "web-0.1.0"]) fresh.tag(name, fresh.head);
  const again = stateOf(fresh);
  assert.equal(again.status, 0, again.said);
  assert.equal(again.output, "state=tagged\n");
  assert.match(again.summary, /^- `pi-0\.1\.0` : tag déjà posé sur ce commit\.$/m);
  assert.match(again.said, /^Aucun tag à poser : ceux de cette release sont déjà sur ce commit\.$/m);

  // main moved after that release without a new version: this commit is no release, and nothing is deployed from it.
  const later = fresh.commit("ANH-99 : corriger une page (#10)", { "docs/page.md": "# Page\n" });
  const nothing = stateOf(fresh, later);
  assert.equal(nothing.status, 1, "a commit that is no release went on");
  assert.equal(nothing.output, "");
  assert.ok(nothing.said.includes("::error title=Rien à publier::Aucun tag n'a été posé. Aucune version de ce commit n'attend son tag, et aucun tag de release ne le désigne : ce commit de main n'est pas une release."), nothing.said);
  assert.match(nothing.summary, new RegExp(`^- \`web-0\\.1\\.0\` : tag d'une release précédente, sur le commit \`${fresh.head}\`\\.$`, "m"));

  // A release of the site alone: its tag waits, the two others stay where the release before put them.
  const partial = fresh.commit("Release : web-0.2.0 (#11)", { "package.json": `${JSON.stringify({ name: "fixture", version: "0.2.0", private: true }, null, 2)}\n` });
  const one = stateOf(fresh, partial);
  assert.equal(one.output, "state=pending\n", one.said);
  assert.match(one.summary, /^- `web-0\.2\.0` : attend son tag, à poser sur ce commit\.$/m);
  assert.match(one.summary, /^- `pi-0\.1\.0` : tag d'une release précédente, /m);

  // Versions of development are no release; a version file that holds no version stops the run.
  const development = repository(context, { pi: "pi-0.0.0-dev", cloud: "cloud-0.0.0-dev", web: "0.0.0-dev" });
  const none = stateOf(development);
  assert.equal(none.status, 1);
  assert.match(none.said, /^::error title=Rien à publier::/m);
  assert.match(none.summary, /^- `pi-0\.0\.0-dev` : version de développement, sans tag\.$/m);
  for (const broken of [{ pi: "0.1.0" }, { cloud: "" }, { pi: "pi-0.1.0\nautre ligne" }, { web: "0.1.0 `x`" }]) {
    const unreadable = stateOf(repository(context, broken));
    assert.equal(unreadable.status, 1, JSON.stringify(broken));
    assert.match(unreadable.said, /^::error title=Version illisible::Aucun tag n'a été posé\. /m, JSON.stringify(broken));
    assert.equal(unreadable.output, "");
  }
});

test("EX-4 the tags are put by scripts/release.sh, told the commit and the run, with a token that stays in that one step", (context) => {
  const tags = job("tags");
  const step = stepNamed(tags, TAG);
  // Only when a version waits for its tag: run again once they are put, the step does not run.
  assert.match(step, /^ {8}if: \$\{\{ steps\.state\.outputs\.state == 'pending' \}\}$/m);
  assert.deepEqual(handed(step), [
    "GH_TOKEN: ${{ github.token }}",
    "GIT_COMMITTER_NAME: github-actions[bot]",
    "GIT_COMMITTER_EMAIL: 41898282+github-actions[bot]@users.noreply.github.com",
  ]);
  // The token of the job that may write is handed to that step and to no other, and never left in the clone.
  assert.deepEqual(stepsOf(tags).filter((written) => written.includes("github.token")).map(labelOf), [TAG]);
  assert.deepEqual(stepNamed(tags, "uses actions/checkout").trimEnd().split("\n").slice(1), ["        with:", "          persist-credentials: false", "          ref: refs/heads/main", "          fetch-depth: 0"]);
  const put = script(tags, TAG);
  assert.equal(put.match(/release\.sh/g)?.length, 1);
  assert.doesNotMatch(put, /--dry-run|git (push|tag)|gh auth|set -[a-z]*x/);

  const given = { GH_TOKEN: TOKEN, GIT_COMMITTER_NAME: "github-actions[bot]", GIT_COMMITTER_EMAIL: "41898282+github-actions[bot]@users.noreply.github.com" };
  const called = run(put, given, { cwd: releaseDouble(context) });
  assert.equal(called.status, 0, called.said);
  const basic = Buffer.from(`x-access-token:${TOKEN}`).toString("base64");
  // The script is told the commit that was tested and the run it is part of, and git is told how to push, for this step only.
  assert.deepEqual(called.calls, [
    `tag --expect-commit ${SHA} --pipeline-run 77|${TOKEN}|anheart/depot|github-actions[bot] <41898282+github-actions[bot]@users.noreply.github.com>|AUTHORIZATION: basic ${basic}`,
  ]);
  // Nothing of the token is printed: the one line that carries its encoded form tells the runner to hide it.
  assert.ok(!called.said.includes(TOKEN));
  assert.deepEqual(called.said.split("\n").filter((line) => line.includes(basic)), [`::add-mask::${basic}`]);
  // A script that refuses fails the job: nothing after it runs.
  assert.equal(run(put, { ...given, DOUBLE_RELEASE_STATUS: "1" }, { cwd: releaseDouble(context) }).status, 1);

  // After it, the job goes on only if a release tag of origin points at this commit.
  assert.doesNotMatch(stepNamed(tags, TAGGED), /env:|secrets\.|if:/);
  const fx = repository(context);
  const found = script(tags, TAGGED);
  const absent = run(found, { GITHUB_SHA: fx.head }, { cwd: fx.work });
  assert.equal(absent.status, 1);
  assert.ok(absent.said.includes(`::error title=Tags absents::Aucun tag de release ne désigne le commit ${fx.head} sur origin. Rien n'est déployé.`), absent.said);
  for (const name of ["web-0.1.0", "pi-0.1.0", "cloud-0.1.0", "autre-1.0.0"]) fx.tag(name, fx.head);
  const present = run(found, { GITHUB_SHA: fx.head }, { cwd: fx.work });
  assert.equal(present.status, 0, present.said);
  assert.match(present.summary, /^Tags de release sur ce commit, sur `origin` : cloud-0\.1\.0 pi-0\.1\.0 web-0\.1\.0 $/m);
});

// ---------------------------------------------------------------------------
// EX-5: Convex, with a key that is checked before and a backend that is checked after
// ---------------------------------------------------------------------------

const GOOD_KEY = `prod:clean-giraffe-153|${SECRET}`;

test("EX-5 the deploy key is read by the three steps that need it, and by nothing that installs", () => {
  const convex = job("convex");
  assert.deepEqual(stepsOf(convex).map(labelOf), [KEY, "uses actions/checkout", STILL_HEAD, "uses actions/setup-node", DEPENDENCIES, DEPLOY_CONVEX, SERVED, REFUSAL, RECORD]);
  assert.deepEqual(block(convex, "    environment"), ["name: production"]);
  // One secret, by its name, through env, in those steps: never the checkout, the install of npm packages or a public check.
  const holding = stepsOf(convex).filter((step) => step.includes("secrets."));
  assert.deepEqual(holding.map(labelOf), [KEY, DEPLOY_CONVEX, SERVED]);
  for (const step of holding) assert.deepEqual(handed(step), ["CONVEX_DEPLOY_KEY: ${{ secrets.CONVEX_DEPLOY_KEY }}"], labelOf(step));
  assert.doesNotMatch(convex, /github\.token|secrets: inherit/);
  for (const name of [STILL_HEAD, DEPENDENCIES, REFUSAL, RECORD]) assert.doesNotMatch(stepNamed(convex, name), /env:/, name);
  // The command line of Convex is the one the lockfile holds, and it is asked to deploy, nothing more.
  assert.equal(script(convex, DEPENDENCIES), "npm ci\n");
  assert.equal(script(convex, DEPLOY_CONVEX), "npx convex deploy\n");
  assert.deepEqual(stepNamed(convex, "uses actions/setup-node").trimEnd().split("\n").slice(1), ["        with:", "          node-version: '24'"]);
  assert.equal(everyCommand().split("convex deploy").length - 1, 1, "Convex is deployed by one step");
  assert.doesNotMatch(convex, /--preview|--admin-key|--url|convex (dev|import|env)|CONVEX_DEPLOYMENT\b/);
});

test("EX-5 Convex is not deployed unless the key aims at the expected production deployment, and no part of the key is ever printed", () => {
  const convex = job("convex");
  assert.equal(labelOf(stepsOf(convex)[0] ?? ""), KEY, "the key is checked before anything is installed");
  const check = script(convex, KEY);
  const judged = (/** @type {string} */ key) => run(check, { NOT_DEPLOYED: "Convex n'a pas été déployé", CONVEX_DEPLOY_KEY: key });
  const right = judged(GOOD_KEY);
  assert.equal(right.status, 0, right.said);
  assert.match(right.said, /^La clé vise le déploiement de production attendu : clean-giraffe-153\.$/m);
  assert.ok(!right.said.includes(SECRET));

  // What GitHub gives for a secret that does not exist: an empty value.
  const missing = judged("");
  assert.equal(missing.status, 1);
  assert.ok(missing.said.includes("::error title=Secret manquant::Convex n'a pas été déployé. Secret absent ou vide dans l'environnement GitHub de ce job : CONVEX_DEPLOY_KEY. "), missing.said);
  for (const [key, held] of /** @type {[string, string][]} */ ([
    [`dev:standing-jay-887|${SECRET}`, "une clé de développement"],
    [`dev:clean-giraffe-153|${SECRET}`, "une clé de développement"],
    [`preview:equipe:projet|${SECRET}`, "une clé de préversion"],
    [`project:equipe:projet|${SECRET}`, "une clé de projet"],
    [`prod:autre-animal-999|${SECRET}`, "la clé de production d'un autre déploiement"],
    [`prod:clean-giraffe-1530|${SECRET}`, "la clé de production d'un autre déploiement"],
    [`prod:xclean-giraffe-153|${SECRET}`, "la clé de production d'un autre déploiement"],
    ["prod:clean-giraffe-153|", "une clé coupée avant sa partie secrète"],
    [`PROD:clean-giraffe-153|${SECRET}`, "une valeur qui n'a pas la forme d'une clé de déploiement"],
    [`prod:clean-giraffe-153 ${SECRET}`, "une valeur qui n'a pas la forme d'une clé de déploiement"],
    [SECRET, "une valeur qui n'a pas la forme d'une clé de déploiement"],
    [` ${GOOD_KEY}`, "une valeur qui n'a pas la forme d'une clé de déploiement"],
  ])) {
    const refused = judged(key);
    assert.equal(refused.status, 1, `"${key}" was accepted`);
    assert.ok(
      refused.said.includes(`::error title=Mauvaise clé::Convex n'a pas été déployé. Le secret CONVEX_DEPLOY_KEY de l'environnement GitHub de ce job ne vise pas le déploiement de production attendu (clean-giraffe-153) : il contient ${held}. `),
      refused.said,
    );
    // Neither the key, nor its secret part, nor the name it carries: a value pasted by mistake may be anything.
    for (const part of [key, SECRET, "standing-jay-887", "autre-animal-999", "clean-giraffe-1530", "equipe:projet"]) {
      assert.ok(!refused.said.includes(part), `"${part}" was printed`);
    }
  }
  assert.doesNotMatch(check, /echo .*\$CONVEX_DEPLOY_KEY|printf .*\$CONVEX_DEPLOY_KEY|\$\{CONVEX_DEPLOY_KEY[%#:]/, "the script writes the key, or a part of it");
});

test("EX-5 after the deployment, the backend must answer the version of this commit and refuse as its contract says", (context) => {
  const convex = job("convex");
  const fx = repository(context);
  // The version: asked of the deployment itself, with the key, by the query the release tooling names.
  const served = script(convex, SERVED);
  const asked = (/** @type {string} */ answer, status = "0") => run(served, { CONVEX_DEPLOY_KEY: GOOD_KEY, DOUBLE_NPX_ANSWER: answer, DOUBLE_NPX_STATUS: status }, { cwd: fx.work });
  const same = asked('"cloud-0.1.0"');
  assert.equal(same.status, 0, same.said);
  assert.deepEqual(same.calls, [`convex run softwareReleases:deployedCloudVersion|${GOOD_KEY}`]);
  assert.match(read("convex/softwareReleases.ts"), /^export const deployedCloudVersion = internalQuery\(/m);
  for (const answer of ['"cloud-0.0.9"', '"cloud-0.1.00"', "cloud-0.1.0", "", '"cloud-0.0.0-dev"']) {
    const other = asked(answer);
    assert.equal(other.status, 1, `${answer} was taken for cloud-0.1.0`);
    assert.ok(other.said.includes("::error title=Version inattendue::Convex a été déployé, mais le déploiement clean-giraffe-153 ne répond pas la version de ce commit (cloud-0.1.0). Le site n'est pas déployé : "), other.said);
    assert.ok(!other.said.includes(SECRET));
  }
  // A query that failed is no answer, whatever it printed before failing.
  const failed = asked('"cloud-0.1.0"', "1");
  assert.equal(failed.status, 1);
  assert.match(failed.said, /^::error title=Version inattendue::Convex a été déployé, mais /m);

  // The refusal: without a key, at the public address of the machine API, with the header a console sends.
  const refusal = script(convex, REFUSAL);
  const address = "https://clean-giraffe-153.convex.site/api/machine/training/poll";
  const polled = (/** @type {string | undefined} */ body, status = "401") =>
    run(refusal, {}, { cwd: fx.work, answers: body === undefined ? {} : { [address]: body }, statuses: { [address]: status } });
  const contract = JSON.parse(read("contracts/machine-api.json"));
  assert.deepEqual(contract.error_codes.unauthorized.statuses, [401]);
  const refused = polled('{"error":"unauthorized","message":"Missing Authorization header"}');
  assert.equal(refused.status, 0, refused.said);
  assert.equal(refused.calls.length, 1);
  assert.ok(refused.calls[0]?.includes(`--header ${contract.header}: ${contract.contract_version} ${address}|none|none`), refused.calls[0]);
  assert.match(read("convex/lib/machineHttpAuth.ts"), /401,\s+"unauthorized",\s+"Missing Authorization header"/);
  for (const [body, status] of /** @type {[string | undefined, string][]} */ ([
    // What the backend of before the contract answers, which is what stays in place if the deployment did not take.
    ['{"error":"Missing Authorization header"}', "401"],
    ['{"error":"unauthorized","message":"Missing Authorization header"}', "200"],
    ['{"error":"contract_unsupported"}', "426"],
    ["<html>404</html>", "404"],
    [undefined, "401"],
  ])) {
    const silent = polled(body, status);
    assert.equal(silent.status, 1, `${status} ${body} was taken for the refusal of the contract`);
    assert.ok(silent.said.includes(`::error title=API machine muette::Convex a été déployé, mais ${address} ne refuse pas une requête sans clé comme le contrat ${contract.contract_version} le dit `), silent.said);
    // It asked six times before giving up.
    assert.equal(silent.calls.filter((call) => !call.startsWith("sleep ")).length, 6);
  }
});

test("EX-5 the versions are not recorded by the pipeline: it prints, once Convex is deployed, what an admin records by hand", (context) => {
  const convex = job("convex");
  const fx = repository(context);
  fx.tag("pi-0.1.0", fx.head, PI_MESSAGE);
  fx.tag("cloud-0.1.0", fx.head, "cloud-0.1.0\n\nComposant : Convex.\n");
  fx.tag("web-0.1.0", fx.head);
  fx.tag("autre-0.1.0", fx.head);
  git(fx.work, "fetch", "--quiet", "--tags", "origin");
  // Each version is dated by its own tag: three tags put one after the other may not share a second.
  const at = (/** @type {string} */ tag) => Number(git(fx.work, "for-each-ref", "--format=%(taggerdate:unix)", `refs/tags/${tag}`)) * 1000;
  assert.ok(at("pi-0.1.0") > 1_600_000_000_000, "the date of the tag was not read");
  const told = run(script(convex, RECORD), { GITHUB_SHA: fx.head }, { cwd: fx.work });
  assert.equal(told.status, 0, told.said);
  assert.deepEqual(told.calls, [], "the step reads the clone and calls nothing");
  const lines = /```json\n([\s\S]*?)\n```/.exec(told.summary)?.[1]?.split("\n") ?? [];
  assert.deepEqual(
    lines.map((line) => JSON.parse(line)).sort((a, b) => a.component.localeCompare(b.component)),
    [
      { component: "cloud", version: "cloud-0.1.0", releasedAt: at("cloud-0.1.0") },
      { component: "pi", version: "pi-0.1.0", validationLevel: "bench", releasedAt: at("pi-0.1.0") },
      { component: "web", version: "web-0.1.0", releasedAt: at("web-0.1.0") },
    ],
  );
  // The argument is the one docs/release.md shows for the mutation, which an admin calls: the pipeline acts for no person.
  assert.match(read("docs/release.md"), /\{"component":"pi","version":"pi-0\.1\.0","validationLevel":"bench","releasedAt":\d+\}/);
  assert.match(told.summary, /^- \*\*Reste à faire à la main\*\* : enregistrer chaque version de cette release dans Convex, par un admin \(mutation `softwareReleases:recordRelease`, docs\/release\.md, section 6\)\./m);
  assert.match(told.summary, /^- Le site n'est pas encore déployé : approuver l'étape 5 aussitôt\.$/m);
  assert.doesNotMatch(everyCommand(), /recordRelease|--identity|convex run (?!softwareReleases:deployedCloudVersion\b)/, "the pipeline calls the mutation in the name of an account");
});

// ---------------------------------------------------------------------------
// EX-6: Vercel, by the jobs of the production button, the site then the simulation
// ---------------------------------------------------------------------------

test("EX-6 the site and the simulation are deployed by the jobs of deploy-production.yml, and by no copy of them", () => {
  // The button can be called, with the two inputs it asks of a person, and declares no secret: its jobs read their own.
  assert.deepEqual(keysUnder(PRODUCTION, "on"), ["workflow_dispatch", "workflow_call"]);
  const call = PRODUCTION.slice(PRODUCTION.indexOf("\n  workflow_call:\n"), PRODUCTION.indexOf("\npermissions:"));
  assert.deepEqual(keysUnder(call, "  workflow_call"), ["inputs"]);
  assert.deepEqual(block(call, "    inputs"), ["project:", "required: true", "type: string", "confirmation:", "required: true", "type: string"]);
  // Called once for each project, by name, the site first; the word the person typed is checked again by the button.
  for (const [id, project] of [["vercel-site", "site"], ["vercel-simulation", "simulation"]]) {
    const text = job(id ?? "");
    assert.equal(own(text, "uses"), "./.github/workflows/deploy-production.yml", id);
    assert.deepEqual(block(text, "    with"), [`project: ${project}`, "confirmation: ${{ inputs.confirmation }}"], id);
    assert.equal(own(text, "secrets"), "inherit", id);
    assert.deepEqual(block(text, "    permissions"), ["contents: read"], id);
    // The choice named is one the button knows: its job runs, the other is skipped.
    assert.ok((jobsOf(PRODUCTION).get(project ?? "") ?? "").includes(`inputs.project == '${project}'`), id);
  }
  assert.equal(ENV.CONFIRMATION_WORD, shared(PRODUCTION).VERCEL_TARGET, "the word the pipeline accepts is not the word the button asks for");
  // Nothing else of the repository calls the button, and `secrets: inherit` is written on those two jobs only.
  const all = readdirSync(join(root, WORKFLOWS)).filter((file) => /\.ya?ml$/.test(file));
  const callers = all.flatMap((file) => [...workflow(file).matchAll(/^ +uses: \.\/\.github\/workflows\/deploy-production\.yml$/gm)].map(() => file));
  assert.deepEqual(callers, ["release.yml", "release.yml"]);
  assert.equal(all.map(workflow).join("\n").split("secrets: inherit").length - 1, 2);
  // The pipeline holds no line of a Vercel deployment: no command line, no token, no identifier.
  assert.doesNotMatch(RELEASE, /vercel@|VERCEL_|\bvercel (pull|build|deploy|promote|alias)\b|npm install --global/);
  // The site after Convex, the simulation after the site was seen to answer.
  assert.ok(before("vercel-site").has("convex") && before("vercel-simulation").has("site-answers"));
});

test("EX-6 the site must serve the version of this commit, and the simulation the viewer page of this commit", (context) => {
  const fx = repository(context);
  for (const id of ["site-answers", "simulation-answers"]) {
    const text = job(id);
    assert.deepEqual(stepsOf(text).map(labelOf), ["uses actions/checkout", id === "site-answers" ? SITE : SIMULATION], id);
    assert.doesNotMatch(text, /env:|secrets\.|github\.token|environment:/, `${id} holds a secret or goes through the environment`);
    assert.deepEqual(block(text, "    permissions"), ["contents: read"], id);
    // The commit the run started on: what was deployed is compared with it.
    assert.deepEqual(stepNamed(text, "uses actions/checkout").trimEnd().split("\n").slice(1), ["        with:", "          persist-credentials: false"], id);
  }
  const site = script(job("site-answers"), SITE);
  const home = "https://www.gauratechnologies.com/";
  const visited = (/** @type {string | undefined} */ page, status = "200") =>
    run(site, {}, { cwd: fx.work, answers: page === undefined ? {} : { [home]: page }, statuses: { [home]: status } });
  // The footer of the site shows the version built from package.json (lib/version.ts, next.config.ts).
  assert.match(read("next.config.ts"), /NEXT_PUBLIC_WEB_VERSION: `web-\$\{packageJson\.version\}`/);
  assert.match(read("components/landing/Footer.tsx"), /\{WEB_VERSION\}/);
  const up = visited('<html><footer><span class="font-mono">web-0.1.0</span></footer></html>');
  assert.equal(up.status, 0, up.said);
  assert.equal(up.summary, "### 5. Site : il répond\n\n- https://www.gauratechnologies.com sert `web-0.1.0`.\n");
  assert.ok(up.calls[0]?.includes("--location") && up.calls[0]?.endsWith(`${home}|none|none`), up.calls[0]);
  for (const [page, status] of /** @type {[string | undefined, string][]} */ ([
    ["<html><span>web-0.0.9</span></html>", "200"],
    ["<html><span>web-inconnue</span></html>", "200"],
    ["<html><span>web-0.1.0</span></html>", "500"],
    [undefined, "200"],
  ])) {
    const down = visited(page, status);
    assert.equal(down.status, 1, `${status} ${page} was taken for the site of this commit`);
    assert.match(down.said, /^::error title=Site inattendu::Le déploiement Vercel du site a réussi, mais https:\/\/www\.gauratechnologies\.com ne sert pas web-0\.1\.0 /m);
    assert.match(down.said, /La simulation n'est pas déployée : /);
    assert.equal(down.summary, "");
  }

  const simulation = script(job("simulation-answers"), SIMULATION);
  const [page, list] = ["https://anheart-simulation.vercel.app/viewer/index.html", "https://anheart-simulation.vercel.app/api/scenarios"];
  const viewer = read("simulation/viewer/index.html");
  assert.match(read("deploy/simulation-vercel/app.py"), /app\.mount\("\/viewer", /);
  assert.match(read("deploy/simulation-vercel/app.py"), /@app\.get\("\/api\/scenarios"\)/);
  const served = (/** @type {Record<string, string>} */ answers, /** @type {Record<string, string>} */ statuses = {}) => run(simulation, {}, { cwd: fx.work, answers, statuses });
  const current = served({ [page]: viewer, [list]: '["manual_27_rpm","auto_30_min"]' });
  assert.equal(current.status, 0, current.said);
  assert.match(current.summary, /^- https:\/\/anheart-simulation\.vercel\.app\/viewer\/index\.html est la page du visualiseur de ce commit, octet pour octet\.$/m);
  for (const [answers, statuses] of /** @type {[Record<string, string>, Record<string, string>][]} */ ([
    // The page of the deployment before: one byte of difference is another page.
    [{ [page]: `${viewer} `, [list]: '["manual_27_rpm"]' }, {}],
    [{ [page]: viewer.replace("<", " <"), [list]: '["manual_27_rpm"]' }, {}],
    [{ [page]: viewer, [list]: "[]" }, {}],
    [{ [page]: viewer, [list]: '{"detail":"Not Found"}' }, {}],
    [{ [page]: viewer, [list]: '["manual_27_rpm"]' }, { [list]: "500" }],
    [{ [page]: viewer, [list]: '["manual_27_rpm"]' }, { [page]: "404" }],
    [{ [list]: '["manual_27_rpm"]' }, {}],
  ])) {
    const other = served(answers, statuses);
    assert.equal(other.status, 1, `went on with ${JSON.stringify(statuses)} ${Object.keys(answers).length}`);
    assert.match(other.said, /^::error title=Simulation inattendue::Le déploiement Vercel de la simulation a réussi, mais /m);
    assert.equal(other.summary, "");
  }
});

// ---------------------------------------------------------------------------
// EX-7: the machines, a stage that changes nothing and says what to do by hand
// ---------------------------------------------------------------------------

test("EX-7 the stage of the machines reaches no machine: it says the version of the Pi and how one is updated by hand", (context) => {
  const machines = job("machines");
  assert.equal(ORDER.at(-1), "machines");
  assert.deepEqual(stepsOf(machines).map(labelOf), ["uses actions/checkout", MACHINES]);
  assert.deepEqual(block(machines, "    permissions"), ["contents: read"]);
  assert.doesNotMatch(machines, /env:|secrets\.|github\.token|environment:/);
  const say = script(machines, MACHINES);
  assert.doesNotMatch(commands(say), WRITES, "the stage calls something that can reach a machine or a deployment");
  assert.doesNotMatch(say, /\bgh\b|https?:\/\//);

  const fx = repository(context);
  const told = (/** @type {string} */ sha) => run(say, { GITHUB_SHA: sha }, { cwd: fx.work });
  const untagged = told(fx.head);
  assert.equal(untagged.status, 0, untagged.said);
  assert.deepEqual(untagged.calls, []);
  assert.match(untagged.summary, /^### 6\. Machines : aucune n'a été mise à jour\n\nCette étape n'a rien changé, sur aucune machine : la mise à jour à distance n'existe pas encore\.\n/);
  assert.match(untagged.summary, /^- Version du Pi de cette release : `pi-0\.1\.0` \(elle n'a pas de tag\)\.$/m);
  fx.tag("pi-0.1.0", fx.head, PI_MESSAGE);
  git(fx.work, "fetch", "--quiet", "--tags", "origin");
  const tagged = told(fx.head);
  assert.match(tagged.summary, /^- Version du Pi de cette release : `pi-0\.1\.0` \(tag posé sur ce commit par cette release\)\.$/m);
  const later = fx.commit("Release : web-0.2.0 (#11)", { "docs/page.md": "# Page\n" });
  assert.match(told(later).summary, new RegExp(`^- Version du Pi de cette release : \`pi-0\\.1\\.0\` \\(tag d'une release précédente, sur le commit \`${fx.head}\` : une console déjà à cette version n'a rien à recevoir\\)\\.$`, "m"));

  // The gesture is the one of docs/deploiement.md, section 7.6, command for command.
  const manual = read("docs/deploiement.md");
  const section = manual.slice(manual.indexOf("### 7.6 Arrêter, mettre à jour"), manual.indexOf("### 7.7 "));
  for (const command of ["bash scripts/pi/deploy.sh pi@", "sudo bash scripts/install.sh"]) {
    assert.ok(section.includes(command), `docs/deploiement.md 7.6 no longer gives "${command}"`);
    assert.ok(tagged.summary.includes(command), `the stage no longer gives "${command}"`);
  }
  assert.match(tagged.summary, /^- Avant d'installer : lire le niveau de validation de cette version dans `CHANGELOG\.md`\./m);
  // The order it states is the one of section 3.4, with the contract of this commit.
  const contract = JSON.parse(read("contracts/machine-api.json")).contract_version;
  assert.ok(
    tagged.summary.includes(
      `- Ordre avec Convex (docs/deploiement.md, section 3.4) : cette release a déployé Convex avant toute console. C'est le bon ordre tant qu'elle garde la majeure du contrat machine (contrat de ce commit : \`${contract}\`). Une release qui change cette majeure demande l'inverse : les consoles d'abord, à la main, avant d'approuver le déploiement de Convex.`,
    ),
    tagged.summary,
  );
});

test("EX-10 docs/deploiement.md gives one rule for the order between Convex and the consoles, in 3.4 as in 3.6", () => {
  const manual = read("docs/deploiement.md");
  const order = manual.slice(manual.indexOf("### 3.4 "), manual.indexOf("### 3.5 "));
  const sync = manual.slice(manual.indexOf("### 3.6 "), manual.indexOf("## 4. "));
  assert.match(manual, /^### 3\.4 Ordre de mise à jour entre Convex et les consoles$/m);
  // The rule, once: the major of the contract decides.
  assert.match(order, /\| La \*\*majeure\*\* du contrat machine[^|]*\| \*\*Les consoles d'abord, Convex ensuite\.\*\*/);
  assert.match(order, /\| Une \*\*mineure\*\* du contrat, ou rien du contrat \| \*\*Convex d'abord, les consoles ensuite\.\*\*/);
  // Section 3.6 applies it to its minor and no longer states an order of its own.
  assert.match(sync, /\*\*Ordre : Convex d'abord, comme pour toute mineure\*\*/);
  assert.match(sync, /\(#34-ordre-de-mise-à-jour-entre-convex-et-les-consoles\)/);
  assert.doesNotMatch(manual, /34-ordre-de-mise-à-jour--les-consoles-dabord-convex-ensuite/, "a link still names the old title of section 3.4");
  // And it says what the pipeline does with it.
  assert.match(order, /pipeline de release[\s\S]*déploie Convex \(étape 4\) avant\s+toute console/);
});

// ---------------------------------------------------------------------------
// EX-9: a public repository
// ---------------------------------------------------------------------------

test("EX-9 each job holds the permissions it needs and no more: one may write to the repository, one reads the alerts", () => {
  // Nothing by default, for the whole workflow.
  assert.match(RELEASE, /^permissions: \{\}$/m);
  assert.deepEqual(
    Object.fromEntries(ORDER.map((id) => [id, block(job(id), "    permissions")])),
    {
      request: ["actions: read"],
      gates: ["contents: read"],
      install: ["contents: read"],
      codeql: ["contents: read", "security-events: write"],
      alerts: ["security-events: read"],
      "dry-run": ["contents: read", "checks: read", "statuses: read"],
      tags: ["contents: write", "checks: read", "statuses: read"],
      convex: ["contents: read"],
      "vercel-site": ["contents: read"],
      "site-answers": ["contents: read"],
      "vercel-simulation": ["contents: read"],
      "simulation-answers": ["contents: read"],
      machines: ["contents: read"],
    },
  );
  assert.equal(RELEASE.split("permissions:").length - 1, ORDER.length + 1, "a permission is set somewhere else");
  assert.doesNotMatch(RELEASE, /write-all|id-token|packages:|deployments:|pull-requests:|issues:|actions: write/);
  // What a called workflow asks for is what its caller grants, and no more.
  assert.deepEqual(block(CI, "permissions"), ["contents: read"]);
  assert.deepEqual(block(INSTALL, "permissions"), ["contents: read"]);
  assert.deepEqual(block(PRODUCTION, "permissions"), ["contents: read"]);
  assert.deepEqual(block(jobsOf(CODEQL).get("analyze") ?? "", "    permissions"), ["contents: read", "security-events: write"]);
  // Over every workflow of the repository: the tags are the one thing a job may write to the repository.
  const all = readdirSync(join(root, WORKFLOWS)).filter((file) => /\.ya?ml$/.test(file));
  const writing = all.flatMap((file) => [...workflow(file).matchAll(/^ +([a-z-]+): write$/gm)].map((match) => `${file}: ${match[1]}`));
  assert.deepEqual(writing.sort(), ["codeql.yml: security-events", "release.yml: contents", "release.yml: security-events"]);
});

test("EX-9 secrets come from the environment production, to the steps that need them, and no script holds an expression", () => {
  // One secret is named in this file; the four of Vercel are read by the jobs of the button, from their environment.
  assert.deepEqual([...new Set([...RELEASE.matchAll(/secrets\.([A-Za-z_]+)/g)].map((match) => match[1]))], ["CONVEX_DEPLOY_KEY"]);
  assert.equal(RELEASE.split("secrets.").length - 1, 3);
  // The environment, and with it the approval: on the two jobs of this file that write, and on the two of the button.
  const through = ORDER.filter((id) => own(job(id), "environment") !== undefined);
  assert.deepEqual(through, ["tags", "convex"]);
  for (const id of through) assert.deepEqual(block(job(id), "    environment"), ["name: production"], id);
  for (const id of ["site", "simulation"]) assert.deepEqual(block(jobsOf(PRODUCTION).get(id) ?? "", "    environment").slice(0, 1), ["name: production"], id);
  // The token of the run: to four steps, each of which asks GitHub for something, and to no other.
  const tokens = ORDER.flatMap((id) => stepsOf(job(id)).filter((step) => step.includes("github.token")).map((step) => `${id}: ${labelOf(step)}`));
  assert.deepEqual(tokens, [`request: ${APPROVAL}`, `alerts: ${ALERTS}`, `dry-run: ${WOULD_TAG}`, `tags: ${TAG}`]);
  assert.equal(RELEASE.split("github.token").length - 1, tokens.length);

  // An expression is a whole value of `if:`, of an `env:` entry, of an output or of an input of a call, or the mode named in `run-name:`.
  for (const line of RELEASE.split("\n").filter((written) => written.includes("${{"))) {
    assert.match(
      line,
      /^run-name: "Release \$\{\{ inputs\.dry_run && 'à blanc' \|\| 'RÉELLE' \}\} : [^$"]*"$|^ {4}if: \$\{\{ needs\.request\.outputs\.mode == '(dry|real)' \}\}$|^ {8}if: \$\{\{ steps\.state\.outputs\.state == 'pending' \}\}$|^ {6}mode: \$\{\{ steps\.mode\.outputs\.mode \}\}$|^ {6}confirmation: \$\{\{ inputs\.confirmation \}\}$|^ {10}[A-Z_]+: \$\{\{ (secrets\.CONVEX_DEPLOY_KEY|github\.token|github\.ref|inputs\.(dry_run|confirmation)|steps\.mode\.outputs\.mode) \}\}$/,
      line.trim(),
    );
  }
  let scripts = 0;
  for (const id of ORDER) {
    for (const step of stepsOf(job(id)).filter((written) => /^ {8}run:/m.test(written))) {
      scripts += 1;
      const text = script(job(id), labelOf(step));
      assert.doesNotMatch(text, /\$\{\{|secrets\.|inputs\.|github\.event/, `${id}: ${labelOf(step)}`);
      // No command that shows the environment or traces what it runs.
      assert.doesNotMatch(text, /printenv|\bset -[a-z]*x|\bxtrace\b|--debug|--verbose|^\s*(env|export -p|declare -p|set)\s*$/m, `${id}: ${labelOf(step)}`);
    }
  }
  assert.ok(scripts >= 18, `${scripts} scripts were read`);
  assert.doesNotMatch(RELEASE, /ACTIONS_STEP_DEBUG|ACTIONS_RUNNER_DEBUG|\btoJSON\(|upload-artifact|download-artifact/);
});

test("EX-9 every action is one ci.yml already uses, pinned to the same full commit, and no checkout leaves a token behind", () => {
  /** @param {string} text */
  const actions = (text) =>
    [...text.matchAll(/^ +(?:- )?uses:[ \t]*(.*)$/gm)].map(([, written = ""]) => {
      const parts = /^([\w.-]+\/[\w./-]+)@([0-9a-f]{40}) # (v\d+\.\d+\.\d+)$/.exec(written);
      return { written, action: parts?.[1], pin: parts ? `${parts[2]} ${parts[3]}` : undefined };
    });
  const reviewed = new Map(actions(CI).map(({ action, pin }) => [action, pin]));
  assert.ok(reviewed.has("actions/checkout") && reviewed.has("actions/setup-node"), "the actions of ci.yml were not read");
  const used = actions(RELEASE);
  // What is not an action is a workflow of this repository, at the commit of the run: never one of another repository.
  const local = used.filter(({ written }) => written.startsWith("./"));
  assert.deepEqual(local.map(({ written }) => written), Object.values(CALLS).map((file) => `./${WORKFLOWS}/${file}`));
  const pinned = used.filter(({ written }) => !written.startsWith("./"));
  assert.deepEqual(pinned.map(({ action }) => action).sort(), [...Array(6).fill("actions/checkout"), "actions/setup-node"]);
  for (const { written, action, pin } of pinned) {
    assert.ok(pin, `"uses: ${written}" is not <action>@<40 hex> # vX.Y.Z`);
    assert.equal(pin, reviewed.get(action), action);
  }
  assert.equal(RELEASE.split("persist-credentials: false").length - 1, 6);
  assert.doesNotMatch(RELEASE, /persist-credentials: true|cache|pull_request_target|npx [^c]|@latest|@canary/);
});

test("EX-9 no job of the pipeline can be taken for a job of ci.yml", () => {
  // Branch protection knows a check by its name alone, and scripts/release.sh reads the six gates by name.
  const taken = new Set();
  for (const text of [CI, CODEQL, INSTALL, PRODUCTION, workflow("deploy-preview.yml")]) {
    for (const [id, lines] of jobsOf(text)) {
      taken.add(id);
      taken.add(own(lines, "name") ?? id);
    }
  }
  assert.ok(taken.has("pi-gate") && taken.has("docs") && taken.has("request") && taken.has("analyze"), "the jobs of the other workflows were not read");
  for (const id of ORDER) {
    if (id === "request") continue;
    for (const shown of [id, own(job(id), "name") ?? id]) assert.ok(!taken.has(shown), `"${shown}" is a job of another workflow`);
  }
  // The first job has the id the buttons give theirs, under another name: a check is known by its name.
  assert.ok(!taken.has(own(job("request"), "name")));
  for (const id of ORDER) assert.ok(own(job(id), "name"), `${id} has no name of its own`);
  // Every job with steps runs on the image the other workflows run on, and cannot run for ever.
  for (const id of ORDER.filter((other) => !(other in CALLS))) {
    assert.equal(own(job(id), "runs-on"), "ubuntu-24.04", id);
    assert.match(own(job(id), "timeout-minutes") ?? "", /^\d+$/, id);
  }
});

// ---------------------------------------------------------------------------
// EX-10: the pipeline and the pages agree
// ---------------------------------------------------------------------------

test("EX-10 what the pipeline takes for production is what docs/deploiement.md says production is", () => {
  const manual = read("docs/deploiement.md");
  const table = manual.slice(manual.indexOf("## 1. Les environnements"), manual.indexOf("## 2. "));
  const row = (/** @type {string} */ start) => table.split("\n").find((line) => line.startsWith(`| ${start}`)) ?? "";
  // Third column of each row: production.
  const production = (/** @type {string} */ start) => row(start).split("|")[3]?.trim() ?? "";
  assert.equal(production("Convex ("), `\`${ENV.CONVEX_PRODUCTION_DEPLOYMENT}\``);
  assert.equal(production("URL des routes machine"), `\`https://${ENV.CONVEX_PRODUCTION_DEPLOYMENT}.convex.site\``);
  const site = new URL(ENV.SITE_URL ?? "");
  assert.equal(`${site.protocol}//${site.host}`, ENV.SITE_URL, "the address of the site is not a bare https origin");
  assert.equal(site.protocol, "https:");
  assert.ok(production("Site (Vercel").split(", ").includes(`\`${site.host}\``), production("Site (Vercel"));
  assert.equal(production("Simulation hébergée"), `\`${ENV.SIMULATION_URL}\``);
  assert.deepEqual(Object.keys(ENV), ["DEPLOY_BRANCH", "CONFIRMATION_WORD", "CONVEX_PRODUCTION_DEPLOYMENT", "SITE_URL", "SIMULATION_URL", "CODEQL_LANGUAGES"]);
  // The settings the product owner makes by hand name the secret this file reads, and the environment it goes through.
  const settings = manual.slice(manual.indexOf("### 5.5 "), manual.indexOf("## 6. "));
  assert.match(settings, /\| `CONVEX_DEPLOY_KEY` \|/);
  assert.match(settings, /Pipeline de release/);
});

test("EX-10 docs/release.md describes the run, what a failure leaves and how to go back", () => {
  const page = read("docs/release.md");
  assert.match(page, /^### La pipeline de release$/m);
  assert.match(page, /« Release en production \(main\) »/);
  assert.match(page, /^### Échec et reprise$/m);
  assert.match(page, /^### Retour arrière$/m);
  // The two states a real run can stop in between two writes, each with what the operator does.
  assert.match(page, /\| Les tags sont posés, Convex n'est pas déployé \|/);
  assert.match(page, /\| Convex est déployé, le site ne l'est pas \|/);
  assert.match(page, /Re-run failed jobs/);
  // Every job of the pipeline is named in the table of the stages.
  for (const id of ORDER) assert.ok(page.includes(`\`${id}\``), `docs/release.md does not name the job ${id}`);
  assert.match(read("docs/framework-de-test.md"), /scripts\/ci\/release-workflow\.test\.mjs/);
});

test("the docs job of ci.yml, a required check that always runs, runs this file", () => {
  const docs = jobsOf(CI).get("docs") ?? "";
  assert.match(docs, /^ {10}node --test .*\bscripts\/ci\/release-workflow\.test\.mjs\b/m);
  assert.doesNotMatch(docs, /^ {4}(if|needs):/m);
  // The real `jq` runs the filters the workflow hands to `gh`: without it these tests prove nothing of them.
  const jq = spawnSync("jq", ["--version"], { encoding: "utf8" });
  assert.equal(jq.status, 0, "jq is not installed: the filters of the workflow cannot be run");
});
