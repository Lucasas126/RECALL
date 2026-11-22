const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");

// DOM methods app.js calls on elements that these tests do not exercise.
const METHODS = new Set(["closest", "remove", "removeChild", "removeEventListener", "insertBefore", "prepend",
  "before", "after", "scrollIntoView", "blur", "click", "select", "getAttribute", "removeAttribute",
  "hasAttribute", "matches", "contains", "dispatchEvent", "getBoundingClientRect", "appendChild", "cloneNode",
  "setSelectionRange", "scrollTo", "animate", "toggleAttribute", "querySelector", "getElementsByTagName"]);

function pickerHarness() {
  const elements = new Map();
  // Methods the page calls that this test does not care about answer with a no-op
  // (and a chainable element), so a new DOM call in app.js does not break the test.
  const lenient = (target) => new Proxy(target, {
    get(obj, key) {
      if (key in obj || typeof key === "symbol" || !METHODS.has(key)) return obj[key];
      return () => lenient(element());
    },
  });
  const element = () => lenient({
    children: [], options: [], attributes: {}, listeners: {}, dataset: {},
    checked: false, value: "", open: false, disabled: false,
    classList: {
      active: new Set(),
      toggle(name, enabled) { if (enabled) this.active.add(name); else this.active.delete(name); },
      add(name) { this.active.add(name); },
      remove(name) { this.active.delete(name); },
      contains(name) { return this.active.has(name); },
    },
    addEventListener(name, callback) { this.listeners[name] = callback; },
    setAttribute(name, value) { this.attributes[name] = value; },
    append(...children) { this.children.push(...children); },
    replaceChildren(...children) { this.children = children; },
    querySelectorAll() { return []; },
    showModal() { this.open = true; },
    close() { this.open = false; },
    focus() { this.focused = true; },
  });
  const context = vm.createContext({
    addEventListener() {},
    document: {
      getElementById(id) {
        if (!elements.has(id)) elements.set(id, element());
        return elements.get(id);
      },
      createElement: element,
      addEventListener() {},
      removeEventListener() {},
      contains() { return false; },
      querySelector() { return element(); },
      querySelectorAll() { return []; },
      documentElement: element(),
      body: element(),
    },
    localStorage: { getItem() { return null; }, setItem() {}, removeItem() {} },
    matchMedia: () => ({ matches: false, addEventListener() {}, addListener() {} }),
    MutationObserver: class { observe() {} disconnect() {} },
    location: { href: "http://localhost/", search: "", hash: "", pathname: "/" },
    setInterval() { return 0; },
    clearInterval() {},
    setTimeout() { return 0; },
    clearTimeout() {},
    requestAnimationFrame() { return 0; },
    fetch: () => new Promise(() => {}),
    navigator: { language: "en", mediaDevices: undefined },
    Option: class { constructor(text = "", value = "") { Object.assign(this, element(), { textContent: text, value }); } },
    Intl,
  });
  context.window = context;
  vm.runInContext(
    readFileSync(path.join(__dirname, "..", "..", "..", "src", "web", "static", "app.js"), "utf8"),
    context,
  );
  elements.get("model-zdr").checked = true;
  elements.get("model-sort").value = "favorites";
  context.document.getElementById("model-info");
  context.document.getElementById("model-filters");
  for (const id of ["settings-thinking", "settings-language"]) {
    context.document.getElementById(id);
  }
  vm.runInContext("state", context).translations = new Proxy({}, {
    get(target, key) { return target[key] ?? key; },
  });
  // Since the provider tabs (2026-11-04) the list only renders when a provider is selected
  // and the catalog belongs to it; the tests that fill a catalog assume OpenRouter.
  const pickerState = vm.runInContext("state", context);
  pickerState.modelProvider = "openrouter";
  pickerState.modelCatalogProvider = "openrouter";
  return {
    elements, context,
    compareModels: vm.runInContext("compareModels", context),
    matchesCapabilities: vm.runInContext("matchesCapabilities", context),
    state: vm.runInContext("state", context),
    updateControls: vm.runInContext("updateModelControls", context),
    render: vm.runInContext("renderModelList", context),
    refresh: vm.runInContext("refreshModels", context),
    reset: vm.runInContext("resetStreams", context),
    applyPreferences: vm.runInContext("applyPreferences", context),
    applyChatModels: vm.runInContext("applyChatModels", context),
    refreshPreferences: vm.runInContext("refreshPreferences", context),
    savePreferences: vm.runInContext("savePreferences", context),
    newChat: vm.runInContext("newChat", context),
  };
}
const { compareModels, matchesCapabilities } = pickerHarness();

