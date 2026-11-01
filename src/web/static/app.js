const $ = (id) => document.getElementById(id);
const state = {
  user: null, agentId: null, chatId: null, busy: false, session: 0,
  requests: new Set(), latest: null, sendQueue: Promise.resolve(),
  // message containers of chats with a reply in flight, so they keep updating while another chat is open
  view: null, views: new Map(), usages: new Map(),
  attachments: [],
  model: null, modelCatalog: null, modelCatalogZdr: null, modelFavorites: [],
  modelBusy: false, modelLoading: false, modelRequest: 0,
  modelFavoritesOnly: false,
  modelTarget: "chat-llm", modelCatalogKind: "llm",
  modelOverride: undefined,
  reasoningEffort: null,
  chatModelRevision: 0,
  settingsModelDraft: null,
  preferences: null, preferencesRequest: 0, settingsBusy: false, settingsOriginal: null,
  translations: null,
  agentConfigs: [], agentForm: { originalId: null, busy: false },
};
// thinking blocks start open (streamed live); closing one keeps them closed from then on
const prefs = { thinkingOpen: localStorage.getItem("thinkingOpen") !== "false" };
const tokenPriceFormat = new Intl.NumberFormat("en-US", {
  style: "currency", currency: "USD", maximumSignificantDigits: 6,
});

function tr(template, values = {}) {
  const text = state.preferences?.language === "es" ? state.translations[template] : template;
  if (typeof text !== "string") throw new Error(`Missing translation: ${template}`);
  return text.replace(/\{(\w+)\}/g, (_, key) => values[key]);
}

function translateInterface() {
  for (const [id, text] of [
    ["settings-open", "Settings"], ["settings-title", "User settings"],
    ["settings-close", "Close"], ["settings-save", "Save settings"],
    ["settings-thinking-label", "Show model thinking"],
    ["settings-language-label", "Alerts and errors"],
    ["settings-note", "Settings are shared with Telegram. Chats without a selected model use your current defaults."],
    ["settings-llm-label", "Default LLM model"], ["settings-stt-label", "Default STT model"],
    ["agent-label", "Agent"], ["chat-model-label", "Chat model"], ["chat-effort-text", "Reasoning effort"],
    ["model-inherit", "Use user default"],
    ["model-scope-note", "Choosing a model for a chat sets only that chat's override. Use user default removes it. Defaults in Settings are saved with Save settings. Default LLM and STT selections require ZDR. Favorites are shared across your agents."],
    ["model-zdr-warning", "ZDR is not required for your next selection. Providers may retain your prompts and replies. Your OpenRouter account may still enforce ZDR."],
    ["tab-chats", "Chats"], ["tab-agents", "Agents"], ["agents-title", "Agents"],
    ["agent-new", "+ New agent"], ["agents-empty", "Select an agent to edit it, or create a new one."],
    ["agent-name-label", "Name"],
    ["agent-prompt-label", "System prompt"], ["agent-behavior-label", "Chat behavior"],
    ["agent-stream-label", "Stream replies"], ["agent-memory-label", "Use memory"],
    ["agent-reasoning-label", "Reasoning"], ["agent-keep-reasoning-label", "Keep reasoning in memory"],
    ["agent-tools-label", "Use tools"], ["agent-associative-label", "Associative memory"],
    ["agent-effort-label", "Reasoning effort"], ["agent-effort-default", "Default"],
    ["agent-tokens-enabled-label", "Limit reasoning tokens"], ["agent-reasoning-tokens-label", "Reasoning tokens"],
    ["agent-retained-label", "Messages kept in context"],
    ["agent-retained-hint", "Empty keeps all messages."], ["agent-save", "Save agent"], ["agent-delete", "Delete agent"],
  ]) $(id).textContent = tr(text);
}

function tokenPrice(value) {
  return value == null ? "unavailable" : `from ${tokenPriceFormat.format(value)}`;
}

async function api(path, options = {}) {
  const session = state.session;
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json", "Accept-Language": state.preferences?.language ?? "en" },
    ...options,
  });
  if (res.status === 401) {
    const error = tr("not logged in");
    if (session === state.session) showLogin();
    throw new Error(error);
  }
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(formatApiError(body.detail) || tr("HTTP {status}", { status: res.status }));
  }
  return res.status === 204 ? null : res.json();
}

function formatApiError(detail) {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail.map((item) => {
      if (typeof item === "string") return item;
      const location = Array.isArray(item.loc) ? item.loc.join(".") : "";
      return location ? `${location}: ${item.msg || JSON.stringify(item)}` : (item.msg || JSON.stringify(item));
    }).join("; ");
  }
  return detail ? JSON.stringify(detail) : "";
}

const chatsUrl = (suffix = "") => `/api/agents/${state.agentId}/chats${suffix}`;

// ------------------------------------------------------------------ login

function showLogin() {
  resetStreams();
  state.user = null;
  state.agentId = null;
  state.chatId = null;
  state.model = null;
  state.attachments = [];
  renderPendingAttachments();
  updateModelControls();
  renderMessages([]);
  setComposer(false);
  state.agentConfigs = [];
  state.agentForm = { originalId: null, busy: false };
  showTab("chats");
  $("app").hidden = true;
  $("login").hidden = false;
}

function renderAgentOptions() {
  $("agent-select").replaceChildren(
    ...state.user.agents.map((a) => {
      const option = document.createElement("option");
      option.value = a.id;
      option.textContent = a.name;
      return option;
    })
  );
  $("agent-select").value = state.agentId ?? "";
}

async function showApp(user) {
  resetStreams();
  state.user = user;
  const session = state.session;
  $("login").hidden = true;
  $("app").hidden = false;
  $("user-name").textContent = user.name;
  renderAgentOptions();
  showTab("chats");
  try {
    await refreshPreferences();
    if (session !== state.session) return;
    await selectAgent(state.preferences.agent_id);
    if (session === state.session && state.agentId) await newChat();
  } catch (err) {
    if (session === state.session) addMessage("assistant").error(err.message);
  }
}

$("login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("login-error").textContent = "";
  try {
    const user = await api("/api/login", {
      method: "POST",
      body: JSON.stringify({ user_id: $("login-user").value }),
    });
    showApp(user);
  } catch (err) {
    $("login-error").textContent = err.message;
  }
});

$("logout").addEventListener("click", async () => {
  await api("/api/logout", { method: "POST" });
  showLogin();
});

// ------------------------------------------------------------------ agents & chats

function chatKey(agentId, chatId) {
  return `${agentId}/${chatId}`;
}

function isGenerating(agentId, chatId) {
  for (const request of state.requests) {
    if (request.agentId === agentId && request.chatId === chatId) return true;
  }
  return false;
}

