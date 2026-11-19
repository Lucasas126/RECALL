const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");

// An empty chat pruned by the server while the user writes or records in it must not break sending or lose the draft.
const source = readFileSync(path.join(__dirname, "..", "..", "..", "src", "web", "static", "app.js"), "utf8");
const grab = (name) => source.match(new RegExp(`(?:async )?function ${name}\\([\\s\\S]*?\\r?\\n\\}\\r?\\n`))[0];

const view = (total, users = 0) => ({ history: { total }, querySelector: () => (users ? {} : null) });
const gone = Object.assign(new Error("unknown chat"), { code: "unknown_chat", status: 404 });

function harness(error, chatView) {
  const applied = [];
  const state = {
    session: 1, preferencesRequest: 1, agentId: "bot", chatId: "c1", chatModelRevision: 0, modelBusy: false, view: chatView,
  };
  const context = vm.createContext({
    state, applied,
    chatsUrl: (suffix) => `/chats${suffix}`,
    api: async () => { if (error) throw error; return { model: "m" }; },
    applyChatModels: (models) => applied.push(models),
  });
  vm.runInContext(`${grab("vanishedEmptyChat")}\n${grab("refreshChatModels")}\nthis.vanishedEmptyChat = vanishedEmptyChat; this.refreshChatModels = refreshChatModels;`, context);
  return { context, applied };
}

test("an empty chat that vanished is recognised from the error code or a 404", () => {
  const { context } = harness(null, view(0));
  assert.equal(context.vanishedEmptyChat(gone, view(0)), true);
  assert.equal(context.vanishedEmptyChat(Object.assign(new Error("x"), { status: 404 }), view(0)), true);
});

test("a chat with messages, or another error, is not treated as a vanished empty chat", () => {
  const { context } = harness(null, view(0));
  assert.equal(context.vanishedEmptyChat(gone, view(3)), false);
  assert.equal(context.vanishedEmptyChat(Object.assign(new Error("x"), { status: 500 }), view(0)), false);
  assert.equal(context.vanishedEmptyChat(gone, null), false);
});

test("reading the models of a vanished empty chat does not fail (the send goes on)", async () => {
  const { context, applied } = harness(gone, view(0));
  await context.refreshChatModels(1, 1);
  assert.deepEqual(applied, []);
});

test("the bubble of the message being sent does not make the chat non-empty", () => {
  const { context } = harness(null, view(0, 1));
  assert.equal(context.vanishedEmptyChat(gone, view(0, 1)), true);
});

test("reading the models of a vanished chat that has messages still fails", async () => {
  const { context } = harness(gone, view(2, 1));
  await assert.rejects(context.refreshChatModels(1, 1), /unknown chat/);
});