const sharedDefaults = {
  show_thinking: true, language: "en", agent_id: "agent-a",
  default_llm_model: "llm/base", default_stt_model: "stt/base",
};
const inheritedModels = {
  model: { id: "llm/base", provider: "openrouter", zdr: true }, model_override: null,
  stt_model: "stt/base",
};
const settlePicker = () => new Promise((resolve) => setImmediate(resolve));
const providerRows = [{ id: "openrouter", label: "OpenRouter", available: true }];
// Answers the picker's loading flow: provider status, catalog of the selected provider and favorites.
// Returns null for any other URL so each test can answer its own endpoints.
function pickerApi(url, { chat = [], stt = [], favorites = [] } = {}) {
  const [pathname, query = ""] = url.split("?");
  const params = new URLSearchParams(query);
  if (pathname === "/api/providers/status") return { providers: providerRows };
  if (pathname === "/api/models/favorites") return favorites;
  if (pathname === "/api/models/catalog") {
    assert.equal(params.get("provider"), "openrouter");
    return { models: params.get("capability") === "stt" ? stt : chat };
  }
  return null;
}

test("default pickers work without a chat, stage both models, and save only changed defaults", async () => {
  const harness = pickerHarness();
  Object.assign(harness.state, {
    user: { agents: [], id: "demo", provider: "openrouter" }, requests: new Set(["in-flight"]), busy: true,
  });
  harness.applyPreferences({ ...sharedDefaults });
  vm.runInContext("fillSettings()", harness.context);
  const requests = [];
  harness.context.fetch = async (url, options) => {
    requests.push({ url, options });
    let body = pickerApi(url, {
      chat: [{ id: "llm/new", name: "New", zdr_providers: [], pricing: {} }],
      stt: [{ id: "stt/new", name: "New STT", zdr_providers: [], pricing_prompt: "1" }],
    });
    if (body === null) {
      assert.equal(url, "/api/preferences");
      assert.equal(options.method, "PATCH");
      body = { ...sharedDefaults, ...JSON.parse(options.body) };
    }
    return { ok: true, json: async () => body };
  };
  for (const [button, target, id] of [
    ["settings-llm", "default-llm", "llm/new"],
    ["settings-stt", "default-stt", "stt/new"],
  ]) {
    harness.elements.get(button).listeners.click();
    await settlePicker();
    assert.equal(harness.state.modelTarget, target);
    assert.equal(harness.elements.get("model-inherit").hidden, true);
    assert.equal(harness.elements.get("model-zdr").disabled, true);
    assert.equal(harness.elements.get("model-list").children[0].children[0].disabled, false);
    await vm.runInContext("selectModel", harness.context)(id);
    assert.equal(harness.elements.get("model-dialog").open, false);
  }
  assert.equal(harness.state.preferences.default_llm_model, "llm/base");
  assert.equal(harness.state.preferences.default_stt_model, "stt/base");
  await harness.elements.get("settings-form").listeners.submit({ preventDefault() {} });
  const save = requests.find(({ options }) => options.method === "PATCH");
  assert.deepEqual(JSON.parse(save.options.body), { default_llm_model: "llm/new", default_stt_model: "stt/new" });
  assert.equal(harness.state.preferences.default_llm_model, "llm/new");
  assert.equal(harness.state.preferences.default_stt_model, "stt/new");
  assert.equal(requests.some(({ url }) => url.includes("/chats/")), false);
});

test("default STT picker uses its own catalog, prices and no LLM favorites or capabilities", async () => {
  const harness = pickerHarness();
  Object.assign(harness.state, {
    user: { agents: [], provider: "openrouter" }, modelTarget: "default-stt",
    settingsOriginal: sharedDefaults, settingsModelDraft: { default_stt_model: "stt/cheap" },
    modelFavorites: ["llm/favorite"], modelFavoritesOnly: false,
  });
  for (const id of ["model-tools", "model-images", "model-reasoning"]) harness.elements.get(id).checked = true;
  harness.elements.get("model-sort").value = "prompt-asc";
  const urls = [];
  harness.context.fetch = async (url) => {
    urls.push(url);
    const body = pickerApi(url, { stt: [
      { id: "stt/expensive", name: "Expensive", pricing_prompt: "10", zdr_providers: [] },
      { id: "stt/cheap", name: "Cheap", pricing_prompt: "2", zdr_providers: [] },
    ] });
    assert.notEqual(body, null, url);
    return { ok: true, json: async () => body };
  };
  await harness.refresh(true);
  assert.equal(harness.state.modelCatalogKind, "stt");
  assert.deepEqual(Array.from(harness.state.modelFavorites), ["llm/favorite"]);
  const rows = harness.elements.get("model-list").children;
  assert.equal(rows.length, 2);
  assert.equal(rows[0].children.length, 3);  // choice, info toggle, details (no star for STT)
  const choice = rows[0].children[0];
  assert.equal(choice.children[1].textContent, "stt/cheap");
  assert.equal(choice.attributes["aria-pressed"], "true");
  const details = rows[0].children[2];
  assert.match(details.textContent, /USD \/ 1M tokens: input from \$2/);
  assert.equal(details.textContent.includes("output"), false);
});

