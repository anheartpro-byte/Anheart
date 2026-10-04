import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test from "node:test";

const require = createRequire(import.meta.url);
const fromNext = createRequire(require.resolve("@next/eslint-plugin-next"));
const fromGlob = createRequire(fromNext.resolve("fast-glob"));
const fromMicromatch = createRequire(fromGlob.resolve("micromatch"));
const braces = fromMicromatch("braces");
const nested = (open, close, depth) => open.repeat(depth) + "x" + close.repeat(depth);

test("lint chain resolves the exact verified depth-guard release", () => {
  const metadata = fromMicromatch("braces/package.json");
  assert.equal(metadata.name, "@dieub/braces-depth-guard");
  assert.equal(metadata.version, "3.0.3-pn.2");
});

for (const [open, close] of [["{", "}"], ["(", ")"]]) {
  test(`nested ${open} patterns stop before recursive stack exhaustion`, () => {
    assert.doesNotThrow(() => braces.parse(nested(open, close, 100)));
    assert.throws(() => braces.parse(nested(open, close, 101)), SyntaxError);
    assert.throws(() => braces(nested(open, close, 3500)), SyntaxError);
  });
}

for (const maxDepth of [undefined, NaN, Infinity, "1000", 1000]) {
  test(`plain maxDepth cannot lift the depth cap: ${String(maxDepth)}`, () => {
    assert.throws(() => braces.parse(nested("{", "}", 101), { maxDepth }), SyntaxError);
  });
}

function ast(depth) {
  let node = { type: "text", value: "x" };
  for (let index = 0; index < depth; index += 1) node = { type: "paren", nodes: [node] };
  return { type: "root", nodes: [node] };
}

for (const operation of ["compile", "stringify", "expand"]) {
  test(`${operation} guards direct AST depth too`, () => {
    assert.doesNotThrow(() => braces[operation](ast(100)));
    assert.throws(() => braces[operation](ast(101)), RangeError);
  });
}

test("ordinary set, padded range, escaping and deduplication still work", () => {
  assert.deepEqual(braces("a/{b,c}/d", { expand: true }), ["a/b/d", "a/c/d"]);
  assert.deepEqual(braces("{01..05..2}", { expand: true }), ["01", "03", "05"]);
  assert.deepEqual(braces("a/\\{b,c\\}/d", { expand: true }), ["a/{b,c}/d"]);
  assert.deepEqual(braces("{a,a,b}", { expand: true, nodupes: true }), ["a", "b"]);
});
