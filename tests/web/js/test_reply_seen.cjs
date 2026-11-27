const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");

const source = readFileSync(path.join(__dirname, "..", "..", "..", "src", "web", "static", "app.js"), "utf8");
const grab = (name) => source.match(new RegExp(`function ${name}\\([\\s\\S]*?\\r?\\n\\}\\r?\\n`))[0];

function harness() {
  const calls = [];
  const timers = [];
  const context = vm.createContext({
    calls, markChatRead: (agent, chat) => calls.push(`${agent}/${chat}`),
    setTimeout: (fn, ms) => timers.push({ fn, ms }), timers,
  });
  vm.runInContext(`const REPLY_SEEN_DELAY_MS = 1500;\n${grab("markReplySeen")}\nthis.markReplySeen = markReplySeen;`, context);
  return { context, calls, timers };
}

test("a reply watched to the end clears the unread mark after a short delay", () => {
  const { context, calls, timers } = harness();
  context.markReplySeen({ agentId: "bot", chatId: "c1" }, true);
  assert.deepEqual(calls, []);
  assert.equal(timers.length, 1);
  assert.equal(timers[0].ms, 1500);
  timers[0].fn();
  assert.deepEqual(calls, ["bot/c1"]);
});

test("a reply that ended in another chat or a hidden tab stays unread", () => {
  const { context, calls, timers } = harness();
  context.markReplySeen({ agentId: "bot", chatId: "c1" }, false);
  assert.equal(timers.length, 0);
  assert.deepEqual(calls, []);
});