async function selectAgent(agentId) {
  if (state.modelBusy) return;
  state.agentId = agentId;
  state.chatId = null;
  state.model = null;
  updateModelControls();
  $("agent-select").value = agentId ?? "";
  renderMessages([]);
  setComposer(false);
  await refreshChats();
}

$("agent-select").addEventListener("change", async (e) => {
  const session = state.session;
  const agentId = e.target.value;
  if (state.modelBusy || state.settingsBusy) {
    $("agent-select").value = state.agentId ?? "";
    return;
  }
  try {
    await savePreferences({ agent_id: agentId });
    if (session !== state.session) return;
    await selectAgent(agentId);
    await newChat();
  } catch (err) {
    if (session === state.session) {
      $("agent-select").value = state.agentId ?? "";
      addMessage("assistant").error(err.message);
    }
  }
});

// Opening a chat from another agent makes that agent the active one, as the agent selector does.
async function openChatFromList(chat) {
  if (state.modelBusy || state.settingsBusy) return;
  const session = state.session;
  if (chat.agent_id !== state.agentId) {
    await savePreferences({ agent_id: chat.agent_id });
    if (session !== state.session) return;
    await selectAgent(chat.agent_id);
  }
  await openChat(chat.id);
}

// ------------------------------------------------------------------ agents tab

function showTab(tab) {
  const agents = tab === "agents";
  $("tab-chats").setAttribute("aria-pressed", String(!agents));
  $("tab-agents").setAttribute("aria-pressed", String(agents));
  $("chat-view").hidden = agents;
  $("agents-view").hidden = !agents;
  $("chat-list").hidden = agents;
  $("new-chat").hidden = agents;
  if (agents) loadAgentConfigs();
}

async function loadAgentConfigs() {
  const session = state.session;
  try {
    const agents = await api("/api/agents");
    if (session !== state.session) return;
    state.agentConfigs = agents;
    renderAgentList();
  } catch (err) {
    if (session === state.session) $("agent-error").textContent = err.message;
  }
}

function renderAgentList() {
  $("agent-list").replaceChildren(
    ...state.agentConfigs.map((agent) => {
      const item = document.createElement("li");
      const button = document.createElement("button");
      button.type = "button";
      button.className = agent.id === state.agentForm.originalId ? "active" : "";
      button.textContent = agent.name;
      button.addEventListener("click", () => openAgent(agent.id));
      item.append(button);
      return item;
    })
  );
}

function openAgent(agentId) {
  if (state.agentForm.busy) return;
  const agent = state.agentConfigs.find((item) => item.id === agentId);
  if (!agent) return;
  state.agentForm.originalId = agent.id;
  $("agent-error").textContent = "";
  $("agent-status").textContent = "";
  fillAgentForm(agent);
  $("agent-delete").hidden = false;
  setAgentFormOpen(true);
}

function openNewAgent() {
  if (state.agentForm.busy) return;
  state.agentForm.originalId = null;
  $("agent-error").textContent = "";
  $("agent-status").textContent = "";
  fillAgentForm({
    name: "", id: "", personality_prompt: "",
    chat_config: {
      stream: true, has_memory: true, reasoning: false, reasoning_effort: "medium", reasoning_tokens: null,
      keep_reasoning_in_memory: false, conversation_messages_retained: 30, tools: true, associative_memory: true,
    },
  });
  $("agent-delete").hidden = true;
  setAgentFormOpen(true);
}

function setAgentFormOpen(open) {
  $("agent-form").hidden = !open;
  $("agents-empty").hidden = open;
  renderAgentList();
}

function fillAgentForm(agent) {
  const config = agent.chat_config;
  $("agent-form-title").textContent = tr(state.agentForm.originalId ? "Edit agent" : "New agent");
  $("agent-name").value = agent.name;
  $("agent-prompt").value = agent.personality_prompt;
  $("agent-stream").checked = config.stream;
  $("agent-memory").checked = config.has_memory;
  $("agent-reasoning").checked = config.reasoning;
  $("agent-keep-reasoning").checked = config.keep_reasoning_in_memory;
  $("agent-tools").checked = config.tools;
  $("agent-associative").checked = config.associative_memory;
  $("agent-effort").value = config.reasoning_effort ?? "";
  $("agent-tokens-enabled").checked = config.reasoning_tokens != null;
  $("agent-reasoning-tokens").disabled = config.reasoning_tokens == null;
  $("agent-reasoning-tokens").value = config.reasoning_tokens ?? "";
  $("agent-retained").value = config.conversation_messages_retained ?? "";
}

// The id is derived from the name once, when the agent is created; it never changes from the form.
function agentIdFromName(name) {
  const slug = name.normalize("NFKD").replace(/[\u0300-\u036f]/g, "").toLowerCase()
    .replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 64);
  return slug || `agent-${Date.now().toString(36)}`;
}

function agentFormBody() {
  const optionalNumber = (id) => {
    const value = $(id).value.trim();
    return value === "" ? null : Number(value);
  };
  const name = $("agent-name").value.trim();
  return {
    id: state.agentForm.originalId ?? agentIdFromName(name),
    name,
    personality_prompt: $("agent-prompt").value,
    chat_config: {
      stream: $("agent-stream").checked,
      has_memory: $("agent-memory").checked,
      reasoning: $("agent-reasoning").checked,
      reasoning_effort: $("agent-effort").value || null,
      reasoning_tokens: $("agent-tokens-enabled").checked ? optionalNumber("agent-reasoning-tokens") : null,
      keep_reasoning_in_memory: $("agent-keep-reasoning").checked,
      conversation_messages_retained: optionalNumber("agent-retained"),
      tools: $("agent-tools").checked,
      associative_memory: $("agent-associative").checked,
    },
  };
}

function setAgentBusy(busy) {
  state.agentForm.busy = busy;
  for (const id of ["agent-new", "agent-save", "agent-delete"]) $(id).disabled = busy;
}

// Reloads the agent list and keeps the sidebar consistent after a save or delete.
async function refreshAgents(previousId, currentId) {
  const me = await api("/api/me");
  state.user.agents = me.agents;
  renderAgentOptions();
  await loadAgentConfigs();
  if (previousId && previousId === state.agentId && previousId !== currentId) {
    // the server moved the preference when the agent was renamed or deleted
    const preferences = await refreshPreferences();
    await selectAgent(preferences.agent_id ?? state.user.agents[0]?.id ?? null);
  }
}

$("tab-chats").addEventListener("click", () => showTab("chats"));
$("tab-agents").addEventListener("click", () => showTab("agents"));
$("agent-new").addEventListener("click", openNewAgent);

