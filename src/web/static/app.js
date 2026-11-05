const $ = (id) => document.getElementById(id);
// inline line icons (currentColor); constants only, never built from user data
const ICONS = {
  clock: "<svg class=\"icon\" viewBox=\"0 0 24 24\" aria-hidden=\"true\" focusable=\"false\"><circle cx=\"12\" cy=\"12\" r=\"9\"/><path d=\"M12 7v5l3 2\"/></svg>",
  alert: "<svg class=\"icon\" viewBox=\"0 0 24 24\" aria-hidden=\"true\" focusable=\"false\"><circle cx=\"12\" cy=\"12\" r=\"9\"/><path d=\"M12 7.5v5.5\"/><path d=\"M12 16.5h.01\"/></svg>",
  send: "<svg class=\"icon\" viewBox=\"0 0 24 24\" aria-hidden=\"true\" focusable=\"false\"><path d=\"M12 19V5\"/><path d=\"m5 12 7-7 7 7\"/></svg>",
  steer: "<svg class=\"icon\" viewBox=\"0 0 24 24\" aria-hidden=\"true\" focusable=\"false\"><polygon points=\"3 11 22 2 13 21 11 13 3 11\"/></svg>",
  pencil: "<svg class=\"icon\" viewBox=\"0 0 24 24\" aria-hidden=\"true\" focusable=\"false\"><path d=\"M12 20h9\"/><path d=\"M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z\"/></svg>",
  trash: "<svg class=\"icon\" viewBox=\"0 0 24 24\" aria-hidden=\"true\" focusable=\"false\"><path d=\"M3 6h18\"/><path d=\"M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6\"/><path d=\"M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2\"/></svg>",
  star: "<svg class=\"icon\" viewBox=\"0 0 24 24\" aria-hidden=\"true\" focusable=\"false\"><polygon points=\"12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2\"/></svg>",
  sun: "<svg class=\"icon\" viewBox=\"0 0 24 24\" aria-hidden=\"true\" focusable=\"false\"><circle cx=\"12\" cy=\"12\" r=\"4\"/><path d=\"M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41\"/></svg>",
  moon: "<svg class=\"icon\" viewBox=\"0 0 24 24\" aria-hidden=\"true\" focusable=\"false\"><path d=\"M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z\"/></svg>",
};
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
  // provider tabs of the open model picker: availability rows, the selected provider, the one the catalog is for
  modelProviders: null, modelProvider: null, modelCatalogProvider: null,
  modelOverride: undefined,
  // where the chat's effective model comes from ("chat" | "agent" | "user") and the agent's own default, if any
  modelSource: undefined, agentDefaultModel: null,
  reasoningEffort: null,
  chatModelRevision: 0,
  settingsModelDraft: null,
  chatInfo: null,  // summary of the open chat (disposable flag and expiry)
  preferences: null, preferencesRequest: 0, settingsBusy: false, settingsOriginal: null,
  translations: null, toolGroupCatalog: [],
  mcpServers: [], mcpTools: new Map(), connectionsPoll: null,
  agentConfigs: [], agentForm: { originalId: null, busy: false },
  dream: null,
};
// thinking blocks start open (streamed live); closing one keeps them closed from then on
const prefs = { thinkingOpen: localStorage.getItem("thinkingOpen") !== "false" };
const tokenPriceFormat = new Intl.NumberFormat("en-US", {
  style: "currency", currency: "USD", maximumSignificantDigits: 6,
});

// Theme is a per-browser preference: "system" removes the override so prefers-color-scheme applies.
function applyTheme(theme) {
  if (theme === "light" || theme === "dark") {
    localStorage.setItem("theme", theme);
    document.documentElement.dataset.theme = theme;
  } else {
    localStorage.removeItem("theme");
    delete document.documentElement.dataset.theme;
  }
  renderThemeToggle();
}

function effectiveTheme() {
  return document.documentElement.dataset.theme
    ?? (matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark");
}

// the toggle shows the theme it would switch to
function renderThemeToggle() {
  $("theme-toggle").innerHTML = effectiveTheme() === "dark" ? ICONS.sun : ICONS.moon;
}
matchMedia("(prefers-color-scheme: light)").addEventListener("change", renderThemeToggle);

function tr(template, values = {}) {
  const text = state.preferences?.language === "es" ? state.translations[template] : template;
  if (typeof text !== "string") throw new Error(`Missing translation: ${template}`);
  return text.replace(/\{(\w+)\}/g, (_, key) => values[key]);
}

function translateInterface() {
  for (const [id, text] of [
    ["settings-title", "User settings"], ["settings-save", "Save settings"],
    ["settings-thinking-label", "Show model thinking"],
    ["settings-assoc-max-label", "Maximum associative memory matches"],
    ["settings-orchestrator-label", "Choose the agent automatically in Telegram"],
    ["settings-orchestrator-hint", "When a Telegram message starts a new chat, a fast model picks the best agent from the agents' descriptions. You can still override it with the buttons. The message text and the agents' names and descriptions are sent to the decision model's provider."],
    ["settings-core-agent-label", "Core agent (used when unsure)"],
    ["settings-disposable-days-label", "Days to keep disposable chats"],
    ["settings-disposable-days-hint", "Disposable chats are deleted after this many days without activity (1–365). Leave empty to use the default of 30."],
    ["settings-assoc-max-hint", "Leave empty to use the application default. The minimum similarity is set by the application, not by you."],
    ["settings-language-label", "Alerts and errors"], ["settings-theme-label", "Theme"],
    ["settings-theme-system", "System"], ["settings-theme-light", "Light"], ["settings-theme-dark", "Dark"],
    ["settings-note", "Settings are shared with Telegram. Chats without a selected model use your current defaults."],
    ["settings-llm-label", "Default LLM model"], ["settings-stt-label", "Default STT model"],
    ["model-zdr-text", "ZDR only (OpenRouter)"],
    ["agent-label", "Agent"], ["chat-model-label", "Chat model"], ["chat-effort-text", "Reasoning effort"],
    ["model-inherit", "Use user default"],
    ["model-scope-note", "Choosing a model for a chat sets only that chat's override. The default entry removes it (the agent's default model, else yours). Defaults in Settings are saved with Save settings. Favorites are shared across your agents."],
    ["model-zdr-warning", "ZDR is not required for your next selection. Providers may retain your prompts and replies. Your OpenRouter account may still enforce ZDR."],
    ["tab-chats", "Chats"], ["tab-agents", "Agents"],
    ["agent-new-label", "New agent"], ["agents-empty", "Select an agent to edit it, or create a new one."],
    ["agent-name-label", "Name"], ["agent-description-label", "Description"],
    ["agent-description-hint", "Optional. Shown in the agent list; not sent to the agent's chat model. The Telegram orchestrator reads it to pick agents: one or two sentences, what topics should this agent answer?"],
    ["agent-prompt-label", "Personality (SOUL.md)"], ["agent-behavior-label", "Chat behavior"],
    ["agent-prompt-hint", "Saved as the agent's SOUL.md. Leave it empty to start with the default SOUL.md; edit it later in the Soul & memory tab."],
    ["soul-default", "Insert default template"],
    ["agent-stream-label", "Stream replies"], ["agent-memory-label", "Use memory"],
    ["agent-reasoning-label", "Reasoning"], ["agent-keep-reasoning-label", "Keep reasoning in memory"],
    ["agent-recall-knowledge-label", "RECALL knowledge (how RECALL works)"],
    ["agent-recall-knowledge-hint", "Gives the agent a built-in, read-only guide to how RECALL works. Takes effect in new chats."],
    ["agent-tools-label", "Use tools"], ["agent-associative-label", "Associative memory"],
    ["agent-associative-off", "Off"], ["agent-associative-llm", "Query from a small model"],
    ["agent-associative-question", "Search with your message"],
    ["agent-associative-execution-label", "Associative search timing"],
    ["agent-associative-parallel", "In parallel with the reply"], ["agent-associative-sequential", "Before the reply"],
    ["agent-tool-groups-label", "Tool groups"],
    ["agent-terminal-legend", "Terminal"], ["agent-terminal-default-label", "Default backend"],
    ["agent-terminal-no-backends", "No terminal backends are configured. Add one under terminal_backends in your user configuration; see docs/user-guide/terminal.md."],
    ["agent-terminal-timeout-label", "Command timeout (seconds)"], ["agent-terminal-dream-label", "Allow in dream"],
    ["agent-terminal-dream-hint", "Dream sessions run unattended: with this on, an agent whose dream tools include the terminal can run commands on its backends without you watching."],
    ["agent-effort-label", "Reasoning effort"], ["agent-effort-default", "Default"],
    ["agent-tokens-enabled-label", "Limit reasoning tokens"], ["agent-reasoning-tokens-label", "Reasoning tokens"],
    ["agent-retained-label", "Messages kept in context"],
    ["agent-retained-hint", "Empty keeps all messages."], ["agent-save", "Save agent"], ["agent-delete", "Delete agent"],
    ["agent-tab-basic", "Basic"], ["agent-advanced-summary", "Advanced"], ["agent-tab-tools", "Tools"],
    ["agent-tab-mcp", "MCP"], ["agent-tab-soul", "Soul & memory"], ["agent-tab-dream", "Dream"],
    ["agent-tab-sessions", "Dream sessions"],
    ["soul-note", "These files are part of every chat's system prompt. Changes apply to new chats."],
    ["soul-title", "Personality (SOUL.md)"], ["soul-save", "Save SOUL.md"],
    ["soul-history-note", "The last versions are saved before every change."], ["soul-version-close", "Close"],
    ["memory-title", "Agent notes (MEMORY.md)"], ["memory-save", "Save MEMORY.md"],
    ["user-memory-title", "About the user (USER.md)"], ["user-memory-save", "Save USER.md"],
    ["memory-entries-note", "Separate memory entries with a line holding only §. Limits are set in the Dream tab."],
    ["dream-note", "In a dream the agent reviews its recent chats in the background and updates its own SOUL, memories, skills and notes. Nothing is sent to you during a dream."],
    ["dream-schedule-label", "Schedule"], ["dream-mode-manual-label", "Manual only"],
    ["dream-mode-every-label", "Every"], ["dream-hours-label", "hours"], ["dream-mode-daily-label", "Daily at"],
    ["dream-enabled-label", "Schedule enabled"],
    ["dream-schedule-hint", "Scheduled sessions are skipped while no chat is pending. You can always start one by hand, from the Dream sessions tab or with /dream start in a chat."],
    ["dream-model-legend", "Model and limits"], ["dream-model-label", "Model"], ["dream-model-browse", "Browse"],
    ["dream-model-hint", "Empty uses the agent's default chat model, else yours."],
    ["agent-model-label", "Default model"], ["agent-model-browse", "Browse"], ["agent-model-clear", "Clear"],
    ["agent-model-hint", "Empty follows your default model. A model chosen inside a chat still takes precedence."],
    ["dream-effort-label", "Reasoning effort"], ["dream-effort-none", "No reasoning request"],
    ["dream-max-steps-label", "Maximum steps"], ["dream-max-steps-hint", "Model calls per session (1–500)."],
    ["dream-max-tokens-label", "Maximum tokens"],
    ["dream-max-tokens-hint", "Input plus output tokens per session (1,000–10,000,000)."],
    ["dream-max-minutes-label", "Maximum minutes"],
    ["dream-max-minutes-hint", "Time limit per session (1–120). One minute before it the agent is told to wrap up; at the limit the session is closed."],
    ["dream-cancel", "Cancel dream"],
    ["dream-tools-legend", "Dream reading tools"],
    ["dream-tools-hint", "Chats, mind palace and the agent's own files are always available."],
    ["dream-tool-web-label", "Web search (needs Tavily)"],
    ["dream-tool-folders-label", "User folders, read-only (needs a folder in the Folders tab)"],
    ["agent-tab-folders", "Folders"],
    ["agent-folders-note", "Folders on the computer running RECALL, such as an Obsidian vault. Each one is a source named after it (name:path/note.md). Chats reach them with the folder tool groups; dreams only read them."],
    ["agent-folders-flags-note", "Indexed folders feed associative memory and query_memory. A read-only folder is never edited, whatever the tool groups."],
    ["agent-folder-add", "Add folder"], ["index-title", "Memory index"], ["index-reindex", "Reindex"],
    ["index-rebuild", "Rebuild from scratch"],
    ["index-hint", "Reindex scans every source now, folders included, and embeds only what changed. Rebuilding embeds everything again."],
    ["dream-prompt-legend", "Dream prompt"],
    ["dream-prompt-hint", "Empty uses the built-in prompt, shown as the placeholder."],
    ["dream-prompt-copy", "Edit a copy of the default"], ["dream-prompt-reset", "Reset to default"],
    ["dream-notify-label", "Notify me on Telegram when a session ends"], ["dream-save", "Save dream settings"],
    ["dream-status-title", "Dream status"], ["dream-sessions-title", "Dream sessions"],
    ["dream-col-date", "Date"], ["dream-col-trigger", "Trigger"], ["dream-col-status", "Status"],
    ["dream-col-chats", "Chats reviewed"], ["dream-col-duration", "Duration"], ["dream-col-tokens", "Tokens"],
    ["dream-sessions-empty", "No dream sessions yet."], ["dream-session-back", "← All sessions"],
    ["agent-mcp-note", "MCP servers act on each user's own account. Connect accounts in Settings > Connections."],
    ["connections-title", "Connections"],
    ["connections-note", "Connect your own account for each service your agents can use. Every user connects separately."],
    ["connections-browser-note", "Sign-in opens in a browser on the computer running RECALL."],
  ]) $(id).textContent = tr(text);
  renderAgentsEmpty();
  $("dream-model").placeholder = tr("Your chat model");
  $("agent-model").placeholder = tr("Your default model");
  for (const [id, text] of [
    ["settings-open", "Settings"], ["settings-close", "Close"],
    ["logout", "Log out"], ["theme-toggle", "Switch theme"],
  ]) {
    $(id).title = tr(text);
    $(id).setAttribute("aria-label", tr(text));
  }
  $("new-disposable-chat").title = tr("New disposable chat");
  $("new-disposable-chat").setAttribute("aria-label", tr("New disposable chat"));
  for (const button of document.querySelectorAll(".nav-toggle")) {
    button.title = tr("Menu");
    button.setAttribute("aria-label", tr("Menu"));
  }
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
    throw httpError(body, res.status);
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

// ---- errors: stable codes -> explanation and action (DOM-free helpers, unit-tested)

function normalizeError(source) {
  if (source == null) return { message: "", code: null };
  if (typeof source === "string") return { message: source, code: null };
  return { message: String(source.message ?? ""), code: typeof source.code === "string" ? source.code : null };
}

function httpError(body, status) {
  const error = new Error(formatApiError(body?.detail) || tr("HTTP {status}", { status }));
  error.code = typeof body?.code === "string" ? body.code : null;
  error.status = status;
  return error;
}

// Title, hint and optional action for an error `code`; null when the code is unknown (the plain message is shown).
// Actions: create-agent, choose-model, new-chat, open-settings; `command` is shown as text, never run.
function errorInfo(code) {
  const chooseModel = { action: "choose-model", label: tr("Choose another model") };
  switch (code) {
    case "no_agents":
      return { title: tr("You have no agents yet"), hint: tr("An agent is the assistant you chat with. Create one to start chatting."),
        action: "create-agent", label: tr("Create an agent") };
    case "missing_api_key":
      return { title: tr("This model's provider has no API key"),
        hint: tr("Set the key on the computer running RECALL and restart it, or use another model."), ...chooseModel };
    case "provider_unreachable":
      return { title: tr("The model's provider is not reachable"),
        hint: tr("Check that its server is running, or use another model."), ...chooseModel };
    case "claude_not_installed":
      return { title: tr("Claude Code is not installed"),
        hint: tr("Install Claude Code on the computer running RECALL, or use another model."), ...chooseModel };
    case "claude_no_own_login":  // no button: only the administrator can set this up
      return { title: tr("No Claude login of your own"),
        hint: tr("Ask the administrator to set up your own Claude login folder, or use another model."), ...chooseModel };
    case "claude_not_logged_in":
      return { title: tr("Claude Code is not logged in"),
        hint: tr("Run this command on the computer running RECALL, then send your message again:"),
        command: "claude auth login", ...chooseModel };
    case "provider_unavailable":
      return { title: tr("The model's provider is not available"),
        hint: tr("Check the provider's status in the model picker, or use another model."), ...chooseModel };
    case "model_unavailable":
      return { title: tr("That model is not available"), hint: tr("Pick a different model for this chat."), ...chooseModel };
    case "rate_limited":
      return { title: tr("The provider is busy"), hint: tr("Wait a moment and try again, or choose another model."), ...chooseModel };
    case "agent_busy":
      return { title: tr("The agent is still replying"), hint: tr("Wait for its current replies to finish, then try again.") };
    case "unknown_chat":
      return { title: tr("This chat no longer exists"), hint: tr("Open another chat or start a new one."),
        action: "new-chat", label: tr("Start a new chat") };
    case "mcp_not_connected":
      return { title: tr("A connection is missing"), hint: tr("Connect the service in Settings, then try again."),
        action: "open-settings", label: tr("Open Settings") };
    default:
      return null;
  }
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
  resetDream();
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
  renderAgentBanner();
}

function currentAgentName() {
  return state.user?.agents.find((a) => a.id === state.agentId)?.name ?? "";
}

// The active agent is the header's headline; the select is stretched invisibly over it as the switcher.
function renderAgentBanner() {
  const name = currentAgentName();
  $("agent-title").textContent = name || "—";
  $("agent-avatar").textContent = [...name][0]?.toUpperCase() ?? "?";
  $("input").placeholder = state.busy
    ? "Send another message to steer the response..."
    : name ? `Message ${name}...` : "Write a message...";
}

async function showApp(user) {
  // read before showTab("chats") below drops the hash of the agents page
  const route = pendingRoute || location.hash;
  pendingRoute = "";
  resetStreams();
  state.user = user;
  const session = state.session;
  $("login").hidden = true;
  $("app").hidden = false;
  $("user-name").textContent = user.name;
  $("user-avatar").textContent = [...user.name][0]?.toUpperCase() ?? "?";
  renderThemeToggle();
  renderAgentOptions();
  showTab("chats");
  try {
    await refreshPreferences();
    if (session !== state.session) return;
    await selectAgent(state.preferences.agent_id);
    if (session === state.session && state.agentId) await newChat();
    // a deep link (for example from a dream notification) opens its agent page after login
    if (session === state.session && route) await routeFromHash(route);
  } catch (err) {
    if (session === state.session) addMessage("assistant").error(err);
  }
}

$("login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("login-error").textContent = "";
  try {
    const user = await api("/api/login", {
      method: "POST",
      body: JSON.stringify({
        user_id: $("login-user").value,
        password: $("login-password").value,
      }),
    });
    $("login-password").value = "";
    showApp(user);
  } catch (err) {
    $("login-error").textContent = err.message;
  }
});

$("logout").addEventListener("click", async () => {
  await api("/api/logout", { method: "POST" });
  showLogin();
});

// ------------------------------------------------------------------ navigation drawer (narrow screens)

function setNav(open) {
  document.body.classList.toggle("nav-open", open);
  for (const button of document.querySelectorAll(".nav-toggle")) button.setAttribute("aria-expanded", String(open));
}
for (const button of document.querySelectorAll(".nav-toggle")) {
  button.addEventListener("click", () => setNav(!document.body.classList.contains("nav-open")));
}
$("nav-scrim").addEventListener("click", () => setNav(false));
document.addEventListener("keydown", (e) => { if (e.key === "Escape") setNav(false); });
// choosing a chat or agent, or starting a new one, closes the drawer
$("sidebar").addEventListener("click", (e) => { if (e.target.closest("#chat-list li, #agent-list button, #new-chat, #new-disposable-chat")) setNav(false); });

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
  renderAgentBanner();
  renderMessages([]);
  setComposer(false);
  if (!agentId && state.user && !state.user.agents.length) renderNoAgents();
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
      addMessage("assistant").error(err);
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
  $("new-chat-row").hidden = agents;
  $("agent-list").hidden = !agents;
  $("agent-new").hidden = !agents;
  if (agents) return loadAgentConfigs();
  stopDreamPoll();
  setDreamHash(null);
  return Promise.resolve();
}

