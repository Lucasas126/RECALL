const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");

// agentsChangedBy: which finished tool calls make the page reload its agents
const source = readFileSync(path.join(__dirname, "..", "..", "..", "src", "web", "static", "app.js"), "utf8");
const grab = (start, end) => source.slice(source.indexOf(start), source.indexOf(end, source.indexOf(start)));
const context = {};
vm.createContext(context);
vm.runInContext(grab("const AGENT_CHANGE_TOOLS", "// The names, pictures and lists of agents") + "this.agentsChangedBy = agentsChangedBy;", context);
const { agentsChangedBy } = context;

test("an applied agent change refreshes the agents", () => {
  for (const name of ["agent_update", "agent_rename", "agent_create", "agent_delete"]) {
    assert.equal(agentsChangedBy({ name, result: JSON.stringify({ status: "applied", approved_by: "user" }) }), true, name);
  }
});

test("a denied, failed, unchanged or read-only call does not", () => {
  assert.equal(agentsChangedBy({ name: "agent_update", result: "error: the user did not approve this change (denied)." }), false);
  assert.equal(agentsChangedBy({ name: "agent_update", result: JSON.stringify({ status: "applied" }), error: true }), false);
  assert.equal(agentsChangedBy({ name: "agent_update", result: JSON.stringify({ status: "unchanged" }) }), false);
  assert.equal(agentsChangedBy({ name: "agent_list", result: JSON.stringify({ status: "applied" }) }), false);
  assert.equal(agentsChangedBy({ name: "terminal", result: "ok" }), false);
  assert.equal(agentsChangedBy(null), false);
});