$("agent-tokens-enabled").addEventListener("change", () => {
  const enabled = $("agent-tokens-enabled").checked;
  $("agent-reasoning-tokens").disabled = !enabled;
  if (enabled && !$("agent-reasoning-tokens").value) $("agent-reasoning-tokens").value = "1024";
});

$("agent-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (state.agentForm.busy) return;
  const session = state.session;
  const previousId = state.agentForm.originalId;
  const body = JSON.stringify(agentFormBody());
  setAgentBusy(true);
  $("agent-error").textContent = "";
  $("agent-status").textContent = "";
  try {
    const saved = await api(previousId ? `/api/agents/${previousId}` : "/api/agents", {
      method: previousId ? "PUT" : "POST", body,
    });
    if (session !== state.session) return;
    state.agentForm.originalId = saved.id;
    $("agent-delete").hidden = false;
    fillAgentForm(saved);
    await refreshAgents(previousId, saved.id);
    $("agent-status").textContent = tr("Agent saved.");
  } catch (err) {
    if (session === state.session) $("agent-error").textContent = err.message;
  } finally {
    if (session === state.session) setAgentBusy(false);
  }
});

$("agent-delete").addEventListener("click", async () => {
  const agentId = state.agentForm.originalId;
  if (!agentId || state.agentForm.busy) return;
  if (!window.confirm(tr("Delete this agent? Its conversations and memory data are kept on disk."))) return;
  const session = state.session;
  setAgentBusy(true);
  $("agent-error").textContent = "";
  try {
    await api(`/api/agents/${agentId}`, { method: "DELETE" });
    if (session !== state.session) return;
    state.agentForm.originalId = null;
    setAgentFormOpen(false);
    await refreshAgents(agentId, null);
    $("agent-status").textContent = tr("Agent deleted.");
  } catch (err) {
    if (session === state.session) $("agent-error").textContent = err.message;
  } finally {
    if (session === state.session) setAgentBusy(false);
  }
});

// ------------------------------------------------------------------ shared settings

function applyPreferences(preferences) {
  state.preferences = preferences;
  $("messages").classList.toggle("hide-thinking", !preferences.show_thinking);
  translateInterface();
  updateModelControls();
}

async function applyServerPreferences(preferences, session, request) {
  if (preferences.language === "es" && !state.translations) {
    state.translations = await api("/api/localization?language=es");
  }
  if (session === state.session && request === state.preferencesRequest && state.user) {
    applyPreferences(preferences);
    await refreshChatModels(session, request);
  }
}

function applyChatModels(models) {
  state.model = models.model;
  state.modelOverride = models.model_override;
  state.reasoningEffort = models.reasoning_effort;
  updateModelControls();
}

async function refreshChatModels(session, preferencesRequest) {
  if (!state.agentId || !state.chatId || state.modelBusy) return;
  const agentId = state.agentId;
  const chatId = state.chatId;
  const revision = state.chatModelRevision;
  const current = () => session === state.session && preferencesRequest === state.preferencesRequest
    && agentId === state.agentId && chatId === state.chatId
    && revision === state.chatModelRevision && !state.modelBusy;
  try {
    const models = await api(chatsUrl(`/${chatId}/models`), { cache: "no-store" });
    if (current()) applyChatModels(models);
  } catch (err) {
    if (current()) throw err;
  }
}

async function refreshPreferences() {
  if (state.settingsBusy && state.preferences) return state.preferences;
  const session = state.session;
  const request = ++state.preferencesRequest;
  const preferences = await api("/api/preferences", { cache: "no-store" });
  await applyServerPreferences(preferences, session, request);
  return preferences;
}

async function savePreferences(changes) {
  const session = state.session;
  const request = ++state.preferencesRequest;
  state.settingsBusy = true;
  updateBusy();
  $("settings-save").disabled = true;
  try {
    const preferences = await api("/api/preferences", {
      method: "PATCH", body: JSON.stringify(changes),
    });
    await applyServerPreferences(preferences, session, request);
    return preferences;
  } finally {
    if (session === state.session) {
      state.settingsBusy = false;
      $("settings-save").disabled = false;
      updateBusy();
    }
  }
}

function fillSettings() {
  state.settingsOriginal = { ...state.preferences };
  state.settingsModelDraft = {
    default_llm_model: state.preferences.default_llm_model,
    default_stt_model: state.preferences.default_stt_model,
  };
  $("settings-thinking").checked = state.preferences.show_thinking;
  $("settings-language").value = state.preferences.language;
  renderSettingsModels();
}

function renderSettingsModels() {
  $("settings-llm").textContent = state.settingsModelDraft?.default_llm_model ?? "";
  $("settings-stt").textContent = state.settingsModelDraft?.default_stt_model ?? "";
  const disabled = !state.settingsOriginal || state.user?.provider !== "openrouter" || state.settingsBusy;
  $("settings-llm").disabled = disabled;
  $("settings-stt").disabled = disabled;
}

$("settings-open").addEventListener("click", async () => {
  const session = state.session;
  state.settingsOriginal = null;
  state.settingsModelDraft = null;
  renderSettingsModels();
  $("settings-error").textContent = "";
  $("settings-save").disabled = true;
  $("settings-dialog").showModal();
  try {
    await refreshPreferences();
    if (session !== state.session) return;
    fillSettings();
    $("settings-save").disabled = state.settingsBusy;
  } catch (err) {
    if (session === state.session) $("settings-error").textContent = err.message;
  }
});

$("settings-close").addEventListener("click", () => $("settings-dialog").close());
$("settings-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (state.settingsBusy || !state.settingsOriginal) return;
  const session = state.session;
  const changes = {};
  const thinking = $("settings-thinking").checked;
  const language = $("settings-language").value;
  if (thinking !== state.settingsOriginal.show_thinking) changes.show_thinking = thinking;
  if (language !== state.settingsOriginal.language) changes.language = language;
  for (const key of ["default_llm_model", "default_stt_model"]) {
    if (state.settingsModelDraft?.[key] !== state.settingsOriginal[key]) changes[key] = state.settingsModelDraft[key];
  }
  $("settings-error").textContent = "";
  try {
    if (Object.keys(changes).length) await savePreferences(changes);
    if (session === state.session) $("settings-dialog").close();
  } catch (err) {
    if (session === state.session) $("settings-error").textContent = err.message;
  }
});

globalThis.addEventListener("focus", async () => {
  if (!state.user || state.settingsBusy || $("settings-dialog").open) return;
  const session = state.session;
  try {
    await refreshPreferences();
  } catch (err) {
    if (session === state.session) addMessage("assistant").error(err.message);
  }
});

