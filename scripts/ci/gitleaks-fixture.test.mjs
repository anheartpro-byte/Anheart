import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { copyFileSync, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import test from "node:test";
import { z } from "zod";

const scanner = process.env.ANHEART_GITLEAKS_BIN ?? "gitleaks";
const config = resolve(".gitleaks.toml");
const workflow = ".github/workflows/ci.yml";
const publicFixture = `pk_test_${Buffer.from("ci-fixture.clerk.accounts.dev$").toString("base64")}`;
const findings = z.array(z.object({ RuleID: z.string(), File: z.string() }));

/** @param {string} directory @param {readonly string[]} args */
function git(directory, args) {
  const result = spawnSync("git", ["-c", "core.hooksPath=/dev/null", "-C", directory, ...args], {
    encoding: "utf8",
    env: {
      ...process.env,
      GIT_CONFIG_NOSYSTEM: "1",
      GIT_CONFIG_GLOBAL: "/dev/null",
      GIT_AUTHOR_NAME: "Synthetic audit fixture",
      GIT_AUTHOR_EMAIL: "fixture@example.invalid",
      GIT_COMMITTER_NAME: "Synthetic audit fixture",
      GIT_COMMITTER_EMAIL: "fixture@example.invalid",
    },
  });
  assert.ifError(result.error);
  assert.equal(result.status, 0, result.stderr);
}

/** @param {import("node:test").TestContext} context */
function repository(context) {
  const directory = mkdtempSync(join(tmpdir(), "anheart-gitleaks-test-"));
  context.after(() => rmSync(directory, { recursive: true }));
  if (existsSync(config)) copyFileSync(config, join(directory, ".gitleaks.toml"));
  git(directory, ["init", "-q", "-b", "main"]);
  git(directory, ["-c", "commit.gpgsign=false", "commit", "-q", "--allow-empty", "-m", "synthetic base"]);
  return directory;
}

/** @param {string} directory @param {string} path @param {string} content */
function commitFile(directory, path, content) {
  const target = join(directory, path);
  mkdirSync(dirname(target), { recursive: true });
  writeFileSync(target, content);
  git(directory, ["add", path]);
  git(directory, ["-c", "commit.gpgsign=false", "commit", "-q", "-m", "synthetic input"]);
}

/** @param {string} directory */
function scan(directory) {
  const report = join(directory, "findings.json");
  const result = spawnSync(scanner, [
    "git", "--redact", "--no-banner", "--log-opts=--all", "--report-path", report, directory,
  ], { encoding: "utf8", cwd: directory });
  assert.ifError(result.error);
  return { status: result.status, findings: findings.parse(JSON.parse(readFileSync(report, "utf8"))) };
}

test("the exact public CI fixture stays accepted after line movement", (context) => {
  const directory = repository(context);
  commitFile(directory, workflow, `${"\n".repeat(40)}NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY: ${publicFixture}\n`);
  const result = scan(directory);
  assert.deepEqual(result, { status: 0, findings: [] });
});

test("the exact public CI fixture stays accepted after squash changes its commit", (context) => {
  const directory = repository(context);
  git(directory, ["switch", "-q", "-c", "synthetic-feature"]);
  commitFile(directory, workflow, `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY: ${publicFixture}\n`);
  git(directory, ["switch", "-q", "main"]);
  git(directory, ["merge", "--squash", "synthetic-feature"]);
  git(directory, ["-c", "commit.gpgsign=false", "commit", "-q", "-m", "synthetic squash"]);
  const result = scan(directory);
  assert.deepEqual(result, { status: 0, findings: [] });
});

const refused = [
  { name: "another publishable value at the CI path", path: workflow, value: `${publicFixture.slice(0, -1)}X` },
  { name: "the fixture at another path", path: "settings.yml", value: publicFixture },
];

for (const { name, path, value } of refused) {
  test(`the scan rejects ${name}`, (context) => {
    const directory = repository(context);
    commitFile(directory, path, `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY: ${value}\n`);
    const result = scan(directory);
    assert.deepEqual(result, { status: 1, findings: [{ RuleID: "generic-api-key", File: path }] });
  });
}

test("an adjacent different credential is rejected beside the accepted fixture", (context) => {
  const directory = repository(context);
  const other = Buffer.from("different-credential.sandbox.invalid$").toString("base64");
  commitFile(directory, workflow, `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY: ${publicFixture}\nAPI_TOKEN: ${other}\n`);
  const result = scan(directory);
  assert.deepEqual(result, { status: 1, findings: [{ RuleID: "generic-api-key", File: workflow }] });
});

test("another inherited detector still rejects a synthetic GitHub token", (context) => {
  const directory = repository(context);
  const other = `ghp_${"aB3dE5gH7jK9mN1pQ3sT5vW7yZ9cD1fG3hJ5kL7n"}`;
  commitFile(directory, workflow, `GITHUB_TOKEN: ${other}\n`);
  const result = scan(directory);
  assert.equal(result.status, 1);
  assert.ok(result.findings.some((finding) => finding.RuleID === "github-pat" && finding.File === workflow));
});
