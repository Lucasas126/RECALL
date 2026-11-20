const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");

// switchAgent (the agent selector), loaded without a browser, with a fake server behind it
const source = readFileSync(path.join(__dirname, "..", "..", "..", "src", "web", "static", "app.js"), "utf8");
const switchAgent = source.match(/async function switchAgent\([\s\S]*?\r?\n\}\r?\n/)[0];

function harness({ failNewChat = false } = {}) {
  const select = { value: "a" };
  const errors = [];
  const state = { agentId: "a", chatId: "old", session: 1, agentSwitch: 0 };
  const log = [];
  const context = vm.createContext({
    state, log, errors,
    $: () => select,
    addMessage: () => ({ error: (err) => errors.push(err.message) }),
    savePreferences: async ({ agent_id }) => { log.push(`prefs ${agent_id}`); },
    selectAgent: async (id) => { state.agentId = id; state.chatId = null; select.value = id; log.push(`select ${id}`); },
    newChat: async () => {
      if (failNewChat) { errors.push("new chat failed"); return false; }
      state.chatId = `fresh-${state.agentId}`;
      return true;
    },
    openChat: async (id) => { state.chatId = id; log.push(`open ${id}`); },
  });
  vm.runInContext(`${switchAgent}\nthis.switchAgent = switchAgent;`, context);
  return { context, state, select, log, errors };
}

test("choosing another agent opens a fresh chat with it", async () => {
  const { context, state, log } = harness();
  await context.switchAgent("b");
  assert.equal(state.agentId, "b");
  assert.equal(state.chatId, "fresh-b");
  assert.deepEqual(log, ["prefs b", "select b"]);
});

test("a fresh chat that cannot open puts the previous agent and chat back", async () => {
  const { context, state, select, log } = harness({ failNewChat: true });
  await context.switchAgent("b");
  assert.equal(state.agentId, "a");
  assert.equal(state.chatId, "old");
  assert.equal(select.value, "a");
  assert.deepEqual(log, ["prefs b", "select b", "prefs a", "select a", "open old"]);
});

test("a switch overtaken by a newer one does not roll anything back", async () => {
  const { context, state, log } = harness({ failNewChat: true });
  context.newChat = async () => { state.agentSwitch += 1; return false; };  // a second choice started meanwhile
  await context.switchAgent("b");
  assert.equal(state.agentId, "b");
  assert.deepEqual(log, ["prefs b", "select b"]);
});

test("a failed preference save reports the error and keeps the selector on the open agent", async () => {
  const { context, state, select, errors } = harness();
  context.savePreferences = async () => { throw new Error("offline"); };
  await context.switchAgent("b");
  assert.equal(select.value, "a");
  assert.equal(state.agentId, "a");
  assert.deepEqual(errors, ["offline"]);
});