async function loadAgentConfigs() {
  const session = state.session;
  try {
    const [agents, toolGroups, mcpServers] = await Promise.all([
      api("/api/agents"), api("/api/tool-groups"), api("/api/mcp"),
    ]);
    if (session !== state.session) return;
    state.agentConfigs = agents;
    state.toolGroupCatalog = toolGroups;
    state.mcpServers = mcpServers;
    state.mcpTools.clear();
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
      const name = document.createElement("span");
      name.className = "title";
      name.textContent = agent.name;
      button.append(name);
      if (agent.description) {
        const description = document.createElement("span");
        description.className = "agent-description";
        description.textContent = agent.description;
        button.title = agent.description;
        button.append(description);
      }
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
  resetDream(agent.id);
  $("agent-error").textContent = "";
  $("agent-status").textContent = "";
  fillAgentForm(agent);
  $("agent-delete").hidden = false;
  setAgentFormOpen(true);
  setDreamHash(agentHash());
}

function openNewAgent() {
  if (state.agentForm.busy) return;
  state.agentForm.originalId = null;
  resetDream();
  $("agent-error").textContent = "";
  $("agent-status").textContent = "";
  fillAgentForm({
    name: "", id: "", description: "",
    chat_config: {
      stream: true, has_memory: true, reasoning: true, reasoning_effort: "medium", reasoning_tokens: null,
      keep_reasoning_in_memory: false, conversation_messages_retained: 30, tools: true, associative_memory: "llm",
      associative_execution: "parallel", recall_knowledge: true,
    },
  });
  $("agent-delete").hidden = true;
  setAgentFormOpen(true);
  setDreamHash("agents");
}

function setAgentFormOpen(open) {
  $("agent-editor").hidden = !open;
  $("agents-empty").hidden = open;
  renderAgentList();
}

function fillAgentForm(agent) {
  const config = agent.chat_config;
  $("agent-form-title").textContent = tr(state.agentForm.originalId ? "Edit agent" : "New agent");
  $("agent-name").value = agent.name;
  $("agent-description").value = agent.description ?? "";
  // only a new agent's form has the personality field: it becomes SOUL.md right after the agent is created;
  // a saved agent's SOUL.md is edited in its own tab, with history
  if (!state.agentForm.originalId) $("agent-prompt").value = "";
  $("agent-prompt-field").hidden = Boolean(state.agentForm.originalId);
  for (const name of DREAM_PANELS) $(`agent-tab-${name}`).hidden = !state.agentForm.originalId;
  $("agent-model").value = config.model ?? "";
  $("agent-stream").checked = config.stream;
  $("agent-memory").checked = config.has_memory;
  $("agent-reasoning").checked = config.reasoning;
  $("agent-keep-reasoning").checked = config.keep_reasoning_in_memory;
  $("agent-recall-knowledge").checked = config.recall_knowledge !== false;
  $("agent-tools").checked = config.tools;
  const selected = new Set(config.tool_groups
    ?? state.toolGroupCatalog.filter((group) => group.default !== false).map((group) => group.id));
  $("agent-tool-groups").replaceChildren(
    Object.assign(document.createElement("legend"), { id: "agent-tool-groups-label", textContent: tr("Tool groups") }),
    ...state.toolGroupCatalog.map((group) => {
      const label = document.createElement("label");
      label.className = "model-filter";
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.name = "agent-tool-group";
      checkbox.value = group.id;
      checkbox.checked = selected.has(group.id);
      const description = translatedText(group.description);
      checkbox.title = description;
      label.title = description;
      const text = document.createElement("span");
      text.textContent = translatedText(group.name);
      label.append(checkbox, text);
      if (group.id === "terminal") checkbox.addEventListener("change", updateTerminalBlock);
      return label;
    }),
  );
  renderTerminalBlock(agent);
  $("agent-tool-groups").append(Object.assign(document.createElement("p"), {
    className: "model-note", textContent: tr("The folder groups need at least one folder, set in the Folders tab."),
  }));
  $("agent-associative").value = config.associative_memory;
  $("agent-associative-execution").value = config.associative_execution;
  $("agent-associative-execution").disabled = config.associative_memory === "off";
  $("agent-effort").value = config.reasoning_effort ?? "";
  $("agent-tokens-enabled").checked = config.reasoning_tokens != null;
  $("agent-reasoning-tokens").disabled = config.reasoning_tokens == null;
  $("agent-reasoning-tokens").value = config.reasoning_tokens ?? "";
  $("agent-retained").value = config.conversation_messages_retained ?? "";
  renderAgentMcp(config.mcp ?? {});
  renderFolders(agent.folders ?? []);
  resetIndexStatus();
  showAgentPanel("basic");
}

// The Terminal block follows the `terminal` tool group: shown (and validated) only while the group is ticked.
function terminalGroupChecked() {
  return Boolean(document.querySelector('input[name="agent-tool-group"][value="terminal"]:checked'));
}

function updateTerminalBlock() {
  const enabled = terminalGroupChecked();
  $("agent-terminal").hidden = !enabled;
  $("agent-terminal").disabled = !enabled;  // a disabled fieldset is not validated, so hidden fields never block saving
}

function renderTerminalBlock(agent) {
  const saved = agent.terminal ?? null;
  const rows = terminalBackendRows(state.user?.terminal_backends, saved);
  $("agent-terminal-no-backends").hidden = rows.length > 0;
  $("agent-terminal-backends").replaceChildren(...rows.map(buildTerminalBackendRow));
  $("agent-terminal-timeout").value = saved?.timeout_seconds ?? 60;
  $("agent-terminal-dream").checked = saved?.allow_in_dream ?? false;
  $("agent-terminal-error").textContent = "";
  refreshTerminalDefault(saved?.default_backend);
  updateTerminalBlock();
}

function buildTerminalBackendRow(row) {
  const make = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  };
  const box = make("div", "terminal-backend");
  box.dataset.name = row.name;
  const allow = document.createElement("input");
  allow.type = "checkbox";
  allow.className = "terminal-allow";
  allow.checked = row.allowed;
  const head = make("label", "model-filter");
  head.append(allow, make("span", null, `${tr("Allow")} ${row.label}`));
  box.append(head);
  if (row.description) box.append(make("p", "model-note", row.description));
  const details = make("div", "terminal-backend-details");
  const workdir = document.createElement("input");
  workdir.className = "terminal-workdir";
  workdir.maxLength = 1024;
  workdir.placeholder = "~";
  workdir.autocomplete = "off";
  workdir.value = row.workdir;
  const workdirLabel = make("label");
  workdirLabel.append(make("span", null, tr("Working directory")), workdir);
  const approvals = document.createElement("select");
  approvals.className = "terminal-approvals";
  for (const [value, label] of [
    ["manual", tr("Manual: ask for everything")], ["smart", tr("Smart: ask for dangerous actions")],
    ["off", tr("Off: never ask (risky)")],
  ]) approvals.append(Object.assign(document.createElement("option"), { value, textContent: label }));
  approvals.value = row.approvals;
  const hint = make("span", "model-note", terminalApprovalHint(row.approvals));
  const approvalsLabel = make("label");
  approvalsLabel.append(make("span", null, tr("Approvals")), approvals, hint);
  approvals.classList.toggle("warning", row.approvals === "off");
  approvals.addEventListener("change", () => {
    hint.textContent = terminalApprovalHint(approvals.value);
    approvals.classList.toggle("warning", approvals.value === "off");
  });
  details.append(workdirLabel, approvalsLabel);
  details.hidden = !row.allowed;
  box.append(details);
  allow.addEventListener("change", () => {
    details.hidden = !allow.checked;
    refreshTerminalDefault($("agent-terminal-default").value);
  });
  return box;
}

// The form's backend rows as plain data (also what buildTerminalConfig takes).
function readTerminalBackendFields() {
  return [...document.querySelectorAll("#agent-terminal-backends .terminal-backend")].map((box) => ({
    name: box.dataset.name,
    allowed: box.querySelector(".terminal-allow").checked,
    workdir: box.querySelector(".terminal-workdir").value,
    approvals: box.querySelector(".terminal-approvals").value,
  }));
}

function refreshTerminalDefault(preferred) {
  const names = readTerminalBackendFields().filter((field) => field.allowed).map((field) => field.name);
  const select = $("agent-terminal-default");
  select.replaceChildren(...names.map((name) => Object.assign(document.createElement("option"), { value: name, textContent: name })));
  select.value = terminalDefaultBackend(names, preferred);
  $("agent-terminal-default-label").parentElement.hidden = names.length < 2;
}


const AGENT_PANELS = ["basic", "tools", "mcp", "folders"];
// panels of a saved agent with their own saves, outside the agent form
const DREAM_PANELS = ["soul", "dream", "sessions"];

function agentPanel(name) {
  return $(name === "dream" ? "dream-form" : `agent-panel-${name}`);
}

// Catalog names and descriptions come from the server in English; show the translation when one exists.
function translatedText(text) {
  return state.preferences?.language === "es" && typeof state.translations?.[text] === "string" ? state.translations[text] : text;
}

function showAgentPanel(panel) {
  if (panel === "advanced") panel = "basic"; // the Advanced tab is now a disclosure inside Basic
  if (DREAM_PANELS.includes(panel) && !state.agentForm.originalId) panel = "basic";
  for (const name of [...AGENT_PANELS, ...DREAM_PANELS]) {
    $(`agent-tab-${name}`).setAttribute("aria-pressed", String(name === panel));
    agentPanel(name).hidden = name !== panel;
  }
  const formPanel = AGENT_PANELS.includes(panel);
  $("agent-form").hidden = !formPanel;
  document.querySelector(".agent-form-actions").hidden = !formPanel;
  if (panel !== "sessions") stopDreamPoll();
  if (panel !== "folders") stopIndexPoll();
  if (state.agentForm.originalId) setDreamHash(agentHash(panel));
  if (panel === "folders") loadIndexStatus();
  if (formPanel) return Promise.resolve();
  if (panel === "sessions") {
    state.dream.sessionId = null;
    $("dream-session-detail").hidden = true;
    $("dream-sessions-view").hidden = false;
  }
  return loadDreamPanel(panel);
}

// One fieldset per catalog server; the server is enabled for the agent while its entry exists.
function renderAgentMcp(selections) {
  if (!state.mcpServers.length) {
    $("agent-mcp-list").replaceChildren(
      Object.assign(document.createElement("p"), { className: "model-note", textContent: tr("No MCP servers are configured.") }),
    );
    return;
  }
  $("agent-mcp-list").replaceChildren(...state.mcpServers.map((server) => {
    const selection = selections[server.id];
    const box = document.createElement("fieldset");
    box.className = "mcp-server";
    box.dataset.server = server.id;
    // keeps a saved tool list when the tools cannot be listed (for example, not connected)
    box.dataset.savedTools = JSON.stringify(selection?.tools ?? null);
    box.append(Object.assign(document.createElement("legend"), { textContent: server.name }));
    if (server.description) {
      box.append(Object.assign(document.createElement("p"), { className: "model-note", textContent: server.description }));
    }
    box.append(Object.assign(document.createElement("p"), {
      className: server.connected ? "mcp-status connected" : "mcp-status",
      textContent: tr(server.connected ? "Connected" : "Not connected"),
    }));
    const enabled = checkboxLabel("mcp-enabled", tr("Enable for this agent"), Boolean(selection));
    const options = document.createElement("div");
    options.className = "mcp-options";
    options.hidden = !selection;
    enabled.input.addEventListener("change", () => {
      options.hidden = !enabled.input.checked;
      if (enabled.input.checked) loadMcpTools(box, server);
    });
    const readOnly = checkboxLabel("mcp-read-only", tr("Read only"), selection?.read_only ?? false);
    readOnly.label.title = tr("Only tools the server marks as read-only are exposed.");
    const instructions = document.createElement("label");
    const textarea = Object.assign(document.createElement("textarea"), {
      className: "mcp-instructions", rows: 3, maxLength: 4000, value: selection?.instructions ?? "",
    });
    instructions.append(Object.assign(document.createElement("span"), { textContent: tr("Instructions for the agent") }), textarea);
    const tools = document.createElement("fieldset");
    tools.className = "mcp-tools";
    const all = checkboxLabel("mcp-all-tools", tr("All tools, including new ones"), selection?.tools == null);
    all.input.addEventListener("change", () => syncMcpToolInputs(box));
    tools.append(
      Object.assign(document.createElement("legend"), { textContent: tr("Tools") }),
      all.label,
      Object.assign(document.createElement("div"), { className: "mcp-tool-list" }),
      Object.assign(document.createElement("p"), { className: "model-note mcp-tools-status" }),
    );
    options.append(readOnly.label, instructions, tools);
    box.append(enabled.label, options);
    if (selection) loadMcpTools(box, server);
    return box;
  }));
}

function checkboxLabel(className, text, checked) {
  const label = document.createElement("label");
  label.className = "model-filter";
  const input = Object.assign(document.createElement("input"), { type: "checkbox", className, checked });
  label.append(input, Object.assign(document.createElement("span"), { textContent: text }));
  return { label, input };
}

async function loadMcpTools(box, server) {
  const status = box.querySelector(".mcp-tools-status");
  if (!server.connected) {
    status.textContent = tr("Connect {server} in Settings > Connections to choose its tools.", { server: server.name });
    return;
  }
  const session = state.session;
  if (!state.mcpTools.has(server.id)) {
    status.textContent = tr("Loading tools…");
    try {
      state.mcpTools.set(server.id, await api(`/api/mcp/${server.id}/tools`));
    } catch (err) {
      if (session === state.session) status.textContent = err.message;
      return;
    }
  }
  if (session !== state.session || !box.isConnected) return;
  status.textContent = "";
  const saved = JSON.parse(box.dataset.savedTools);
  const chosen = new Set(saved ?? []);
  box.querySelector(".mcp-tool-list").replaceChildren(...state.mcpTools.get(server.id).map((tool) => {
    const text = tool.read_only ? tool.name : `${tool.name} (${tr("changes data")})`;
    const item = checkboxLabel("mcp-tool", text, saved == null || chosen.has(tool.name));
    item.input.value = tool.name;
    item.input.dataset.readOnly = String(tool.read_only);
    item.label.title = tool.description;
    return item.label;
  }));
  syncMcpToolInputs(box);
}

function syncMcpToolInputs(box) {
  const all = box.querySelector(".mcp-all-tools").checked;
  for (const input of box.querySelectorAll(".mcp-tool")) {
    input.disabled = all;
    if (all) input.checked = true;
  }
}

function agentMcpBody() {
  const selections = {};
  for (const box of document.querySelectorAll("#agent-mcp-list .mcp-server")) {
    if (!box.querySelector(".mcp-enabled").checked) continue;
    const toolInputs = [...box.querySelectorAll(".mcp-tool")];
    let tools = null;
    if (!box.querySelector(".mcp-all-tools").checked) {
      tools = toolInputs.length
        ? toolInputs.filter((input) => input.checked).map((input) => input.value)
        : (JSON.parse(box.dataset.savedTools) ?? []);
    }
    selections[box.dataset.server] = {
      tools,
      read_only: box.querySelector(".mcp-read-only").checked,
      instructions: box.querySelector(".mcp-instructions").value.trim(),
    };
  }
  return selections;
}

// ---- user folders and the memory index

function renderFolders(folders) {
  $("agent-folder-list").replaceChildren(...folders.map(folderRow));
  syncFolderEmpty();
}

function syncFolderEmpty() {
  const list = $("agent-folder-list");
  if (!list.querySelector(".folder-row")) {
    list.replaceChildren(Object.assign(document.createElement("p"), { className: "model-note folder-empty", textContent: tr("No folders yet.") }));
  }
}

