const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");

// the pure subagent task-card helpers, loaded without the DOM
const source = readFileSync(path.join(__dirname, "..", "..", "..", "src", "web", "static", "app.js"), "utf8");
const code = source.slice(source.indexOf("// ---- task cards: pure helpers"), source.indexOf("// ---- end task cards"));
const context = vm.createContext({});
vm.runInContext(
  `${code}\nObject.assign(this, { taskStatusLabel, formatElapsed, taskElapsed, taskActionId, isTaskStartAction, taskCardState, isWakeMessage, wakeBubbleState, tasksNeedPolling, finishedProcessIds, shouldAttachWake });`,
  context,
);
const plain = (value) => JSON.parse(JSON.stringify(value));

const started = { name: "task_start", arguments: { goal: "Summarise the logs" }, result: JSON.stringify({ id: "abc123", status: "running" }) };

test("status labels", () => {
  assert.equal(context.taskStatusLabel("running"), "Running");
  assert.equal(context.taskStatusLabel("timed_out"), "Timed out");
  assert.equal(context.taskStatusLabel("cancelled"), "Cancelled");
  assert.equal(context.taskStatusLabel("nope"), "Unknown");
});

test("elapsed time format", () => {
  assert.equal(context.formatElapsed(0), "0s");
  assert.equal(context.formatElapsed(45), "45s");
  assert.equal(context.formatElapsed(185), "3m 05s");
  assert.equal(context.formatElapsed(3720), "1h 02m");
  assert.equal(context.formatElapsed(null), "");
  assert.equal(context.formatElapsed(-1), "");
});

test("a running task counts up from its start, a finished one is fixed", () => {
  const now = Date.parse("2026-11-05T10:01:00+01:00");
  const running = { status: "running", started_at: "2026-11-05T10:00:00+01:00", elapsed_s: 3 };
  assert.equal(context.taskElapsed(running, now), 60);
  assert.equal(context.taskElapsed({ ...running, status: "done", elapsed_s: 42 }, now), 42);
  assert.equal(context.taskElapsed(null, now), null);
});

test("the task id comes from the stored step result", () => {
  assert.equal(context.isTaskStartAction(started), true);
  assert.equal(context.isTaskStartAction({ name: "task_list" }), false);
  assert.equal(context.taskActionId(started), "abc123");
  assert.equal(context.taskActionId({ name: "task_start", result: "error: limit reached" }), null);
  assert.equal(context.taskActionId({ name: "task_start", result: { id: "x1" } }), "x1");
  // a stored wait=true result is truncated, so it is not valid JSON any more: the id must still be found
  const truncated = '{"id": "25e1ccdcf21a", "status": "done", "result": "# Tortilla de patatas\\n\\n## Ingre';
  assert.equal(context.taskActionId({ name: "task_start", result: truncated }), "25e1ccdcf21a");
});

test("a card of a running task offers stop, steer and (with a worker chat) open", () => {
  const record = {
    id: "abc123", title: "Summarise the logs", status: "running", target: { kind: "ephemeral" }, usage: { in: 100, out: 20 },
    child: { chat_id: "c1" }, started_at: "2026-11-05T10:00:00+01:00", result_preview: "",
  };
  const view = plain(context.taskCardState(record, started, Date.parse("2026-11-05T10:00:05+01:00")));
  assert.equal(view.status, "running");
  assert.equal(view.elapsed, "5s");
  assert.equal(view.ephemeral, true);
  assert.equal(view.tokens, 120);
  assert.deepEqual([view.canStop, view.canSteer, view.canOpen, view.final], [true, true, true, false]);
});

