// Read workflow concurrency settings for ci-workflow.test.mjs and
// analysis-workflows.test.mjs. The evaluator accepts `${{ ... }}` expressions
// in values, dotted context properties, `||`, and `==` with single-quoted
// strings. An unsupported expression form throws and fails the test.

import assert from "node:assert/strict";

/**
 * Evaluate a dotted context property, `||`, or equality with a single-quoted
 * string.
 * @param {string} source
 * @param {Record<string, unknown>} context
 * @returns {unknown}
 */
export const evaluateExpression = (source, context) => {
  const expression = source.trim();
  const equality = /^([a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*)\s*==\s*'([^']*)'$/.exec(expression);
  if (equality) return evaluateExpression(equality[1] ?? "", context) === equality[2];
  const alternatives = expression.split(/\s*\|\|\s*/);
  if (alternatives.length > 1) {
    assert.ok(alternatives.every(Boolean), `unsupported expression: ${source}`);
    for (const alternative of alternatives) {
      const value = evaluateExpression(alternative, context);
      if (value) return value;
    }
    return undefined;
  }
  assert.match(expression, /^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*$/, `unsupported expression: ${source}`);
  return expression.split(".").reduce((value, part) => value?.[part], context);
};

/**
 * Evaluate a literal or a string containing `${{ ... }}` expressions.
 * @param {string} source
 * @param {Record<string, unknown>} context
 * @returns {unknown}
 */
export const evaluateWorkflowValue = (source, context) => {
  if (source === "true") return true;
  if (source === "false") return false;
  const onlyExpression = /^\$\{\{\s*([^}]+?)\s*\}\}$/.exec(source);
  if (onlyExpression) return evaluateExpression(onlyExpression[1] ?? "", context);
  let expressions = 0;
  const result = source.replace(/\$\{\{\s*([^}]+?)\s*\}\}/g, (_match, expression) => {
    expressions += 1;
    return String(evaluateExpression(expression, context) ?? "");
  });
  assert.equal(source.split("${{").length - 1, expressions, `unclosed expression in: ${source}`);
  assert.doesNotMatch(result, /\$\{\{|\}\}/, `unclosed expression in: ${source}`);
  return result;
};

/** Read the top-level concurrency fields from a workflow source.
 * @param {string} source
 * @returns {Record<string, string>}
 */
export const concurrencyOf = (source) => {
  const head = source.slice(0, source.indexOf("\njobs:\n"));
  const block = /^concurrency:\n((?: {2}[^\n]*\n)+)/m.exec(head)?.[1] ?? "";
  const fields = Object.fromEntries([...block.matchAll(/^ {2}([a-z][a-z-]*):[ \t]*(.*)$/gm)].map((match) => [match[1], match[2]]));
  assert.deepEqual(Object.keys(fields).sort(), ["cancel-in-progress", "group"]);
  return fields;
};

/** Read a workflow's top-level `name:` value.
 * @param {string} source
 * @returns {string | undefined}
 */
export const workflowName = (source) => /^name:[ \t]*(.+)$/m.exec(source)?.[1];