test("resetting the LLM chat override calls the nullable endpoint without saving user preferences", async () => {
  const harness = pickerHarness();
  Object.assign(harness.state, {
    user: { agents: [], provider: "openrouter" }, agentId: "agent-a", chatId: "chat",
    model: { id: "llm/override", zdr: true }, modelOverride: { id: "llm/override" },
  });
  harness.applyPreferences({ ...sharedDefaults });
  const calls = [];
  // Since the model reload flow, selecting PUTs the choice and then reads the chat's models back.
  harness.context.fetch = async (url, options) => {
    calls.push(url);
    if (url.endsWith("/models")) {
      return { ok: true, json: async () => ({
        model: { id: "llm/base", provider: "openrouter", zdr: true }, model_override: null, model_source: "user",
      }) };
    }
    assert.equal(options.method, "PUT");
    assert.equal(JSON.parse(options.body).model_id, null);
    return { ok: true, json: async () => ({ id: "llm/base", provider: "openrouter", zdr: true }) };
  };
  const select = vm.runInContext("selectModel", harness.context);
  await select(null);
  assert.equal(harness.state.modelOverride, null);
  assert.deepEqual(calls, ["/api/agents/agent-a/chats/chat/model", "/api/agents/agent-a/chats/chat/models"]);
  assert.equal(harness.state.preferences.default_llm_model, "llm/base");
  assert.equal(harness.elements.get("model-policy").textContent.includes("User default"), true);
  harness.context.fetch = async (url) => ({
    ok: true, json: async () => url === "/api/preferences"
      ? { ...sharedDefaults, default_llm_model: "llm/new", default_stt_model: "stt/new" }
      : { ...inheritedModels, model: { ...inheritedModels.model, id: "llm/new" } },
  });
  await harness.refreshPreferences();
  assert.equal(harness.state.model.id, "llm/new");
});

test("refreshing shared defaults does not change explicit chat overrides", () => {
  const harness = pickerHarness();
  Object.assign(harness.state, {
    user: { agents: [], provider: "openrouter" }, chatId: "chat",
    model: { id: "llm/override", zdr: false }, modelOverride: { id: "llm/override" }, modelSource: "chat",
  });
  harness.applyPreferences({ ...sharedDefaults, default_llm_model: "llm/new", default_stt_model: "stt/new" });
  assert.equal(harness.state.model.id, "llm/override");
  assert.equal(harness.state.model.zdr, false);
  assert.equal(harness.elements.get("model-policy").textContent.includes("Chat override"), true);
});

test("shared preference refresh uses authoritative Telegram overrides without reloading history", async () => {
  const harness = pickerHarness();
  Object.assign(harness.state, { user: { agents: [], id: "demo", provider: "openrouter" }, agentId: "agent-a", chatId: "chat" });
  harness.applyPreferences(sharedDefaults);
  harness.applyChatModels(inheritedModels);
  const history = { savedMessage: true };
  harness.elements.get("messages").children = [history];
  const current = {
    model: { id: "llm/telegram", provider: "openrouter", zdr: false },
    model_override: { id: "llm/telegram", provider: "openrouter", zdr: false },
  };
  const requests = [];
  harness.context.fetch = async (url) => {
    requests.push(url);
    assert.ok(["/api/preferences", "/api/agents/agent-a/chats/chat/models"].includes(url));
    return { ok: true, json: async () => url === "/api/preferences"
      ? { ...sharedDefaults, default_llm_model: "llm/new-default", default_stt_model: "stt/new-default" } : current };
  };
  await harness.refreshPreferences();
  assert.equal(harness.state.model.id, "llm/telegram");
  assert.equal(harness.state.model.zdr, false);
  assert.equal(harness.state.modelOverride.id, "llm/telegram");
  assert.equal(harness.elements.get("messages").children[0], history);
  assert.equal(requests.length, 2);
  current.model = { id: "llm/new-default", provider: "openrouter", zdr: true };
  current.model_override = null;
  await harness.refreshPreferences();
  assert.equal(harness.state.model.id, "llm/new-default");
  assert.equal(harness.state.modelOverride, null);
});