async function refreshChats() {
  if (!state.user) return;
  const session = state.session;
  const keep = state.chatId ? `?keep=${encodeURIComponent(state.chatId)}` : "";
  const chats = await api(`/api/chats${keep}`);
  if (session !== state.session) return;
  const list = $("chat-list");
  list.replaceChildren(
    ...chats.map((chat) => {
      const li = document.createElement("li");
      li.classList.toggle("active", chat.id === state.chatId && chat.agent_id === state.agentId);
      const generating = isGenerating(chat.agent_id, chat.id);
      li.classList.toggle("generating", generating);
      const meta = document.createElement("div");
      meta.className = "chat-meta";
      const title = document.createElement("span");
      title.className = "title" + (chat.title ? "" : " empty");
      title.textContent = chat.title || tr("New chat");
      const agent = document.createElement("span");
      agent.className = "chat-agent";
      agent.textContent = chat.agent_name;
      meta.append(title, agent);
      const del = document.createElement("button");
      del.className = "del";
      del.textContent = "✕";
      del.title = "Delete chat";
      del.disabled = generating || state.modelBusy;
      del.dataset.agent = chat.agent_id;
      del.dataset.chat = chat.id;
      del.addEventListener("click", (e) => {
        e.stopPropagation();
        deleteChat(chat);
      });
      li.append(meta, del);
      li.addEventListener("click", () => openChatFromList(chat));
      return li;
    })
  );
}

async function newChat() {
  if (state.modelBusy || state.settingsBusy || !state.user) return;
  const session = state.session;
  try {
    const preferences = await refreshPreferences();
    if (session !== state.session || state.modelBusy || state.settingsBusy) return;
    if (!preferences.agent_id) throw new Error(tr("No agents are configured for this user."));
    if (preferences.agent_id !== state.agentId) await selectAgent(preferences.agent_id);
    const agentId = state.agentId;
    const chat = await api(chatsUrl(), { method: "POST" });
    if (session !== state.session || agentId !== state.agentId) return;
    // creating a chat discards the previous empty one; stop requesting its state while opening the new one
    state.chatId = null;
    state.reasoningEffort = null;
    await openChat(chat.id);
  } catch (err) {
    if (session === state.session) addMessage("assistant").error(err.message);
  }
}

$("new-chat").addEventListener("click", newChat);

async function openChat(chatId) {
  if (state.modelBusy) return;
  const session = state.session;
  const agentId = state.agentId;
  await refreshPreferences();
  if (session !== state.session || agentId !== state.agentId) return;
  const revision = ++state.chatModelRevision;
  const data = await api(chatsUrl(`/${chatId}`));
  if (session !== state.session || agentId !== state.agentId || revision !== state.chatModelRevision || state.modelBusy) return;
  state.chatId = chatId;
  applyChatModels(data);
  const key = chatKey(agentId, chatId);
  if (isGenerating(agentId, chatId) && state.views.has(key)) {
    activateView(state.views.get(key));
    const usage = state.usages.get(key);
    if (usage) showUsage(usage);
    else $("usage").hidden = true;
  } else {
    renderMessages(data.messages);
  }
  updateBusy();
  await refreshChats();
  $("input").focus();
}

async function deleteChat(chat) {
  if (isGenerating(chat.agent_id, chat.id) || state.modelBusy || !confirm(tr("Delete \"{title}\"?", { title: chat.title || tr("New chat") }))) return;
  await api(`/api/agents/${chat.agent_id}/chats/${chat.id}`, { method: "DELETE" });
  if (state.chatId === chat.id && state.agentId === chat.agent_id) {
    state.chatId = null;
    state.model = null;
    updateModelControls();
    renderMessages([]);
    setComposer(false);
  }
  await refreshChats();
}

// ------------------------------------------------------------------ models

function updateModelControls() {
  const openrouter = state.user?.provider === "openrouter";
  $("model-picker").textContent = state.model?.id ?? "No chat selected";
  $("model-picker").title = openrouter ? "Choose a model and routing policy for this chat" : "Model browsing requires OpenRouter";
  $("model-picker").disabled = !openrouter || !state.chatId || state.busy || state.modelBusy;
  const required = state.model?.zdr !== false;
  $("model-policy").textContent = state.model
    ? (openrouter ? (required ? "ZDR required" : "ZDR not required") : state.model.provider) : "";
  if (state.model && state.modelOverride !== undefined) {
    $("model-policy").textContent += ` · ${tr(state.modelOverride === null ? "User default" : "Chat override")}`;
  }
  updateReasoningEffortControls();
  $("model-policy").classList.toggle("privacy-warning", openrouter && !required);
  $("model-refresh").disabled = state.modelLoading || state.modelBusy;
  $("model-close").disabled = state.modelBusy;
  $("model-zdr").disabled = state.modelBusy || state.modelLoading || state.modelTarget !== "chat-llm";
  $("model-inherit").hidden = state.modelTarget.startsWith("default-");
  $("model-inherit").disabled = !state.chatId || state.busy || state.modelBusy;
  renderSettingsModels();
  $("model-zdr-warning").hidden = $("model-zdr").checked;
  $("model-favorites-only").setAttribute("aria-pressed", String(state.modelFavoritesOnly));
  $("model-favorites-only").textContent = state.modelFavoritesOnly ? "\u2605" : "\u2606";
  $("model-favorites-only").title = state.modelFavoritesOnly ? "Show all models" : "Show favorites only";
  const activeFilters = ["model-tools", "model-images", "model-reasoning"]
    .filter((id) => $(id).checked).length;
  $("model-filters-label").textContent = activeFilters ? `Filters (${activeFilters})` : "Filters";
  $("model-filters").dataset.active = String(activeFilters > 0);
}

function updateReasoningEffortControls() {
  const effort = state.chatId ? state.reasoningEffort : null;
  $("chat-effort-default").textContent = effort?.default ? `${tr("Default")} (${effort.default})` : tr("Default");
  $("chat-effort").value = effort?.override ?? "";
  $("chat-effort").disabled = !effort?.reasoning || effort.supported !== true
    || !state.chatId || state.busy || state.modelBusy;
  let note = "";
  if (effort) {
    if (!effort.reasoning) note = tr("Reasoning is off for this agent.");
    else if (effort.supported === false) note = tr("This model does not support reasoning effort.");
    else if (effort.supported === null) note = tr("Reasoning effort support could not be verified right now.");
    else note = tr(effort.override === null ? "Agent default" : "Chat override");
  }
  $("effort-policy").textContent = note;
}

