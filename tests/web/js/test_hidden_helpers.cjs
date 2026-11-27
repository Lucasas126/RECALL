const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");

// the hidden chats / hidden agents helpers of app.js (pure block), loaded without the DOM
const source = readFileSync(path.join(__dirname, "..", "..", "..", "src", "web", "static", "app.js"), "utf8");
const code = source.match(/\/\/ ---- settings helpers[\s\S]*?\/\/ ---- end settings helpers/)[0];
const context = vm.createContext({});
vm.runInContext(`${code}\nthis.api = { selectableAgents, splitHiddenAgents, sharedAgentNote, chatListQuery, readShowHidden, writeShowHidden };`, context);
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
  assert.deepEqual(plain(api.splitHiddenAgents([])), { shown: [], shared: [], hidden: [] });
});

test("shared agents get their own section; a hidden shared one stays under Hidden", () => {
  const list = [...agents, { id: "team", shared: true }, { id: "team-ghost", shared: true, hidden: true }];
  const split = api.splitHiddenAgents(list);
  assert.deepEqual(split.shown.map((a) => a.id), ["nova", "sage"]);
  assert.deepEqual(split.shared.map((a) => a.id), ["team"]);
  assert.deepEqual(split.hidden.map((a) => a.id), ["ghost", "team-ghost"]);
});

test("the shared note names the billing user and is empty for own agents", () => {
  const tr = (text, values = {}) => text.replace(/\{(\w+)\}/g, (_, key) => values[key]);  // like the page's tr()
  assert.equal(api.sharedAgentNote({ id: "nova" }, tr), "");
  assert.equal(api.sharedAgentNote({ shared: true, billing_user: "user-a" }, tr), "Shared agent · uses the providers and subscriptions of user-a");
  const agent = { shared: true, billing_user: "user-a", members: [{ id: "user-a", name: "Ana" }, { id: "user-b", name: "Ben" }] };
  assert.equal(api.sharedAgentNote(agent, tr, "user-b"), "Shared agent · uses the providers and subscriptions of Ana");
  assert.equal(api.sharedAgentNote(agent, tr, "user-a"), "Shared agent · uses your providers and subscriptions");
  assert.match(api.sharedAgentNote({ shared: true }, tr), /the space owner/);
});

test("the chat list query keeps the open chat and asks for hidden chats only when shown", () => {
  assert.equal(api.chatListQuery(null, false), "");
  assert.equal(api.chatListQuery("c 1", false), "?keep=c%201");
  assert.equal(api.chatListQuery(null, true), "?include_hidden=true");
  assert.equal(api.chatListQuery("c1", true), "?keep=c1&include_hidden=true");
});

test("the hidden button is remembered per key, with its own default, and survives broken storage", () => {
  const store = new Map();
  const storage = { getItem: (k) => store.get(k) ?? null, setItem: (k, v) => store.set(k, v) };
  assert.equal(api.readShowHidden(storage, "showHiddenChats", false), false);
  assert.equal(api.readShowHidden(storage, "showHiddenAgents", true), true);
  api.writeShowHidden(storage, "showHiddenChats", true);
  api.writeShowHidden(storage, "showHiddenAgents", false);
  assert.equal(api.readShowHidden(storage, "showHiddenChats", false), true);
  assert.equal(api.readShowHidden(storage, "showHiddenAgents", true), false);
  const broken = { getItem() { throw new Error("blocked"); }, setItem() { throw new Error("blocked"); } };
  assert.equal(api.readShowHidden(broken, "showHiddenAgents", true), true);
  api.writeShowHidden(broken, "showHiddenChats", true);
});
