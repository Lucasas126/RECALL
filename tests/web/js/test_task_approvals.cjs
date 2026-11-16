const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");

// the pure task-card helpers (approval attribution, awaiting-approval status), loaded without the DOM
const source = readFileSync(path.join(__dirname, "..", "..", "..", "src", "web", "static", "app.js"), "utf8");
const code = source.slice(source.indexOf("// ---- task cards: pure helpers"), source.indexOf("// ---- end task cards"));
const context = vm.createContext({});
vm.runInContext(`${code}\nObject.assign(this, { approvalTaskLabel, taskDisplayStatus, taskStatusLabel, taskCardState });`, context);

const task = { id: "abc123", title: "Check the logs", kind: "ephemeral", worker: "ephemeral" };

test("attribution label of an approval asked by a task", () => {
  assert.equal(context.approvalTaskLabel({ task }), "Task Check the logs (ephemeral) wants to run:");
  assert.equal(context.approvalTaskLabel({ task: { ...task, kind: "agent", worker: "Scout" } }), "Task Check the logs (Scout) wants to run:");
});

test("an approval that is not a task's has no label", () => {
  assert.equal(context.approvalTaskLabel({}), "");
  assert.equal(context.approvalTaskLabel(null), "");
  assert.equal(context.approvalTaskLabel({ task: "x" }), "");
  assert.equal(context.approvalTaskLabel({ task: { title: "no id" } }), "");
});

test("the label falls back to the id and goes through the translator", () => {
  assert.equal(context.approvalTaskLabel({ task: { id: "abc123" } }), "Task abc123 (ephemeral) wants to run:");
  const seen = [];
  const label = context.approvalTaskLabel({ task }, (template, values) => (seen.push([template, values]), "T"));
  assert.equal(label, "T");
  assert.equal(seen[0][0], "Task {title} ({worker}) wants to run:");
});

test("a running task that waits for an approval says so; a finished one never does", () => {
  assert.equal(context.taskDisplayStatus({ status: "running", awaiting_approval: 1 }), "awaiting_approval");
  assert.equal(context.taskDisplayStatus({ status: "running", awaiting_approval: 0 }), "running");
  assert.equal(context.taskDisplayStatus({ status: "done", awaiting_approval: 2 }), "done");
  assert.equal(context.taskStatusLabel("awaiting_approval"), "Awaiting approval");
  const view = context.taskCardState({ id: "abc123", status: "running", awaiting_approval: 1, target: { kind: "ephemeral" } }, { name: "task_start", arguments: { goal: "x" } }, 0);
  assert.equal(view.statusLabel, "Awaiting approval");
  assert.equal(view.awaitingApproval, true);
  assert.equal(view.running, true);
});
