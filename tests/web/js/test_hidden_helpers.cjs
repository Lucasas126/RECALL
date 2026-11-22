const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");

// the hidden chats / hidden agents helpers of app.js (pure block), loaded without the DOM
const source = readFileSync(path.join(__dirname, "..", "..", "..", "src", "web", "static", "app.js"), "utf8");
const code = source.match(/\/\/ ---- settings helpers[\s\S]*?\/\/ ---- end settings helpers/)[0];
const context = vm.createContext({});
vm.runInContext(`${code}\nthis.api = { selectableAgents, splitHiddenAgents, chatListQuery, readShowHiddenChats, writeShowHiddenChats };`, context);
const api = context.api;
const plain = (value) => JSON.parse(JSON.stringify(value));
const agents = [{ id: "nova" }, { id: "ghost", hidden: true }, { id: "sage", hidden: false }];

test("selectors offer only visible agents, plus the open one", () => {
  assert.deepEqual(api.selectableAgents(agents).map((a) => a.id), ["nova", "sage"]);
  assert.deepEqual(api.selectableAgents(agents, "ghost").map((a) => a.id), ["nova", "ghost", "sage"]);
  assert.deepEqual(plain(api.selectableAgents(null)), []);
});

test("the agent list splits into shown and hidden sections", () => {
  const split = api.splitHiddenAgents(agents);
  assert.deepEqual(split.shown.map((a) => a.id), ["nova", "sage"]);
  assert.deepEqual(split.hidden.map((a) => a.id), ["ghost"]);
  assert.deepEqual(plain(api.splitHiddenAgents([])), { shown: [], hidden: [] });
});

test("the chat list query keeps the open chat and asks for hidden chats only when shown", () => {
  assert.equal(api.chatListQuery(null, false), "");
  assert.equal(api.chatListQuery("c 1", false), "?keep=c%201");
  assert.equal(api.chatListQuery(null, true), "?include_hidden=true");
  assert.equal(api.chatListQuery("c1", true), "?keep=c1&include_hidden=true");
});

test("the toggle is remembered in storage and is off by default or when storage fails", () => {
  const store = new Map();
  const storage = { getItem: (k) => store.get(k) ?? null, setItem: (k, v) => store.set(k, v) };
  assert.equal(api.readShowHiddenChats(storage), false);
  api.writeShowHiddenChats(storage, true);
  assert.equal(api.readShowHiddenChats(storage), true);
  api.writeShowHiddenChats(storage, false);
  assert.equal(api.readShowHiddenChats(storage), false);
  const broken = { getItem() { throw new Error("blocked"); }, setItem() { throw new Error("blocked"); } };
  assert.equal(api.readShowHiddenChats(broken), false);
  api.writeShowHiddenChats(broken, true);
});
