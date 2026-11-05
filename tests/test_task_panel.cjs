const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");

// the pure helpers of the task panel (all the user's tasks across chats), loaded without the DOM
const source = readFileSync(path.join(__dirname, "..", "src", "web", "static", "app.js"), "utf8");
const code = source.slice(source.indexOf("// ---- task cards: pure helpers"), source.indexOf("// ---- end task cards"));
const context = vm.createContext({});
vm.runInContext(
  `${code}\nObject.assign(this, { formatTokenCount, taskPanelFilter, taskPanelSort, taskBudgetState, taskPanelRow, taskPanelNeedsPolling });`,
  context,
);
const plain = (value) => JSON.parse(JSON.stringify(value));

const task = (id, status, extra = {}) => ({
  id, status, title: `task ${id}`, created_at: `2026-11-05T10:0${id}:00+01:00`, started_at: `2026-11-05T10:0${id}:00+01:00`,
  target: { kind: "ephemeral" }, usage: { in: 0, out: 0 }, parent: { agent_id: "a", chat_id: "c", agent_name: "Agent A", title: "Chat" },
  child: { chat_id: "k" }, ...extra,
});

test("token counts are short", () => {
  assert.equal(context.formatTokenCount(0), "0");
  assert.equal(context.formatTokenCount(950), "950");
  assert.equal(context.formatTokenCount(1500), "1.5k");
  assert.equal(context.formatTokenCount(12400), "12k");
  assert.equal(context.formatTokenCount(1_000_000), "1M");
  assert.equal(context.formatTokenCount(2_540_000), "2.5M");
  assert.equal(context.formatTokenCount(-5), "0");
  assert.equal(context.formatTokenCount("x"), "0");
});

test("the filter splits running from finished tasks", () => {
  const tasks = [task(1, "running"), task(2, "done"), task(3, "failed"), task(4, "running"), task(5, "cancelled"), task(6, "timed_out")];
  assert.deepEqual(context.taskPanelFilter(tasks, "running").map((t) => t.id), [1, 4]);
  assert.deepEqual(context.taskPanelFilter(tasks, "finished").map((t) => t.id), [2, 3, 5, 6]);
  assert.equal(context.taskPanelFilter(tasks, "all").length, 6);
  assert.equal(context.taskPanelFilter(tasks, "anything").length, 6);
  assert.deepEqual(plain(context.taskPanelFilter(undefined, "all")), []);
});

test("the list is newest first and stable", () => {
  const sorted = context.taskPanelSort([task(1, "done"), task(3, "done"), task(2, "done")]);
  assert.deepEqual(sorted.map((t) => t.id), [3, 2, 1]);
  const same = context.taskPanelSort([{ id: "a" }, { id: "b" }, { id: "c" }]);
  assert.deepEqual(same.map((t) => t.id), ["c", "b", "a"], "no times: the later in the (oldest first) list is the newer");
  assert.deepEqual(plain(context.taskPanelSort(null)), []);
});

test("today's usage line", () => {
  assert.deepEqual(plain(context.taskBudgetState({ used: 250_000, limit: 1_000_000 })), { used: "250k", limit: "1M", over: false, percent: 25 });
  assert.deepEqual(plain(context.taskBudgetState({ used: 1_000_000, limit: 1_000_000 })), { used: "1M", limit: "1M", over: true, percent: 100 });
  assert.deepEqual(plain(context.taskBudgetState({ used: 5000, limit: 0 })), { used: "5k", limit: null, over: false, percent: 0 });
  assert.deepEqual(plain(context.taskBudgetState(undefined)), { used: "0", limit: null, over: false, percent: 0 });
});

test("a row names the target, the parent agent and chat, and offers the actions", () => {
  const now = Date.parse("2026-11-05T10:01:30+01:00");
  const row = plain(context.taskPanelRow(task(1, "running", { usage: { in: 100, out: 20 } }), now));
  assert.equal(row.status, "running");
  assert.equal(row.statusLabel, "Running");
  assert.equal(row.ephemeral, true);
  assert.equal(row.parentAgent, "Agent A");
  assert.equal(row.parentTitle, "Chat");
  assert.equal(row.canOpenParent, true);
  assert.equal(row.elapsed, "30s");
  assert.equal(row.tokens, 120);
  assert.deepEqual([row.canStop, row.canSteer, row.canOpenChat], [true, true, true]);
  const done = plain(context.taskPanelRow(task(2, "done", { elapsed_s: 42, target: { kind: "agent", agent_id: "b", name: "Agent B" } }), now));
  assert.equal(done.target, "Agent B");
  assert.equal(done.ephemeral, false);
  assert.equal(done.elapsed, "42s");
  assert.deepEqual([done.canStop, done.canSteer, done.canOpenChat], [false, false, true]);
});

test("a row of an unattended task has no parent chat to open", () => {
  const row = plain(context.taskPanelRow(task(1, "done", { unattended: true, parent: { agent_id: "a", chat_id: "", agent_name: "Agent A", title: "" }, child: {} }), 0));
  assert.equal(row.scheduled, true);
  assert.equal(row.canOpenParent, false);
  assert.equal(row.parentTitle, "");
  assert.equal(row.canOpenChat, false);
});

test("a running task that waits for an approval says so", () => {
  const row = plain(context.taskPanelRow(task(1, "running", { awaiting_approval: 1 }), 0));
  assert.equal(row.awaitingApproval, true);
  assert.equal(row.statusLabel, "Awaiting approval");
  assert.equal(plain(context.taskPanelRow(task(1, "running", { awaiting_approval: 1, approval_elsewhere: "telegram" }), 0)).statusLabel, "Awaiting approval (answer it in Telegram)");
});

test("the panel polls only while something runs", () => {
  assert.equal(context.taskPanelNeedsPolling({ tasks: [task(1, "done"), task(2, "running")] }), true);
  assert.equal(context.taskPanelNeedsPolling({ tasks: [task(1, "done"), task(2, "failed")] }), false);
  assert.equal(context.taskPanelNeedsPolling({}), false);
  assert.equal(context.taskPanelNeedsPolling(undefined), false);
});