test("old chat model metadata cannot replace a different chat's selection", async () => {
  const harness = pickerHarness();
  Object.assign(harness.state, { user: { agents: [], id: "demo" }, agentId: "agent-a", chatId: "old" });
  let respond;
  harness.context.fetch = async (url) => url === "/api/preferences"
    ? { ok: true, json: async () => sharedDefaults }
    : new Promise((resolve) => { respond = resolve; });
  const pending = harness.refreshPreferences();
  await settlePicker();
  Object.assign(harness.state, { agentId: "agent-b", chatId: "new" });
  harness.applyChatModels({ ...inheritedModels, model: { id: "llm/new-chat" } });
  respond({ ok: true, json: async () => inheritedModels });
  await pending;
  assert.equal(harness.state.model.id, "llm/new-chat");
});

test("delayed metadata cannot undo a model selected locally after the read started", async () => {
  const harness = pickerHarness();
  Object.assign(harness.state, { user: { agents: [], id: "demo", provider: "openrouter" }, agentId: "agent-a", chatId: "chat" });
  let respond;
  harness.context.fetch = async (url, options) => {
    if (url === "/api/preferences") return { ok: true, json: async () => sharedDefaults };
    if (options.method === "PUT") return { ok: true, json: async () => ({ id: "llm/local", provider: "openrouter", zdr: true }) };
    if (respond) { // selectModel reads the chat's models back after its PUT
      const local = { id: "llm/local", provider: "openrouter", zdr: true };
      return { ok: true, json: async () => ({ model: local, model_override: local, model_source: "chat" }) };
    }
    return new Promise((resolve) => { respond = resolve; });
  };
  const pending = harness.refreshPreferences();
  await settlePicker();
  await vm.runInContext("selectModel", harness.context)("llm/local");
  respond({ ok: true, json: async () => inheritedModels });
  await pending;
  assert.equal(harness.state.model.id, "llm/local");
  assert.equal(harness.state.modelOverride.id, "llm/local");
});

test("old metadata cannot undo a newer preference save or leak into a reset session", async () => {
  const harness = pickerHarness();
  Object.assign(harness.state, { user: { agents: [], id: "demo" }, agentId: "agent-a", chatId: "chat" });
  let respond;
  let first = true;
  const latest = { ...inheritedModels, model: { id: "llm/latest", provider: "openrouter", zdr: true } };
  harness.context.fetch = async (url) => {
    if (url === "/api/preferences") return { ok: true, json: async () => sharedDefaults };
    if (first) {
      first = false;
      return new Promise((resolve) => { respond = resolve; });
    }
    return { ok: true, json: async () => latest };
  };
  const pending = harness.refreshPreferences();
  await settlePicker();
  await harness.savePreferences({ show_thinking: true });
  respond({ ok: true, json: async () => inheritedModels });
  await pending;
  assert.equal(harness.state.model.id, "llm/latest");
  first = true;
  const beforeReset = harness.refreshPreferences();
  await settlePicker();
  harness.reset();
  respond({ ok: true, json: async () => latest });
  await beforeReset;
  assert.equal(harness.state.modelOverride, undefined);
  assert.equal(harness.state.preferences, null);
});

test("current metadata errors propagate without substituting user defaults for an unknown chat selection", async () => {
  const harness = pickerHarness();
  Object.assign(harness.state, { user: { agents: [], id: "demo" }, agentId: "agent-a", chatId: "chat" });
  harness.applyChatModels(inheritedModels);
  harness.context.fetch = async (url) => url === "/api/preferences"
    ? { ok: true, json: async () => sharedDefaults }
    : { ok: false, status: 503, json: async () => ({ detail: "Metadata offline" }) };
  await assert.rejects(harness.refreshPreferences(), /Metadata offline/);
});

test("session reset discards default drafts and ignores an in-flight STT catalog", async () => {
  const harness = pickerHarness();
  Object.assign(harness.state, {
    user: { agents: [], provider: "openrouter" }, modelTarget: "default-stt",
    settingsOriginal: sharedDefaults, settingsModelDraft: { default_stt_model: "stt/unsaved" },
    modelOverride: null,
  });
  let respond;
  harness.context.fetch = async (url) => {
    const body = pickerApi(url);
    if (!url.startsWith("/api/models/catalog")) return { ok: true, json: async () => body };
    return new Promise((resolve) => { respond = resolve; }); // the catalog stays in flight
  };
  const loading = harness.refresh(true);
  await settlePicker();
  harness.reset();
  respond({ ok: true, json: async () => ({ models: [{ id: "stt/stale", name: "Stale", pricing_prompt: "1" }] }) });
  await loading;
  assert.equal(harness.state.modelCatalog, null);
  assert.equal(harness.state.settingsModelDraft, null);
  assert.equal(harness.state.modelTarget, "chat-llm");
  assert.equal(harness.state.modelOverride, undefined);
});