$("chat-effort").addEventListener("change", async (e) => {
  if (state.busy || state.modelBusy || !state.chatId) return;
  const session = state.session;
  const agentId = state.agentId;
  const chatId = state.chatId;
  state.chatModelRevision++;
  state.modelBusy = true;
  updateBusy();
  try {
    const models = await api(chatsUrl(`/${chatId}/reasoning-effort`), {
      method: "PUT", body: JSON.stringify({ effort: e.target.value || null }),
    });
    if (session !== state.session || agentId !== state.agentId || chatId !== state.chatId) return;
    applyChatModels(models);
  } catch (err) {
    if (session === state.session) {
      e.target.value = state.reasoningEffort?.override ?? "";
      addMessage("assistant").error(err.message);
    }
  } finally {
    if (session === state.session) {
      state.modelBusy = false;
      updateBusy();
    }
  }
});

function compareModels(a, b, order, favorites) {
  const byName = () => a.name.localeCompare(b.name) || a.id.localeCompare(b.id);
  if (order === "favorites") {
    return Number(favorites.has(b.id)) - Number(favorites.has(a.id)) || byName();
  }
  if (order === "name") return byName();
  const [field, direction] = order.split("-");
  const aPrice = a.pricing?.[field];
  const bPrice = b.pricing?.[field];
  if (aPrice == null || bPrice == null) {
    return Number(aPrice == null) - Number(bPrice == null) || byName();
  }
  const difference = Number(aPrice) - Number(bPrice);
  return (direction === "desc" ? -difference : difference) || byName();
}

function matchesCapabilities(model, { tools, images, reasoning }) {
  if (images && !model.input_modalities?.includes("image")) return false;
  if (!tools && !reasoning) return true;
  return (model.parameter_routes ?? []).some((parameters) =>
    (!tools || parameters.includes("tools"))
    && (!reasoning || parameters.includes("reasoning") || parameters.includes("reasoning_effort"))
  );
}

function renderModelList() {
  const list = $("model-list");
  list.replaceChildren();
  updateModelControls();
  if (state.modelLoading) {
    $("model-status").textContent = tr("Loading current model catalog...");
    return;
  }
  const stt = state.modelTarget.endsWith("-stt");
  if (!state.modelCatalog || state.modelCatalogKind !== (stt ? "stt" : "llm") || state.modelCatalogZdr !== $("model-zdr").checked) {
    $("model-status").textContent = tr("Catalog unavailable. Use Refresh to retry.");
    return;
  }
  const saved = new Set(stt ? [] : state.modelFavorites);
  const availableIds = new Set(state.modelCatalog.map((model) => model.id));
  const models = new Map(state.modelCatalog.map((model) => [model.id, model]));
  for (const id of saved) {
    if (!models.has(id)) models.set(id, { id, name: id, zdr_providers: [] });
  }
  const query = $("model-search").value.trim().toLocaleLowerCase();
  const capabilities = {
    tools: $("model-tools").checked,
    images: $("model-images").checked,
    reasoning: $("model-reasoning").checked,
  };
  const matches = [...models.values()].filter((model) =>
    (!state.modelFavoritesOnly || saved.has(model.id))
    && (stt || matchesCapabilities(model, capabilities))
    && (!query || model.id.toLocaleLowerCase().includes(query) || model.name.toLocaleLowerCase().includes(query))
  ).sort((a, b) => compareModels(a, b, $("model-sort").value, saved));
  $("model-status").textContent = matches.length
    ? `${matches.length} ${tr(matches.length === 1 ? "model" : "models")} - ${tr($("model-zdr").checked ? "ZDR only" : "all routes allowed")}`
    : tr("No models match. Try another search or relax the filters.");
  for (const model of matches) {
    const available = availableIds.has(model.id);
    const row = document.createElement("li");
    row.className = "model-row";
    const choose = document.createElement("button");
    choose.type = "button";
    choose.className = "model-choice";
    choose.disabled = !available || (state.busy && !state.modelTarget.startsWith("default-")) || state.modelBusy;
    const selected = state.modelTarget.startsWith("default-")
      ? state.settingsModelDraft?.[stt ? "default_stt_model" : "default_llm_model"]
      : state.model?.id;
    choose.setAttribute("aria-pressed", String(
      model.id === selected && (state.modelTarget !== "chat-llm" || (state.model?.zdr !== false) === $("model-zdr").checked)
    ));
    const name = document.createElement("strong");
    name.textContent = model.name;
    const id = document.createElement("small");
    id.textContent = model.id;
    const providers = document.createElement("small");
    providers.textContent = !available ? tr("Unavailable under the current filter")
      : model.zdr_providers.length ? `ZDR routes: ${model.zdr_providers.join(", ")}`
      : "No active ZDR routes";
    const pricing = document.createElement("small");
    pricing.textContent = `USD / 1M tokens: input ${tokenPrice(model.pricing?.prompt)}`
      + (stt ? "" : ` | output ${tokenPrice(model.pricing?.completion)}`);
    choose.append(name, id, pricing, providers);
    choose.addEventListener("click", () => selectModel(model.id));
    const star = document.createElement("button");
    star.type = "button";
    star.className = "model-star";
    star.textContent = saved.has(model.id) ? "\u2605" : "\u2606";
    const label = saved.has(model.id) ? `Remove ${model.id} from favorites` : `Add ${model.id} to favorites`;
    star.setAttribute("aria-label", label);
    star.setAttribute("aria-pressed", String(saved.has(model.id)));
    star.title = label;
    star.disabled = state.modelBusy || state.busy;
    star.addEventListener("click", () => toggleFavorite(model.id));
    row.append(choose);
    if (!stt) row.append(star);
    list.append(row);
  }
}

async function refreshModels(initial = false) {
  if (state.modelBusy) return;
  const session = state.session;
  const request = ++state.modelRequest;
  const zdr = $("model-zdr").checked;
  const stt = state.modelTarget.endsWith("-stt");
  state.modelLoading = true;
  $("model-error").textContent = "";
  renderModelList();
  try {
    const [models, favorites] = await Promise.all([
      api(`/api/${stt ? "speech-to-text/models" : "models"}?zdr=${zdr}`),
      stt ? Promise.resolve([]) : api("/api/models/favorites"),
    ]);
    if (session !== state.session || request !== state.modelRequest) return;
    state.modelCatalog = stt ? models.map((model) => ({
      ...model, pricing: { prompt: model.pricing_prompt },
    })) : models;
    state.modelCatalogKind = stt ? "stt" : "llm";
    state.modelCatalogZdr = zdr;
    if (!stt) state.modelFavorites = favorites;
    if (initial) state.modelFavoritesOnly = favorites.length > 0;
  } catch (err) {
    if (session !== state.session || request !== state.modelRequest) return;
    state.modelCatalog = null;
    state.modelCatalogZdr = null;
    $("model-error").textContent = err.message;
  } finally {
    if (session === state.session && request === state.modelRequest) {
      state.modelLoading = false;
      renderModelList();
    }
  }
}

