const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");

// openChat and the preference helpers it uses, loaded without the DOM and with every collaborator stubbed
const source = readFileSync(path.join(__dirname, "..", "..", "..", "src", "web", "static", "app.js"), "utf8");
const grab = (name) => source.match(new RegExp(`(?:async )?function ${name}\\([\\s\\S]*?\\r?\\n\\}\\r?\\n`))[0];

function harness({ preferencesDelay = 0, chatDelay = 0 } = {}) {
  const log = [];
  const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const state = {
    session: 1, agentId: "bot", chatId: "old", chatInfo: null, user: { id: "u" }, modelBusy: false, chatModelRevision: 0,
    preferencesRequest: 0, settingsBusy: false, views: new Map(), usages: new Map(), translations: {},
  };
  const context = vm.createContext({
    state, log, wait,
    $: () => ({ hidden: true, focus() { log.push("focus"); } }),
    chatsUrl: (suffix = "") => `/api/agents/bot/chats${suffix}`,
    chatQuery: () => "?limit=25",
    api: async (url) => {
      log.push(`start ${url}`);
      if (url === "/api/preferences") { await wait(preferencesDelay); log.push("end preferences"); return { language: "en" }; }
      if (url.endsWith("/chat-1?limit=25")) {
        await wait(chatDelay);
        log.push("end chat");
        return { chat: { active: false }, messages: [{ role: "user", content: "hi" }], model: { id: "m" } };
      }
      log.push(`unexpected ${url}`);
      return {};
    },
    applyPreferences: () => log.push("applyPreferences"),
    refreshChatModels: async () => log.push("refreshChatModels"),
    markChatRead: () => {}, applyChatModels: () => log.push("applyChatModels"), rememberChat: () => {},
    chatKey: (a, c) => `${a}/${c}`, isGenerating: () => false, forgetApprovals: () => {}, renderMessages: () => log.push("render"),
    followReply: () => {}, updateBusy: () => {}, syncApprovals: () => {}, ensureTaskPolling: () => {}, startProcessPolling: () => {},
    refreshChats: async () => log.push("refreshChats"), activateView: () => {}, showUsage: () => {},
  });
  vm.runInContext(
    `${grab("applyServerPreferences")}\n${grab("refreshPreferences")}\n${grab("openChat")}\n`
    + "this.openChat = openChat; this.refreshPreferences = refreshPreferences;",
    context,
  );
  return { context, log, state };
}

test("the preferences and the chat are requested together, before either answers", async () => {
  const { context, log } = harness({ preferencesDelay: 20, chatDelay: 20 });
  await context.openChat("chat-1");
  const starts = log.filter((entry) => entry.startsWith("start"));
  assert.deepEqual(starts, ["start /api/preferences", "start /api/agents/bot/chats/chat-1?limit=25"]);
  assert.ok(log.indexOf("start /api/agents/bot/chats/chat-1?limit=25") < log.indexOf("end preferences"), "the chat waited for the preferences");
});

test("opening a chat does not ask for the models of the chat it is leaving", async () => {
  const { context, log } = harness();
  await context.openChat("chat-1");
  assert.ok(!log.includes("refreshChatModels"), "an extra /models request was made");
  assert.ok(log.includes("applyChatModels"), "the models of the opened chat were not applied");
});

test("the chat is drawn and the input focused before the sidebar refresh", async () => {
  const { context, log } = harness();
  await context.openChat("chat-1");
  assert.ok(log.indexOf("render") < log.indexOf("focus"));
  assert.ok(log.indexOf("focus") < log.indexOf("refreshChats"));
});

test("refreshPreferences keeps refreshing the open chat's models by default", async () => {
  const { context, log } = harness();
  await context.refreshPreferences();
  assert.ok(log.includes("refreshChatModels"));
  log.length = 0;
  await context.refreshPreferences({ models: false });
  assert.ok(!log.includes("refreshChatModels"));
  assert.ok(log.includes("applyPreferences"));
});

test("a switch that was overtaken (another session or a busy picker) draws nothing", async () => {
  const { context, log, state } = harness({ chatDelay: 20 });
  const pending = context.openChat("chat-1");
  state.session = 2;
  await pending;
  assert.ok(!log.includes("render"));
  assert.notEqual(state.chatId, "chat-1");
});