function folderRow(folder) {
  const row = document.createElement("fieldset");
  row.className = "folder-row";
  const field = (labelText, input, hint) => {
    const label = document.createElement("label");
    label.append(Object.assign(document.createElement("span"), { textContent: labelText }), input);
    if (hint) label.append(Object.assign(document.createElement("span"), { className: "model-note", textContent: hint }));
    return label;
  };
  const name = Object.assign(document.createElement("input"), {
    className: "folder-name", value: folder.name ?? "", required: true, maxLength: 32,
    pattern: "[a-z0-9_\\-]{1,32}", autocomplete: "off", spellcheck: false,
  });
  const path = Object.assign(document.createElement("input"), {
    className: "folder-path", value: folder.path ?? "", required: true, maxLength: 1024, autocomplete: "off", spellcheck: false,
  });
  const flags = document.createElement("div");
  flags.className = "folder-flags";
  const indexed = checkboxLabel("folder-indexed", tr("Indexed"), folder.indexed ?? true);
  const readOnly = checkboxLabel("folder-read-only", tr("Read-only"), folder.read_only ?? false);
  const remove = Object.assign(document.createElement("button"), { type: "button", className: "folder-remove", textContent: tr("Remove") });
  remove.addEventListener("click", () => { row.remove(); syncFolderEmpty(); });
  flags.append(indexed.label, readOnly.label, remove);
  row.append(
    field(tr("Folder name"), name, tr("Lowercase letters, digits, - or _ (up to 32); not mind_palace, conversations, agent or dream.")),
    field(tr("Folder path"), path),
    flags,
  );
  return row;
}

function agentFoldersBody() {
  return [...document.querySelectorAll("#agent-folder-list .folder-row")].map((row) => ({
    name: row.querySelector(".folder-name").value.trim(),
    path: row.querySelector(".folder-path").value.trim(),
    indexed: row.querySelector(".folder-indexed").checked,
    read_only: row.querySelector(".folder-read-only").checked,
  }));
}

$("agent-folder-add").addEventListener("click", () => {
  $("agent-folder-list").querySelector(".folder-empty")?.remove();
  const row = folderRow({ name: "", path: "", indexed: true, read_only: false });
  $("agent-folder-list").append(row);
  row.querySelector(".folder-name").focus();
});

let indexPoll = null;
let indexRequest = 0;

function stopIndexPoll() {
  clearTimeout(indexPoll);
  indexPoll = null;
}

function resetIndexStatus() {
  stopIndexPoll();
  indexRequest += 1;
  const saved = Boolean(state.agentForm.originalId);
  $("index-status-text").textContent = saved ? "" : tr("Index status is available once the agent is saved.");
  $("index-status-error").textContent = "";
  $("index-reindex").disabled = !saved;
  $("index-rebuild").disabled = !saved;
}

function indexStatusText(status) {
  const parts = [tr("{chunks} chunks from {files} files and {chats} chats. Sources: {sources}.", {
    chunks: status.chunks, files: status.files, chats: status.chats, sources: status.sources.join(", "),
  })];
  parts.push(status.last_reconcile ? tr("Last reconcile: {date}.", { date: formatDate(status.last_reconcile) }) : tr("Never reconciled yet."));
  if (status.folders_reconciled_at) parts.push(tr("Folders scanned: {date}.", { date: formatDate(status.folders_reconciled_at) }));
  if (status.rebuilding) parts.push(tr("Rebuilding…"));
  else if (status.busy) parts.push(tr("Indexing…"));
  return parts.join(" ");
}

async function loadIndexStatus() {
  const agentId = state.agentForm.originalId;
  if (!agentId) return;
  stopIndexPoll();
  const request = ++indexRequest;
  const session = state.session;
  try {
    const status = await api(`/api/agents/${encodeURIComponent(agentId)}/index`);
    if (request !== indexRequest || session !== state.session) return;
    showIndexStatus(status);
  } catch (err) {
    if (request === indexRequest && session === state.session) $("index-status-error").textContent = err.message;
  }
}

function showIndexStatus(status) {
  $("index-status-text").textContent = indexStatusText(status);
  $("index-status-error").textContent = status.error ? tr("Index error: {error}", { error: status.error }) : "";
  stopIndexPoll();
  if ((status.busy || status.rebuilding) && !$("agent-panel-folders").hidden) indexPoll = setTimeout(loadIndexStatus, 2000);
}

async function reindexAgent(full) {
  const agentId = state.agentForm.originalId;
  if (!agentId) return;
  if (full && !confirm(tr("Rebuild the whole index? Every note and chat is embedded again, which costs embedding requests."))) return;
  const request = ++indexRequest;
  const session = state.session;
  $("index-status-error").textContent = "";
  $("index-reindex").disabled = $("index-rebuild").disabled = true;
  try {
    const status = await api(`/api/agents/${encodeURIComponent(agentId)}/index/reindex`, {
      method: "POST", body: JSON.stringify({ full }),
    });
    if (request !== indexRequest || session !== state.session) return;
    showIndexStatus({ ...status, busy: true });
    $("agent-status").textContent = tr("Reindex queued.");
  } catch (err) {
    if (request === indexRequest && session === state.session) $("index-status-error").textContent = err.message;
  } finally {
    $("index-reindex").disabled = $("index-rebuild").disabled = !state.agentForm.originalId;
  }
}

$("index-reindex").addEventListener("click", () => reindexAgent(false));
$("index-rebuild").addEventListener("click", () => reindexAgent(true));

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
  const saved = state.agentConfigs.find((agent) => agent.id === state.agentForm.originalId);
  return {
    id: state.agentForm.originalId ?? agentIdFromName(name),
    name,
    description: $("agent-description").value.trim(),
    // edited in the Dream tab; sent back unchanged so saving the agent does not reset it
    ...(saved ? { dream: saved.dream } : {}),
    folders: agentFoldersBody(),
    terminal: buildTerminalConfig(saved?.terminal, terminalGroupChecked(), {
      backends: readTerminalBackendFields(), defaultBackend: $("agent-terminal-default").value,
      timeout: $("agent-terminal-timeout").value, allowInDream: $("agent-terminal-dream").checked,
    }),
    chat_config: {
      model: $("agent-model").value.trim() || null,  // null follows the user's default model
      stream: $("agent-stream").checked,
      has_memory: $("agent-memory").checked,
      reasoning: $("agent-reasoning").checked,
      reasoning_effort: $("agent-effort").value || null,
      reasoning_tokens: $("agent-tokens-enabled").checked ? optionalNumber("agent-reasoning-tokens") : null,
      keep_reasoning_in_memory: $("agent-keep-reasoning").checked,
      recall_knowledge: $("agent-recall-knowledge").checked,
      conversation_messages_retained: optionalNumber("agent-retained"),
      tools: $("agent-tools").checked,
      tool_groups: [...document.querySelectorAll('input[name="agent-tool-group"]:checked')].map((input) => input.value),
      mcp: agentMcpBody(),
      associative_memory: $("agent-associative").value,
      associative_execution: $("agent-associative-execution").value,
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
  state.user.terminal_backends = me.terminal_backends ?? [];
  renderAgentOptions();
  renderAgentsEmpty();
  await loadAgentConfigs();
  if (previousId && previousId === state.agentId && previousId !== currentId) {
    // the server moved the preference when the agent was renamed or deleted
    const preferences = await refreshPreferences();
    await selectAgent(preferences.agent_id ?? state.user.agents[0]?.id ?? null);
  }
}

$("tab-chats").addEventListener("click", async () => {
  await showTab("chats");
  // an agent created from the empty state has no chat yet
  if (state.user && state.agentId && !state.chatId) await newChat();
});
$("tab-agents").addEventListener("click", () => showTab("agents"));
$("agent-new").addEventListener("click", openNewAgent);
for (const name of [...AGENT_PANELS, ...DREAM_PANELS]) $(`agent-tab-${name}`).addEventListener("click", () => showAgentPanel(name));
// a field the browser rejects may sit in a hidden panel; show that panel so it can point at the field
$("agent-form").addEventListener("invalid", (e) => {
  const panel = e.target.closest(".agent-panel");
  if (panel) showAgentPanel(panel.id.replace("agent-panel-", ""));
  // a rejected field inside the collapsed Advanced section must be visible too
  const details = e.target.closest("details");
  if (details) details.open = true;
}, true);

$("agent-associative").addEventListener("change", () => {
  $("agent-associative-execution").disabled = $("agent-associative").value === "off";
});
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
  const soul = previousId ? "" : $("agent-prompt").value;
  setAgentBusy(true);
  $("agent-error").textContent = "";
  $("agent-terminal-error").textContent = "";
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
    // the open chat may now inherit another default model
    if (saved.id === state.agentId) await refreshChatModels(session, state.preferencesRequest);
    // the agent exists now; its personality goes to SOUL.md through the soul route
    if (soul.trim()) {
      await api(`/api/agents/${saved.id}/soul`, { method: "PUT", body: JSON.stringify({ content: soul }) });
    }
    $("agent-status").textContent = tr("Agent saved.");
  } catch (err) {
    if (session === state.session) {
      if (terminalGroupChecked() && isTerminalError(err.message)) {
        $("agent-terminal-error").textContent = err.message;
        showAgentPanel("tools");
      } else {
        $("agent-error").textContent = err.message;
      }
    }
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
    resetDream();
    setAgentFormOpen(false);
    setDreamHash("agents");
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
  // application-wide size limits (settings.yaml) arrive with the preferences
  if (preferences.limits) $("agent-prompt").maxLength = preferences.limits.soul_chars;
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
  state.modelSource = models.model_source;
  state.agentDefaultModel = models.agent_default_model ?? null;
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
  $("settings-assoc-max").value = state.preferences.associative_max_matches ?? "";
  $("settings-disposable-days").value = state.preferences.disposable_retention_days ?? "";
  $("settings-orchestrator").checked = state.preferences.orchestrator ?? state.preferences.orchestrator_default ?? false;
  const core = $("settings-core-agent");
  core.replaceChildren(new Option(tr("Same as preferred agent"), ""), ...(state.user?.agents ?? []).map((a) => new Option(a.name, a.id)));
  core.value = state.preferences.core_agent_id ?? "";
  $("settings-theme").value = localStorage.getItem("theme") ?? "system";
  renderSettingsModels();
}

function renderSettingsModels() {
  $("settings-llm").textContent = state.settingsModelDraft?.default_llm_model ?? "";
  $("settings-stt").textContent = state.settingsModelDraft?.default_stt_model ?? "";
  const disabled = !state.settingsOriginal || !state.user || state.settingsBusy;
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
  loadConnections();
  try {
    await refreshPreferences();
    if (session !== state.session) return;
    fillSettings();
    $("settings-save").disabled = state.settingsBusy;
  } catch (err) {
    if (session === state.session) $("settings-error").textContent = err.message;
  }
});

$("settings-theme").addEventListener("change", (e) => applyTheme(e.target.value));
$("theme-toggle").addEventListener("click", () => {
  applyTheme(effectiveTheme() === "dark" ? "light" : "dark");
  $("settings-theme").value = localStorage.getItem("theme") ?? "system";
});
$("settings-close").addEventListener("click", () => $("settings-dialog").close());
$("settings-dialog").addEventListener("close", stopConnectionsPoll);
$("connections").addEventListener("toggle", () => {
  if (!$("connections").open) stopConnectionsPoll();
  else if (state.mcpServers.some((server) => server.pending)) startConnectionsPoll();
});

// ------------------------------------------------------------------ providers (model picker)

const PROVIDER_NAMES = { openrouter: "OpenRouter", ollama: "Ollama", claude_subscription: "Claude subscription" };

function providerName(id) {
  return PROVIDER_NAMES[id] ?? id;
}

// a bare model id belongs to the user's main provider; only known provider names count as a prefix
function providerOfReference(reference) {
  const head = reference?.split(":", 1)[0];
  return reference?.includes(":") && head in PROVIDER_NAMES ? head : state.user?.provider ?? null;
}

// short, translated reason a provider tab is disabled (the server sends a code, never prose)
function providerReason(provider) {
  switch (provider.reason) {
    case "no_api_key": return tr("API key variable not set");
    case "unreachable": return tr("Ollama not reachable");
    case "cli_missing": return tr("Claude Code not installed");
    case "logged_out": return tr("Run `claude auth login`");
    case "no_own_login": return tr("No Claude login of your own");
    default: return tr("Claude Code login unknown");
  }
}

function providerMemoryKey() {
  return `modelProvider:${state.user?.id}`;
}

function rememberProvider(id) {
  try { localStorage.setItem(providerMemoryKey(), id); } catch { /* storage may be blocked */ }
}

function currentModelReference() {
  switch (state.modelTarget) {
    case "dream-llm": return $("dream-model").value.trim() || state.preferences?.default_llm_model;
    case "agent-llm": return $("agent-model").value.trim() || state.preferences?.default_llm_model;
    case "default-llm": return state.settingsModelDraft?.default_llm_model;
    case "default-stt": return state.settingsModelDraft?.default_stt_model;
    default: return state.model?.id;
  }
}

// "Any" is the picker's first tab: the models of every available provider together (chat models only)
const ANY_PROVIDER = "any";

function withAnyProvider(rows) {
  return [{ id: ANY_PROVIDER, label: "Any", available: rows.some((row) => row.available) }, ...rows];
}

// Opens on Any when the user last chose it; else on the provider of the model selected now, else the last
// one used, else the main one, else the first usable one. Any is never a fallback for a real provider.
function pickInitialProvider(rows, selected, remembered, main) {
  const usable = new Set(rows.filter((row) => row.available).map((row) => row.id));
  if (remembered === ANY_PROVIDER && usable.has(ANY_PROVIDER)) return ANY_PROVIDER;
  return [selected, remembered, main, ...rows.map((row) => row.id)]
    .find((id) => id !== ANY_PROVIDER && usable.has(id)) ?? null;
}

function initialProvider(rows) {
  let remembered = null;
  try { remembered = localStorage.getItem(providerMemoryKey()); } catch { /* storage may be blocked */ }
  return pickInitialProvider(rows, providerOfReference(currentModelReference()), remembered, state.user?.provider);
}

// the OpenRouter checkbox decides for OpenRouter rows (and for Any, which contains them); other providers
// follow the user's setting, which does not filter their rows
function zdrFor(provider) {
  return provider === "openrouter" || provider === ANY_PROVIDER ? $("model-zdr").checked : Boolean(state.user?.require_zdr);
}

function modelZdr() {
  return zdrFor(state.modelProvider);
}

function renderProviderTabs() {
  const tabs = $("model-providers");
  const rows = state.modelProviders;
  tabs.hidden = !rows;
  if (!rows) {
    delete tabs.dataset.key;
    tabs.replaceChildren();
    return;
  }
  const key = JSON.stringify([rows.map((row) => [row.id, row.available, row.reason]), state.preferences?.language]);
  if (tabs.dataset.key !== key) {
    tabs.dataset.key = key;
    tabs.setAttribute("aria-label", tr("Provider"));
    tabs.replaceChildren(...rows.map((row) => {
      const tab = document.createElement("button");
      tab.type = "button";
      tab.className = "provider-tab";
      tab.dataset.provider = row.id;
      tab.setAttribute("role", "tab");
      tab.disabled = !row.available;
      tab.title = row.detail ?? "";
      const any = row.id === ANY_PROVIDER;
      if (any) tab.title = tr("Models of every available provider");
      tab.append(Object.assign(document.createElement("strong"), { textContent: any ? tr("Any") : row.label }));
      const experimental = tr(state.user?.require_zdr ? "experimental, not ZDR" : "experimental");
      const note = row.available ? (row.experimental ? experimental : "") : providerReason(row);
      if (note) tab.append(Object.assign(document.createElement("small"), { textContent: note }));
      tab.addEventListener("click", () => chooseProvider(row.id));
      return tab;
    }));
  }
  for (const tab of tabs.children) tab.setAttribute("aria-selected", String(tab.dataset.provider === state.modelProvider));
}

function chooseProvider(id) {
  if (state.modelBusy || state.modelLoading || id === state.modelProvider) return;
  state.modelProvider = id;
  rememberProvider(id);
  refreshModels();
}

function favoritesOf(provider) {
  if (provider === ANY_PROVIDER) return state.modelFavorites;
  return state.modelFavorites.filter((id) => providerOfReference(id) === provider);
}

// ------------------------------------------------------------------ mcp connections

async function loadConnections() {
  const session = state.session;
  $("connections-error").textContent = "";
  try {
    const servers = await api("/api/mcp");
    if (session !== state.session) return;
    state.mcpServers = servers;
    renderConnections();
  } catch (err) {
    if (session === state.session) $("connections-error").textContent = err.message;
  }
}

function renderConnections() {
  const servers = state.mcpServers;
  if (!servers.length) {
    $("connection-list").replaceChildren(
      Object.assign(document.createElement("li"), { className: "model-note", textContent: tr("No MCP servers are configured.") }),
    );
  } else {
    $("connection-list").replaceChildren(...servers.map(connectionItem));
  }
  $("connections-count").textContent = servers.length
    ? `(${servers.filter((server) => server.connected).length}/${servers.length})` : "";
  if (servers.some((server) => server.pending)) startConnectionsPoll();
  else stopConnectionsPoll();
}

function connectionItem(server) {
  const item = document.createElement("li");
  item.className = "connection";
  const status = server.pending ? "Waiting for sign-in…" : (server.connected ? "Connected" : "Not connected");
  const heading = document.createElement("div");
  heading.append(
    Object.assign(document.createElement("strong"), { textContent: server.name }),
    Object.assign(document.createElement("span"), {
      className: server.connected ? "mcp-status connected" : "mcp-status", textContent: ` ${tr(status)}`,
    }),
  );
  item.append(heading);
  if (server.description) {
    item.append(Object.assign(document.createElement("p"), { className: "model-note", textContent: server.description }));
  }
  if (server.error) {
    item.append(Object.assign(document.createElement("p"), {
      className: "error", textContent: tr("Sign-in failed: {error}", { error: server.error }),
    }));
  }
  if (server.pending && /^https?:\/\//.test(server.auth_url ?? "")) {
    item.append(Object.assign(document.createElement("a"), {
      href: server.auth_url, target: "_blank", rel: "noopener noreferrer", textContent: tr("Open sign-in page"),
    }));
  }
  if (server.requires_auth) {
    const actions = document.createElement("div");
    actions.className = "agent-actions";
    const connect = Object.assign(document.createElement("button"), {
      type: "button", textContent: tr(server.connected ? "Reconnect" : "Connect"), disabled: server.pending,
    });
    connect.addEventListener("click", () => changeConnection(server, "connect"));
    actions.append(connect);
    if (server.connected || server.pending) {
      const disconnect = Object.assign(document.createElement("button"), { type: "button", textContent: tr("Disconnect") });
      disconnect.addEventListener("click", () => {
        if (window.confirm(tr("Disconnect {server}? Your stored credentials for it are deleted.", { server: server.name }))) {
          changeConnection(server, "disconnect");
        }
      });
      actions.append(disconnect);
    }
    item.append(actions);
  }
  return item;
}