async function toggleFavorite(modelId) {
  if (state.modelBusy || state.busy || state.modelLoading) return;
  const session = state.session;
  const saved = state.modelFavorites.includes(modelId);
  const zdr = $("model-zdr").checked;
  state.modelBusy = true;
  $("model-error").textContent = "";
  updateBusy();
  try {
    const favorites = await api(`/api/models/favorites${saved ? "/" + encodeURIComponent(modelId) : ""}`, {
      method: saved ? "DELETE" : "POST",
      ...(saved ? {} : { body: JSON.stringify({ model_id: modelId, zdr }) }),
    });
    if (session === state.session) state.modelFavorites = favorites;
  } catch (err) {
    if (session === state.session) $("model-error").textContent = err.message;
  } finally {
    if (session === state.session) {
      state.modelBusy = false;
      updateBusy();
    }
  }
}

async function selectModel(modelId) {
  const target = state.modelTarget;
  const defaultSelection = target.startsWith("default-");
  const stt = target.endsWith("-stt");
  if (state.modelBusy || (state.modelLoading && modelId !== null)) return;
  if (defaultSelection) {
    if (!state.settingsOriginal || state.settingsBusy || modelId === null) return;
    state.settingsModelDraft[stt ? "default_stt_model" : "default_llm_model"] = modelId;
    renderSettingsModels();
    $("model-dialog").close();
    return;
  }
  if (state.busy || !state.chatId) return;
  const session = state.session;
  const agentId = state.agentId;
  const chatId = state.chatId;
  const zdr = $("model-zdr").checked;
  state.chatModelRevision++;
  state.modelBusy = true;
  $("model-error").textContent = "";
  updateBusy();
  try {
    await api(chatsUrl(`/${chatId}/model`), {
      method: "PUT", body: JSON.stringify({ model_id: modelId, zdr }),
    });
    if (session !== state.session || agentId !== state.agentId || chatId !== state.chatId) return;
    // reload the whole chat state: the server may clear an unsupported reasoning effort too
    const models = await api(chatsUrl(`/${chatId}/models`), { cache: "no-store" });
    if (session !== state.session || agentId !== state.agentId || chatId !== state.chatId) return;
    applyChatModels(models);
    $("usage").hidden = true;
    $("model-dialog").close();
  } catch (err) {
    if (session === state.session) $("model-error").textContent = err.message;
  } finally {
    if (session === state.session) {
      state.modelBusy = false;
      updateBusy();
    }
  }
}

function openModelPicker(target) {
  const defaultSelection = target.startsWith("default-");
  const stt = target.endsWith("-stt");
  if (state.modelBusy || !state.user || state.user.provider !== "openrouter") return;
  if (defaultSelection ? (!state.settingsOriginal || state.settingsBusy) : (!state.chatId || state.busy)) return;
  const previousStt = state.modelTarget.endsWith("-stt");
  state.modelTarget = target;
  $("model-search").value = "";
  $("model-error").textContent = "";
  $("model-zdr").checked = true;
  state.modelFavoritesOnly = !stt && state.modelFavorites.length > 0;
  $("model-favorites-only").hidden = stt;
  $("model-filters").hidden = stt;
  if (stt) $("model-sort").value = "prompt-asc";
  else if (previousStt) $("model-sort").value = "favorites";
  for (const option of $("model-sort").querySelectorAll("option")) {
    option.disabled = stt && (option.value === "favorites" || option.value.startsWith("completion-"));
  }
  $("model-title").textContent = tr(stt ? "Speech-to-text models" : "OpenRouter models");
  $("model-subtitle").textContent = tr(defaultSelection
    ? (stt ? "Choose the user's default STT model. This does not set a chat override." : "Choose the user's default LLM model. This does not set a chat override.")
    : "Choose a model for this chat.");
  $("model-filters").open = false;
  $("model-info").open = false;
  $("model-dialog").showModal();
  $("model-search").focus();
  refreshModels(true);
}
$("model-picker").addEventListener("click", () => openModelPicker("chat-llm"));
$("settings-llm").addEventListener("click", () => openModelPicker("default-llm"));
$("settings-stt").addEventListener("click", () => openModelPicker("default-stt"));
$("model-inherit").addEventListener("click", () => selectModel(null));
$("model-close").addEventListener("click", () => $("model-dialog").close());
$("model-dialog").addEventListener("cancel", (event) => {
  if (state.modelBusy) event.preventDefault();
});
$("model-refresh").addEventListener("click", () => refreshModels());
$("model-search").addEventListener("input", renderModelList);
$("model-favorites-only").addEventListener("click", () => {
  state.modelFavoritesOnly = !state.modelFavoritesOnly;
  renderModelList();
});
$("model-sort").addEventListener("change", renderModelList);
for (const id of ["model-tools", "model-images", "model-reasoning"]) {
  $(id).addEventListener("change", renderModelList);
}
$("model-zdr").addEventListener("change", () => refreshModels());

// ------------------------------------------------------------------ messages

function setComposer(enabled) {
  $("input").disabled = !enabled || state.modelBusy;
  $("attach").disabled = !enabled || state.modelBusy;
  $("send").disabled = !enabled || state.modelBusy;
}

function updateBusy() {
  // busy refers to the open chat only; other chats may keep generating in the background
  state.busy = isGenerating(state.agentId, state.chatId);
  $("agent-select").disabled = state.modelBusy || state.settingsBusy;
  $("new-chat").disabled = state.modelBusy || state.settingsBusy;
  $("settings-open").disabled = state.settingsBusy;
  for (const button of $("chat-list").querySelectorAll(".del")) {
    button.disabled = isGenerating(button.dataset.agent, button.dataset.chat) || state.modelBusy;
  }
  for (const li of $("chat-list").querySelectorAll("li")) {
    const del = li.querySelector(".del");
    li.classList.toggle("generating", Boolean(del) && isGenerating(del.dataset.agent, del.dataset.chat));
  }
  setComposer(Boolean(state.chatId));
  updateModelControls();
  if ($("model-dialog").open) renderModelList();
  $("send").textContent = state.busy ? "Steer" : "Send";
  $("input").placeholder = state.busy
    ? "Send another message to steer the response..."
    : "Write a message...";
}

