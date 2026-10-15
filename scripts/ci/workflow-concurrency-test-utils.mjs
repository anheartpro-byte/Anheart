import assert from "node:assert/strict";

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

export const concurrencyOf = (source) => {
  const head = source.slice(0, source.indexOf("\njobs:\n"));
  const block = /^concurrency:\n((?: {2}[^\n]*\n)+)/m.exec(head)?.[1] ?? "";
  const fields = Object.fromEntries([...block.matchAll(/^ {2}([a-z][a-z-]*):[ \t]*(.*)$/gm)].map((match) => [match[1], match[2]]));
  assert.deepEqual(Object.keys(fields).sort(), ["cancel-in-progress", "group"]);
  return fields;
};

export const workflowName = (source) => /^name:[ \t]*(.+)$/m.exec(source)?.[1];