async function changeConnection(server, action) {
  const session = state.session;
  $("connections-error").textContent = "";
  for (const button of $("connection-list").querySelectorAll("button")) button.disabled = true;
  try {
    const status = await api(
      action === "connect" ? `/api/mcp/${server.id}/connect` : `/api/mcp/${server.id}/connection`,
      { method: action === "connect" ? "POST" : "DELETE" },
    );
    if (session !== state.session) return;
    state.mcpServers = state.mcpServers.map((item) => (item.id === status.id ? status : item));
    state.mcpTools.delete(status.id);
  } catch (err) {
    if (session === state.session) $("connections-error").textContent = err.message;
  }
  if (session === state.session) renderConnections();
}

function startConnectionsPoll() {
  if (state.connectionsPoll || !$("settings-dialog").open || !$("connections").open) return;
  state.connectionsPoll = setInterval(loadConnections, 3000);
}

function stopConnectionsPoll() {
  clearInterval(state.connectionsPoll);
  state.connectionsPoll = null;
}
$("settings-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (state.settingsBusy || !state.settingsOriginal) return;
  const session = state.session;
  const changes = {};
  const thinking = $("settings-thinking").checked;
  const language = $("settings-language").value;
  if (thinking !== state.settingsOriginal.show_thinking) changes.show_thinking = thinking;
  if (language !== state.settingsOriginal.language) changes.language = language;
  const rawMax = $("settings-assoc-max").value.trim();
  const maxMatches = rawMax === "" ? null : Number(rawMax);
  if (maxMatches !== (state.settingsOriginal.associative_max_matches ?? null)) changes.associative_max_matches = maxMatches;
  const rawDays = $("settings-disposable-days").value.trim();
  const retentionDays = rawDays === "" ? null : Number(rawDays);
  if (retentionDays !== (state.settingsOriginal.disposable_retention_days ?? null)) changes.disposable_retention_days = retentionDays;
  const orchestratorBefore = state.settingsOriginal.orchestrator ?? state.settingsOriginal.orchestrator_default ?? false;
  if ($("settings-orchestrator").checked !== orchestratorBefore) changes.orchestrator = $("settings-orchestrator").checked;
  const coreAgent = $("settings-core-agent").value || null;
  if (coreAgent !== (state.settingsOriginal.core_agent_id ?? null)) changes.core_agent_id = coreAgent;
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
    if (session === state.session) addMessage("assistant").error(err);
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
      del.innerHTML = ICONS.trash;
      del.title = "Delete chat";
      del.disabled = generating || state.modelBusy;
      del.dataset.agent = chat.agent_id;
      del.dataset.chat = chat.id;
      del.addEventListener("click", (e) => {
        e.stopPropagation();
        deleteChat(chat);
      });
      if (chat.disposable) {
        const badge = document.createElement("span");
        badge.className = "chat-badge";
        badge.innerHTML = ICONS.clock;
        badge.title = tr("Disposable chat, deleted on {date}", { date: expiryDate(chat.expires_at) });
        li.append(badge);
      }
      li.append(meta);
      // empty chats are pruned, so only chats with messages can keep a title
      if (chat.turns > 0) {
        const rename = document.createElement("button");
        rename.className = "chat-action rename";
        rename.innerHTML = ICONS.pencil;
        rename.title = tr("Rename chat");
        rename.addEventListener("click", (e) => {
          e.stopPropagation();
          startRenameChat(chat, title);
        });
        title.addEventListener("dblclick", (e) => {
          e.stopPropagation();
          startRenameChat(chat, title);
        });
        li.append(rename);
      }
      li.append(del);
      li.addEventListener("click", () => openChatFromList(chat));
      return li;
    })
  );
}

function startRenameChat(chat, title) {
  const input = document.createElement("input");
  input.className = "rename-input";
  input.value = chat.title;
  input.maxLength = 60;
  input.setAttribute("aria-label", tr("Rename chat"));
  let finished = false;
  const finish = async (save) => {
    if (finished) return;
    finished = true;
    const value = input.value.trim();
    const session = state.session;
    try {
      if (save && value && value !== chat.title) {
        await api(`/api/agents/${chat.agent_id}/chats/${chat.id}/title`, {
          method: "PUT", body: JSON.stringify({ title: value }),
        });
      }
    } catch (err) {
      if (session === state.session) addMessage("assistant").error(err);
    }
    if (session === state.session) await refreshChats();
  };
  input.addEventListener("click", (e) => e.stopPropagation());
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      finish(true);
    } else if (e.key === "Escape") {
      finish(false);
    }
  });
  input.addEventListener("blur", () => finish(true));
  title.replaceWith(input);
  input.focus();
  input.select();
}

async function newChat({ disposable = false } = {}) {
  if (state.modelBusy || state.settingsBusy || !state.user) return;
  const session = state.session;
  try {
    const preferences = await refreshPreferences();
    if (session !== state.session || state.modelBusy || state.settingsBusy) return;
    if (!preferences.agent_id) {
      renderNoAgents();
      return;
    }
    if (preferences.agent_id !== state.agentId) await selectAgent(preferences.agent_id);
    const agentId = state.agentId;
    const chat = await api(chatsUrl(), {
      method: "POST", ...(disposable ? { body: JSON.stringify({ disposable: true }) } : {}),
    });
    if (session !== state.session || agentId !== state.agentId) return;
    // creating a chat discards the previous empty one; stop requesting its state while opening the new one
    state.chatId = null;
    state.reasoningEffort = null;
    await openChat(chat.id);
  } catch (err) {
    if (session === state.session) addMessage("assistant").error(err);
  }
}

$("new-chat").addEventListener("click", () => newChat());
$("new-disposable-chat").addEventListener("click", () => newChat({ disposable: true }));

// the date a disposable chat is deleted on, in the browser's locale
function expiryDate(iso) {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? "" : date.toLocaleDateString(undefined, { year: "numeric", month: "long", day: "numeric" });
}

// what the open chat shows about being disposable: the banner (disposable chats) or the small action (normal chats)
function disposableView(chat) {
  if (!chat) return { banner: null, canMakeDisposable: false };
  if (!chat.disposable) return { banner: null, canMakeDisposable: true };
  return {
    banner: tr("Disposable chat: it will be deleted on {date} and nothing here will be saved to the agent's memory.", { date: expiryDate(chat.expires_at) }),
    canMakeDisposable: false,
  };
}

function renderDisposable() {
  const chat = state.chatId && state.chatInfo?.id === state.chatId ? state.chatInfo : null;
  const view = disposableView(chat);
  $("disposable-banner").hidden = view.banner === null;
  $("disposable-banner-text").textContent = view.banner ?? "";
  $("disposable-convert").textContent = tr("Make it a normal chat");
  $("chat-make-disposable").hidden = !view.canMakeDisposable;
  $("chat-make-disposable").textContent = tr("Make it disposable");
  $("chat-make-disposable").title = tr("Make it disposable");
  const locked = state.busy || state.modelBusy;
  $("disposable-convert").disabled = locked;
  $("chat-make-disposable").disabled = locked;
}

async function setChatDisposable(disposable) {
  if (!state.chatId || state.busy || state.modelBusy) return;
  const session = state.session;
  const agentId = state.agentId;
  const chatId = state.chatId;
  try {
    const chat = await api(chatsUrl(`/${chatId}/disposable`), { method: "POST", body: JSON.stringify({ disposable }) });
    if (session !== state.session || agentId !== state.agentId || chatId !== state.chatId) return;
    state.chatInfo = chat;
    renderDisposable();
    await refreshChats();
  } catch (err) {
    if (session === state.session) addMessage("assistant").error(err);
  }
}

$("disposable-convert").addEventListener("click", () => setChatDisposable(false));
$("chat-make-disposable").addEventListener("click", () => setChatDisposable(true));

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
  state.chatInfo = data.chat;
  applyChatModels(data);
  const key = chatKey(agentId, chatId);
  if (isGenerating(agentId, chatId) && state.views.has(key)) {
    activateView(state.views.get(key));
    const usage = state.usages.get(key);
    if (usage) showUsage(usage);
    else $("usage").hidden = true;
  } else {
    forgetApprovals(agentId, chatId);
    renderMessages(data.messages);
  }
  updateBusy();
  syncApprovals(agentId, chatId);
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

// ZDR wording appears only while it is in play: the user requires it or this chat opted in
function modelPolicy(model) {
  if (!model) return "";
  const zdr = model.zdr || model.require_zdr;
  if (model.provider === "claude_subscription") return tr(zdr ? "Claude subscription, not ZDR" : "Claude subscription");
  if (model.provider === "ollama") {
    if (!/cloud$/.test(model.id)) return tr("Local Ollama model");
    return tr(zdr ? "Ollama cloud model, not ZDR" : "Ollama cloud model");
  }
  if (!zdr) return "";
  return model.zdr ? "ZDR required" : "ZDR not required";
}

function updateModelControls() {
  const openrouter = state.model?.provider === "openrouter";
  $("model-picker").textContent = state.model?.id ?? "No chat selected";
  $("model-picker").title = "Choose a model and routing policy for this chat";
  $("model-picker").disabled = !state.user || !state.chatId || state.busy || state.modelBusy;
  const parts = [modelPolicy(state.model)];
  // where the effective model comes from: the chat's own selection, the agent's default or the user's default
  const sourceHint = { chat: "Chat override", agent: "Agent default", user: "User default" }[state.modelSource];
  if (state.model && sourceHint) parts.push(tr(sourceHint));
  $("model-policy").textContent = parts.filter(Boolean).join(" · ");
  updateReasoningEffortControls();
  renderDisposable();
  $("model-policy").classList.toggle("privacy-warning", openrouter && Boolean(state.model.require_zdr) && !state.model.zdr);
  $("model-refresh").disabled = state.modelLoading || state.modelBusy;
  $("model-close").disabled = state.modelBusy;
  const zdrApplies = state.modelProvider === "openrouter" || state.modelProvider === ANY_PROVIDER;
  $("model-zdr-label").hidden = !zdrApplies;
  $("model-zdr").disabled = state.modelBusy || state.modelLoading || state.modelTarget !== "chat-llm";
  const fieldTarget = state.modelTarget === "dream-llm" || state.modelTarget === "agent-llm";
  $("model-inherit").hidden = state.modelTarget.startsWith("default-");
  $("model-inherit").disabled = fieldTarget ? state.modelBusy : (!state.chatId || state.busy || state.modelBusy);
  // in a chat the entry falls back to the agent's default when it has one; elsewhere it empties the field
  $("model-inherit").textContent = tr(state.modelTarget === "chat-llm" && state.agentDefaultModel
    ? "Use agent default" : "Use user default");
  renderSettingsModels();
  $("model-zdr-warning").hidden = $("model-zdr").checked || !zdrApplies || !state.user?.require_zdr;
  renderProviderTabs();
  $("model-favorites-only").setAttribute("aria-pressed", String(state.modelFavoritesOnly));
  $("model-favorites-only").title = state.modelFavoritesOnly ? "Show all models" : "Show favorites only";
  const activeFilters = ["model-tools", "model-images", "model-reasoning"]
    .filter((id) => $(id).checked).length;
  $("model-filters-label").textContent = activeFilters ? `Filters (${activeFilters})` : "Filters";
  $("model-filters").dataset.active = String(activeFilters > 0);
}

// why the composer's effort selector is unavailable: `status` is the label shown inside the control itself
// (so it also reads on the narrow layout, where the side note is hidden); null when the selector works
function reasoningEffortState(effort) {
  if (!effort) return { status: null, note: "" };
  if (!effort.reasoning) {
    return { status: tr("Disabled in this agent"), note: tr("Reasoning is disabled in this agent. Enable it in Agents > Basic > Advanced > Reasoning.") };
  }
  if (effort.supported === false) {
    return { status: tr("Not available in this model"), note: tr("This model does not support reasoning effort.") };
  }
  if (effort.supported === null) {
    return { status: tr("Unknown for this model"), note: tr("Reasoning effort support could not be verified right now.") };
  }
  return { status: null, note: tr(effort.override === null ? "Agent default" : "Chat override") };
}