function resetStreams() {
  state.session++;
  for (const request of state.requests) request.controller.abort();
  state.requests.clear();
  state.views.clear();
  state.usages.clear();
  state.view = null;
  state.latest = null;
  state.sendQueue = Promise.resolve();
  state.preferences = null;
  state.preferencesRequest++;
  state.settingsOriginal = null;
  state.settingsBusy = false;
  state.settingsModelDraft = null;
  state.modelOverride = undefined;
  state.reasoningEffort = null;
  state.modelTarget = "chat-llm";
  state.modelCatalogKind = "llm";
  state.chatModelRevision++;
  $("settings-dialog").close();
  $("settings-save").disabled = false;
  $("messages").classList.toggle("hide-thinking", false);
  state.modelCatalog = null;
  state.modelCatalogZdr = null;
  $("model-sort").value = "favorites";
  for (const id of ["model-tools", "model-images", "model-reasoning"]) $(id).checked = false;
  state.modelFavorites = [];
  state.modelFavoritesOnly = false;
  state.modelBusy = false;
  state.modelLoading = false;
  state.modelRequest++;
  state.attachments = [];
  renderPendingAttachments();
  $("model-dialog").close();
  $("model-list").replaceChildren();
  $("model-error").textContent = "";
  $("usage").hidden = true;
  updateBusy();
}

function sameSession(request) {
  return request.session === state.session && request.userId === state.user?.id;
}

function isCurrent(request) {
  return sameSession(request) && request.agentId === state.agentId && request.chatId === state.chatId;
}

function activateView(container) {
  state.view = container;
  $("messages").replaceChildren(container);
  scrollDown();
}

function renderMessages(messages) {
  const container = document.createElement("div");
  container.className = "chat-view";
  state.view = container;
  $("messages").replaceChildren(container);
  $("usage").hidden = true;
  for (const m of messages) {
    const view = addMessage(m.role, container);
    if (m.reasoning) view.thinking(m.reasoning);
    for (const action of m.actions ?? []) view.action(action);
    if (m.associative_matches) view.memoryMatches(m.associative_matches);
    view.text(m.content);
    if (m.interrupted) view.interrupted();
    if (m.usage_report) showUsage(m.usage_report);
    else if (m.role === "assistant" && m.usage?.in != null) {
      const output = m.usage.out == null ? "unknown" : m.usage.out;
      showUsage(
        `context: saved usage | in=${m.usage.in} out=${output}\n`
        + "Detailed context breakdown is unavailable for this older reply."
      );
    }
  }
  scrollDown();
}

function showUsage(report) {
  $("usage").hidden = false;
  $("usage").querySelector("summary").textContent = report.split("\n")[0];
  $("usage").querySelector("pre").textContent = report;
}

function addMessage(role, parent = state.view ?? $("messages")) {
  const el = document.createElement("div");
  el.className = `msg ${role}`;
  const body = document.createElement("div");
  body.className = "body";
  let raw = "";
  let thinking = null;
  let memoryIndicator = null;
  let actionCount = 0;
  let attachmentBox = null;
  el.append(body);
  parent.append(el);
  function renderBody() {
    if (role === "assistant") {
      body.innerHTML = DOMPurify.sanitize(marked.parse(raw, { breaks: true }));
      for (const a of body.querySelectorAll("a")) {
        a.target = "_blank";
        a.rel = "noopener noreferrer";
      }
    } else {
      body.textContent = raw;
    }
  }
  function renderAttachmentNames(attachments) {
    if (!attachments?.length) return;
    if (!attachmentBox) {
      attachmentBox = document.createElement("div");
      attachmentBox.className = "message-attachments";
      el.insertBefore(attachmentBox, body);
    }
    attachmentBox.replaceChildren(...attachments.map((attachment) => {
      const item = document.createElement("span");
      item.className = "attachment-name";
      item.textContent = attachment.name || attachment.modality || "attachment";
      return item;
    }));
  }
  return {
    text(chunk) {
      if (Array.isArray(chunk)) {
        const textParts = chunk.filter((part) => part.type === "text").map((part) => part.text);
        raw += textParts.join("");
        renderAttachmentNames(chunk.filter((part) => part.type !== "text").map((part) => ({
          name: part.name || part.type,
        })));
        renderBody();
        return;
      }
      raw += chunk;
      renderBody();
    },
    attachments: renderAttachmentNames,
    thinking(chunk) {
      if (!thinking) {
        thinking = document.createElement("details");
        thinking.className = "thinking";
        thinking.open = prefs.thinkingOpen;
        const summary = document.createElement("summary");
        summary.textContent = "thinking";
        // a user click on the summary sets the preference for every thinking block
        summary.addEventListener("click", () => {
          prefs.thinkingOpen = !thinking.open;
          localStorage.setItem("thinkingOpen", prefs.thinkingOpen);
        });
        thinking.append(summary);
        thinking.append(document.createTextNode(""));
        el.prepend(thinking);
      }
      thinking.lastChild.textContent += chunk;
    },
    notice(text) {
      const note = document.createElement("div");
      note.className = "notice";
      note.setAttribute("role", "status");
      note.textContent = text;
      el.insertBefore(note, body);
    },
    memoryMatches(count) {
      if (!Number.isInteger(count) || count < 1) return;
      if (!memoryIndicator) {
        memoryIndicator = document.createElement("div");
        memoryIndicator.className = "memory-indicator";
        memoryIndicator.setAttribute("role", "status");
        el.insertBefore(memoryIndicator, body);
      }
      memoryIndicator.textContent = `Associative memory: ${count} match${count === 1 ? "" : "es"}`;
    },
    action(a) {
      actionCount++;
      const item = document.createElement("details");
      item.className = "action" + (a.error ? " failed" : "");
      const summary = document.createElement("summary");
      const args = Object.entries(a.arguments ?? {})
        .map(([k, v]) => `${k}=${JSON.stringify(v)}`)
        .join(", ");
      summary.textContent = `${a.name}(${args})`;
      const result = document.createElement("pre");
      result.textContent = a.result;
      item.append(summary, result);
      el.insertBefore(item, body);
    },
    interrupted(snapshot = null) {
      if (snapshot) {
        raw = snapshot.content;
        renderBody();
        if (thinking) thinking.lastChild.textContent = "";
        if (snapshot.reasoning) this.thinking(snapshot.reasoning);
        else if (thinking) {
          thinking.remove();
          thinking = null;
        }
        for (const action of snapshot.actions.slice(actionCount)) this.action(action);
      }
      if (!el.classList.contains("interrupted")) {
        el.classList.add("interrupted");
        const note = document.createElement("div");
        note.className = "interruption";
        note.setAttribute("role", "status");
        note.textContent = tr("Interrupted by a new message");
        el.append(note);
      }
    },
    error(message) {
      el.classList.add("error");
      const note = document.createElement("div");
      note.textContent = `[error] ${message}`;
      el.append(note);
    },
  };
}

function scrollDown() {
  const box = $("messages");
  box.scrollTop = box.scrollHeight;
}

const MAX_FILE_BYTES = 10 * 1024 * 1024;
const MAX_TOTAL_FILE_BYTES = 25 * 1024 * 1024;

