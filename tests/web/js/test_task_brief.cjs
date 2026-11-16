const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");

// the pure task-brief helper, loaded without the DOM
const source = readFileSync(path.join(__dirname, "..", "..", "..", "src", "web", "static", "app.js"), "utf8");
const code = source.slice(source.indexOf("// ---- task cards: pure helpers"), source.indexOf("// ---- end task cards"));
const context = vm.createContext({});
vm.runInContext(`${code}\nObject.assign(this, { taskBriefState });`, context);
const plain = (value) => JSON.parse(JSON.stringify(value));

test("metadata wins over the text", () => {
  const state = context.taskBriefState({
    role: "user", content: "Task abc\n\nGoal:\nignored",
    task_brief: { task_id: "abc123", from: "Aria", user: "user-a", goal: "Find X", context: "ctx", instructions_present: true },
  });
  assert.deepEqual(plain(state), {
    kind: "brief", taskId: "abc123", from: "Aria", user: "user-a", goal: "Find X", context: "ctx", instructionsPresent: true,
  });
});

test("follow-up and correction metadata", () => {
  assert.deepEqual(plain(context.taskBriefState({ role: "user", content: "x", task_message: { kind: "followup", text: "more" } })),
    { kind: "note", note: "followup", text: "more" });
  assert.equal(context.taskBriefState({ role: "user", content: "x", task_message: { kind: "other", text: "t" } }), null);
});

test("legacy text: ephemeral worker, with and without context", () => {
  const plainTask = context.taskBriefState({ role: "user", content: "Task t1\n\nGoal:\nDo it\nnow" });
  assert.deepEqual(plain(plainTask), {
    kind: "brief", taskId: "t1", from: "", user: "", goal: "Do it\nnow", context: "", instructionsPresent: false,
  });
  const withContext = context.taskBriefState({ role: "user", content: "Task t1\n\nGoal:\nDo it\n\nContext:\nline1\nline2" });
  assert.equal(withContext.goal, "Do it");
  assert.equal(withContext.context, "line1\nline2");
});

test("legacy text: task for an existing agent, scheduled task, follow-up, correction", () => {
  const asked = context.taskBriefState({ role: "user", content: "Task from Aria (user-a)\n\nTask t2\n\nGoal:\nG\n\nContext:\nC" });
  assert.deepEqual(plain(asked), { kind: "brief", taskId: "t2", from: "Aria", user: "user-a", goal: "G", context: "C", instructionsPresent: false });
  const scheduled = context.taskBriefState({ role: "user", content: "Scheduled task (schedule) for user-a\n\nTask t3\n\nGoal:\nG" });
  assert.equal(scheduled.taskId, "t3");
  assert.equal(scheduled.user, "user-a");
  assert.deepEqual(plain(context.taskBriefState({ role: "user", content: "Follow-up from the parent agent:\ncompare" })),
    { kind: "note", note: "followup", text: "compare" });
  assert.deepEqual(plain(context.taskBriefState({ role: "user", content: "Correction from the parent agent:\nuse B" })),
    { kind: "note", note: "correction", text: "use B" });
});

test("ordinary messages are not briefs", () => {
  assert.equal(context.taskBriefState({ role: "user", content: "hello" }), null);
  assert.equal(context.taskBriefState({ role: "assistant", content: "Task t1\n\nGoal:\nx" }), null);
  assert.equal(context.taskBriefState(null), null);
});
