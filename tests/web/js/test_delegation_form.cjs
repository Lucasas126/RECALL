const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");

// the pure helpers of the agent form's Delegation block, loaded without the DOM
const source = readFileSync(path.join(__dirname, "..", "..", "..", "src", "web", "static", "app.js"), "utf8");
const take = (name) => source.match(new RegExp(`function ${name}\\([\\s\\S]*?\\r?\\n\\}\\r?\\n`))[0];
const names = ["delegationTargetRows", "buildDelegationConfig", "isDelegationError"];
const context = vm.createContext({});
vm.runInContext(`${names.map(take).join("\n")}\n${names.map((n) => `this.${n} = ${n};`).join(" ")}`, context);
const f = context;
const plain = (value) => JSON.parse(JSON.stringify(value));

const agents = [{ id: "planner", name: "Planner" }, { id: "scout", name: "Scout" }, { id: "atlas", name: "" }];
const saved = {
  enabled: false, allowed_groups: [], max_parallel: 2, timeout_seconds: 120, max_tokens: 50000, default_model: null,
  targets: [{ agent: "scout", remember: true }, { agent: "gone", remember: false }],
};

test("target rows list the other agents, mark the saved ones and keep a vanished target visible", () => {
  const rows = plain(f.delegationTargetRows(agents, "planner", saved));
  assert.deepEqual(rows.map((row) => row.id), ["scout", "atlas", "gone"]);
  assert.deepEqual(rows[0], { id: "scout", name: "Scout", allowed: true, remember: true, missing: false });
  assert.deepEqual(rows[1], { id: "atlas", name: "atlas", allowed: false, remember: false, missing: false });
  assert.deepEqual(rows[2], { id: "gone", name: "gone", allowed: true, remember: false, missing: true });
  assert.deepEqual(plain(f.delegationTargetRows(agents, "planner", null)).map((row) => row.allowed), [false, false]);
  assert.deepEqual(plain(f.delegationTargetRows(undefined, "planner", null)), []);
});

test("a saved block comes back unchanged while the delegate group is off", () => {
  assert.deepEqual(plain(f.buildDelegationConfig(saved, false, { enabled: true, targets: [] })), plain(saved));
  assert.equal(f.buildDelegationConfig(null, false, {}), null);
});

test("the form fields round-trip the block: limits, groups, targets and the memory flag", () => {
  const fields = {
    enabled: false, allowedGroups: [], maxParallel: 2, timeout: 120, maxTokens: 50000,
    targets: [
      { id: "scout", allowed: true, remember: true }, { id: "atlas", allowed: false, remember: true },
      { id: "gone", allowed: true, remember: false },
    ],
  };
  assert.deepEqual(plain(f.buildDelegationConfig(saved, true, fields)), plain(saved));
});

test("untouched defaults send nulls for the limits and no targets", () => {
  const body = plain(f.buildDelegationConfig(null, true, { enabled: true, allowedGroups: ["web"], targets: [] }));
  assert.deepEqual(body, {
    enabled: true, allowed_groups: ["web"], max_parallel: null, timeout_seconds: null, max_tokens: null, default_model: null, targets: [],
  });
});

test("the worker model is trimmed and an empty field sends null", () => {
  const send = (defaultModel) => plain(f.buildDelegationConfig(null, true, { enabled: true, allowedGroups: [], targets: [], defaultModel })).default_model;
  assert.equal(send("  openrouter:vendor/m  "), "openrouter:vendor/m");
  assert.equal(send("  "), null);
});

test("an unchecked target is dropped and its memory flag never survives alone", () => {
  const body = plain(f.buildDelegationConfig(saved, true, {
    enabled: true, allowedGroups: ["web"], targets: [{ id: "scout", allowed: false, remember: true }],
  }));
  assert.deepEqual(body.targets, []);
});

test("delegation save errors are recognised", () => {
  assert.equal(f.isDelegationError("delegation.targets lists agents that are not yours: x"), true);
  assert.equal(f.isDelegationError("Something else"), false);
});
