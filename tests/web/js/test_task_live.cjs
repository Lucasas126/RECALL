const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");

// the pure helpers of the live task view (card activity line, dialog stream outcome), loaded without the DOM
const source = readFileSync(path.join(__dirname, "..", "..", "..", "src", "web", "static", "app.js"), "utf8");
const code = source.slice(source.indexOf("// ---- task cards: pure helpers"), source.indexOf("// ---- end task cards"));
const context = vm.createContext({});
vm.runInContext(`${code}\nObject.assign(this, { taskActivityLabel, taskLiveNext, taskCardState });`, context);
const plain = (value) => JSON.parse(JSON.stringify(value));

test("activity labels", () => {
  assert.deepEqual(plain(context.taskActivityLabel({ kind: "reasoning" })), ["Thinking…", {}]);
  assert.deepEqual(plain(context.taskActivityLabel({ kind: "text" })), ["Writing…", {}]);
  assert.deepEqual(
    plain(context.taskActivityLabel({ kind: "tool", tool: "terminal", detail: "ls -la\nsecond" })),
    ["Running {tool}: {detail}", { tool: "terminal", detail: "ls -la" }],
  );
  assert.deepEqual(plain(context.taskActivityLabel({ kind: "tool", tool: "memory_read", detail: "" })), ["Running {tool}", { tool: "memory_read" }]);
});

test("no activity means no label", () => {
  assert.equal(context.taskActivityLabel(null), null);
  assert.equal(context.taskActivityLabel({}), null);
  assert.equal(context.taskActivityLabel({ kind: "tool", tool: "" }), null);
});

test("the card carries the activity only while the task runs", () => {
  const record = { id: "a1", status: "running", activity: { kind: "text" }, child: { chat_id: "c" } };
  assert.deepEqual(plain(context.taskCardState(record, null, Date.now()).activity), ["Writing…", {}]);
  assert.equal(context.taskCardState({ ...record, status: "done" }, null, Date.now()).activity, null);
  assert.equal(context.taskCardState({ ...record, activity: null }, null, Date.now()).activity, null);
});

test("what the dialog does when the live stream ends", () => {
  assert.equal(context.taskLiveNext({ unavailable: false, failed: false }), "reload");
  assert.equal(context.taskLiveNext({ unavailable: true, failed: false }), "poll");
  assert.equal(context.taskLiveNext({ unavailable: false, failed: true }), "poll");
});
