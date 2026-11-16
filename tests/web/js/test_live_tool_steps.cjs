const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");

// the live tool-step helpers and the task card state, loaded without the DOM
const source = readFileSync(path.join(__dirname, "..", "..", "..", "src", "web", "static", "app.js"), "utf8");
const live = source.slice(source.indexOf("function toolStepStart("), source.indexOf("// What to draw for a stored message"));
const tasks = source.slice(source.indexOf("// ---- task cards: pure helpers"), source.indexOf("// A wake turn: a subagent result"));
const context = vm.createContext({});
vm.runInContext(
  `${live}\n${tasks}\nthis.apply = toolStepsApply; this.stop = toolStepStop; this.elapsed = toolStepElapsed; this.cardState = taskCardState; this.bind = toolStepBindTask; this.match = taskUnboundMatch;`,
  context,
);
const plain = (value) => JSON.parse(JSON.stringify(value));
const start = (id, name = "terminal", args = { command: "ls" }) => ({ id, name, arguments: args });
const final = (id, name = "terminal", extra = {}) => ({ id, name, arguments: {}, result: "ok", error: false, ...extra });

test("a start adds a running step and the final event completes the same step in place", () => {
  let r = context.apply([], "tool_start", start("a"), 1000);
  r = context.apply(r.steps, "tool_start", start("b", "read"), 1000);
  assert.deepEqual(plain(r.steps).map((s) => [s.id, s.phase]), [["a", "running"], ["b", "running"]]);
  r = context.apply(r.steps, "tool", final("b", "read"), 2000);
  assert.equal(r.index, 1);
  assert.equal(r.steps.length, 2);
  assert.deepEqual(plain(r.steps).map((s) => s.phase), ["running", "done"]);
  assert.equal(r.steps[1].action.result, "ok");
  assert.equal("id" in r.steps[1].action, false);
});

test("a failed final event marks the step failed", () => {
  const started = context.apply([], "tool_start", start("a"), 0);
  const r = context.apply(started.steps, "tool", final("a", "terminal", { error: true }), 5);
  assert.equal(r.steps[0].phase, "failed");
});

test("a final event without a start (older server or non-streamed reply) becomes a finished step", () => {
  const r = context.apply([], "tool", final("x"), 0);
  assert.equal(r.steps.length, 1);
  assert.equal(r.steps[0].phase, "done");
  const noId = context.apply([], "tool", { name: "read", arguments: {}, result: "" }, 0);
  assert.equal(noId.steps[0].phase, "done");
});

test("a final event without an id matches the oldest running step of that name", () => {
  let r = context.apply([], "tool_start", start("a", "read"), 0);
  r = context.apply(r.steps, "tool_start", start("b", "write"), 0);
  r = context.apply(r.steps, "tool", { name: "write", arguments: {}, result: "x", error: false }, 1);
  assert.deepEqual(plain(r.steps).map((s) => s.phase), ["running", "done"]);
});

test("a replayed duplicate start does not add a second step", () => {
  let r = context.apply([], "tool_start", start("a"), 0);
  r = context.apply(r.steps, "tool_start", start("a"), 5);
  assert.equal(r.steps.length, 1);
  assert.equal(r.index, 0);
});

test("stopping turns only running steps into stopped", () => {
  const steps = [{ id: "a", phase: "running", startedAt: 0 }, { id: "b", phase: "done", startedAt: 0 }];
  assert.deepEqual(plain(steps.map(context.stop)).map((s) => s.phase), ["stopped", "done"]);
});

test("elapsed seconds count up while running and are empty afterwards", () => {
  const step = { phase: "running", startedAt: 1000 };
  assert.equal(context.elapsed(step, 1000), "0s");
  assert.equal(context.elapsed(step, 8400), "7s");
  assert.equal(context.elapsed(step, 1000 + 75000), "1m 15s");
  assert.equal(context.elapsed({ ...step, phase: "done" }, 9000), "");
});

test("a task_start step that has not returned yet is a 'starting' card, not a failed launch", () => {
  const started = context.apply([], "tool_start", start("t", "task_start", { goal: "Check the logs" }), 1000);
  const view = context.cardState(null, started.steps[0].action, 4000, false);
  assert.equal(view.status, "starting");
  assert.equal(view.title, "Check the logs");
  assert.equal(view.elapsed, "3s");
  assert.equal(view.error, "");
  const stopped = context.cardState(null, { ...started.steps[0].action, pending: false, stopped: true }, 5000, false);
  assert.equal(stopped.status, "stopped");
  assert.equal(stopped.error, "");
});

test("a task_started event binds the running task_start step to its task id, even before it returns", () => {
  const goal = "find X";
  let r = context.apply([], "tool_start", start("a", "task_start", { goal: "other", wait: true }), 1000);
  r = context.apply(r.steps, "tool_start", start("b", "task_start", { goal, wait: true }), 1000);
  const bound = context.bind(r.steps, { task_id: "aaaaaaaaaaaa", goal });
  assert.equal(bound.index, 1);  // same goal wins over the older unbound step
  assert.equal(bound.steps[1].action.taskId, "aaaaaaaaaaaa");
  assert.equal(context.cardState(null, bound.steps[1].action, 0).id, "aaaaaaaaaaaa");
  assert.equal(context.bind(bound.steps, { task_id: "aaaaaaaaaaaa", goal }).index, -1);  // replay: no double binding
  const second = context.bind(bound.steps, { task_id: "bbbbbbbbbbbb", goal: "unknown" });
  assert.equal(second.index, 0);  // no goal match: the oldest unbound one
  // the id survives the final event
  const done = context.apply(bound.steps, "tool", final("b", "task_start", { result: "not json" }), 2000);
  assert.equal(done.steps[1].action.taskId, "aaaaaaaaaaaa");
});

test("a running task_start step without an id matches the newest free running task of the chat with its goal", () => {
  const action = { name: "task_start", arguments: { goal: "find X" }, pending: true };
  const records = [
    { id: "old", status: "running", goal: "find X", created_at: "2026-01-01T00:00:00" },
    { id: "new", status: "running", goal: "find X", created_at: "2026-01-02T00:00:00" },
    { id: "fin", status: "done", goal: "find X", created_at: "2026-01-03T00:00:00" },
    { id: "diff", status: "running", goal: "other", created_at: "2026-01-04T00:00:00" },
  ];
  assert.equal(context.match(records, new Set(), action).id, "new");
  assert.equal(context.match(records, new Set(["new"]), action).id, "old");
  assert.equal(context.match(records, new Set(["new", "old"]), action), null);
});