test("a finished task shows its result and no stop or steer; an agent target is named", () => {
  const record = {
    id: "abc123", title: "T", status: "done", target: { kind: "agent", agent_id: "scout" }, elapsed_s: 90,
    started_at: "2026-11-05T10:00:00+01:00", result_preview: "all good", child: { chat_id: "c1" },
  };
  const view = plain(context.taskCardState(record, started, Date.now()));
  assert.equal(view.elapsed, "1m 30s");
  assert.equal(view.target, "scout");
  assert.equal(view.ephemeral, false);
  assert.equal(view.result, "all good");
  assert.deepEqual([view.canStop, view.canSteer, view.final], [false, false, true]);
});

test("without a record the card is running, unknown once the list was fetched, failed when the launch failed", () => {
  assert.equal(context.taskCardState(null, started, 0).status, "running");
  assert.equal(context.taskCardState(null, started, 0, true).status, "unknown");
  assert.equal(context.taskCardState(null, started, 0, true).canStop, false);
  const failed = context.taskCardState(null, { name: "task_start", arguments: { goal: "x" }, result: "error: limit reached: 3 running tasks" }, 0);
  assert.equal(failed.status, "failed");
  assert.match(failed.error, /limit reached/);
  assert.equal(context.taskCardState(null, started, 0).title, "Summarise the logs");
});

test("wake turns are detected by source and parsed for titles and body", () => {
  assert.equal(context.isWakeMessage({ role: "user", source: "task", content: "x" }), true);
  assert.equal(context.isWakeMessage({ role: "user", content: "x" }), false);
  assert.equal(context.isWakeMessage({ role: "assistant", source: "task" }), false);
  const content = [
    "[RECALL task result, not written by the user]\nTasks you started earlier have ended.",
    'task abc123 "Summarise the logs" - done, 12 s, 300 tokens\n<task_result untrusted="true">\nAll fine\n</task_result>',
    'task def456 "Check disk" - failed, 3 s, 10 tokens\n<task_result untrusted="true">\n(no result text)\n</task_result>',
    "If the user's latest request is still unanswered, answer it first.",
  ].join("\n\n");
  const info = plain(context.wakeBubbleState({ role: "user", source: "task", task_ids: ["abc123", "def456"], content }));
  assert.deepEqual(info.titles, ["Summarise the logs", "Check disk"]);
  assert.deepEqual(info.ids, ["abc123", "def456"]);
  assert.ok(info.body.startsWith("task abc123"));
  assert.ok(!info.body.includes("[RECALL task result"));
  assert.ok(!info.body.includes("If the user's latest request"));
});

test("polling continues while a task runs or a wake is pending, and stops otherwise", () => {
  assert.equal(context.tasksNeedPolling({ tasks: [{ status: "running" }] }), true);
  assert.equal(context.tasksNeedPolling({ tasks: [{ status: "done" }], pending_wake: true }), true);
  assert.equal(context.tasksNeedPolling({ tasks: [{ status: "done" }, { status: "failed" }], pending_wake: false }), false);
  assert.equal(context.tasksNeedPolling({ tasks: [] }), false);
  // grace window after a background process ended: its wake reply may still start
  assert.equal(context.tasksNeedPolling({ tasks: [] }, 2000, 1000), true);
  assert.equal(context.tasksNeedPolling({ tasks: [] }, 2000, 2000), false);
  assert.equal(context.tasksNeedPolling({ tasks: [] }, 0, 1000), false);
});

test("a wake reply is attached to only when the chat is generating and this page follows nothing", () => {
  assert.equal(context.shouldAttachWake({ active: true, generating: false }), true);
  assert.equal(context.shouldAttachWake({ active: true, generating: true }), false);
  assert.equal(context.shouldAttachWake({ active: false, generating: false }), false);
  assert.equal(context.shouldAttachWake({ active: true, generating: false, current: false }), false);
});


test("finishedProcessIds lists the processes gone from the list", () => {
  assert.deepEqual(Array.from(context.finishedProcessIds(["a", "b"], ["b", "c"])), ["a"]);
  assert.deepEqual(Array.from(context.finishedProcessIds([], ["a"])), []);
  assert.deepEqual(Array.from(context.finishedProcessIds(undefined, undefined)), []);
});
