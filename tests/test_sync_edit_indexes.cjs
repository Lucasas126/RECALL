// After a reply ends, the user bubbles get their stored index so they can be edited. A stopped reply (an extra or
// missing assistant bubble) must not leave them without it. Runs the real function from app.js.
const assert = require("assert");
const fs = require("fs");
const path = require("path");

const src = fs.readFileSync(path.join(__dirname, "..", "src", "web", "static", "app.js"), "utf8");
const start = src.indexOf("async function syncEditIndexes(request)");
const end = src.indexOf('// 12345 -> "12.345"');
assert(start > 0 && end > start, "syncEditIndexes not found in app.js");

const run = async (bubbles, messages, windowed = null) => {
  const set = [];
  const view = { querySelectorAll: (selector) => { assert.strictEqual(selector, ":scope > .msg.user"); return bubbles; } };
  const make = new Function(
    "api", "chatsUrl", "isCurrent", "isGenerating", "state", "setEditable", "isWakeMessage", "chatQuery",
    src.slice(start, end) + "; return syncEditIndexes;",
  );
  const sync = make(
    async () => ({ messages, window: windowed }), (p) => p, () => true, () => false, { view },
    (el, index) => set.push([el.id, index]),
    (m) => m.source === "task" || m.source === "process",
    () => "?limit=25",
  );
  await sync({ chatId: "c", agentId: "a" });
  return set;
};

(async () => {
  const stored = [
    { index: 0, role: "user" }, { index: 1, role: "assistant" },
    { index: 2, role: "user" }, { index: 3, role: "assistant", interrupted: true },
  ];
  assert.deepStrictEqual(await run([{ id: "u0" }, { id: "u1" }], stored), [["u0", 0], ["u1", 2]]);
  // a wake turn is stored as a user message but never drawn as a user bubble
  assert.deepStrictEqual(
    await run([{ id: "u0" }], [{ index: 0, role: "user" }, { index: 1, role: "user", source: "task" }, { index: 2, role: "assistant" }]),
    [["u0", 0]],
  );
  // more stored user messages than bubbles, with nothing hidden, is a real mismatch: nothing changes
  assert.deepStrictEqual(await run([{ id: "u0" }], stored, { hidden: 0, total: 4, first_index: 0 }), []);
  // a long chat opened at its end: the window leaves out the START, so the bubbles are the last user messages and pair from the end
  const tail = [
    { index: 40, role: "user" }, { index: 41, role: "assistant" }, { index: 42, role: "user" }, { index: 43, role: "assistant" },
  ];
  assert.deepStrictEqual(await run([{ id: "u0" }, { id: "u1" }], tail, { hidden: 40, total: 44, first_index: 40 }), [["u0", 40], ["u1", 42]]);
  // fewer stored than drawn while older messages are hidden (the reply just added is not stored yet): the last ones pair
  assert.deepStrictEqual(await run([{ id: "u0" }, { id: "u1" }, { id: "u2" }], tail, { hidden: 40, total: 44, first_index: 40 }), [["u1", 40], ["u2", 42]]);
  console.log("ok");
})();