function updateReasoningEffortControls() {
  const effort = state.chatId ? state.reasoningEffort : null;
  const { status, note } = reasoningEffortState(effort);
  const select = $("chat-effort");
  $("chat-effort-default").textContent = effort?.default ? `${tr("Default")} (${effort.default})` : tr("Default");
  $("chat-effort-status").textContent = status ?? "";
  select.value = status ? "unavailable" : (effort?.override ?? "");
  select.dataset.unavailable = String(Boolean(status));
  select.disabled = Boolean(status) || !effort || !state.chatId || state.busy || state.modelBusy;
  $("effort-policy").textContent = note;
  select.title = note || tr("Reasoning effort");
  select.setAttribute("aria-label", status ? `${tr("Reasoning effort")}: ${status}` : tr("Reasoning effort"));
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
      addMessage("assistant").error(err);
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
  if (!state.modelProvider) {
    $("model-status").textContent = tr("No provider is available for this kind of model.");
    return;
  }
  if (!state.modelCatalog || state.modelCatalogKind !== (stt ? "stt" : "llm") || state.modelCatalogZdr !== modelZdr()
      || state.modelCatalogProvider !== state.modelProvider) {
    $("model-status").textContent = tr("Catalog unavailable. Use Refresh to retry.");
    return;
  }
  const saved = new Set(stt ? [] : favoritesOf(state.modelProvider));
  const availableIds = new Set(state.modelCatalog.map((model) => model.id));
  const models = new Map(state.modelCatalog.map((model) => [model.id, model]));
  for (const id of saved) {
    if (!models.has(id)) models.set(id, { id, name: id, zdr_providers: [], provider: providerOfReference(id) });
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
  const zdrInPlay = Boolean(state.user?.require_zdr) || $("model-zdr").checked;
  const routes = (state.modelProvider === "openrouter" || state.modelProvider === ANY_PROVIDER) && zdrInPlay
    ? ` - ${tr($("model-zdr").checked ? "ZDR only" : "all routes allowed")}` : "";
  $("model-status").textContent = matches.length
    ? `${matches.length} ${tr(matches.length === 1 ? "model" : "models")}${routes}`
    : tr("No models match. Try another search or relax the filters.");
  for (const model of matches) {
    const available = availableIds.has(model.id);
    const row = document.createElement("li");
    row.className = "model-row";
    const choose = document.createElement("button");
    choose.type = "button";
    choose.className = "model-choice";
    choose.disabled = !available || (state.busy && state.modelTarget === "chat-llm") || state.modelBusy;
    let selected = state.model?.id;
    if (state.modelTarget === "dream-llm") selected = $("dream-model").value.trim();
    else if (state.modelTarget === "agent-llm") selected = $("agent-model").value.trim();
    else if (state.modelTarget.startsWith("default-")) {
      selected = state.settingsModelDraft?.[stt ? "default_stt_model" : "default_llm_model"];
    }
    // each row follows its own provider (the Any tab mixes them); the routing toggle only applies to OpenRouter rows
    const provider = model.provider ?? state.modelProvider;
    const subscription = provider === "claude_subscription";
    const local = provider === "ollama";
    const cloud = local && model.local === false;
    choose.setAttribute("aria-pressed", String(
      model.id === selected && (provider !== "openrouter" || state.modelTarget !== "chat-llm"
        || Boolean(state.model?.zdr) === $("model-zdr").checked)
    ));
    const name = document.createElement("strong");
    name.textContent = model.name;
    const id = document.createElement("small");
    id.textContent = model.id;
    const providers = document.createElement("small");
    providers.textContent = !available ? tr("Unavailable under the current filter")
      : subscription ? tr(zdrInPlay ? "Experimental: uses your own Claude Code login. Not ZDR." : "Experimental: uses your own Claude Code login.")
      : cloud ? tr(zdrInPlay ? "Ollama cloud model: runs on Ollama's servers. Not ZDR." : "Ollama cloud model: runs on Ollama's servers.")
      : local ? tr("Runs on your Ollama server.")
      : !zdrInPlay ? ""
      : model.zdr_providers?.length ? `ZDR routes: ${model.zdr_providers.join(", ")}`
      : "No active ZDR routes";
    const pricing = document.createElement("small");
    pricing.textContent = subscription ? tr("Counts against your Claude plan limits; no per-token price.")
      : local ? tr("Local model; no per-token price.")
      : `USD / 1M tokens: input ${tokenPrice(model.pricing?.prompt)}`
        + (stt ? "" : ` | output ${tokenPrice(model.pricing?.completion)}`);
    choose.append(name);
    if (state.modelProvider === ANY_PROVIDER) {
      choose.append(Object.assign(document.createElement("small"), {
        className: "model-provider-tag", textContent: model.provider_label ?? providerName(provider),
      }));
    }
    choose.append(id, pricing, providers);
    choose.addEventListener("click", () => selectModel(model.id, provider));
    const star = document.createElement("button");
    star.type = "button";
    star.className = "model-star";
    star.innerHTML = ICONS.star;
    const label = saved.has(model.id) ? `Remove ${model.id} from favorites` : `Add ${model.id} to favorites`;
    star.setAttribute("aria-label", label);
    star.setAttribute("aria-pressed", String(saved.has(model.id)));
    star.title = label;
    star.disabled = state.modelBusy || state.busy;
    star.addEventListener("click", () => toggleFavorite(model.id, provider));
    row.append(choose);
    if (!stt) row.append(star);
    list.append(row);
  }
}

async function refreshModels(initial = false, recheck = initial) {
  if (state.modelBusy) return;
  const session = state.session;
  const request = ++state.modelRequest;
  const stt = state.modelTarget.endsWith("-stt");
  state.modelLoading = true;
  $("model-error").textContent = "";
  renderModelList();
  try {
    const [status, favorites] = await Promise.all([
      recheck ? api(`/api/providers/status?capability=${stt ? "stt" : "chat"}`, { cache: "no-store" }) : null,
      stt ? Promise.resolve([]) : api("/api/models/favorites"),
    ]);
    if (session !== state.session || request !== state.modelRequest) return;
    if (status) {
      state.modelProviders = stt ? status.providers : withAnyProvider(status.providers);
      state.modelProvider = initialProvider(state.modelProviders);
    } else if (state.modelProvider && !state.modelProviders?.some((row) => row.id === state.modelProvider && row.available)) {
      state.modelProvider = initialProvider(state.modelProviders ?? []);
    }
    if (!stt) state.modelFavorites = favorites;
    state.modelCatalog = null;
    state.modelCatalogProvider = null;
    if (!state.modelProvider) return;
    const provider = state.modelProvider;
    const zdr = modelZdr();
    renderProviderTabs();
    const catalog = await api(
      `/api/models/catalog?capability=${stt ? "stt" : "chat"}${provider === ANY_PROVIDER ? "" : `&provider=${provider}`}${stt ? "" : `&zdr=${zdr}`}`,  // speech-to-text follows the server's policy
    );
    if (session !== state.session || request !== state.modelRequest) return;
    state.modelCatalog = stt ? catalog.models.map((model) => ({
      ...model, pricing: { prompt: model.pricing_prompt },
    })) : catalog.models;
    state.modelCatalogKind = stt ? "stt" : "llm";
    state.modelCatalogZdr = zdr;
    state.modelCatalogProvider = provider;
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

async function toggleFavorite(modelId, provider = state.modelProvider) {
  if (state.modelBusy || state.busy || state.modelLoading) return;
  const session = state.session;
  const saved = state.modelFavorites.includes(modelId);
  const zdr = zdrFor(provider);
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

async function selectModel(modelId, provider = state.modelProvider) {
  const target = state.modelTarget;
  const defaultSelection = target.startsWith("default-");
  const stt = target.endsWith("-stt");
  if (state.modelBusy || (state.modelLoading && modelId !== null)) return;
  if (modelId !== null && state.modelProvider) rememberProvider(state.modelProvider);
  if (target === "dream-llm") {
    // only fills the Dream tab's field; Save dream settings stores it
    $("dream-model").value = modelId ?? "";
    $("model-dialog").close();
    return;
  }
  if (target === "agent-llm") {
    // only fills the agent form's field; saving the agent stores it
    $("agent-model").value = modelId ?? "";
    $("model-dialog").close();
    return;
  }
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
  const zdr = zdrFor(provider);
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
  if (state.modelBusy || !state.user) return;
  if (target === "dream-llm") {
    if (!state.dream?.agentId) return;
  } else if (target === "agent-llm") {
    if ($("agent-editor").hidden || state.agentForm.busy) return;
  } else if (defaultSelection ? (!state.settingsOriginal || state.settingsBusy) : (!state.chatId || state.busy)) return;
  const previousStt = state.modelTarget.endsWith("-stt");
  state.modelTarget = target;
  state.modelProviders = null;
  state.modelProvider = null;
  state.modelCatalog = null;
  state.modelCatalogProvider = null;
  $("model-search").value = "";
  $("model-error").textContent = "";
  $("model-zdr").checked = Boolean(state.user?.require_zdr);
  state.modelFavoritesOnly = false;  // the filter is off whenever the selector opens; favorites still sort first
  $("model-favorites-only").hidden = stt;
  $("model-filters").hidden = stt;
  if (stt) $("model-sort").value = "prompt-asc";
  else if (previousStt) $("model-sort").value = "favorites";
  for (const option of $("model-sort").querySelectorAll("option")) {
    option.disabled = stt && (option.value === "favorites" || option.value.startsWith("completion-"));
  }
  $("model-title").textContent = tr(stt ? "Speech-to-text models" : "Models");
  let subtitle = "Choose a model for this chat.";
  if (target === "dream-llm") subtitle = "Choose the model for this agent's dreams. Use user default leaves the field empty.";
  else if (target === "agent-llm") subtitle = "Choose the default model for this agent. Use user default clears the field.";
  else if (defaultSelection) {
    subtitle = stt ? "Choose the user's default STT model. This does not set a chat override." : "Choose the user's default LLM model. This does not set a chat override.";
  }
  $("model-subtitle").textContent = tr(subtitle);
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
$("model-refresh").addEventListener("click", () => refreshModels(false, true));
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
  $("new-disposable-chat").disabled = state.modelBusy || state.settingsBusy;
  $("settings-open").disabled = state.settingsBusy;
  for (const button of $("chat-list").querySelectorAll(".del")) {
    button.disabled = isGenerating(button.dataset.agent, button.dataset.chat) || state.modelBusy;
  }
  for (const li of $("chat-list").querySelectorAll("li")) {
    const del = li.querySelector(".del");
    li.classList.toggle("generating", Boolean(del) && isGenerating(del.dataset.agent, del.dataset.chat));
  }
  setComposer(Boolean(state.chatId));
  updateEditButtons();
  updateModelControls();
  if ($("model-dialog").open) renderModelList();
  $("send").innerHTML = state.busy ? ICONS.steer : ICONS.send;
  $("send").title = state.busy ? "Steer" : "Send";
  $("send").setAttribute("aria-label", state.busy ? "Steer" : "Send");
  renderAgentBanner();
}

function resetStreams() {
  state.session++;
  for (const request of state.requests) request.controller.abort();
  state.requests.clear();
  state.views.clear();
  state.usages.clear();
  resetApprovals();
  state.view = null;
  state.latest = null;
  state.sendQueue = Promise.resolve();
  state.preferences = null;
  state.preferencesRequest++;
  state.settingsOriginal = null;
  state.settingsBusy = false;
  state.settingsModelDraft = null;
  state.modelOverride = undefined;
  state.modelSource = undefined;
  state.agentDefaultModel = null;
  state.reasoningEffort = null;
  state.modelTarget = "chat-llm";
  state.modelCatalogKind = "llm";
  state.chatModelRevision++;
  $("settings-dialog").close();
  $("settings-save").disabled = false;
  $("messages").classList.toggle("hide-thinking", false);
  state.modelCatalog = null;
  state.modelCatalogZdr = null;
  state.modelProviders = null;
  state.modelProvider = null;
  state.modelCatalogProvider = null;
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
    if (m.role === "user" && m.index != null) setEditable(view.el, m.index);
    if (m.reasoning) view.thinking(m.reasoning);
    for (const action of m.actions ?? []) view.action(action);
    if (m.associative_skipped) view.memory({ state: "skipped" });
    else if (m.associative_matches != null) view.memory({ state: "done", matches: m.associative_matches });
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

// ------------------------------------------------------------------ editing earlier messages

// Marks a user bubble as editable: `index` is the turn's position in the stored chat file.
function setEditable(el, index) {
  el.dataset.index = index;
  if (el.querySelector(":scope > .msg-edit")) return;
  const button = document.createElement("button");
  button.type = "button";
  button.className = "msg-edit";
  button.innerHTML = ICONS.pencil;
  button.title = tr("Edit message");
  button.setAttribute("aria-label", tr("Edit message"));
  button.disabled = state.busy || state.modelBusy;
  button.addEventListener("click", () => startEdit(el));
  el.prepend(button);
}

// While a reply streams the edit buttons are disabled (the server could cancel it, but a disabled button is simpler).
function updateEditButtons() {
  for (const button of document.querySelectorAll(".msg-edit")) button.disabled = state.busy || state.modelBusy;
}

// Number of stored messages shown after a bubble (RECALL's own command replies are not stored).
function messagesAfter(el) {
  let count = 0;
  for (let next = el.nextElementSibling; next; next = next.nextElementSibling) {
    if (next.classList.contains("msg") && !next.classList.contains("command")) count++;
  }
  return count;
}

function startEdit(el) {
  if (state.busy || state.modelBusy || el.classList.contains("editing")) return;
  const body = el.querySelector(":scope > .body");
  const index = Number(el.dataset.index);
  const form = document.createElement("div");
  form.className = "msg-edit-form";
  const field = document.createElement("textarea");
  field.value = body.textContent;
  field.rows = Math.min(10, Math.max(2, field.value.split("\n").length));
  field.setAttribute("aria-label", tr("Edit message text"));
  const after = messagesAfter(el);
  const notice = document.createElement("div");
  notice.className = "msg-edit-notice";
  notice.setAttribute("role", "status");
  notice.textContent = after === 0 ? "" : after === 1
    ? tr("This removes the message after it.") : tr("This removes the {count} messages after it.", { count: after });
  const actions = document.createElement("div");
  actions.className = "msg-edit-actions";
  const save = document.createElement("button");
  save.type = "button";
  save.textContent = tr("Save and resend");
  const cancel = document.createElement("button");
  cancel.type = "button";
  cancel.className = "secondary";
  cancel.textContent = tr("Cancel editing");
  actions.append(save, cancel);
  form.append(field, notice, actions);
  const close = () => {
    form.remove();
    body.hidden = false;
    el.classList.remove("editing");
    el.querySelector(":scope > .msg-edit")?.focus();
  };
  const submit = () => {
    const text = field.value.trim();
    const attachments = [...el.querySelectorAll(".message-attachments .attachment-name")].map((n) => ({ name: n.textContent }));
    if (!text && !attachments.length) return;
    if (state.busy || state.modelBusy) return;
    dispatchMessage(text, [], { index, el, shown: attachments });
  };
  save.addEventListener("click", submit);
  cancel.addEventListener("click", close);
  field.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      e.preventDefault();
      close();
    } else if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      submit();
    }
  });
  el.classList.add("editing");
  body.hidden = true;
  body.after(form);
  field.focus();
  field.setSelectionRange(field.value.length, field.value.length);
}

// Once a reply is stored, give the bubbles added live their stored index so they can be edited too.
async function syncEditIndexes(request) {
  const data = await api(chatsUrl(`/${request.chatId}`));
  if (!isCurrent(request) || isGenerating(request.agentId, request.chatId)) return;
  const bubbles = [...state.view.querySelectorAll(":scope > .msg.user, :scope > .msg.assistant")]
    .filter((el) => !el.classList.contains("error-only"));
  if (bubbles.length !== data.messages.length) return;
  bubbles.forEach((el, position) => {
    const m = data.messages[position];
    if (m.role === "user" && el.classList.contains("user")) setEditable(el, m.index);
  });
}

function showUsage(report) {
  $("usage").hidden = false;
  $("usage").querySelector("summary").textContent = report.split("\n")[0];
  $("usage").querySelector("pre").textContent = report;
}

// ------------------------------------------------------------------ terminal approvals

// "Dangerous command" for an action the policy flags, "Approval required" for one the agent's approvals setting asks about.
function approvalTitle(approval) {
  return approval?.risk === "dangerous" ? tr("Dangerous command") : tr("Approval required");
}

// Seconds left as m:ss; never negative.
function formatCountdown(seconds) {
  const total = Math.max(0, Math.ceil(Number(seconds) || 0));
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
}

// Whole seconds until `expiresAt` (epoch seconds) at `nowMs` (epoch milliseconds); 0 once it has passed.
function approvalRemaining(expiresAt, nowMs) {
  const left = Math.ceil(Number(expiresAt) - nowMs / 1000);
  return Number.isFinite(left) ? Math.max(0, left) : 0;
}

// The outcome kind an `approval_resolved` event shows: the reason (timeout, cancel, shutdown) wins over the decision.
function approvalOutcome(decision, reason) {
  if (reason === "timed out") return "timed_out";
  if (reason === "cancelled" || reason === "service shutdown") return "cancelled";
  return decision === "approved" ? "approved" : "denied";
}

function approvalOutcomeLabel(kind) {
  switch (kind) {
    case "approved": return tr("Approved");
    case "denied": return tr("Denied");
    case "timed_out": return tr("Timed out");
    case "cancelled": return tr("Cancelled");
    case "expired": return tr("Expired");
    default: return tr("Resolved");
  }
}

// One line for a settled card: "Approved: terminal [local] first 60 chars of the command".
function approvalSummary(approval, kind) {
  const command = String(approval?.command ?? "").replace(/\s+/g, " ").trim();
  const short = command.length > 60 ? `${command.slice(0, 60)}…` : command;
  const backend = approval?.backend ? ` [${approval.backend}]` : "";
  return `${approvalOutcomeLabel(kind)}: ${approval?.tool ?? ""}${backend} ${short}`.trim();
}

// Position of the tool step an approval belongs to: same tool and the same command (the pending list cuts commands short).
// `steps` is [{name, command}] in display order; -1 when none matches.
function approvalStepIndex(steps, approval) {
  const command = String(approval?.command ?? "");
  return steps.findIndex((step) => step.name === approval?.tool && command !== ""
    && (String(step.command ?? "") === command || String(step.command ?? "").startsWith(command)));
}

function approvalPendingLabel(count) {
  return count === 1
    ? tr("1 approval pending, jump to it")
    : tr("{count} approvals pending, jump to it", { count });
}

// Splits a message's text at the given offsets: segment i is raw[starts[i]] up to raw[starts[i + 1]].
function textSegments(raw, starts) {
  return starts.map((start, i) => raw.slice(start, i + 1 < starts.length ? starts[i + 1] : undefined));
}

function approvalInitialState() {
  return { phase: "pending", kind: null, error: null, decision: null };
}

function approvalActive(current) {
  return current.phase === "pending" || current.phase === "submitting";
}

// Card state machine. Phases: pending -> submitting -> resolved | expired; a failed answer goes back to pending.
// Actions: submit {decision}, answered {status}, failed {status, message}, resolved {decision, reason}, gone {expired}.
// The server's `approval_resolved` is the truth: it overrides an "expired" mark, but a settled card never changes again.
function approvalTransition(current, action) {
  const settled = current.phase === "resolved" || current.phase === "expired";
  switch (action.type) {
    case "submit":
      return current.phase === "pending" ? { phase: "submitting", kind: null, error: null, decision: action.decision } : current;
    case "resolved":
      if (current.phase === "resolved") return current;
      return { phase: "resolved", kind: approvalOutcome(action.decision, action.reason), error: null, decision: current.decision };
    case "answered":
      if (settled) return current;
      return { phase: "resolved", kind: approvalOutcome(action.status, null), error: null, decision: current.decision };
    case "failed":
      if (settled) return current;
      if (action.status === 404) return { phase: "expired", kind: "expired", error: null, decision: current.decision };
      return { phase: "pending", kind: null, error: action.message || "", decision: null };
    case "gone":
      if (settled) return current;
      return { phase: "resolved", kind: action.expired ? "timed_out" : "unknown", error: null, decision: current.decision };
    default:
      return current;
  }
}

const approvalEntries = new Map();
let approvalTimer = null;
let approvalTicks = 0;

function buildApprovalCard(entry) {
  const approval = entry.data;
  const make = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  };
  const card = make("div", `approval-card${approval.risk === "dangerous" ? " dangerous" : ""}`);
  card.setAttribute("role", "group");
  card.dataset.approvalId = approval.id;
  const title = make("span", "approval-title", approvalTitle(approval));
  title.id = `approval-title-${approval.id}`;
  card.setAttribute("aria-labelledby", title.id);
  const summary = make("button", "approval-summary");
  summary.type = "button";
  summary.hidden = true;
  summary.setAttribute("aria-expanded", "false");
  summary.addEventListener("click", () => {
    const open = card.classList.toggle("collapsed") === false;
    summary.setAttribute("aria-expanded", String(open));
  });
  const detail = make("div", "approval-detail");
  const countdown = make("span", "approval-countdown");
  countdown.setAttribute("role", "timer");
  countdown.title = tr("Time left");
  const head = make("div", "approval-head");
  head.append(title, make("span", "approval-tool", approval.tool ?? ""));
  if (approval.backend) head.append(make("span", "approval-backend", approval.backend));
  head.append(countdown);
  // focusable so a long command can be scrolled with the keyboard
  const command = make("pre", "approval-command", approval.command ?? "");
  command.tabIndex = 0;
  detail.append(head, command);
  card.append(summary, detail);
  for (const [label, value] of [[tr("Working directory"), approval.cwd], [tr("Reason"), approval.reason]]) {
    if (!value) continue;
    const row = make("p", "approval-meta");
    row.append(make("strong", null, `${label}: `), document.createTextNode(value));
    detail.append(row);
  }
  const approve = make("button", "primary", tr("Approve"));
  const deny = make("button", null, tr("Deny"));
  approve.type = deny.type = "button";
  approve.addEventListener("click", () => answerApproval(entry, "approve"));
  deny.addEventListener("click", () => answerApproval(entry, "deny"));
  const actions = make("div", "approval-actions");
  actions.append(approve, deny);
  const status = make("p", "approval-status");
  status.setAttribute("role", "status");
  const error = make("p", "approval-error");
  error.setAttribute("role", "alert");
  detail.append(actions, status, error);
  entry.nodes = { card, summary, countdown, actions, approve, deny, status, error };
  return card;
}

function renderApproval(entry) {
  const { phase, kind, error, decision } = entry.state;
  const { card, summary, countdown, actions, approve, deny, status, error: errorNode } = entry.nodes;
  const active = approvalActive(entry.state);
  card.classList.toggle("resolved", !active);
  summary.hidden = active;
  if (!active) {
    summary.textContent = approvalSummary(entry.data, kind);
    // a settled card folds up once so long histories stay compact; the user can unfold it
    if (!entry.folded) {
      entry.folded = true;
      card.classList.add("collapsed");
    }
  }
  updateApprovalIndicator();
  card.dataset.outcome = kind ?? "";
  actions.hidden = !active;
  approve.disabled = deny.disabled = phase === "submitting";
  countdown.hidden = !active;
  countdown.textContent = formatCountdown(approvalRemaining(entry.data.expires_at, Date.now()));
  if (phase === "submitting") {
    status.textContent = tr(decision === "deny" ? "Denying…" : "Approving…");
  } else if (kind) {
    status.replaceChildren(Object.assign(document.createElement("span"), {
      className: `approval-badge ${kind}`, textContent: approvalOutcomeLabel(kind),
    }));
  } else {
    status.textContent = "";
  }
  errorNode.textContent = phase === "expired" ? tr("This approval expired or was already answered.") : error ?? "";
}

function applyApproval(entry, action) {
  entry.state = approvalTransition(entry.state, action);
  renderApproval(entry);
}

