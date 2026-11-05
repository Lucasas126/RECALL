const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");

// the last-open-chat helpers (address bar link and per-user storage), loaded without the DOM
const source = readFileSync(path.join(__dirname, "..", "src", "web", "static", "app.js"), "utf8");
const code = source.match(/\/\/ ---- last open chat[\s\S]*?\/\/ ---- end last open chat/)[0];
const context = vm.createContext({ encodeURIComponent, decodeURIComponent });
vm.runInContext(
  `${code}\nthis.chatHash = chatHash; this.parseChatHash = parseChatHash; this.read = readLastChat; this.write = writeLastChat; this.clear = clearLastChat;`,
  context,
);
const memory = (initial = {}) => ({
  data: { ...initial },
  getItem(k) { return k in this.data ? this.data[k] : null; },
  setItem(k, v) { this.data[k] = String(v); },
  removeItem(k) { delete this.data[k]; },
});
const broken = { getItem() { throw new Error("blocked"); }, setItem() { throw new Error("blocked"); }, removeItem() { throw new Error("blocked"); } };

test("a chat link round-trips, including characters that need escaping", () => {
  const hash = "#" + context.chatHash("aria", "20261105-111844-a66d9c");
  assert.deepEqual({ ...context.parseChatHash(hash) }, { agentId: "aria", chatId: "20261105-111844-a66d9c" });
  const odd = "#" + context.chatHash("a b/c", "x?y#z");
  assert.deepEqual({ ...context.parseChatHash(odd) }, { agentId: "a b/c", chatId: "x?y#z" });
});

test("other hashes are not chat links", () => {
  for (const hash of ["", "#", "#agents/aria", "#chat/aria", "#chat//x", "#chat/a/b/c", "#chat/%E0%A4%A/x", null, undefined]) {
    assert.equal(context.parseChatHash(hash), null, String(hash));
  }
});

test("the last chat is stored per user and cleared on demand", () => {
  const storage = memory();
  context.write(storage, "user-a", "aria", "c1");
  assert.deepEqual({ ...context.read(storage, "user-a") }, { agentId: "aria", chatId: "c1" });
  assert.equal(context.read(storage, "user-b"), null);
  context.clear(storage, "user-a");
  assert.equal(context.read(storage, "user-a"), null);
});

test("broken or corrupt storage never throws", () => {
  assert.equal(context.read(broken, "user-a"), null);
  context.write(broken, "user-a", "aria", "c1");
  context.clear(broken, "user-a");
  assert.equal(context.read(memory({ "lastChat:user-a": "not json" }), "user-a"), null);
  assert.equal(context.read(memory({ "lastChat:user-a": JSON.stringify({ agentId: 1 }) }), "user-a"), null);
});
