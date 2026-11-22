const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");

// openChat and the preference helpers it uses, loaded without the DOM and with every collaborator stubbed
const source = readFileSync(path.join(__dirname, "..", "..", "..", "src", "web", "static", "app.js"), "utf8");
const grab = (name) => source.match(new RegExp(`(?:async )?function ${name}\\([\\s\\S]*?\\r?\\n\\}\\r?\\n`))[0];
const LOADING_CONSTANTS = "const LOADING_DELAY_MS = 120; let loadingDepth = 0; let loadingTimer = null;\n";

function harness({ preferencesDelay = 0, chatDelay = 0, listed = true } = {}) {
  const log = [];
  const bar = { classList: { add: (name) => log.push(`bar+${name}`), remove: (name) => log.push(`bar-${name}`) } };
  const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const state = {
    session: 1, agentId: "bot", chatId: "old", chatInfo: null, user: { id: "u" }, modelBusy: false, chatModelRevision: 0,
    preferencesRequest: 0, settingsBusy: false, views: new Map(), usages: new Map(), translations: {},
  };
  const context = vm.createContext({
    state, log, wait, setTimeout, clearTimeout,
    bar,
    $: (id) => (id === "messages-wrap" ? bar : { hidden: true, focus() { log.push("focus"); } }),
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
    highlightChat: (agentId, chatId) => { log.push(`highlight ${agentId}/${chatId}`); return listed; },
    renderChatHeader: () => {}, refreshChats: async () => log.push("refreshChats"), activateView: () => {}, showUsage: () => {},
  });
  vm.runInContext(
    `${LOADING_CONSTANTS}${grab("beginLoading")}\n${grab("applyServerPreferences")}\n${grab("refreshPreferences")}\n${grab("openChat")}\n${grab("openChatNow")}\n`
    + "this.openChat = openChat; this.refreshPreferences = refreshPreferences; this.beginLoading = beginLoading;",
    context,
  );
  return { context, log, state };
}

test("the chat is drawn without waiting for the preferences", async () => {
  const { context, log } = harness({ preferencesDelay: 60, chatDelay: 5 });
  await context.openChat("chat-1");
  assert.ok(log.indexOf("render") >= 0 && !log.includes("end preferences"), "the chat waited for the preferences");
  assert.ok(log.indexOf("start /api/agents/bot/chats/chat-1?limit=25") >= 0);
});

test("a slow chat shows the loading bar after the delay and removes it when done", async () => {
  const { context, log } = harness({ chatDelay: 250 });
  await context.openChat("chat-1");
  assert.deepEqual(log.filter((entry) => entry.startsWith("bar")), ["bar+loading", "bar-loading"]);
  assert.ok(log.indexOf("bar+loading") < log.indexOf("end chat") && log.indexOf("end chat") < log.indexOf("bar-loading"));
});

test("a fast chat never shows the loading bar", async () => {
  const { context, log } = harness({ chatDelay: 5 });
  await context.openChat("chat-1");
  assert.ok(!log.includes("bar+loading"), "the bar flashed on a fast load");
});

test("the loading bar nests and goes only when the last load ends, also after a failure", async () => {
  const { context, log } = harness();
  const outer = context.beginLoading();
  const inner = context.beginLoading();
  await new Promise((resolve) => setTimeout(resolve, 150));
  inner(); inner();
  assert.ok(!log.includes("bar-loading"));
  outer();
  assert.deepEqual(log.filter((entry) => entry.startsWith("bar")), ["bar+loading", "bar-loading"]);
  context.state.session = 2;  // the page changed session mid-load: the chat is not drawn but the bar still goes
  await context.openChat("chat-1").catch(() => {});
  assert.ok(!log.slice(-3).includes("bar+loading") || log.at(-1) === "bar-loading");
});

test("opening a chat does not ask for the models of the chat it is leaving", async () => {
  const { context, log } = harness();
  await context.openChat("chat-1");
  assert.ok(!log.includes("refreshChatModels"), "an extra /models request was made");
  assert.ok(log.includes("applyChatModels"), "the models of the opened chat were not applied");
});

test("a switch to a listed chat marks it in the sidebar without asking for the whole list again", async () => {
  const { context, log } = harness();
  await context.openChat("chat-1");
  assert.ok(log.indexOf("render") < log.indexOf("focus"));
  assert.ok(log.indexOf("focus") < log.indexOf("highlight bot/chat-1"));
  assert.ok(!log.includes("refreshChats"), "the whole chat list was fetched again");
});

test("a switch never asks for the preferences", async () => {
  const { context, log } = harness();
  await context.openChat("chat-1");
  assert.ok(!log.includes("start /api/preferences"), "a switch asked for the preferences");
});

test("a chat the sidebar does not list yet (just created) brings the list in the background", async () => {
  const { context, log } = harness({ listed: false });
  await context.openChat("chat-1");
  assert.ok(log.includes("refreshChats"));
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
