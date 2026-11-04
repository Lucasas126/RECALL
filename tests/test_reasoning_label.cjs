const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");

// the composer's effort selector state, loaded without the DOM
const source = readFileSync(path.join(__dirname, "..", "src", "web", "static", "app.js"), "utf8");
const code = source.match(/function reasoningEffortState\([\s\S]*?\r?\n\}\r?\n/)[0];
const context = vm.createContext({ tr: (text) => text });
vm.runInContext(`${code}\nthis.reasoningEffortState = reasoningEffortState;`, context);
const state = (effort) => context.reasoningEffortState(effort);

test("agent with reasoning off says so and where to enable it", () => {
  const result = state({ reasoning: false, supported: true, override: null, default: "medium" });
  assert.equal(result.status, "Disabled in this agent");
  assert.match(result.note, /Agents > Basic > Advanced > Reasoning/);
});

test("unsupported and unverified models get their own label", () => {
  assert.equal(state({ reasoning: true, supported: false, override: null }).status, "Not available in this model");
  assert.equal(state({ reasoning: true, supported: null, override: null }).status, "Unknown for this model");
});

test("working selector has no status label", () => {
  assert.equal(state({ reasoning: true, supported: true, override: null }).status, null);
  assert.equal(state({ reasoning: true, supported: true, override: "high" }).note, "Chat override");
  assert.equal(state(null).status, null);
});