test("shared thinking visibility applies to the whole message history", () => {
  const harness = pickerHarness();
  harness.applyPreferences({ show_thinking: false, language: "es", agent_id: "agent-a" });
  assert.equal(harness.elements.get("messages").classList.contains("hide-thinking"), true);
  harness.applyPreferences({ show_thinking: true, language: "es", agent_id: "agent-a" });
  assert.equal(harness.elements.get("messages").classList.contains("hide-thinking"), false);
});

test("refreshing shared settings preserves the open agent and chat", async () => {
  const harness = pickerHarness();
  Object.assign(harness.state, { user: { agents: [], id: "demo" }, agentId: "agent-a", chatId: "existing" });
  harness.context.fetch = async (url, options) => {
    assert.equal(options.cache, "no-store");
    assert.ok(["/api/preferences", "/api/agents/agent-a/chats/existing/models"].includes(url));
    return { ok: true, json: async () => url === "/api/preferences"
      ? { ...sharedDefaults, show_thinking: false, language: "es", agent_id: "agent-b" } : inheritedModels };
  };
  await harness.refreshPreferences();
  assert.equal(harness.state.preferences.agent_id, "agent-b");
  assert.equal(harness.state.agentId, "agent-a");
  assert.equal(harness.state.chatId, "existing");
});

test("an older preferences response cannot undo a completed settings save", async () => {
  const harness = pickerHarness();
  harness.state.user = { id: "demo", agents: [] };
  let resolveRead;
  harness.context.fetch = (url, options) => options.method === "PATCH"
    ? Promise.resolve({
      ok: true, json: async () => ({ show_thinking: false, language: "en", agent_id: "agent-a" }),
    })
    : new Promise((resolve) => { resolveRead = resolve; });
  const pending = harness.refreshPreferences();
  await harness.savePreferences({ show_thinking: false });
  resolveRead({ ok: true, json: async () => ({ show_thinking: true, language: "en", agent_id: "agent-a" }) });
  await pending;
  assert.equal(harness.state.preferences.show_thinking, false);
  assert.equal(harness.state.settingsBusy, false);
});

test("the settings form patches only fields edited since opening", async () => {
  const harness = pickerHarness();
  harness.state.user = { id: "demo", agents: [{ id: "agent-a", name: "First" }] };
  harness.state.preferences = { show_thinking: true, language: "es", agent_id: "agent-a" };
  harness.state.settingsOriginal = { show_thinking: true, show_agent_avatar: true, language: "en", agent_id: "agent-a" };
  // the form reads the staged default models from the draft (they are not form inputs)
  harness.state.settingsModelDraft = { default_llm_model: undefined, default_stt_model: undefined };
  harness.elements.get("settings-thinking").checked = false;
  harness.elements.get("settings-agent-avatar").checked = true;
  harness.elements.get("settings-language").value = "en";
  let saved;
  harness.context.fetch = async (url, options) => {
    saved = JSON.parse(options.body);
    return { ok: true, json: async () => ({ show_thinking: false, language: "es", agent_id: "agent-a" }) };
  };
  await harness.elements.get("settings-form").listeners.submit({ preventDefault() {} });
  assert.deepEqual(saved, { show_thinking: false });
  assert.equal(harness.elements.has("settings-agent"), false);
  assert.equal(harness.state.preferences.language, "es");
  assert.equal(harness.elements.get("settings-dialog").open, false);
});

test("settings failures retain prior preferences and expose an error", async () => {
  const harness = pickerHarness();
  harness.state.user = { id: "demo", agents: [] };
  harness.state.preferences = { show_thinking: true, language: "en", agent_id: "agent-a" };
  harness.state.preferences.show_agent_avatar = true;
  harness.state.settingsOriginal = { ...harness.state.preferences };
  harness.state.settingsModelDraft = { default_llm_model: undefined, default_stt_model: undefined };
  harness.elements.get("settings-thinking").checked = false;
  harness.elements.get("settings-agent-avatar").checked = true;
  harness.elements.get("settings-language").value = "en";
  harness.context.fetch = async () => ({ ok: false, status: 500, json: async () => ({ detail: "Cannot save" }) });
  await harness.elements.get("settings-form").listeners.submit({ preventDefault() {} });
  assert.equal(harness.elements.get("settings-error").textContent, "Cannot save");
  assert.equal(harness.state.preferences.show_thinking, true);
  assert.equal(harness.state.settingsBusy, false);
});

