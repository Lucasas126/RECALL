const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");

// highlightChat and the click handler of a chat row, loaded without a browser
const source = readFileSync(path.join(__dirname, "..", "..", "..", "src", "web", "static", "app.js"), "utf8");
const highlight = source.match(/function highlightChat\([\s\S]*?\r?\n\}\r?\n/)[0];
const handler = source.match(/li\.addEventListener\("click", async \(\) => \{[\s\S]*?\r?\n      \}\);/)[0];

function row(agent, chat, active = false) {
  const classes = new Set(active ? ["active"] : []);
  const del = { dataset: { agent, chat } };
  return { classes, classList: { toggle: (name, on) => (on ? classes.add(name) : classes.delete(name)), contains: (name) => classes.has(name) }, querySelector: () => del };
}

function harness({ open, fails = false, busy = false } = {}) {
  const rows = [row("a", "c1", true), row("a", "c2"), row("b", "c3")];
  const section = { classList: { toggle() { throw new Error("a section heading has no highlight"); } }, querySelector: () => null };
  const state = { agentId: "a", chatId: "c1", modelBusy: busy, settingsBusy: false };
  const log = [];
  const clicks = {};
  const li = { addEventListener: (type, fn) => { clicks[type] = fn; } };
  const context = vm.createContext({
    state, log, li, rows, $: () => ({ children: [section, ...rows] }),
    chat: { agent_id: "a", id: "c2" },
    openChatFromList: async () => {
      log.push(rows.map((r) => r.classes.has("active")).join());  // what the sidebar shows while the chat is loading
      if (fails) throw new Error("could not open");
      await open?.(state);
    },
  });
  vm.runInContext(`${highlight}\n${handler}\nthis.highlightChat = highlightChat;`, context);
  return { context, clicks, rows, state, log };
}

const shown = (rows) => rows.map((r) => r.classes.has("active"));

test("a click marks the chosen chat at once, before it has loaded", async () => {
  const { clicks, rows, log } = harness();
  await clicks.click();
  assert.deepEqual(log, ["false,true,false"]);
  assert.deepEqual(shown(rows), [false, true, false]);
});

test("the same chat id under another agent is not marked", () => {
  const { context, rows } = harness();
  context.highlightChat("b", "c3");
  assert.deepEqual(shown(rows), [false, false, true]);
  context.highlightChat("a", "c3");
  assert.deepEqual(shown(rows), [false, false, false]);
});

test("a chat that fails to open puts the mark back on the chat that is open, and the error is not swallowed", async () => {
  const { clicks, rows } = harness({ fails: true });
  await assert.rejects(clicks.click(), /could not open/);
  assert.deepEqual(shown(rows), [true, false, false]);
});

test("nothing is marked while the picker is busy: the click is ignored", async () => {
  const { clicks, rows, log } = harness({ busy: true });
  await clicks.click();
  assert.deepEqual(log, []);
  assert.deepEqual(shown(rows), [true, false, false]);
});
