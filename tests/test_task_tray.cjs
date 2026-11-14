const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");

// the pure helper of the running-subagents tray, loaded without the DOM
const source = readFileSync(path.join(__dirname, "..", "src", "web", "static", "app.js"), "utf8");
const code = source.slice(source.indexOf("// ---- task cards: pure helpers"), source.indexOf("// ---- end task cards"));
const context = vm.createContext({});
vm.runInContext(`${code}\nObject.assign(this, { taskTrayRows });`, context);
const plain = (value) => JSON.parse(JSON.stringify(value));

const NOW = Date.parse("2026-11-05T10:05:00+01:00");
const task = (id, status, extra = {}) => ({
  id: String(id), status, title: `task ${id}`, goal: `goal ${id}`, model: "m", created_at: `2026-11-05T10:0${id}:00+01:00`,
  started_at: `2026-11-05T10:0${id}:00+01:00`, parent: { agent_id: "a", chat_id: "c" }, child: { chat_id: "k" }, ...extra,
});

test("only running tasks of the open chat, oldest first", () => {
  const rows = plain(context.taskTrayRows([
    task(3, "running"), task(1, "running"), task(2, "done"), task(4, "running", { parent: { agent_id: "a", chat_id: "other" } }),
    task(5, "stopped"),
  ], "a", "c", NOW));
  assert.deepEqual(rows.map((row) => row.id), ["1", "3"]);
});

test("row fields: title, goal, model, live clock, open chat", () => {
  const [row] = plain(context.taskTrayRows([task(1, "running", { title: "" })], "a", "c", NOW));
  assert.equal(row.title, "goal 1");
  assert.equal(row.model, "m");
  assert.equal(row.elapsed, "4m 00s");
  assert.equal(row.canOpenChat, true);
  const [later] = plain(context.taskTrayRows([task(1, "running")], "a", "c", NOW + 5000));
  assert.equal(later.elapsed, "4m 05s");
  const [none] = plain(context.taskTrayRows([task(1, "running", { child: {}, model: null })], "a", "c", NOW));
  assert.equal(none.canOpenChat, false);
  assert.equal(none.model, "");
});

test("nothing to show without records", () => {
  assert.deepEqual(plain(context.taskTrayRows([], "a", "c", NOW)), []);
  assert.deepEqual(plain(context.taskTrayRows(new Map().values(), "a", "c", NOW)), []);
});