// Draws the card for a pending approval once (events and the pending list may both report it); `place(node)` inserts it.
function showApproval(approval, place) {
  const existing = approvalEntries.get(approval.id);
  if (existing) return existing;
  const entry = {
    id: approval.id, createdAt: Date.now(), state: approvalInitialState(),
    data: { agent_id: state.agentId, chat_id: state.chatId, ...approval },
  };
  place(buildApprovalCard(entry), entry.data);
  approvalEntries.set(entry.id, entry);
  renderApproval(entry);
  if (!approvalTimer) approvalTimer = setInterval(approvalTimerTick, 1000);
  return entry;
}

function settleApproval({ id, decision, reason }) {
  const entry = approvalEntries.get(id);
  if (entry) applyApproval(entry, { type: "resolved", decision, reason });
}

async function answerApproval(entry, decision) {
  if (entry.state.phase !== "pending") return;
  applyApproval(entry, { type: "submit", decision });
  const { agent_id: agentId, chat_id: chatId, id } = entry.data;
  try {
    const result = await api(
      `/api/agents/${encodeURIComponent(agentId)}/chats/${encodeURIComponent(chatId)}/approvals/${encodeURIComponent(id)}`,
      { method: "POST", body: JSON.stringify({ decision }) },
    );
    applyApproval(entry, { type: "answered", status: result?.status });
  } catch (err) {
    applyApproval(entry, {
      type: "failed", status: err.status,
      message: tr("Could not send the answer: {error}", { error: err.message }),
    });
  }
}

// Once a second: countdowns; every third second: ask the server which approvals are still pending, which also
// settles cards whose resolution this page never saw (a reload, another client or a dropped stream).
function approvalTimerTick() {
  const chats = new Map();
  for (const entry of approvalEntries.values()) {
    if (!approvalActive(entry.state)) continue;
    entry.nodes.countdown.textContent = formatCountdown(approvalRemaining(entry.data.expires_at, Date.now()));
    chats.set(`${entry.data.agent_id}/${entry.data.chat_id}`, [entry.data.agent_id, entry.data.chat_id]);
  }
  if (!chats.size) {
    clearInterval(approvalTimer);
    approvalTimer = null;
    return;
  }
  approvalTicks++;
  updateApprovalIndicator();
  if (approvalTicks % 3 === 0) for (const [agentId, chatId] of chats.values()) syncApprovals(agentId, chatId);
}

// Restored cards go into the last assistant message, before the tool step they belong to, else at its end; never at the top.
function placeApprovalInView(node, approval) {
  if (!state.view) return;
  const bubbles = [...state.view.querySelectorAll(":scope > .msg.assistant")].filter((el) => !el.classList.contains("error-only"));
  const last = bubbles[bubbles.length - 1];
  const reply = last ? replyViews.get(last) : null;
  if (reply) reply.placeApproval(node, approval);
  else state.view.append(node);
  scrollDown();
}

const replyViews = new WeakMap();

// Small bar under the chat while any approval of this chat waits for an answer; it jumps to the first one.
function updateApprovalIndicator() {
  const pending = [...approvalEntries.values()].filter((entry) => approvalActive(entry.state)
    && entry.data.agent_id === state.agentId && entry.data.chat_id === state.chatId && entry.nodes?.card.isConnected);
  let bar = document.getElementById("approval-pending");
  if (!pending.length) {
    if (bar) bar.hidden = true;
    return;
  }
  if (!bar) {
    bar = document.createElement("button");
    bar.id = "approval-pending";
    bar.type = "button";
    bar.setAttribute("role", "status");
    bar.addEventListener("click", () => {
      const first = [...approvalEntries.values()].find((entry) => approvalActive(entry.state) && entry.nodes?.card.isConnected);
      first?.nodes.card.scrollIntoView({ block: "center", behavior: "smooth" });
    });
    $("messages").after(bar);
  }
  bar.hidden = false;
  const label = approvalPendingLabel(pending.length);
  if (bar.textContent !== label) bar.textContent = label;
}

// Loads a chat's pending approvals: draws the missing cards (current chat only) and settles cards the server no longer lists.
async function syncApprovals(agentId, chatId) {
  const session = state.session;
  const startedAt = Date.now();
  let pending;
  try {
    pending = await api(`/api/agents/${encodeURIComponent(agentId)}/chats/${encodeURIComponent(chatId)}/approvals`);
  } catch {
    return;
  }
  if (session !== state.session || !Array.isArray(pending)) return;
  const listed = new Set(pending.map((approval) => approval.id));
  if (state.agentId === agentId && state.chatId === chatId) {
    for (const approval of pending) showApproval({ agent_id: agentId, chat_id: chatId, ...approval }, placeApprovalInView);
  }
  for (const entry of approvalEntries.values()) {
    const data = entry.data;
    if (data.agent_id !== agentId || data.chat_id !== chatId || !approvalActive(entry.state)) continue;
    if (listed.has(entry.id) || entry.createdAt >= startedAt) continue;
    applyApproval(entry, { type: "gone", expired: approvalRemaining(data.expires_at, Date.now()) === 0 });
  }
}

// A chat view that is drawn again loses its cards: forget them so the pending list can draw them back.
function forgetApprovals(agentId, chatId) {
  for (const [id, entry] of approvalEntries) {
    if (entry.data.agent_id === agentId && entry.data.chat_id === chatId) approvalEntries.delete(id);
  }
  updateApprovalIndicator();
}

function resetApprovals() {
  approvalEntries.clear();
  clearInterval(approvalTimer);
  approvalTimer = null;
  updateApprovalIndicator();
}

// ------------------------------------------------------------------ terminal settings (agent form)

function terminalApprovalHint(value) {
  switch (value) {
    case "smart": return tr("Only dangerous actions wait for your approval; the rest run on their own.");
    case "off": return tr("Nothing asks. Everything runs without approval, except what the hardline always refuses.");
    default: return tr("Every command and file operation waits for your approval.");
  }
}

// Backend choices by name and type (never hosts or keys), with the user's description; a saved backend that no longer
// exists stays selectable so the error can be seen. `savedNames` are the backends the agent already uses.
function terminalBackendOptions(backends, savedNames) {
  const options = (backends ?? []).map((backend) => ({
    value: backend.name, label: `${backend.name} (${backend.type})`, description: backend.description ?? "",
  }));
  for (const saved of savedNames ?? []) {
    if (!options.some((option) => option.value === saved)) {
      options.push({ value: saved, label: tr("{name} (not defined)", { name: saved }), description: "" });
    }
  }
  return options;
}

// One row per backend of the user (plus saved ones that vanished): allowed or not, with its saved or default settings.
function terminalBackendRows(backends, saved) {
  const used = saved?.backends ?? {};
  return terminalBackendOptions(backends, Object.keys(used)).map((option) => {
    const entry = used[option.value];
    return {
      name: option.value, label: option.label, description: option.description, allowed: Boolean(entry),
      workdir: entry?.workdir ?? "~", approvals: entry?.approvals ?? "manual",
    };
  });
}

// The default backend among the allowed names: the preferred one when it is still allowed, else the first.
function terminalDefaultBackend(allowedNames, preferred) {
  return allowedNames.includes(preferred) ? preferred : allowedNames[0] ?? "";
}

// The `terminal` block to save: the form's fields while the group is enabled, else the saved block untouched.
// Per-backend fields the form does not edit (timeout_seconds) are kept from the saved block.
function buildTerminalConfig(saved, enabled, fields) {
  if (!enabled) return saved ?? null;
  const allowed = (fields.backends ?? []).filter((field) => field.allowed);
  if (!allowed.length) return null;
  const backends = {};
  for (const field of allowed) {
    backends[field.name] = {
      ...(saved?.backends?.[field.name] ?? {}),
      workdir: field.workdir.trim() || "~",
      approvals: field.approvals,
    };
  }
  const { backend: _old, workdir: _oldWorkdir, approvals: _oldApprovals, ...rest } = saved ?? {};
  return {
    ...rest,
    backends,
    default_backend: terminalDefaultBackend(allowed.map((field) => field.name), fields.defaultBackend),
    timeout_seconds: Number(fields.timeout),
    allow_in_dream: Boolean(fields.allowInDream),
  };
}

// Save errors about the terminal block (missing backend, local disabled, bad fields) belong next to it.
function isTerminalError(message) {
  return /terminal/i.test(String(message ?? ""));
}

function addMessage(role, parent = state.view ?? $("messages")) {
  const el = document.createElement("div");
  el.className = `msg ${role}`;
  if (role === "assistant") el.dataset.author = currentAgentName();
  else if (role === "command") el.dataset.author = "RECALL";
  let body = document.createElement("div");
  body.className = "body";
  let raw = "";
  // The reply is a timeline: text before a step (tool call, approval card) stays above it, later text opens a new body below it.
  const bodies = [body];
  const starts = [0];
  let thinking = null;
  let thinkingRaw = "";
  let memoryIndicator = null;
  let actionCount = 0;
  let attachmentBox = null;
  el.append(body);
  parent.append(el);
  function renderOne(target, text) {
    if (role === "assistant") {
      target.innerHTML = DOMPurify.sanitize(marked.parse(text, { breaks: true }));
      for (const a of target.querySelectorAll("a")) {
        a.target = "_blank";
        a.rel = "noopener noreferrer";
      }
    } else {
      target.textContent = text;
    }
  }
  function renderBody(all = false) {
    if (all) {
      const parts = textSegments(raw, starts);
      bodies.forEach((target, i) => renderOne(target, parts[i]));
    } else {
      renderOne(body, raw.slice(starts[starts.length - 1]));
    }
  }
  // Puts a step in the timeline at the current point: after the text so far, with fresh text below it.
  function addStep(node) {
    if (raw.length > starts[starts.length - 1]) {
      const next = document.createElement("div");
      next.className = "body";
      body.after(node, next);
      body = next;
      bodies.push(body);
      starts.push(raw.length);
    } else {
      el.insertBefore(node, body);
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
  const view = {
    el,
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
        const text = document.createElement("div");
        text.className = "thinking-text";
        thinking.append(text);
        if (memoryIndicator) memoryIndicator.after(thinking);
        else el.prepend(thinking);
      }
      // models end their reasoning with newlines; trimming keeps an empty line from showing under the block
      thinkingRaw += chunk;
      thinking.lastChild.textContent = thinkingRaw.trimEnd();
    },
    notice(text) {
      const note = document.createElement("div");
      note.className = "notice";
      note.setAttribute("role", "status");
      note.textContent = text;
      el.insertBefore(note, body);
    },
    memory({ state, matches = 0 }) {
      if (!memoryIndicator) {
        memoryIndicator = document.createElement("div");
        memoryIndicator.setAttribute("role", "status");
        el.prepend(memoryIndicator);  // always the first element of the bubble, above any thinking
      }
      const outcome = state === "done" ? (matches > 0 ? "found" : "none") : state;
      memoryIndicator.className = `memory-indicator ${outcome}`;
      memoryIndicator.dataset.state = state;
      if (outcome === "searching") memoryIndicator.textContent = tr("Associative memory: searching");
      else if (outcome === "skipped") memoryIndicator.textContent = tr("Associative memory: skipped");
      else if (outcome === "none") memoryIndicator.textContent = tr("Associative memory: no matches");
      else if (matches === 1) memoryIndicator.textContent = tr("Associative memory: {count} match", { count: matches });
      else memoryIndicator.textContent = tr("Associative memory: {count} matches", { count: matches });
    },
    settleMemory() {
      // a search still running when the reply ends, fails or is interrupted was not used
      if (memoryIndicator?.dataset.state === "searching") this.memory({ state: "skipped" });
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
      item.dataset.tool = a.name;
      item.dataset.command = String(a.arguments?.command ?? "");
      addStep(item);
    },
    approval(approval) {
      showApproval(approval, (node) => addStep(node));
    },
    // Restored pending card: before its tool step when one matches, else at the end of the message.
    placeApproval(node, approval) {
      const steps = [...el.querySelectorAll(":scope > details.action")];
      const index = approvalStepIndex(steps.map((step) => ({ name: step.dataset.tool, command: step.dataset.command })), approval);
      if (index >= 0) steps[index].before(node);
      else el.append(node);
    },
    interrupted(snapshot = null) {
      this.settleMemory();
      if (snapshot) {
        raw = snapshot.content;
        for (let i = starts.length - 1; i > 0; i--) if (starts[i] > raw.length) starts[i] = raw.length;
        renderBody(true);
        thinkingRaw = "";
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
    error(source) {
      this.settleMemory();
      // with nothing streamed yet the card replaces the reply bubble; otherwise it follows the partial reply
      const bare = !raw && !el.querySelector(".action, .thinking, .approval-card");
      if (bare) el.classList.add("error-only");
      const card = errorCard(source, () => {
        card.remove();
        if (bare) el.remove();
      });
      el.append(card);
    },
  };
  replyViews.set(el, view);
  return view;
}


async function runErrorAction(action) {
  if (action === "create-agent") {
    await showTab("agents");
    openNewAgent();
  } else if (action === "choose-model") {
    if (state.chatId && !state.busy) openModelPicker("chat-llm");
    else $("settings-open").click();
  } else if (action === "new-chat") {
    await newChat();
  } else if (action === "open-settings") {
    $("settings-open").click();
  }
}

// A styled, dismissable error block; `onDismiss` removes it (and any wrapper that is left empty).
function errorCard(source, onDismiss) {
  const { message, code } = normalizeError(source);
  const info = errorInfo(code);
  const card = document.createElement("div");
  card.className = "error-card";
  card.setAttribute("role", "alert");
  const icon = document.createElement("span");
  icon.className = "error-card-icon";
  icon.innerHTML = ICONS.alert;
  const main = document.createElement("div");
  main.className = "error-card-main";
  main.append(Object.assign(document.createElement("strong"), {
    className: "error-card-title", textContent: info?.title ?? tr("Something went wrong"),
  }));
  if (info?.hint) main.append(Object.assign(document.createElement("p"), { className: "error-card-hint", textContent: info.hint }));
  if (info?.command) main.append(Object.assign(document.createElement("code"), { className: "error-card-command", textContent: info.command }));
  if (message && message !== info?.title) {
    main.append(Object.assign(document.createElement("p"), {
      className: info ? "error-card-detail" : "error-card-text", textContent: message,
    }));
  }
  const actions = document.createElement("div");
  actions.className = "error-card-actions";
  if (info?.action) {
    const act = Object.assign(document.createElement("button"), { type: "button", className: "primary", textContent: info.label });
    act.addEventListener("click", () => runErrorAction(info.action));
    actions.append(act);
  }
  const dismiss = Object.assign(document.createElement("button"), { type: "button", className: "link", textContent: tr("Dismiss") });
  dismiss.addEventListener("click", () => (onDismiss ?? (() => card.remove()))());
  actions.append(dismiss);
  main.append(actions);
  card.append(icon, main);
  return card;
}

// Shown instead of the chat when the user has no agents at all.
function renderNoAgents() {
  const container = document.createElement("div");
  container.className = "chat-view";
  state.view = container;
  $("messages").replaceChildren(container);
  $("usage").hidden = true;
  const panel = document.createElement("section");
  panel.className = "empty-state";
  const create = Object.assign(document.createElement("button"), {
    type: "button", className: "primary", textContent: tr("Create an agent"),
  });
  create.addEventListener("click", () => runErrorAction("create-agent"));
  panel.append(
    Object.assign(document.createElement("h2"), { textContent: tr("You have no agents yet") }),
    Object.assign(document.createElement("p"), { textContent: tr("An agent is the assistant you chat with. Create one to start chatting.") }),
    create,
  );
  container.append(panel);
  setComposer(false);
}

function renderAgentsEmpty() {
  const none = Boolean(state.user) && !state.user.agents.length;
  $("agents-empty").textContent = tr(none
    ? "You have no agents yet. Use New agent to create your first one."
    : "Select an agent to edit it, or create a new one.");
}

function scrollDown() {
  const box = $("messages");
  box.scrollTop = box.scrollHeight;
}

const MAX_FILE_BYTES = 10 * 1024 * 1024;
const MAX_TOTAL_FILE_BYTES = 25 * 1024 * 1024;

function isPdf(file) {
  return file.type === "application/pdf" || /\.pdf$/i.test(file.name);
}

function attachmentModality(file) {
  if (file.type.startsWith("image/")) return "image";
  if (file.type.startsWith("video/")) return "video";
  if (file.type.startsWith("audio/")) return "audio";
  if (isPdf(file)) return "document";
  return null;
}

function readAttachment(file) {
  const pdf = isPdf(file);
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve({
      modality: attachmentModality(file),
      name: file.name,
      mime_type: pdf ? "application/pdf" : file.type,
      format: pdf ? "pdf" : file.type.split("/")[1].split(";")[0],
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
      if (!attachmentModality(file)) throw new Error(tr("{name}: only image, video, audio and PDF files are supported", { name: file.name }));
      if (file.size > MAX_FILE_BYTES) throw new Error(tr("{name}: maximum size is 10 MB", { name: file.name }));
      return file;
    });
    const total = state.attachments.reduce((sum, item) => sum + item.size, 0)
      + valid.reduce((sum, file) => sum + file.size, 0);
    if (total > MAX_TOTAL_FILE_BYTES) throw new Error(tr("Attachments exceed the 25 MB total limit"));
    state.attachments.push(...await Promise.all(valid.map(readAttachment)));
    renderPendingAttachments();
  } catch (err) {
    addMessage("assistant").error(err);
  }
});

$("composer").addEventListener("submit", async (e) => {
  e.preventDefault();
  const content = $("input").value.trim();
  if ((!content && !state.attachments.length) || !state.user || !state.chatId || state.modelBusy) return;
  const attachments = state.attachments.splice(0);
  renderPendingAttachments();
  $("input").value = "";
  await dispatchMessage(content, attachments);
});