function attachmentModality(file) {
  if (file.type.startsWith("image/")) return "image";
  if (file.type.startsWith("video/")) return "video";
  if (file.type.startsWith("audio/")) return "audio";
  return null;
}

function readAttachment(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve({
      modality: attachmentModality(file),
      name: file.name,
      mime_type: file.type,
      format: file.type.split("/")[1].split(";")[0],
      data_url: reader.result,
      size: file.size,
    });
    reader.onerror = () => reject(new Error(`Could not read ${file.name}`));
    reader.readAsDataURL(file);
  });
}

function renderPendingAttachments() {
  let box = $("pending-attachments");
  if (!box) {
    box = document.createElement("div");
    box.id = "pending-attachments";
    $("composer").insertBefore(box, $("input"));
  }
  box.replaceChildren(...state.attachments.map((attachment, index) => {
    const item = document.createElement("span");
    item.className = "pending-attachment";
    item.textContent = attachment.name;
    const remove = document.createElement("button");
    remove.type = "button";
    remove.textContent = "×";
    remove.title = `Remove ${attachment.name}`;
    remove.addEventListener("click", () => {
      state.attachments.splice(index, 1);
      renderPendingAttachments();
    });
    item.append(remove);
    return item;
  }));
}

$("attach").addEventListener("click", () => $("file-input").click());
$("file-input").addEventListener("change", async (event) => {
  const files = [...event.target.files];
  event.target.value = "";
  try {
    const valid = files.map((file) => {
      if (!attachmentModality(file)) throw new Error(tr("{name}: only image, video and audio files are supported", { name: file.name }));
      if (file.size > MAX_FILE_BYTES) throw new Error(tr("{name}: maximum size is 10 MB", { name: file.name }));
      return file;
    });
    const total = state.attachments.reduce((sum, item) => sum + item.size, 0)
      + valid.reduce((sum, file) => sum + file.size, 0);
    if (total > MAX_TOTAL_FILE_BYTES) throw new Error(tr("Attachments exceed the 25 MB total limit"));
    state.attachments.push(...await Promise.all(valid.map(readAttachment)));
    renderPendingAttachments();
  } catch (err) {
    addMessage("assistant").error(err.message);
  }
});

$("composer").addEventListener("submit", async (e) => {
  e.preventDefault();
  const content = $("input").value.trim();
  if ((!content && !state.attachments.length) || !state.user || !state.chatId || state.modelBusy) return;
  const attachments = state.attachments.splice(0);
  renderPendingAttachments();
  $("input").value = "";
  const key = chatKey(state.agentId, state.chatId);
  state.views.set(key, state.view);
  const userMessage = addMessage("user", state.view);
  userMessage.text(content);
  userMessage.attachments(attachments);
  const reply = addMessage("assistant", state.view);
  const request = {
    userId: state.user.id, agentId: state.agentId, chatId: state.chatId,
    session: state.session, controller: new AbortController(), reply,
    generationId: null, done: false,
  };
  state.requests.add(request);
  state.latest = request;
  state.usages.delete(key);
  $("usage").hidden = true;
  updateBusy();
  scrollDown();
  try {
    await streamReply(content, attachments, request);
  } catch (err) {
    request.controller.abort();
    if (sameSession(request) && err.name !== "AbortError") reply.error(err.message);
  } finally {
    state.requests.delete(request);
    if (sameSession(request)) {
      if (!isGenerating(request.agentId, request.chatId) && !isCurrent(request)) {
        state.views.delete(key);
        state.usages.delete(key);
      }
      updateBusy();
      if (!isGenerating(request.agentId, request.chatId)) {
        if (isCurrent(request)) $("input").focus();
        try {
          await refreshChats();
        } catch (err) {
          if (isCurrent(request)) reply.error(err.message);
        }
      }
    }
  }
});

$("input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    $("composer").requestSubmit();
  }
});

async function streamReply(content, attachments, request) {
  // Serialize acceptance, not generation, so rapid sends reach the service in order.
  const response = state.sendQueue.then(async () => {
    if (!sameSession(request)) throw new DOMException("Session changed", "AbortError");
    await refreshPreferences();
    if (!sameSession(request)) throw new DOMException("Session changed", "AbortError");
    return fetch(`/api/agents/${request.agentId}/chats/${request.chatId}/messages`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "Accept-Language": state.preferences?.language ?? "en" },
      body: JSON.stringify({
        content,
        attachments: attachments.map(({ size, ...attachment }) => attachment),
      }),
      signal: request.controller.signal,
    });
  });
  state.sendQueue = response.then(() => undefined, () => undefined);
  const res = await response;
  if (res.status === 401) {
    const error = tr("not logged in");
    if (sameSession(request)) showLogin();
    throw new Error(error);
  }
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(formatApiError(body.detail) || tr("HTTP {status}", { status: res.status }));
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      const { value, done } = await reader.read();
      buffer += done ? decoder.decode() : decoder.decode(value, { stream: true });
      let end;
      while ((end = buffer.indexOf("\n\n")) !== -1) {
        handleEvent(buffer.slice(0, end), request);
        buffer = buffer.slice(end + 2);
      }
      if (done) break;
      if (isCurrent(request)) scrollDown();
    }
    if (sameSession(request) && !request.done) {
      throw new Error(tr("Response stream ended before completion"));
    }
  } finally {
    reader.releaseLock();
  }
}

function handleEvent(raw, request) {
  if (!sameSession(request)) return;
  const reply = request.reply;
  let event = "message";
  let data = "";
  for (const line of raw.split("\n")) {
    if (line.startsWith("event: ")) event = line.slice(7);
    else if (line.startsWith("data: ")) data += line.slice(6);
    else if (line.startsWith("id: ")) {
      const id = line.slice(4);
      if (request.generationId && request.generationId !== id) {
        throw new Error(tr("Generation changed within a response"));
      }
      request.generationId = id;
    }
  }
  const payload = data ? JSON.parse(data) : "";
  if (event === "text") reply.text(payload);
  else if (event === "reasoning") reply.thinking(payload);
  else if (event === "tool") reply.action(payload);
  else if (event === "memory") reply.memoryMatches(payload.matches);
  else if (event === "notice") reply.notice(payload);
  else if (event === "interrupted") reply.interrupted(payload);
  else if (event === "error") reply.error(payload);
  else if (event === "done") request.done = true;
  else if (event === "usage" && request === state.latest) {
    state.usages.set(chatKey(request.agentId, request.chatId), payload);
    // the first line is the headline; the full breakdown stays folded until expanded
    if (isCurrent(request)) showUsage(payload);
  }
}

// ------------------------------------------------------------------ boot

api("/api/me").then(showApp, showLogin);