test("new chats use the agent selected by the other channel", async () => {
  const harness = pickerHarness();
  Object.assign(harness.state, {
    user: { agents: [], id: "demo", provider: "ollama" }, agentId: "agent-a", chatId: "existing",
  });
  const requests = [];
  harness.context.fetch = async (url, options) => {
    requests.push([url, options.method]);
    let data;
    if (url === "/api/preferences") data = { show_thinking: false, language: "en", agent_id: "agent-b" };
    else if (options.method === "POST") data = { id: "new-chat" };
    else if (url.endsWith("/models")) data = inheritedModels;
    else if (url.endsWith("/new-chat")) data = { messages: [], ...inheritedModels, model: { id: "local", zdr: true } };
    else data = [];
    return { ok: true, json: async () => data };
  };
  await harness.newChat();
  assert.equal(harness.state.agentId, "agent-b");
  assert.equal(harness.state.chatId, "new-chat");
  assert.equal(requests.some(([url, method]) => url === "/api/agents/agent-b/chats" && method === "POST"), true);
});

test("saved language localizes settings and errors without translating interpolated content", () => {
  const harness = pickerHarness();
  Object.assign(harness.state.translations, {
    Settings: "Ajustes",
    "Show model thinking": "Mostrar el thinking del modelo",
    "{name}: maximum size is 10 MB": "{name}: el tamaño máximo es de 10 MB.",
  });
  harness.applyPreferences({ show_thinking: true, language: "es", agent_id: "agent-a" });
  // settings-open became an icon button: its label lives in title and aria-label, not textContent
  assert.equal(harness.elements.get("settings-open").title, "Ajustes");
  assert.equal(harness.elements.get("settings-open").attributes["aria-label"], "Ajustes");
  assert.equal(harness.elements.get("settings-thinking-label").textContent, "Mostrar el thinking del modelo");
  const tr = vm.runInContext("tr", harness.context);
  assert.equal(tr("{name}: maximum size is 10 MB", { name: "Original {raw}.png" }),
    "Original {raw}.png: el tamaño máximo es de 10 MB.");
  harness.applyPreferences({ show_thinking: true, language: "en", agent_id: "agent-a" });
  assert.equal(harness.elements.get("settings-open").title, "Settings");
  assert.equal(harness.elements.get("settings-open").attributes["aria-label"], "Settings");
});

test("API requests carry the current alert language", async () => {
  const harness = pickerHarness();
  harness.applyPreferences({ show_thinking: true, language: "es", agent_id: "agent-a" });
  let language;
  harness.context.fetch = async (url, options) => {
    language = options.headers["Accept-Language"];
    return { ok: true, json: async () => [] };
  };
  await vm.runInContext("api", harness.context)("/api/models");
  assert.equal(language, "es");
});
const models = [
  { id: "vendor/z", name: "Zulu", pricing: { prompt: "2", completion: "0" } },
  { id: "vendor/a", name: "Alpha", pricing: { prompt: "10", completion: "0.5" } },
  { id: "vendor/free", name: "Free", pricing: { prompt: "0", completion: "0" } },
  { id: "vendor/unknown", name: "Unknown", pricing: { prompt: null, completion: null } },
  { id: "vendor/retired", name: "Retired" },
];
const favorites = new Set(["vendor/a"]);
const sortedIds = (order, input = models) =>
  [...input].sort((a, b) => compareModels(a, b, order, favorites)).map((model) => model.id);

test("input prices sort numerically with unknowns last in both directions", () => {
  assert.deepEqual(sortedIds("prompt-asc"), [
    "vendor/free", "vendor/z", "vendor/a", "vendor/retired", "vendor/unknown",
  ]);
  assert.deepEqual(sortedIds("prompt-desc"), [
    "vendor/a", "vendor/z", "vendor/free", "vendor/retired", "vendor/unknown",
  ]);
});

test("output ordering uses output prices and preserves valid zero prices", () => {
  assert.deepEqual(sortedIds("completion-asc"), [
    "vendor/free", "vendor/z", "vendor/a", "vendor/retired", "vendor/unknown",
  ]);
  assert.deepEqual(sortedIds("completion-desc"), [
    "vendor/a", "vendor/free", "vendor/z", "vendor/retired", "vendor/unknown",
  ]);
});

test("favorites are pinned only in favorites-first mode", () => {
  assert.deepEqual(sortedIds("favorites"), [
    "vendor/a", "vendor/free", "vendor/retired", "vendor/unknown", "vendor/z",
  ]);
  const input = [
    { id: "vendor/z", name: "Zulu" },
    { id: "vendor/a", name: "Alpha" },
  ];
  assert.deepEqual(sortedIds("name", input), ["vendor/a", "vendor/z"]);
  const saved = new Set(["vendor/z"]);
  assert.equal(compareModels(input[0], input[1], "name", saved) > 0, true);
});