// Sends a message and streams the reply below it. `edit` ({index, el, shown}) rewinds the chat to that
// stored user message first: its bubble and everything after it disappear and the new text takes its place.
async function dispatchMessage(content, attachments, edit = null) {
  const key = chatKey(state.agentId, state.chatId);
  state.views.set(key, state.view);
  if (edit) {
    while (edit.el.nextElementSibling) edit.el.nextElementSibling.remove();
    edit.el.remove();
    $("usage").hidden = true;
  }
  const userMessage = addMessage("user", state.view);
  userMessage.text(content);
  userMessage.attachments(edit ? edit.shown : attachments);
  // /dream commands are answered by RECALL itself (not stored, not the model): shown as a system reply
  const command = !edit && !attachments.length && DREAM_COMMAND.test(content);
  const reply = addMessage(command ? "command" : "assistant", state.view);
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
    await streamReply(content, attachments, request, edit?.index ?? null);
  } catch (err) {
    request.controller.abort();
    if (sameSession(request) && err.name !== "AbortError") {
      reply.error(err);
      // a rejected edit leaves the server chat as it was: show it again instead of the truncated view
      if (edit && !command) request.reloadAfter = true;
    }
  } finally {
    state.requests.delete(request);
    if (sameSession(request)) {
      syncApprovals(request.agentId, request.chatId);
      if (!isGenerating(request.agentId, request.chatId) && !isCurrent(request)) {
        state.views.delete(key);
        state.usages.delete(key);
      }
      updateBusy();
      if (!isGenerating(request.agentId, request.chatId)) {
        if (isCurrent(request)) $("input").focus();
        try {
          await refreshChats();
          if (isCurrent(request)) {
            if (request.reloadAfter) {
              forgetApprovals(request.agentId, request.chatId);
              renderMessages((await api(chatsUrl(`/${request.chatId}`))).messages);
              syncApprovals(request.agentId, request.chatId);
            }
            else await syncEditIndexes(request);
          }
        } catch (err) {
          if (isCurrent(request)) reply.error(err);
        }
      }
    }
  }
}

$("input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    $("composer").requestSubmit();
  }
});

async function streamReply(content, attachments, request, editIndex = null) {
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
        ...(editIndex == null ? {} : { edit_index: editIndex }),
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
    throw httpError(body, res.status);
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
  else if (event === "memory") reply.memory(payload);
  else if (event === "notice") reply.notice(payload);
  else if (event === "approval") reply.approval(payload);
  else if (event === "approval_resolved") settleApproval(payload);
  else if (event === "interrupted") reply.interrupted(payload);
  else if (event === "error") reply.error(payload);
  else if (event === "done") {
    reply.settleMemory();
    request.done = true;
  }
  else if (event === "usage" && request === state.latest) {
    state.usages.set(chatKey(request.agentId, request.chatId), payload);
    // the first line is the headline; the full breakdown stays folded until expanded
    if (isCurrent(request)) showUsage(payload);
  }
}

// ------------------------------------------------------------------ dream mode (agent page)

// Soul & memory, Dream and Dream sessions tabs of a saved agent. Every load is tagged with the
// agent it was made for, so a slow reply for a previous agent never fills the current page.
const DREAM_POLL_MS = 4000;
const TRACE_RESULT_CHARS = 4000;
// same grammar as the server's /dream parser: such messages are answered by RECALL, not the model
const DREAM_COMMAND = /^\/dream(?:@\w+)?(?:\s[\s\S]*)?$/i;
const numberFormat = new Intl.NumberFormat("en-US");

function dreamUrl(suffix) {
  return `/api/agents/${encodeURIComponent(state.dream.agentId)}${suffix}`;
}

function resetDream(agentId = null) {
  stopDreamPoll();
  state.dream = {
    agentId, token: (state.dream?.token ?? 0) + 1, settings: null, status: null,
    sessions: [], sessionId: null, loaded: new Set(), memoryLimits: {},
  };
}

// a check made after every await: same login, same agent page, same load generation
function dreamCurrent(session, token) {
  return session === state.session && token === state.dream.token;
}

function pad(value) {
  return String(value).padStart(2, "0");
}

// local "YYYY-MM-DD HH:MM", the format the /dream reply and the Telegram notification use
function formatDate(iso) {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

// sessions without a summary only have their id, which is the local start time
function sessionDate(session) {
  if (session.started_at) return formatDate(session.started_at);
  const match = /^(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})/.exec(session.id ?? "");
  return match ? `${match[1]}-${match[2]}-${match[3]} ${match[4]}:${match[5]}` : session.id;
}

function formatDuration(seconds) {
  if (seconds == null) return "—";
  const total = Math.round(seconds);
  if (total < 60) return `${total} s`;
  const minutes = Math.floor(total / 60);
  if (minutes < 60) return `${minutes} min ${pad(total % 60)} s`;
  return `${Math.floor(minutes / 60)} h ${pad(minutes % 60)} min`;
}

function formatCount(value) {
  return value == null ? "—" : numberFormat.format(value);
}

function dreamStatusLabel(status) {
  const labels = {
    completed: tr("completed"), budget_exhausted: tr("out of budget"), failed: tr("failed"),
    cancelled: tr("cancelled"), timed_out: tr("timed out"), running: tr("running"), interrupted: tr("interrupted"),
  };
  return labels[status] ?? status ?? "—";
}

function dreamTriggerLabel(trigger) {
  const labels = {
    web: tr("web UI"), command: tr("/dream command"), schedule: tr("schedule"), manual: tr("manual"),
  };
  return labels[trigger] ?? trigger ?? "—";
}

function element(tag, props = {}, ...children) {
  const node = Object.assign(document.createElement(tag), props);
  node.append(...children);
  return node;
}

function descriptionList(rows) {
  const list = document.createElement("dl");
  for (const [term, value] of rows) {
    list.append(element("dt", { textContent: term }), element("dd", {}, value));
  }
  return list;
}

function markdown(text) {
  const body = element("div", { className: "body" });
  body.innerHTML = DOMPurify.sanitize(marked.parse(text ?? "", { breaks: true }));
  for (const link of body.querySelectorAll("a")) {
    link.target = "_blank";
    link.rel = "noopener noreferrer";
  }
  return body;
}

function setDreamHash(path) {
  const target = path ? `#${path}` : `${location.pathname}${location.search}`;
  if (location.hash !== (path ? `#${path}` : "")) history.replaceState(null, "", target);
}

function agentHash(panel = null, sessionId = null) {
  const id = encodeURIComponent(state.agentForm.originalId ?? "");
  if (!state.agentForm.originalId) return "agents";
  if (sessionId) return `agents/${id}/dreams/${encodeURIComponent(sessionId)}`;
  const suffix = { soul: "/soul", dream: "/dream", sessions: "/dreams" }[panel] ?? "";
  return `agents/${id}${suffix}`;
}

// Loads what a panel shows the first time it is opened for the current agent.
async function loadDreamPanel(panel) {
  const agentId = state.agentForm.originalId;
  if (!agentId) return;
  if (state.dream.agentId !== agentId) resetDream(agentId);
  if (panel === "soul" && !state.dream.loaded.has("soul")) {
    state.dream.loaded.add("soul");
    await Promise.all([loadSoul(), loadSoulHistory(), loadMemory("memory"), loadMemory("user")]);
  } else if (panel === "dream" && !state.dream.loaded.has("dream")) {
    state.dream.loaded.add("dream");
    await loadDreamSettings();
  } else if (panel === "sessions") {
    // the list and status may have changed in the background: reload them on every visit
    await Promise.all([refreshDreamStatus(), refreshDreamSessions()]);
  }
}

// ---- SOUL.md and history

function renderCount(id, chars, limit) {
  $(id).textContent = tr("{chars} / {limit} characters", { chars: formatCount(chars), limit: formatCount(limit) });
  $(id).classList.toggle("over", chars > limit);
}

async function loadSoul() {
  const session = state.session;
  const token = state.dream.token;
  $("soul-error").textContent = "";
  try {
    const soul = await api(dreamUrl("/soul"));
    if (!dreamCurrent(session, token)) return;
    $("soul-content").value = soul.content;
    $("soul-content").dataset.max = soul.max_chars;
    $("soul-content").maxLength = soul.max_chars;
    state.defaultSoul = soul.default || "";
    renderCount("soul-count", soul.chars, soul.max_chars);
    updateSoulDefault();
  } catch (err) {
    if (dreamCurrent(session, token)) $("soul-error").textContent = err.message;
  }
}

// the default template is offered only while the editor is empty; inserting it does not save
function updateSoulDefault() {
  $("soul-default").hidden = Boolean($("soul-content").value.trim()) || !state.defaultSoul;
}

async function loadSoulHistory() {
  const session = state.session;
  const token = state.dream.token;
  try {
    const versions = await api(dreamUrl("/soul/history"));
    if (!dreamCurrent(session, token)) return;
    $("soul-history-title").textContent = tr("SOUL history ({count})", { count: versions.length });
    if (!versions.length) {
      $("soul-history-list").replaceChildren(element("li", { className: "model-note", textContent: tr("No saved versions yet.") }));
      return;
    }
    $("soul-history-list").replaceChildren(...versions.map((version) => {
      const view = element("button", { type: "button", textContent: tr("View") });
      view.addEventListener("click", () => viewSoulVersion(version));
      const restore = element("button", { type: "button", textContent: tr("Restore") });
      restore.addEventListener("click", () => restoreSoulVersion(version));
      return element("li", {},
        element("span", { textContent: `${formatDate(version.saved_at)} · ${tr("{count} characters", { count: formatCount(version.chars) })}` }),
        element("span", { className: "agent-actions" }, view, restore),
      );
    }));
  } catch (err) {
    if (dreamCurrent(session, token)) $("soul-error").textContent = err.message;
  }
}

async function viewSoulVersion(version) {
  const session = state.session;
  const token = state.dream.token;
  try {
    const saved = await api(dreamUrl(`/soul/history/${encodeURIComponent(version.version)}`));
    if (!dreamCurrent(session, token)) return;
    $("soul-version-title").textContent = tr("Version saved {date}", { date: formatDate(version.saved_at) });
    $("soul-version-content").textContent = saved.content;
    $("soul-version").hidden = false;
  } catch (err) {
    if (dreamCurrent(session, token)) $("soul-error").textContent = err.message;
  }
}

async function restoreSoulVersion(version) {
  if (!window.confirm(tr("Restore the SOUL.md version saved {date}? The current text is saved to the history first.", { date: formatDate(version.saved_at) }))) return;
  const session = state.session;
  const token = state.dream.token;
  $("soul-error").textContent = "";
  $("soul-status").textContent = "";
  try {
    await api(dreamUrl(`/soul/history/${encodeURIComponent(version.version)}/restore`), { method: "POST" });
    if (!dreamCurrent(session, token)) return;
    $("soul-version").hidden = true;
    await Promise.all([loadSoul(), loadSoulHistory()]);
    if (dreamCurrent(session, token)) $("soul-status").textContent = tr("Version restored.");
  } catch (err) {
    if (dreamCurrent(session, token)) $("soul-error").textContent = err.message;
  }
}

async function saveSoul() {
  const session = state.session;
  const token = state.dream.token;
  $("soul-error").textContent = "";
  $("soul-status").textContent = "";
  $("soul-save").disabled = true;
  try {
    const soul = await api(dreamUrl("/soul"), {
      method: "PUT", body: JSON.stringify({ content: $("soul-content").value }),
    });
    if (!dreamCurrent(session, token)) return;
    $("soul-content").value = soul.content;
    renderCount("soul-count", soul.chars, soul.max_chars);
    updateSoulDefault();
    $("soul-status").textContent = tr("Saved. New chats use it.");
    await loadSoulHistory();
  } catch (err) {
    if (dreamCurrent(session, token)) $("soul-error").textContent = err.message;
  } finally {
    $("soul-save").disabled = false;
  }
}

// ---- MEMORY.md and USER.md

const MEMORY_IDS = { memory: "memory", user: "user-memory" };

async function loadMemory(target) {
  const session = state.session;
  const token = state.dream.token;
  const id = MEMORY_IDS[target];
  $(`${id}-error`).textContent = "";
  try {
    const memory = await api(dreamUrl(`/memories/${target}`));
    if (!dreamCurrent(session, token)) return;
    state.dream.memoryLimits[target] = memory.limit;
    $(`${id}-content`).value = memory.content;
    renderCount(`${id}-count`, memory.chars, memory.limit);
  } catch (err) {
    if (dreamCurrent(session, token)) $(`${id}-error`).textContent = err.message;
  }
}

async function saveMemory(target) {
  const session = state.session;
  const token = state.dream.token;
  const id = MEMORY_IDS[target];
  $(`${id}-error`).textContent = "";
  $(`${id}-status`).textContent = "";
  $(`${id}-save`).disabled = true;
  try {
    const memory = await api(dreamUrl(`/memories/${target}`), {
      method: "PUT", body: JSON.stringify({ content: $(`${id}-content`).value }),
    });
    if (!dreamCurrent(session, token)) return;
    $(`${id}-content`).value = memory.content;
    renderCount(`${id}-count`, memory.chars, memory.limit);
    $(`${id}-status`).textContent = tr("Saved. New chats use it.");
  } catch (err) {
    if (dreamCurrent(session, token)) $(`${id}-error`).textContent = err.message;
  } finally {
    $(`${id}-save`).disabled = false;
  }
}

// ---- dream settings

function dreamScheduleMode() {
  return document.querySelector('input[name="dream-mode"]:checked')?.value ?? "manual";
}

function syncDreamScheduleInputs() {
  const mode = dreamScheduleMode();
  $("dream-every-hours").disabled = mode !== "every";
  $("dream-daily-at").disabled = mode !== "daily";
  $("dream-enabled").disabled = mode === "manual";
  $("dream-every-hours").required = mode === "every";
  $("dream-daily-at").required = mode === "daily";
}

function fillDreamSettings(settings) {
  const dream = settings.dream;
  const mode = dream.every_hours != null ? "every" : dream.daily_at != null ? "daily" : "manual";
  $(`dream-mode-${mode}`).checked = true;
  $("dream-every-hours").value = dream.every_hours ?? "";
  $("dream-daily-at").value = dream.daily_at ?? "";
  $("dream-enabled").checked = dream.enabled;
  $("dream-model").value = dream.model ?? "";
  $("dream-effort").value = dream.reasoning_effort ?? "";
  $("dream-max-steps").value = dream.max_steps;
  $("dream-max-tokens").value = dream.max_tokens;
  $("dream-max-minutes").value = dream.max_minutes;
  $("dream-tool-web").checked = dream.tools.includes("web");
  $("dream-tool-folders").checked = dream.tools.includes("folders");
  $("dream-prompt").value = dream.prompt;
  $("dream-prompt").placeholder = settings.default_prompt;
  $("dream-notify").checked = dream.notify_telegram;
  $("dream-model-browse").disabled = !state.user;
  syncDreamScheduleInputs();
}

async function loadDreamSettings() {
  const session = state.session;
  const token = state.dream.token;
  $("dream-error").textContent = "";
  try {
    const settings = await api(dreamUrl("/dream/settings"));
    if (!dreamCurrent(session, token)) return;
    state.dream.settings = settings;
    fillDreamSettings(settings);
  } catch (err) {
    if (dreamCurrent(session, token)) $("dream-error").textContent = err.message;
  }
}

function dreamSettingsBody() {
  const mode = dreamScheduleMode();
  return {
    dream: {
      enabled: mode !== "manual" && $("dream-enabled").checked,
      every_hours: mode === "every" ? Number($("dream-every-hours").value) : null,
      daily_at: mode === "daily" ? $("dream-daily-at").value : null,
      model: $("dream-model").value.trim() || null,
      reasoning_effort: $("dream-effort").value || null,
      max_steps: Number($("dream-max-steps").value),
      max_tokens: Number($("dream-max-tokens").value),
      max_minutes: Number($("dream-max-minutes").value),
      tools: ["web", "folders"].filter((tool) => $(`dream-tool-${tool}`).checked),
      prompt: $("dream-prompt").value.trim() ? $("dream-prompt").value : "",
      notify_telegram: $("dream-notify").checked,
    },
  };
}

async function saveDreamSettings(event) {
  event.preventDefault();
  const session = state.session;
  const token = state.dream.token;
  const agentId = state.dream.agentId;
  $("dream-error").textContent = "";
  $("dream-status-text").textContent = "";
  $("dream-save").disabled = true;
  try {
    const settings = await api(dreamUrl("/dream/settings"), {
      method: "PUT", body: JSON.stringify(dreamSettingsBody()),
    });
    if (!dreamCurrent(session, token)) return;
    state.dream.settings = settings;
    fillDreamSettings(settings);
    // keep the agent form's copy current, so saving the agent later does not revert these fields
    const agent = state.agentConfigs.find((item) => item.id === agentId);
    if (agent) Object.assign(agent, { dream: settings.dream });
    // the memory limits shown in the Soul & memory tab may have changed
    state.dream.loaded.delete("soul");
    $("dream-status-text").textContent = tr("Dream settings saved.");
    await refreshDreamStatus();
    const next = state.dream.status?.schedule.next_run;
    if (dreamCurrent(session, token) && next) {
      $("dream-status-text").textContent += ` ${tr("Next run: {date}.", { date: formatDate(next) })}`;
    }
  } catch (err) {
    if (dreamCurrent(session, token)) $("dream-error").textContent = err.message;
  } finally {
    $("dream-save").disabled = false;
  }
}

// ---- status, start and polling

function scheduleText(schedule) {
  if (schedule.every_hours == null && schedule.daily_at == null) return tr("Manual only");
  const plan = schedule.every_hours != null
    ? tr("Every {hours} h", { hours: schedule.every_hours })
    : tr("Daily at {time}", { time: schedule.daily_at });
  return schedule.enabled ? plan : tr("{plan} (schedule disabled)", { plan });
}

function renderDreamStatus(status) {
  const last = status.last_session;
  let lastValue = tr("No dream sessions yet.");
  if (last) {
    const open = element("button", { type: "button", className: "link", textContent: `${sessionDate(last)} · ${dreamStatusLabel(last.status)}` });
    open.addEventListener("click", () => openDreamSession(last.id));
    lastValue = element("span", {}, open);
    const summary = (last.summary ?? "").split(/\s+/).join(" ").trim();
    if (summary) lastValue.append(element("span", { className: "dream-last-summary", textContent: summary.length > 300 ? `${summary.slice(0, 299)}…` : summary }));
  }
  let running = status.running
    ? tr("Running since {date}", { date: formatDate(status.last_started_at) })
    : tr("Not running");
  if (status.running && status.cancelling) running += ` · ${tr("cancelling…")}`;
  $("dream-status-list").replaceChildren(...descriptionList([
    [tr("State"), element("span", { className: status.running ? "dream-running" : "", textContent: running })],
    [tr("Last session"), lastValue],
    [tr("Schedule"), scheduleText(status.schedule)],
    [tr("Next run"), status.schedule.next_run ? formatDate(status.schedule.next_run) : "—"],
    [tr("Pending chats"), formatCount(status.pending_chats)],
  ]).childNodes);
  $("dream-start").disabled = status.running;
  $("dream-start").textContent = tr(status.running ? "Dream running…" : "Start dream");
  $("dream-cancel").hidden = !status.running;
  $("dream-cancel").disabled = Boolean(status.cancelling);
}

