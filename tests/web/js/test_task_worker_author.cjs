const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");

// the pure author helpers of the worker chat dialog, loaded without the DOM
const source = readFileSync(path.join(__dirname, "..", "..", "..", "src", "web", "static", "app.js"), "utf8");
const cut = (from, to) => source.slice(source.indexOf(from), source.indexOf(to));
const code = cut("function replyAuthor(", "// The agent a message @mentions") + cut("function taskWorkerAuthor(", "function renderTaskChatMessages(");
const context = vm.createContext({});
vm.runInContext(`${code}\nObject.assign(this, { taskWorkerAuthor });`, context);
const plain = (value) => JSON.parse(JSON.stringify(value));
const agents = [
  { id: "a", name: "Agent A", avatar: "a.png" },
  { id: "b", name: "Agent B", avatar: "b.png", hidden: true },
];

test("the worker's agent is the author, not the open chat's agent", () => {
  assert.deepEqual(plain(context.taskWorkerAuthor({ task: { agent_id: "b" } }, null, agents, "a")), { id: "b", name: "Agent B", avatar: "b.png" });
});

test("before the chat loads the task record names the worker's agent", () => {
  assert.equal(context.taskWorkerAuthor(null, { child: { agent_id: "b" } }, agents, "a").name, "Agent B");
});

test("an ephemeral worker of the same agent keeps the launcher", () => {
  assert.equal(context.taskWorkerAuthor({ task: { agent_id: "a" } }, { child: { agent_id: "a" } }, agents, "a").name, "Agent A");
});

test("without any data it falls back to the open chat's agent", () => {
  assert.equal(context.taskWorkerAuthor(null, undefined, agents, "a").id, "a");
});