test("price ties break by name then ID", () => {
  const input = [
    { id: "vendor/b", name: "Same", pricing: { prompt: "0.1" } },
    { id: "vendor/a", name: "Same", pricing: { prompt: "0.1" } },
    { id: "vendor/c", name: "Earlier", pricing: { prompt: "0.1" } },
  ];
  assert.deepEqual(sortedIds("prompt-desc", input), ["vendor/c", "vendor/a", "vendor/b"]);
});

test("ordering uses unrounded prices and does not mutate the source catalog", () => {
  const input = [
    { id: "vendor/a", name: "Alpha", pricing: { prompt: "0.12345679" } },
    { id: "vendor/z", name: "Zulu", pricing: { prompt: "0.12345678" } },
  ];
  assert.deepEqual(sortedIds("prompt-asc", input), ["vendor/z", "vendor/a"]);
  assert.deepEqual(input.map((model) => model.id), ["vendor/a", "vendor/z"]);
  assert.deepEqual(sortedIds("prompt-asc", []), []);
});

test("capability filters leave all models visible when disabled", () => {
  assert.equal(matchesCapabilities({}, {}), true);
});

test("tools and reasoning must be supported by the same route", () => {
  const split = { parameter_routes: [["tools"], ["reasoning"]] };
  assert.equal(matchesCapabilities(split, { tools: true }), true);
  assert.equal(matchesCapabilities(split, { reasoning: true }), true);
  assert.equal(matchesCapabilities(split, { tools: true, reasoning: true }), false);
  const combined = { parameter_routes: [["tools", "reasoning_effort"]] };
  assert.equal(matchesCapabilities(combined, { tools: true, reasoning: true }), true);
});

test("image input composes with parameter filters and excludes unknown capabilities", () => {
  const vision = { input_modalities: ["text", "image"], parameter_routes: [["tools", "reasoning"]] };
  assert.equal(matchesCapabilities(vision, { tools: true, images: true, reasoning: true }), true);
  assert.equal(matchesCapabilities({ ...vision, input_modalities: ["text"] }, { images: true }), false);
  for (const filter of ["tools", "images", "reasoning"]) {
    assert.equal(matchesCapabilities({}, { [filter]: true }), false);
  }
  assert.equal(matchesCapabilities({ parameter_routes: [["include_reasoning"]] }, { reasoning: true }), false);
});

test("capability filters compose with price sorting without pinning favorites", () => {
  const input = [
    { ...models[0], parameter_routes: [["tools"]] },
    { ...models[1], parameter_routes: [["tools"]] },
    { ...models[2], parameter_routes: [] },
  ];
  assert.deepEqual(sortedIds("prompt-asc", input.filter((model) =>
    matchesCapabilities(model, { tools: true })
  )), ["vendor/z", "vendor/a"]);
});

test("favorite star toggles filtering and exposes its pressed state", () => {
  const { elements, state, render } = pickerHarness();
  // favorites are per provider now: an ID without a prefix belongs to the user's main provider
  state.user = { provider: "openrouter", agents: [] };
  state.modelCatalog = models.map((model) => ({ ...model, zdr_providers: [] }));
  state.modelCatalogZdr = true;
  state.modelFavorites = ["vendor/a"];
  render();
  const star = elements.get("model-favorites-only");
  assert.equal(star.attributes["aria-pressed"], "false");
  assert.equal(elements.get("model-list").children.length, 5);
  star.listeners.click();
  assert.equal(star.attributes["aria-pressed"], "true");
  // the star is now an SVG icon (index.html:1044); only pressed state and title change
  assert.equal(star.title, "Show all models");
  assert.equal(elements.get("model-list").children.length, 1);
  star.listeners.click();
  assert.equal(star.attributes["aria-pressed"], "false");
  assert.equal(star.title, "Show favorites only");
  assert.equal(elements.get("model-list").children.length, 5);
});

test("collapsed filters count active capabilities and still filter results", () => {
  const { elements, state, render } = pickerHarness();
  state.modelCatalog = [
    { ...models[0], zdr_providers: [], input_modalities: ["image"], parameter_routes: [["tools", "reasoning"]] },
    { ...models[1], zdr_providers: [], input_modalities: ["text"], parameter_routes: [] },
  ];
  state.modelCatalogZdr = true;
  render();
  assert.equal(elements.get("model-filters-label").textContent, "Filters");
  assert.equal(elements.get("model-filters").dataset.active, "false");
  for (const [index, id] of ["model-tools", "model-images", "model-reasoning"].entries()) {
    elements.get(id).checked = true;
    elements.get(id).listeners.change();
    assert.equal(elements.get("model-filters-label").textContent, `Filters (${index + 1})`);
  }
  assert.equal(elements.get("model-filters").open, false);
  assert.equal(elements.get("model-filters").dataset.active, "true");
  assert.equal(elements.get("model-list").children.length, 1);
  elements.get("model-search").value = "Alpha";
  elements.get("model-search").listeners.input();
  assert.equal(elements.get("model-list").children.length, 0);
});