async function refreshDreamStatus() {
  const session = state.session;
  const token = state.dream.token;
  try {
    const status = await api(dreamUrl("/dream/status"), { cache: "no-store" });
    if (!dreamCurrent(session, token)) return;
    $("dream-status-error").textContent = "";
    const wasRunning = state.dream.status?.running;
    state.dream.status = status;
    renderDreamStatus(status);
    if (status.running) startDreamPoll();
    else if (wasRunning) {
      // the session just ended: show its final summary and trace
      stopDreamPoll();
      await refreshDreamSessions();
      if (state.dream.sessionId) await openDreamSession(state.dream.sessionId, { quiet: true });
    }
  } catch (err) {
    if (dreamCurrent(session, token)) $("dream-status-error").textContent = err.message;
  }
}

async function startDream() {
  const session = state.session;
  const token = state.dream.token;
  $("dream-status-error").textContent = "";
  $("dream-start-status").textContent = "";
  $("dream-start").disabled = true;
  try {
    const result = await api(dreamUrl("/dream/start"), { method: "POST" });
    if (!dreamCurrent(session, token)) return;
    $("dream-start-status").textContent = {
      started: tr("Dream session started."),
      started_nothing_pending: tr("Nothing is pending; starting a dream session anyway."),
      already_running: tr("A dream session is already running for this agent."),
    }[result.status] ?? result.status;
    await Promise.all([refreshDreamStatus(), refreshDreamSessions()]);
    // the session's trace file may appear just after the reply: open it once it is listed
    for (let attempt = 0; result.session_id && attempt < 5 && dreamCurrent(session, token); attempt++) {
      if (state.dream.sessions.some((item) => item.id === result.session_id)) {
        if (!state.dream.sessionId) await openDreamSession(result.session_id);
        break;
      }
      await new Promise((resolve) => setTimeout(resolve, 500));
      await refreshDreamSessions();
    }
  } catch (err) {
    if (dreamCurrent(session, token)) {
      $("dream-status-error").textContent = err.message;
      $("dream-start").disabled = false;
    }
  }
}

async function cancelDream() {
  if (!window.confirm(tr("Cancel the running dream session? A tool call in progress finishes first; chats it has not marked reviewed stay pending."))) return;
  const session = state.session;
  const token = state.dream.token;
  $("dream-status-error").textContent = "";
  $("dream-cancel").disabled = true;
  try {
    const result = await api(dreamUrl("/dream/cancel"), { method: "POST" });
    if (!dreamCurrent(session, token)) return;
    $("dream-start-status").textContent = result.status === "not_running"
      ? tr("No dream session is running for this agent.")
      : tr("Cancelling the dream session: a running tool call finishes first, then the session stops.");
    await refreshDreamStatus();
  } catch (err) {
    if (dreamCurrent(session, token)) {
      $("dream-status-error").textContent = err.message;
      $("dream-cancel").disabled = false;
    }
  }
}

function startDreamPoll() {
  if (state.dream.poll) return;
  state.dream.poll = setInterval(async () => {
    if ($("agent-panel-sessions").hidden || $("agents-view").hidden) {
      stopDreamPoll();
      return;
    }
    await refreshDreamStatus();
    if (!state.dream.poll) return;
    await refreshDreamSessions();
    const open = state.dream.sessions.find((item) => item.id === state.dream.sessionId);
    if (open?.status === "running") await openDreamSession(open.id, { quiet: true });
  }, DREAM_POLL_MS);
}

function stopDreamPoll() {
  if (state.dream?.poll) clearInterval(state.dream.poll);
  if (state.dream) state.dream.poll = null;
}

// ---- sessions list and detail

async function refreshDreamSessions() {
  const session = state.session;
  const token = state.dream.token;
  try {
    const sessions = await api(dreamUrl("/dream/sessions"), { cache: "no-store" });
    if (!dreamCurrent(session, token)) return;
    state.dream.sessions = sessions;
    renderDreamSessions();
  } catch (err) {
    if (dreamCurrent(session, token)) $("dream-status-error").textContent = err.message;
  }
}

function renderDreamSessions() {
  const sessions = state.dream.sessions;
  $("dream-sessions-empty").hidden = sessions.length > 0;
  $("dream-sessions").hidden = sessions.length === 0;
  $("dream-sessions").tBodies[0].replaceChildren(...sessions.map((item) => {
    const open = element("button", { type: "button", className: "link", textContent: sessionDate(item) });
    open.addEventListener("click", () => openDreamSession(item.id));
    const row = element("tr", { className: item.id === state.dream.sessionId ? "active" : "" });
    row.append(
      element("td", {}, open),
      element("td", { textContent: item.trigger ? dreamTriggerLabel(item.trigger) : "—" }),
      element("td", {}, element("span", { className: `dream-badge ${item.status}`, textContent: dreamStatusLabel(item.status) })),
      element("td", { textContent: item.chats_reviewed ? formatCount(item.chats_reviewed.length) : "—" }),
      element("td", { textContent: formatDuration(item.duration_seconds) }),
      element("td", { textContent: formatCount(item.tokens?.total) }),
    );
    return row;
  }));
}

function showDreamList() {
  state.dream.sessionId = null;
  $("dream-session-detail").hidden = true;
  $("dream-sessions-view").hidden = false;
  renderDreamSessions();
  setDreamHash(agentHash("sessions"));
}

async function openDreamSession(sessionId, { quiet = false } = {}) {
  const session = state.session;
  const token = state.dream.token;
  if (!quiet) {
    state.dream.sessionId = sessionId;
    $("dream-session-body").replaceChildren(element("p", { className: "model-note", textContent: tr("Loading…") }));
    $("dream-sessions-view").hidden = true;
    $("dream-session-detail").hidden = false;
    setDreamHash(agentHash("sessions", sessionId));
  }
  try {
    const detail = await api(dreamUrl(`/dream/sessions/${encodeURIComponent(sessionId)}`), { cache: "no-store" });
    if (!dreamCurrent(session, token) || state.dream.sessionId !== sessionId) return;
    const listed = state.dream.sessions.find((item) => item.id === sessionId);
    const scroll = $("agents-view").scrollTop;
    $("dream-session-body").replaceChildren(...renderDreamSession(detail, listed?.status));
    if (quiet) $("agents-view").scrollTop = scroll;
  } catch (err) {
    if (dreamCurrent(session, token) && state.dream.sessionId === sessionId) {
      $("dream-session-body").replaceChildren(element("p", { className: "error", textContent: err.message }));
    }
  }
}

function renderDreamSession(detail, listedStatus) {
  const summary = detail.summary;
  const status = summary?.status ?? listedStatus ?? "running";
  const started = summary ?? { id: detail.id };
  const parts = [element("h4", {}, `${tr("Dream session")} ${sessionDate(started)} · `,
    element("span", { className: `dream-badge ${status}`, textContent: dreamStatusLabel(status) }))];
  if (!summary) {
    parts.push(element("p", { className: "model-note", textContent: tr(status === "running"
      ? "This session is still running; the trace below updates automatically."
      : "This session ended without a summary (the process stopped while it ran).") }));
  } else {
    const tokens = summary.tokens ?? {};
    parts.push(descriptionList([
      [tr("Trigger"), dreamTriggerLabel(summary.trigger)],
      [tr("Model"), summary.model ?? "—"],
      [tr("Duration"), formatDuration(summary.duration_seconds)],
      [tr("Steps"), formatCount(summary.steps)],
      [tr("Tool calls"), `${formatCount(summary.tool_calls)} (${tr("{count} failed", { count: formatCount(summary.errors) })})`],
      [tr("Tokens"), tr("{total} (input {input}, output {output})", {
        total: formatCount(tokens.total), input: formatCount(tokens.input), output: formatCount(tokens.output),
      })],
      [tr("Pending chats"), tr("{before} before, {after} after", {
        before: formatCount(summary.pending_before), after: formatCount(summary.pending_after),
      })],
      [tr("Chats reviewed"), summary.chats_reviewed?.length ? summary.chats_reviewed.join(", ") : "—"],
      ...(summary.timing ? [
        [tr("Time limit"), tr("{minutes} min, deadline {date}", {
          minutes: formatCount(summary.timing.max_minutes), date: formatDate(summary.timing.deadline_at),
        })],
        [tr("Wrap-up warning"), summary.timing.warning_at ? formatDate(summary.timing.warning_at) : "—"],
        [tr("Stop requested"), summary.timing.stop_requested_at ? formatDate(summary.timing.stop_requested_at) : "—"],
        [tr("Closed"), formatDate(summary.timing.closed_at)],
      ] : []),
    ]));
    if (summary.error) parts.push(element("p", { className: "error", textContent: summary.error }));
    parts.push(element("h5", { textContent: tr("Summary") }),
      summary.summary ? markdown(summary.summary) : element("p", { className: "model-note", textContent: tr("No summary.") }));
    parts.push(element("h5", { textContent: tr("Files changed") }), renderFilesChanged(summary));
  }
  parts.push(element("h5", { textContent: tr("Trace") }), renderTrace(detail.events));
  return parts;
}

function renderFilesChanged(summary) {
  const files = summary.files_changed ?? [];
  if (!files.length) return element("p", { className: "model-note", textContent: tr("No files changed.") });
  const changes = { created: tr("created"), modified: tr("modified"), deleted: tr("deleted") };
  const list = element("div", { className: "dream-files" });
  for (const file of files) {
    const diff = summary.diffs?.[file.path];
    const label = [element("code", { textContent: file.path }), ` · ${changes[file.change] ?? file.change}`];
    if (diff) {
      list.append(element("details", { className: "dream-file", open: files.length <= 3 },
        element("summary", {}, ...label), renderDiff(diff)));
    } else {
      list.append(element("div", { className: "dream-file" }, ...label));
    }
  }
  return list;
}

function renderDiff(diff) {
  const pre = element("pre", { className: "diff" });
  for (const line of diff.replace(/\n$/, "").split("\n")) {
    let kind = "";
    if (line.startsWith("+++") || line.startsWith("---")) kind = "diff-file";
    else if (line.startsWith("@@")) kind = "diff-hunk";
    else if (line.startsWith("+")) kind = "diff-add";
    else if (line.startsWith("-")) kind = "diff-del";
    pre.append(element("span", { className: kind, textContent: `${line}\n` }));
  }
  return pre;
}

// a <pre> cut to TRACE_RESULT_CHARS, with a button that shows the rest
function expandablePre(text) {
  const pre = element("pre", { textContent: text.length > TRACE_RESULT_CHARS ? `${text.slice(0, TRACE_RESULT_CHARS)}…` : text });
  if (text.length <= TRACE_RESULT_CHARS) return [pre];
  const more = element("button", { type: "button", className: "link", textContent: tr("Show all ({count} characters)", { count: formatCount(text.length) }) });
  more.addEventListener("click", () => {
    pre.textContent = text;
    more.remove();
  });
  return [pre, more];
}

function renderTrace(events) {
  const trace = element("div", { className: "dream-trace" });
  if (!events?.length) {
    trace.append(element("p", { className: "model-note", textContent: tr("The trace is empty.") }));
    return trace;
  }
  let step = null;
  for (const event of events) {
    if (event.step != null && event.step !== step && event.type !== "session_finished") {
      step = event.step;
      trace.append(element("div", { className: "trace-step", textContent: tr("Step {step}", { step }) }));
    }
    trace.append(renderTraceEvent(event));
  }
  return trace;
}

function renderTraceEvent(event) {
  const time = event.ts ? formatDate(event.ts).slice(11) : "";
  const note = (text) => element("div", { className: "trace-note", textContent: time ? `${time} · ${text}` : text });
  switch (event.type) {
    case "session_started":
      return note(tr("Session started ({trigger}) with {model}; {pending} pending chats; limits {steps} steps, {tokens} tokens, {minutes} min.", {
        trigger: dreamTriggerLabel(event.trigger), model: event.model ?? "—", pending: formatCount(event.pending),
        steps: formatCount(event.max_steps), tokens: formatCount(event.max_tokens), minutes: formatCount(event.max_minutes),
      }));
    case "usage":
      return note(tr("Tokens: input {input}, output {output}", {
        input: formatCount(event.input_tokens), output: formatCount(event.output_tokens),
      }));
    case "reasoning":
      return element("details", { className: "thinking" },
        element("summary", { textContent: tr("Reasoning") }), element("div", { className: "thinking-text", textContent: event.text ?? "" }));
    case "assistant":
      return element("div", { className: "trace-assistant" }, markdown(event.text));
    case "tool_call":
      return element("div", { className: "trace-call" },
        element("div", { className: "trace-call-name", textContent: `→ ${event.name}` }),
        ...expandablePre(JSON.stringify(event.arguments ?? {}, null, 2)));
    case "tool_result":
      return element("details", { className: `action trace-result${event.error ? " failed" : ""}` },
        element("summary", { textContent: `${event.error ? tr("Failed") : tr("Result")} · ${event.name} · ${tr("{count} characters", { count: formatCount((event.result ?? "").length) })}` }),
        ...expandablePre(event.result ?? ""));
    case "wrap_up_warning":
      return element("div", { className: "trace-stop" },
        element("div", { textContent: `${time ? `${time} · ` : ""}${tr("Wrap-up warning sent to the agent")}` }),
        ...expandablePre(event.text ?? ""));
    case "force_closed":
      return element("div", { className: "trace-stop", textContent: `${time ? `${time} · ` : ""}${{
        timed_out: tr("Time limit reached"), cancelled: tr("Cancel requested"),
      }[event.reason] ?? event.reason}: ${event.aborted_model_call
        ? tr("the model call in flight was aborted and its reply discarded.")
        : event.skipped_tool_calls
          ? tr("{count} remaining tool calls were skipped.", { count: formatCount(event.skipped_tool_calls) })
          : tr("the session was closed before the next model call.")}` });
    case "error":
      return element("div", { className: "trace-error error", textContent: `\u26A0 ${event.message ?? event.error ?? ""}` });
    case "session_finished":
      return note(tr("Session finished: {status}", { status: dreamStatusLabel(event.status) }) + (event.error ? ` — ${event.error}` : ""));
    default:
      return element("pre", { className: "trace-note", textContent: JSON.stringify(event, null, 2) });
  }
}

// ---- deep links: #agents/<agent id>[/soul|/dream|/dreams[/<session id>]]

// the link the page was opened with; kept until a login shows the app
let pendingRoute = location.hash;
const AGENT_ROUTE = /^#agents(?:\/([^/]+)(?:\/(soul|dream|dreams)(?:\/([^/]+))?)?)?$/;

async function routeFromHash(hash = location.hash) {
  const match = AGENT_ROUTE.exec(hash);
  if (!match || !state.user) return false;
  await showTab("agents");
  const agentId = match[1] && decodeURIComponent(match[1]);
  if (!agentId || !state.agentConfigs.some((agent) => agent.id === agentId)) return true;
  openAgent(agentId);
  const panel = { soul: "soul", dream: "dream", dreams: "sessions" }[match[2]] ?? "basic";
  await showAgentPanel(panel);
  if (match[3]) await openDreamSession(decodeURIComponent(match[3]));
  return true;
}

globalThis.addEventListener("hashchange", () => routeFromHash());

$("soul-content").addEventListener("input", () => {
  renderCount("soul-count", $("soul-content").value.length, Number($("soul-content").dataset.max || 15000));
});
$("soul-save").addEventListener("click", saveSoul);
$("soul-default").addEventListener("click", () => {
  $("soul-content").value = state.defaultSoul;
  renderCount("soul-count", state.defaultSoul.length, Number($("soul-content").dataset.max) || state.defaultSoul.length);
  updateSoulDefault();
  $("soul-status").textContent = tr("Template inserted. Save to apply it.");
});
$("soul-content").addEventListener("input", updateSoulDefault);
$("soul-version-close").addEventListener("click", () => { $("soul-version").hidden = true; });
for (const [target, id] of Object.entries(MEMORY_IDS)) {
  $(`${id}-content`).addEventListener("input", () => {
    renderCount(`${id}-count`, $(`${id}-content`).value.length, state.dream.memoryLimits[target] ?? 0);
  });
  $(`${id}-save`).addEventListener("click", () => saveMemory(target));
}
for (const radio of document.querySelectorAll('input[name="dream-mode"]')) {
  radio.addEventListener("change", syncDreamScheduleInputs);
}
// typing a value in a schedule field selects that schedule
$("dream-every-hours").closest("label").addEventListener("click", () => {
  if (!$("dream-mode-every").checked) { $("dream-mode-every").checked = true; syncDreamScheduleInputs(); }
});
$("dream-daily-at").closest("label").addEventListener("click", () => {
  if (!$("dream-mode-daily").checked) { $("dream-mode-daily").checked = true; syncDreamScheduleInputs(); }
});
$("dream-prompt-copy").addEventListener("click", () => {
  if ($("dream-prompt").value.trim() && !window.confirm(tr("Replace the current prompt with the default one?"))) return;
  $("dream-prompt").value = state.dream.settings?.default_prompt ?? "";
});
$("dream-prompt-reset").addEventListener("click", () => {
  if ($("dream-prompt").value.trim() && !window.confirm(tr("Clear the custom prompt and use the default one? Save to apply."))) return;
  $("dream-prompt").value = "";
});
$("dream-model-browse").addEventListener("click", () => openModelPicker("dream-llm"));
$("agent-model-browse").addEventListener("click", () => openModelPicker("agent-llm"));
$("agent-model-clear").addEventListener("click", () => { $("agent-model").value = ""; });
$("dream-form").addEventListener("submit", saveDreamSettings);
$("dream-start").addEventListener("click", startDream);
$("dream-cancel").addEventListener("click", cancelDream);
$("dream-session-back").addEventListener("click", showDreamList);

resetDream();

// ------------------------------------------------------------------ boot

api("/api/me").then(showApp, showLogin);