test("opening closes disclosures, retains capabilities and restores safe defaults", () => {
  const { elements, state, updateControls } = pickerHarness();
  // opening resets the ZDR checkbox to the user's require_zdr setting (app.js openModelPicker)
  state.user = { provider: "openrouter", require_zdr: true, agents: [] };
  state.chatId = "chat";
  state.modelCatalog = [];
  state.modelCatalogZdr = true;
  state.modelFavorites = ["vendor/a"];
  elements.get("model-zdr").checked = false;
  elements.get("model-tools").checked = true;
  elements.get("model-filters").open = true;
  elements.get("model-info").open = true;
  elements.get("model-search").value = "old query";
  elements.get("model-sort").value = "prompt-desc";
  updateControls();
  elements.get("model-picker").listeners.click();
  assert.equal(elements.get("model-dialog").open, true);
  assert.equal(elements.get("model-filters").open, false);
  assert.equal(elements.get("model-info").open, false);
  assert.equal(elements.get("model-tools").checked, true);
  assert.equal(elements.get("model-filters-label").textContent, "Filters (1)");
  assert.equal(elements.get("model-search").value, "");
  assert.equal(elements.get("model-search").focused, true);
  assert.equal(elements.get("model-sort").value, "prompt-desc");
  assert.equal(elements.get("model-zdr").checked, true);
  assert.equal(elements.get("model-zdr-warning").hidden, true);
  // since 096e36a the favorites filter is off whenever the selector opens (favorites still sort first)
  assert.equal(elements.get("model-favorites-only").attributes["aria-pressed"], "false");
});

test("non-ZDR warning remains visible independently of collapsed information", () => {
  const { elements, state, updateControls } = pickerHarness();
  // the warning only shows for OpenRouter/Any when the user's account requires ZDR
  state.user = { provider: "openrouter", require_zdr: true, agents: [] };
  elements.get("model-zdr").checked = false;
  updateControls();
  assert.equal(elements.get("model-info").open, false);
  assert.equal(elements.get("model-zdr-warning").hidden, false);
});

test("session reset clears favorite filter and capability choices", () => {
  const { elements, state, reset } = pickerHarness();
  state.modelFavorites = ["vendor/a"];
  state.modelFavoritesOnly = true;
  elements.get("model-sort").value = "prompt-asc";
  for (const id of ["model-tools", "model-images", "model-reasoning"]) elements.get(id).checked = true;
  elements.get("model-dialog").open = true;
  reset();
  assert.equal(elements.get("model-dialog").open, false);
  assert.equal(state.modelFavoritesOnly, false);
  assert.equal(state.modelFavorites.length, 0);
  assert.equal(elements.get("model-favorites-only").attributes["aria-pressed"], "false");
  assert.equal(elements.get("model-filters-label").textContent, "Filters");
  assert.equal(elements.get("model-sort").value, "favorites");
  for (const id of ["model-tools", "model-images", "model-reasoning"]) assert.equal(elements.get(id).checked, false);
});

test("initial catalog loading never activates the favorites filter and sorts saved IDs first", async () => {
  for (const saved of [[], ["vendor/a"]]) {
    const { elements, context, state, refresh } = pickerHarness();
    state.user = { provider: "openrouter", agents: [] };
    context.fetch = async (url) => {
      const body = pickerApi(url, {
        chat: models.map((model) => ({ ...model, zdr_providers: [] })), favorites: saved,
      });
      assert.notEqual(body, null, url);
      return { ok: true, status: 200, json: async () => body };
    };
    await refresh(true);
    assert.equal(state.modelLoading, false);
    // 096e36a: the filter stays off when the selector opens, even with saved favorites
    assert.equal(elements.get("model-favorites-only").attributes["aria-pressed"], "false");
    const rows = elements.get("model-list").children;
    assert.equal(rows.length, 5);
    if (saved.length) assert.equal(rows[0].children[0].children[1].textContent, "vendor/a");
  }
});

test("native dialog cancellation is blocked only while saving a selection", () => {
  const { elements, state } = pickerHarness();
  for (const busy of [false, true]) {
    state.modelBusy = busy;
    let prevented = false;
    elements.get("model-dialog").listeners.cancel({
      preventDefault() { prevented = true; },
    });
    assert.equal(prevented, busy);
  }
});
