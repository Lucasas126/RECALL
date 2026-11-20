const $ = (id) => document.getElementById(id);
// inline line icons (currentColor); constants only, never built from user data
const ICONS = {
  clock: "<svg class=\"icon\" viewBox=\"0 0 24 24\" aria-hidden=\"true\" focusable=\"false\"><circle cx=\"12\" cy=\"12\" r=\"9\"/><path d=\"M12 7v5l3 2\"/></svg>",
  alert: "<svg class=\"icon\" viewBox=\"0 0 24 24\" aria-hidden=\"true\" focusable=\"false\"><circle cx=\"12\" cy=\"12\" r=\"9\"/><path d=\"M12 7.5v5.5\"/><path d=\"M12 16.5h.01\"/></svg>",
  send: "<svg class=\"icon\" viewBox=\"0 0 24 24\" aria-hidden=\"true\" focusable=\"false\"><path d=\"M12 19V5\"/><path d=\"m5 12 7-7 7 7\"/></svg>",
  stop: "<svg class=\"icon\" viewBox=\"0 0 24 24\" aria-hidden=\"true\" focusable=\"false\"><rect x=\"6\" y=\"6\" width=\"12\" height=\"12\" rx=\"1.5\" fill=\"currentColor\"/></svg>",
  steer: "<svg class=\"icon\" viewBox=\"0 0 24 24\" aria-hidden=\"true\" focusable=\"false\"><polygon points=\"3 11 22 2 13 21 11 13 3 11\"/></svg>",
  pencil: "<svg class=\"icon\" viewBox=\"0 0 24 24\" aria-hidden=\"true\" focusable=\"false\"><path d=\"M12 20h9\"/><path d=\"M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z\"/></svg>",
  trash: "<svg class=\"icon\" viewBox=\"0 0 24 24\" aria-hidden=\"true\" focusable=\"false\"><path d=\"M3 6h18\"/><path d=\"M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6\"/><path d=\"M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2\"/></svg>",
  star: "<svg class=\"icon\" viewBox=\"0 0 24 24\" aria-hidden=\"true\" focusable=\"false\"><polygon points=\"12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2\"/></svg>",
  sun: "<svg class=\"icon\" viewBox=\"0 0 24 24\" aria-hidden=\"true\" focusable=\"false\"><circle cx=\"12\" cy=\"12\" r=\"4\"/><path d=\"M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41\"/></svg>",
  moon: "<svg class=\"icon\" viewBox=\"0 0 24 24\" aria-hidden=\"true\" focusable=\"false\"><path d=\"M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z\"/></svg>",
};
const state = {
  versionInfo: null, user: null, agentId: null, chatId: null, busy: false, session: 0,
  requests: new Set(), latest: null, sendQueue: Promise.resolve(),
  // message containers of chats with a reply in flight, so they keep updating while another chat is open
  view: null, views: new Map(), usages: new Map(),
  attachments: [],
  model: null, modelCatalog: null, modelCatalogZdr: null, modelFavorites: [],
  agentSwitch: 0, modelBusy: false, modelLoading: false, modelRequest: 0,
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
  translations: null, toolGroupCatalog: [], levels: null, adminUsers: [],
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

// The associative-memory dialog: the search query and the injected fragments (source, score, excerpt), all as
// text nodes. Returns null when there is nothing to show beyond the indicator's own text.
function memoryDetails(fragments, query) {
  const hits = Array.isArray(fragments) ? fragments : [];
  if (!hits.length && !query) return null;
  const body = document.createDocumentFragment();
  if (query) {
    body.append(Object.assign(document.createElement("p"), {
      className: "memory-query", textContent: tr("Search query: {query}", { query }),
    }));
  }
  if (hits.length) {
    const list = document.createElement("ol");
    list.className = "memory-fragments";
    for (const hit of hits) {
      const item = document.createElement("li");
      const source = document.createElement("div");
      source.className = "memory-fragment-source";
      source.textContent = hit.label ? `${hit.address} > ${hit.label}` : String(hit.address ?? "");
      if (typeof hit.score === "number") {
        source.append(Object.assign(document.createElement("span"), {
          className: "memory-fragment-score", textContent: ` · ${tr("score {score}", { score: hit.score.toFixed(2) })}`,
        }));
      }
      const excerpt = markdown(hit.excerpt ?? "");  // sanitized, like any message body
      excerpt.classList.add("memory-fragment-excerpt");
      item.append(source, excerpt);
      list.append(item);
    }
    body.append(list);
  }
  return body;
}

function openMemoryDialog(summary, fragments, query) {
  const body = memoryDetails(fragments, query);
  if (!body) return;
  $("assoc-title").textContent = summary;
  $("assoc-body").replaceChildren(body);
  const dialog = $("assoc-dialog");
  if (!dialog.open) dialog.showModal();
}

function tr(template, values = {}) {
  const text = state.preferences?.language === "es" ? state.translations[template] : template;
  if (typeof text !== "string") throw new Error(`Missing translation: ${template}`);
  return text.replace(/\{(\w+)\}/g, (_, key) => values[key]);
}

// ---- version line
// "RECALL v0.1.0", plus the build id (a short git sha) when the server reports one
function versionText(info, translate) {
  if (!info || !info.version) return "";
  return info.build
    ? translate("RECALL v{version} ({build})", { version: info.version, build: info.build })
    : translate("RECALL v{version}", { version: info.version });
}
// ---- end version line

function renderVersion() {
  const text = versionText(state.versionInfo, tr);
  for (const id of ["login-version", "settings-version"]) $(id).textContent = text;
}

// ---- settings helpers (pure: no DOM, no state; tested without a browser)
// The sections of the user settings dialog and the preferences each one holds (`connections` has none).
const SETTINGS_SECTIONS = {
  general: ["show_thinking", "show_agent_avatar", "language", "default_agent_id"],
  models: ["default_llm_model", "default_stt_model", "stt_language"],
  chats: ["associative_max_matches", "disposable_retention_days"],
  telegram: ["orchestrator", "core_agent_id"],
  connections: [],
};
// Saving null for these means "follow the default"; the others always hold a value.
const NULLABLE_PREFERENCES = new Set([
  "default_llm_model", "default_stt_model", "stt_language", "associative_max_matches", "disposable_retention_days",
  "orchestrator", "core_agent_id", "default_agent_id",
]);

// The agent new chats open with when none is picked: the saved default if the user still has it, else the first agent.
function defaultAgentId(preferences, agents) {
  const list = agents ?? [];
  const saved = preferences?.default_agent_id;
  if (saved && list.some((agent) => agent.id === saved)) return saved;
  return list.length ? list[0].id : null;
}

// True when the scroll position is within `slack` pixels of the bottom (the user is following the latest content).
function nearBottom(scrollHeight, scrollTop, clientHeight, slack = 30) {
  return scrollHeight - scrollTop - clientHeight <= slack;
}

// Which section is shown and which tab is pressed; an unknown name falls back to the first section.
function settingsSectionView(active) {
  const names = Object.keys(SETTINGS_SECTIONS);
  const current = names.includes(active) ? active : names[0];
  return Object.fromEntries(names.map((name) => [name, { hidden: name !== current, pressed: name === current }]));
}

// The section a preference key belongs to, or null.
function settingsSectionOf(key) {
  return Object.keys(SETTINGS_SECTIONS).find((name) => SETTINGS_SECTIONS[name].includes(key)) ?? null;
}

// Number of pending changes per section, for the dots on the tabs.
function sectionChangeCounts(changes) {
  const counts = Object.fromEntries(Object.keys(SETTINGS_SECTIONS).map((name) => [name, 0]));
  for (const key of Object.keys(changes)) {
    const section = settingsSectionOf(key);
    if (section) counts[section] += 1;
  }
  return counts;
}

// null, undefined and "" all mean "no value"; numbers and numeric strings compare as numbers.
function sameValue(a, b) {
  const empty = (value) => value === null || value === undefined || value === "";
  if (empty(a) || empty(b)) return empty(a) && empty(b);
  if (typeof a === "number" || typeof b === "number") return Number(a) === Number(b);
  return a === b;
}

function differsFromDefault(value, defaultValue) {
  return !sameValue(value, defaultValue);
}

// Reads typed text. Thousands separators ("500,000", "500.000", "500 000") are accepted; decimals are not.
// error: null | "format" | "min" | "max"; value is the number whenever the text is numeric.
function parseNumberInput(raw, spec = {}) {
  const text = String(raw ?? "").trim();
  if (text === "") return { empty: true, value: null, error: null };
  if (!/^\d+$/.test(text) && !/^\d{1,3}(?:[.,\s\u00a0\u202f]\d{3})+$/.test(text)) {
    return { empty: false, value: null, error: "format" };
  }
  const value = Number(text.replace(/[.,\s\u00a0\u202f]/g, ""));
  if (spec.min != null && value < spec.min) return { empty: false, value, error: "min" };
  if (spec.max != null && value > spec.max) return { empty: false, value, error: "max" };
  return { empty: false, value, error: null };
}

function clampNumber(value, spec = {}) {
  let result = value;
  if (spec.min != null && result < spec.min) result = spec.min;
  if (spec.max != null && result > spec.max) result = spec.max;
  return result;
}

// One step of the - / + buttons; an empty field starts at its start value, else the default, else the minimum.
function stepNumber(current, direction, spec = {}) {
  if (current === null || current === undefined) return clampNumber(spec.start ?? spec.default ?? spec.min ?? 0, spec);
  return clampNumber(current + direction * (spec.step || 1), spec);
}

function formatNumber(value, language = "en") {
  return new Intl.NumberFormat(language === "es" ? "es" : "en").format(value);
}

// "Allowed: 1–20", "Minimum: 1" or "" depending on the bounds the backend reports.
function rangeText(spec, translate, language = "en") {
  const fmt = (n) => formatNumber(n, language);
  if (spec.min != null && spec.max != null) return translate("Allowed: {min}–{max}", { min: fmt(spec.min), max: fmt(spec.max) });
  if (spec.min != null) return translate("Minimum: {min}", { min: fmt(spec.min) });
  if (spec.max != null) return translate("Maximum: {max}", { max: fmt(spec.max) });
  return "";
}

// What to send for one preference: undefined (unchanged), null (reset: follow the default) or the new value.
function preferenceChange(key, value, original, defaultValue) {
  if (sameValue(value, original)) return undefined;
  if (NULLABLE_PREFERENCES.has(key) && sameValue(value, defaultValue)) return null;
  return value;
}

// The PATCH body for the settings dialog: only the preferences whose value differs from what was loaded.
function settingsChanges(current, original, defaults) {
  const changes = {};
  for (const key of Object.keys(current)) {
    const change = preferenceChange(key, current[key], original[key], defaults[key]);
    if (change !== undefined) changes[key] = change;
  }
  return changes;
}
// ---- end settings helpers

// ---- number fields and setting rows (DOM)
// Every setting is a `.setting-row`: label, help, control, a muted "Default: X" line and a reset button that is
// enabled only while the value differs from the default. Number inputs (`input[data-number]`) become a text
// field with - / + buttons, a unit and inline validation.
const settingRows = [];
const numberSpecs = new Map(); // input id -> () => ({ default, min, max, step })

function uiLanguage() {
  return state.preferences?.language ?? "en";
}

function unitLabel(unit) {
  switch (unit) {
    case "matches": return tr("matches");
    case "days": return tr("days");
    case "steps": return tr("steps");
    case "tokens": return tr("tokens");
    case "minutes": return tr("minutes");
    case "seconds": return tr("seconds");
    case "messages": return tr("messages");
    case "rounds": return tr("rounds");
    case "percent": return "%";
    default: return "";
  }
}

function numberSpec(input) {
  const spec = numberSpecs.get(input.id)?.() ?? {};
  return { ...spec, step: Number(input.dataset.step) || 1, start: input.dataset.start ? Number(input.dataset.start) : undefined };
}

function numberMessageElement(input) {
  const field = input.closest(".num-field");
  let element = input.closest(".setting-row")?.querySelector(".setting-error");
  if (!element) {
    element = field.nextElementSibling?.classList.contains("setting-error") ? field.nextElementSibling : null;
    if (!element) {
      element = Object.assign(document.createElement("span"), { className: "setting-error error" });
      element.setAttribute("role", "alert");
      field.after(element);
    }
  }
  return element;
}

function showNumberMessage(input, text, blocking) {
  numberMessageElement(input).textContent = text;
  if (blocking) input.setAttribute("aria-invalid", "true");
  else input.removeAttribute("aria-invalid");
}

function numberErrorText(parsed, spec) {
  if (parsed.error === "format") return tr("Enter a whole number.");
  if (parsed.error === "min") return tr("Must be at least {min}.", { min: formatNumber(spec.min, uiLanguage()) });
  if (parsed.error === "max") return tr("Must be at most {max}.", { max: formatNumber(spec.max, uiLanguage()) });
  return "";
}

// null when empty, NaN when the text is not valid, else the number
function numberValue(id) {
  const input = $(id);
  const parsed = parseNumberInput(input.value, numberSpec(input));
  if (parsed.empty) return null;
  return parsed.error ? Number.NaN : parsed.value;
}

function setNumberValue(input, value) {
  input.value = value == null ? "" : formatNumber(value, uiLanguage());
  showNumberMessage(input, "", false);
}

function fireChange(element) {
  element.dispatchEvent(new Event("input", { bubbles: true }));
  element.dispatchEvent(new Event("change", { bubbles: true }));
}

// Enter or blur: out-of-range numbers are clamped (and the message says so); text that is no number stays and blocks saving.
function commitNumber(input) {
  const spec = numberSpec(input);
  const parsed = parseNumberInput(input.value, spec);
  if (parsed.empty) return showNumberMessage(input, "", false);
  if (parsed.error === "format") return showNumberMessage(input, numberErrorText(parsed, spec), true);
  if (parsed.error) {
    setNumberValue(input, clampNumber(parsed.value, spec));
    return showNumberMessage(input, tr("Adjusted to the allowed range ({min}–{max}).", {
      min: spec.min == null ? "…" : formatNumber(spec.min, uiLanguage()),
      max: spec.max == null ? "…" : formatNumber(spec.max, uiLanguage()),
    }), false);
  }
  setNumberValue(input, parsed.value);
}

function stepNumberField(input, direction) {
  const spec = numberSpec(input);
  const parsed = parseNumberInput(input.value, spec);
  const current = parsed.value;
  setNumberValue(input, stepNumber(current, direction, spec));
  fireChange(input);
}

function setupNumberFields() {
  for (const input of document.querySelectorAll("input[data-number]")) {
    const field = document.createElement("span");
    field.className = "num-field";
    const button = (direction, text) => {
      const element = Object.assign(document.createElement("button"), { type: "button", className: "num-step", textContent: text });
      element.dataset.direction = String(direction);
      element.addEventListener("click", () => stepNumberField(input, direction));
      return element;
    };
    const unit = Object.assign(document.createElement("span"), { className: "num-unit model-note" });
    input.before(field);
    field.append(button(-1, "−"), input, button(1, "+"), unit);
    input.addEventListener("input", () => {
      const parsed = parseNumberInput(input.value, numberSpec(input));
      showNumberMessage(input, numberErrorText(parsed, numberSpec(input)), parsed.error === "format");
    });
    input.addEventListener("change", () => commitNumber(input));
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter") { event.preventDefault(); commitNumber(input); fireChange(input); }
      else if (event.key === "ArrowUp" || event.key === "ArrowDown") {
        event.preventDefault();
        stepNumberField(input, event.key === "ArrowUp" ? 1 : -1);
      }
    });
  }
}

function refreshNumberFields() {
  for (const input of document.querySelectorAll("input[data-number]")) {
    const field = input.closest(".num-field");
    if (!field) continue;
    const spec = numberSpec(input);
    const [minus, plus] = field.querySelectorAll(".num-step");
    minus.disabled = plus.disabled = input.disabled;
    minus.setAttribute("aria-label", tr("Decrease"));
    plus.setAttribute("aria-label", tr("Increase"));
    field.querySelector(".num-unit").textContent = unitLabel(input.dataset.unit);
    input.placeholder = spec.default == null ? "" : formatNumber(spec.default, uiLanguage());
  }
}

// The first enabled number input in `scope` whose text is not a valid number; it already shows its message.
function firstInvalidNumber(scope) {
  for (const input of scope.querySelectorAll("input[data-number]")) {
    if (input.disabled) continue;
    const parsed = parseNumberInput(input.value, numberSpec(input));
    if (parsed.error) {
      showNumberMessage(input, numberErrorText(parsed, numberSpec(input)), true);
      return input;
    }
  }
  return null;
}

// Makes a field visible (its tab, its section, the Advanced disclosure) and focuses it.
function revealField(input) {
  const details = input.closest("details");
  if (details) details.open = true;
  const panel = input.closest(".agent-panel");
  if (panel?.id.startsWith("agent-panel-")) showAgentPanel(panel.id.replace("agent-panel-", ""));
  if (panel?.id === "dream-form") showAgentPanel("dream");
  const section = input.closest(".settings-section");
  if (section?.id.startsWith("settings-section-")) showSettingsSection(section.id.replace("settings-section-", ""));
  input.focus();
}

// A row is wired once: `read()` the current value, `write(v)` set it, `getDefault()` the effective default
// (undefined while unknown), `describe(v)` the default as text.
function registerRow(controlId, config) {
  const element = $(controlId).closest(".setting-row");
  const row = { id: controlId, element, ...config };
  settingRows.push(row);
  element.querySelector(".setting-reset").addEventListener("click", () => {
    const value = row.getDefault();
    if (value === undefined) return;
    row.write(value);
    fireChange(element);
  });
  return row;
}

function refreshRow(row) {
  const defaultValue = row.getDefault();
  const current = row.read();
  const hint = row.element.querySelector(".setting-default");
  const reset = row.element.querySelector(".setting-reset");
  const unknown = defaultValue === undefined;
  let text = unknown ? "" : tr("Default: {value}", { value: row.describe(defaultValue) });
  if (!unknown && row.range) {
    const range = rangeText(row.range(), tr, uiLanguage());
    if (range) text += ` · ${range}`;
  }
  hint.textContent = text;
  reset.disabled = unknown || current === undefined || !differsFromDefault(current, defaultValue);
  const label = tr("Reset to default");
  reset.title = label;
  reset.setAttribute("aria-label", label);
}

function refreshRows(scope = document) {
  refreshNumberFields();
  for (const row of settingRows) if (scope.contains(row.element)) refreshRow(row);
}

// typing, stepping, resetting: keep the default hints and reset buttons of the form in step
for (const type of ["input", "change"]) {
  document.addEventListener(type, (event) => {
    const scope = event.target.closest?.("#settings-form, #agent-form, #dream-form");
    if (scope) refreshRows(scope);
  });
}

// ---- row kinds
function optionText(selectId, value) {
  const option = [...$(selectId).options].find((item) => item.value === (value ?? ""));
  return option ? option.textContent : String(value);
}
const onOff = (value) => tr(value ? "On" : "Off");

function checkboxRow(id, getDefault, afterWrite) {
  return registerRow(id, {
    getDefault, describe: onOff, read: () => $(id).checked, write: (value) => { $(id).checked = value; afterWrite?.(); },
  });
}
function selectRow(id, getDefault, afterWrite) {
  return registerRow(id, {
    getDefault, describe: (value) => optionText(id, value), read: () => $(id).value || null,
    write: (value) => { $(id).value = value ?? ""; afterWrite?.(); },
  });
}
function numberRow(id, getSpec, nullText) {
  numberSpecs.set(id, getSpec);
  return registerRow(id, {
    getDefault: () => { const spec = getSpec(); return spec ? spec.default : undefined; },
    describe: (value) => (value == null ? nullText() : `${formatNumber(value, uiLanguage())} ${unitLabel($(id).dataset.unit)}`.trim()),
    range: () => getSpec() ?? {},
    read: () => numberValue(id), write: (value) => setNumberValue($(id), value),
  });
}
// ---- end number fields and setting rows

function translateInterface() {
  renderVersion();
  for (const [id, text] of [
    ["settings-title", "User settings"], ["settings-save", "Save settings"],
    ["tasks-title", "Tasks"], ["tasks-empty", "No tasks to show."],
    ["tasks-tab-tasks", "Tasks"], ["tasks-tab-automations", "Automations"],
    ["automations-hint", "Things an agent does later. Ask an agent that has the Automations tool group, or create one here."],
    ["automation-new", "New automation"], ["automations-empty", "No automations yet."],
    ["automation-agent-label", "Agent"], ["automation-title-label", "Title"], ["automation-instruction-label", "Instruction"],
    ["automation-kind-label", "When"], ["automation-minutes-label", "Minutes"], ["automation-cron-label", "Cron"],
    ["automation-at-label", "Date and time"], ["automation-process-label", "Process id"],
    ["automation-note", "Times are in Europe/Madrid."], ["automation-save", "Save"], ["automation-cancel", "Cancel"],
    ["task-dialog-banner-text", "Internal worker chat: read-only, not part of your chat list. Use Steer on the task card to correct it."],
    ["settings-thinking-label", "Show model thinking"],
    ["settings-thinking-hint", "Shows the model's reasoning above its replies in the chat."],
    ["settings-agent-avatar-label", "Show the agent's picture in replies"],
    ["settings-agent-avatar-hint", "Shows the agent's profile picture at the start of its replies in the chat. Agents without a picture are not affected."],
    ["settings-assoc-max-label", "Maximum associative memory matches"],
    ["settings-assoc-max-hint", "Most memory fragments suggested to the agent per message. The minimum similarity is set by the application, not by you."],
    ["settings-orchestrator-label", "Choose the agent automatically in Telegram"],
    ["settings-orchestrator-hint", "When a Telegram message starts a new chat, a fast model picks the best agent from the agents' descriptions. You can still override it with the buttons. The message text and the agents' names and descriptions are sent to the decision model's provider."],
    ["settings-default-agent-label", "Default agent"],
    ["settings-default-agent-hint", "Used for new chats when you do not pick an agent, and by the Telegram router when it is unsure (unless a core agent is set)."],
    ["settings-core-agent-label", "Core agent (used when unsure)"],
    ["settings-core-agent-hint", "The agent that answers greetings and general talk when no agent is clearly the best fit."],
    ["settings-disposable-days-label", "Days to keep disposable chats"],
    ["settings-disposable-days-hint", "Disposable chats are deleted after this many days without activity."],
    ["settings-language-label", "Alerts and errors"], ["settings-theme-label", "Theme"],
    ["settings-language-hint", "Language of alerts and error messages, here and in Telegram."],
    ["settings-theme-hint", "Applied at once and remembered in this browser only."],
    ["agent-avatar-legend", "Profile picture"], ["agent-avatar-upload", "Upload image"], ["agent-avatar-remove", "Remove"],
    ["agent-avatar-free-label", "No circle or border"],
    ["agent-avatar-free-hint", "For transparent PNGs: the image is shown as it is, without cropping."],
    ["settings-avatar-title", "Profile picture"], ["settings-avatar-upload", "Upload image"], ["settings-avatar-remove", "Remove"],
    ["settings-avatar-note", "Your profile picture is shown next to your name. It applies at once and needs no Save."],
    ["settings-avatar-hint", "PNG, JPEG, WEBP or GIF, up to 5 MiB. Shown at 32 px."],
    ["settings-avatar-free-label", "No circle or border"],
    ["settings-avatar-free-hint", "For transparent PNGs: the image is shown as it is, without cropping."],
    ["settings-theme-system", "System"], ["settings-theme-light", "Light"], ["settings-theme-dark", "Dark"],
    ["settings-note", "Settings are shared with Telegram. Chats without a selected model use your current defaults."],
    ["settings-llm-label", "Default LLM model"], ["settings-stt-label", "Default STT model"],
    ["settings-llm-hint", "Used by chats that have no model of their own and by agents that do not set one."],
    ["settings-stt-hint", "Transcribes voice messages to text."],
    ["settings-stt-language-label", "Voice message language"],
    ["settings-stt-language-hint", "The language you speak in voice messages. Auto-detect can misread short clips."],
    ["settings-stt-language-auto", "Auto-detect"],
    ["settings-tab-general-text", "General"], ["settings-tab-models-text", "Models"],
    ["settings-tab-chats-text", "Chats & memory"], ["settings-tab-telegram-text", "Telegram"],
    ["settings-tab-connections-text", "Connections"], ["settings-tab-providers-text", "Providers"], ["providers-title", "Providers"], ["openrouter-key-title", "OpenRouter"], ["openrouter-key-label", "API key"],
    ["openrouter-key-save", "Save key"], ["openrouter-key-remove", "Remove key"],
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
    ["agent-identity-legend", "Identity"], ["agent-model-legend", "Model and reasoning"],
    ["agent-effort-hint", "How much the model thinks before answering. Models without effort levels ignore it."],
    ["agent-reasoning-hint", "Asks models that support it to reason before answering."],
    ["agent-keep-reasoning-hint", "Keeps the model's reasoning in the saved conversation instead of dropping it."],
    ["agent-reasoning-tokens-hint", "Caps the tokens the model may spend reasoning. Not every model supports a limit."],
    ["agent-stream-hint", "Shows the reply as it is written instead of all at once."],
    ["agent-tools-hint", "Lets the agent call tools; the groups below choose which."],
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
    ["agent-terminal-timeout-label", "Command timeout (seconds)"],
    ["agent-level-legend", "Permission level"], ["agent-level-label", "Level"],
    ["agent-rules-label", "Approval rules"],
    ["agent-rules-hint", "Rules of this agent for commands and file operations. The strictest verdict wins: deny beats ask, ask beats allow. Hardline commands and protected paths are always refused."],
    ["agent-rules-default-label", "When no rule matches"], ["agent-rules-default-inherit", "Follow the user and installation defaults"],
    ["agent-rules-default-ask", "Ask"], ["agent-rules-default-deny", "Deny"], ["agent-rules-default-allow", "Allow"],
    ["agent-rules-empty", "This agent has no rules of its own."], ["agent-rules-add", "Add rule"],
    ["agent-rules-effective-summary", "Effective rules (read only)"],
    ["settings-tab-users-text", "Users"], ["users-title", "Users"],
    ["users-note", "The maximum level is the highest permission level a user may give their agents. Only administrators change it, never their own. A level cannot go below the level of one of the user's agents."],
    ["agent-approval-rules-label", "Learned approvals"], ["agent-approval-rules-hint", "Saved when you choose Always for this agent on an approval card. Remove one to be asked again, or reset them all."],
    ["agent-approval-rules-empty", "No learned approvals."], ["agent-approval-rules-reset", "Reset to defaults"],
    ["agent-effort-label", "Reasoning effort"], ["agent-effort-default", "Default"],
    ["agent-tokens-enabled-label", "Limit reasoning tokens"],
    ["agent-delegation-legend", "Delegation"], ["agent-delegation-enabled-label", "Delegation enabled"],
    ["agent-delegation-enabled-hint", "Switches the task tools off without removing the group."],
    ["agent-delegation-groups-label", "Tool groups a task may be given"],
    ["agent-delegation-parallel-label", "Parallel tasks per chat"],
    ["agent-delegation-parallel-hint", "Can only lower the application's limit. Empty uses the application's limit."],
    ["agent-delegation-timeout-label", "Time limit per task"], ["agent-delegation-tokens-label", "Token limit per task"],
    ["agent-delegation-targets-label", "Agents it may hand tasks to"],
    ["agent-delegation-model-label", "Default worker model"],
    ["agent-delegation-model-hint", "Model of fresh workers when the agent names none. Empty uses the model of the chat."],
    ["agent-delegation-targets-hint", "Without any, it can only use fresh workers. An agent that can write its own memory keeps what it learns from the task."],
    ["agent-delegation-no-agents", "You have no other agents."],
    ["agent-max-hops-label", "Tool rounds per turn"],
    ["agent-compaction-enabled-label", "Compact long chats"],
    ["agent-compaction-enabled-hint", "When the context grows past the trigger, a small model summarises the older part; the recent messages stay word for word. /compact does it on demand."],
    ["agent-compaction-mode-label", "Compaction units"],
    ["agent-compaction-percent", "Percent of the context window"], ["agent-compaction-tokens", "Tokens"],
    ["agent-compaction-trigger-label", "Compact at"],
    ["agent-compaction-keep-label", "Keep word for word"],
    ["agent-compaction-keep-hint", "The most recent part of the chat that is never summarised."],
    ["agent-compaction-model-label", "Summary model"],
    ["agent-compaction-model-hint", "Empty uses the chat's own model."],
    ["agent-max-hops-hint", "How many tool round-trips the agent may do before it must answer. Empty uses the application value."],
    ["agent-save", "Save agent"], ["agent-delete", "Delete agent"],
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
    ["dream-schedule-reset-label", "Reset the whole schedule"],
    ["dream-schedule-hint", "Scheduled sessions are skipped while no chat is pending. You can always start one by hand, from the Dream sessions tab or with /dream start in a chat."],
    ["dream-model-legend", "Model and limits"], ["dream-model-label", "Model"], ["dream-model-browse", "Browse"],
    ["dream-model-hint", "Empty uses the agent's default chat model, else yours."],
    ["agent-model-label", "Default model"], ["agent-model-browse", "Browse"], ["agent-delegation-model-browse", "Browse"],
    ["agent-model-hint", "Empty follows your default model. A model chosen inside a chat still takes precedence."],
    ["dream-effort-label", "Reasoning effort"], ["dream-effort-none", "No reasoning request"],
    ["dream-max-tokens-label", "Maximum tokens"],
    ["dream-max-tokens-hint", "Input plus output tokens per session; 0 = no cap."],
    ["dream-max-minutes-label", "Maximum minutes"],
    ["dream-max-minutes-hint", "Time limit per session. One minute before it the agent is told to wrap up; at the limit the session is closed."],
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
    ["dream-notify-label", "Notify me on Telegram when a session ends"],
    ["dream-delegate-label", "Read long chats with read-only workers"],
    ["dream-delegate-model-label", "Worker model"], ["dream-delegate-model-browse", "Browse"],
    ["dream-delegate-model-hint", "Model of the reading workers, e.g. a cheaper one. Empty uses the dream's model."],
    ["dream-delegate-hint", "Needs the Delegate tool group and level collaborator or higher. Worker tokens count toward the session."], ["dream-save", "Save dream settings"],
    ["dream-status-title", "Dream status"], ["dream-sessions-title", "Dream sessions"],
    ["dream-col-date", "Date"], ["dream-col-trigger", "Trigger"], ["dream-col-status", "Status"],
    ["dream-col-chats", "Chats reviewed"], ["dream-col-duration", "Duration"], ["dream-col-tokens", "Tokens"],
    ["dream-sessions-empty", "No dream sessions yet."], ["dream-session-back", "← All sessions"],
    ["agent-mcp-note", "MCP servers act on each user's own account. Connect accounts in Settings > Connections."],
    ["connections-title", "Connections"],
    ["connections-note", "Connect your own account for each service your agents can use. Every user connects separately."],
    ["connections-browser-note", "Sign-in opens in a browser on the computer running RECALL."],
    ["claude-login-title", "Claude subscription"], ["claude-login-step-note", "Open the link, sign in to Claude, then paste the code it shows."],
    ["claude-login-link", "Open Claude sign-in"], ["claude-login-send", "Send code"], ["claude-login-cancel", "Cancel"],
  ]) $(id).textContent = tr(text);
  $("settings-nav").setAttribute("aria-label", tr("Settings sections"));
  renderAgentsEmpty();
  renderAgentAvatarField();
  refreshRows();
  $("dream-model").placeholder = tr("Your chat model");
  $("chat-view").dataset.dropLabel = tr("Drop files to attach them");
  $("dream-delegate-model").placeholder = tr("The dream's model");
  $("agent-model").placeholder = tr("Your default model");
  $("agent-delegation-model").placeholder = tr("The model of the chat");
  for (const [id, text] of [
    ["settings-open", "Settings"], ["settings-close", "Close"], ["tasks-open", "Tasks"], ["tasks-close", "Close"], ["assoc-close", "Close"],
    ["logout", "Log out"], ["jump-latest", "Jump to latest"], ["claude-usage-toggle", "Claude plan usage"], ["claude-usage-body", "Claude plan usage"], ["theme-toggle", "Switch theme"],
  ]) {
    $(id).title = tr(text);
    $(id).setAttribute("aria-label", tr(text));
  }
  for (const [filter, text] of [["all", "All"], ["running", "Running"], ["finished", "Finished"]]) {
    document.querySelector(`#tasks-filter [data-filter="${filter}"]`).textContent = tr(text);
  }
  $("tasks-filter").setAttribute("aria-label", tr("Filter tasks"));
  $("tasks-tabs").setAttribute("aria-label", tr("Tasks and automations"));
  const kindLabels = { every: "Every N minutes", cron: "On a cron schedule", at: "Once, at a date and time", process_end: "When a background process ends" };
  for (const option of $("automation-kind").options) option.textContent = tr(kindLabels[option.value]);
  $("automation-title").placeholder = tr("Short name (optional)");
  $("automation-instruction").placeholder = tr("What the agent must do when it fires. It will not have this screen: be specific.");
  $("new-disposable-chat").title = tr("New disposable chat");
  $("new-disposable-chat").setAttribute("aria-label", tr("New disposable chat"));
  syncNavToggles();
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
// Actions: create-agent, choose-model, new-chat, open-settings, open-providers; `command` is shown as text, never run.
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
        hint: tr("Sign in again in Settings, Providers, then send your message again."),
        action: "open-providers", label: tr("Open Settings") };
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

// ---- profile pictures (avatarView and avatarFileProblem are DOM-free and unit-tested)
const AVATAR_MAX_BYTES = 5 * 1024 * 1024;
const AVATAR_TYPES = ["image/png", "image/jpeg", "image/webp", "image/gif"];

// What to draw for a user or an agent: the stored image (`?v=<version>` makes a new upload replace the cached one)
// or, without one, the first letter of the name. `free` = shown as it is, without the circle and border.
function avatarView(avatar, name) {
  const letter = [...(name ?? "")][0]?.toUpperCase() ?? "?";
  if (!avatar || typeof avatar.url !== "string" || !avatar.url.startsWith("/api/")) return { src: null, free: false, letter };
  return { src: `${avatar.url}?v=${encodeURIComponent(avatar.version ?? "")}`, free: avatar.style === "free", letter };
}

// The big picture at the start of an assistant reply: null without an image, else what to draw.
function messageAvatar(avatar) {
  const view = avatarView(avatar, "");
  return view.src ? { src: view.src, free: view.free } : null;
}

// A message template (with values) when the file cannot be uploaded, else null; the server checks again.
function avatarFileProblem(file) {
  if (!AVATAR_TYPES.includes(file.type)) return { template: "Use a PNG, JPEG, WEBP or GIF image.", values: {} };
  if (file.size > AVATAR_MAX_BYTES) return { template: "The image is larger than {limit} MiB.", values: { limit: AVATAR_MAX_BYTES / (1024 * 1024) } };
  return null;
}

// The image is decorative (the name is already text); a failed load falls back to the letter.
function renderAvatar(element, avatar, name) {
  const view = avatarView(avatar, name);
  if (view.free && view.src) element.dataset.avatarStyle = "free"; else delete element.dataset.avatarStyle;
  if (!view.src) { element.replaceChildren(view.letter); return; }
  const image = document.createElement("img");
  image.alt = "";
  image.draggable = false;
  image.addEventListener("error", () => { delete element.dataset.avatarStyle; element.replaceChildren(view.letter); });
  image.src = view.src;
  element.replaceChildren(image);
}

function renderUserAvatar() {
  renderAvatar($("user-avatar"), state.user?.avatar, state.user?.name);
  renderUserAvatarField();
}

function renderUserAvatarField() {
  renderAvatar($("settings-avatar-preview"), state.user?.avatar, state.user?.name);
  $("settings-avatar-remove").hidden = !state.user?.avatar;
  $("settings-avatar-free").checked = state.preferences?.avatar_style === "free";
}

function renderAgentAvatarField() {
  const saved = Boolean(state.agentForm.originalId);
  const avatar = state.agentForm.avatar;
  // the checkbox previews the style before the agent is saved
  renderAvatar($("agent-avatar-preview"), avatar && { ...avatar, style: $("agent-avatar-free").checked ? "free" : "circle" }, $("agent-name").value.trim());
  $("agent-avatar-upload").disabled = !saved;
  $("agent-avatar-remove").hidden = !avatar;
  $("agent-avatar-hint").textContent = tr(saved ? "PNG, JPEG, WEBP or GIF, up to 5 MiB. Shown at 32 px." : "Save the agent first to add a profile picture.");
}

// Uploads (a File) or removes (null) a picture at once; returns whether it worked, the error shown in `errorElement`.
async function changeAvatar(path, file, errorElement) {
  errorElement.textContent = "";
  try {
    if (file) {
      const problem = avatarFileProblem(file);
      if (problem) throw new Error(tr(problem.template, problem.values));
      await api(path, {
        method: "PUT", body: file,
        headers: { "Content-Type": file.type, "Accept-Language": state.preferences?.language ?? "en" },
      });
    } else {
      await api(path, { method: "DELETE" });
    }
    return true;
  } catch (err) {
    errorElement.textContent = err.message;
    return false;
  }
}

// After an upload, a removal or a style change: the pictures come back with /api/me.
async function refreshAvatars(session) {
  const me = await api("/api/me");
  if (session !== state.session || !state.user) return;
  state.user.agents = me.agents;
  state.user.avatar = me.avatar ?? null;
  for (const config of state.agentConfigs) config.avatar = me.agents.find((a) => a.id === config.id)?.avatar ?? null;
  state.agentForm.avatar = state.agentConfigs.find((a) => a.id === state.agentForm.originalId)?.avatar ?? null;
  renderAgentBanner();
  renderUserAvatar();
  renderAgentAvatarField();
}

function pickAvatar(fileInput, path, errorElement) {
  const file = fileInput.files[0];
  fileInput.value = "";  // the same file can be chosen again later
  if (!file) return;
  const session = state.session;
  const target = path();
  if (!target) return;
  changeAvatar(target, file, errorElement).then(async (done) => {
    if (done && session === state.session) { await refreshAvatars(session); errorElement.textContent = ""; }
    return done;
  }).catch((err) => { errorElement.textContent = err.message; });
}

function removeAvatar(path, errorElement) {
  const session = state.session;
  const target = path();
  if (!target) return;
  changeAvatar(target, null, errorElement).then(async (done) => {
    if (done && session === state.session) await refreshAvatars(session);
  }).catch((err) => { errorElement.textContent = err.message; });
}

const agentAvatarPath = () => (state.agentForm.originalId ? `/api/agents/${encodeURIComponent(state.agentForm.originalId)}/avatar` : null);
$("agent-avatar-upload").addEventListener("click", () => $("agent-avatar-file").click());
$("agent-avatar-file").addEventListener("change", () => pickAvatar($("agent-avatar-file"), agentAvatarPath, $("agent-avatar-error")));
$("agent-avatar-remove").addEventListener("click", () => removeAvatar(agentAvatarPath, $("agent-avatar-error")));
$("agent-avatar-free").addEventListener("change", renderAgentAvatarField);
$("settings-avatar-upload").addEventListener("click", () => $("settings-avatar-file").click());
$("settings-avatar-file").addEventListener("change", () => pickAvatar($("settings-avatar-file"), () => "/api/me/avatar", $("settings-avatar-error")));
$("settings-avatar-remove").addEventListener("click", () => removeAvatar(() => "/api/me/avatar", $("settings-avatar-error")));
// the user's picture style is a preference that applies at once, like the Appearance options
$("settings-avatar-free").addEventListener("change", async () => {
  const session = state.session;
  const style = $("settings-avatar-free").checked ? "free" : "circle";
  $("settings-avatar-error").textContent = "";
  try {
    const preferences = await api("/api/preferences", { method: "PATCH", body: JSON.stringify({ avatar_style: style }) });
    if (session !== state.session) return;
    if (state.preferences) state.preferences.avatar_style = preferences.avatar_style;
    await refreshAvatars(session);
  } catch (err) {
    if (session === state.session) {
      $("settings-avatar-error").textContent = err.message;
      renderUserAvatarField();
    }
  }
});
// ---- end profile pictures

function currentAgentAvatar() {
  return state.user?.agents.find((a) => a.id === state.agentId)?.avatar ?? null;
}

function currentAgentName() {
  return state.user?.agents.find((a) => a.id === state.agentId)?.name ?? "";
}

// Who wrote an assistant reply: the chat's agent, or another agent the user @mentioned (`author_agent` on a stored turn,
// the `speaker` event live). Its current name and picture come from the agent list; the stored name is the fallback.
function replyAuthor(authorId, storedName, agents, currentId) {
  const id = authorId || currentId;
  const agent = (agents ?? []).find((a) => a.id === id);
  return { id, name: agent?.name ?? storedName ?? id ?? "", avatar: agent?.avatar ?? null };
}

// The active agent is the header's headline; the select is stretched invisibly over it as the switcher.
function renderAgentBanner() {
  const name = currentAgentName();
  $("agent-title").textContent = name || "—";
  renderAvatar($("agent-avatar"), state.user?.agents.find((a) => a.id === state.agentId)?.avatar, name);
  $("input").placeholder = state.busy
    ? "Steer the response..."
    : name ? `Message ${name}...` : "Write a message...";
}

// ---- last open chat: #chat/<agent id>/<chat id> in the address bar, plus a per-user fallback in localStorage
const LAST_CHAT_PREFIX = "lastChat:";
const CHAT_ROUTE = /^#chat\/([^/]+)\/([^/]+)$/;
function chatHash(agentId, chatId) {
  return `chat/${encodeURIComponent(agentId)}/${encodeURIComponent(chatId)}`;
}
function parseChatHash(hash) {
  const match = CHAT_ROUTE.exec(hash ?? "");
  if (!match) return null;
  try {
    return { agentId: decodeURIComponent(match[1]), chatId: decodeURIComponent(match[2]) };
  } catch { return null; }
}
function readLastChat(storage, userId) {
  try {
    const value = JSON.parse(storage.getItem(LAST_CHAT_PREFIX + userId));
    return value && typeof value.agentId === "string" && typeof value.chatId === "string"
      ? { agentId: value.agentId, chatId: value.chatId } : null;
  } catch { return null; }
}
function writeLastChat(storage, userId, agentId, chatId) {
  try { storage.setItem(LAST_CHAT_PREFIX + userId, JSON.stringify({ agentId, chatId })); } catch { /* private mode: the address bar still holds it */ }
}
function clearLastChat(storage, userId) {
  try { storage.removeItem(LAST_CHAT_PREFIX + userId); } catch { /* nothing stored */ }
}
// ---- end last open chat

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
  renderUserAvatar();
  renderThemeToggle();
  renderAgentOptions();
  refreshClaudeUsage();
  showTab("chats");
  try {
    await refreshPreferences();
    if (session !== state.session) return;
    await selectAgent(state.preferences.agent_id);
    if (session === state.session && state.agentId && !(await restoreChat(route))) {
      // no chat to reopen: a new chat opens with the default agent, not the last selected one
      const agentId = defaultAgentId(state.preferences, state.user.agents);
      if (agentId && agentId !== state.agentId) {
        await savePreferences({ agent_id: agentId });
        if (session !== state.session) return;
        await selectAgent(agentId);
      }
      if (session === state.session) await newChat();
    }
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
  if (state.user) clearLastChat(localStorage, state.user.id);
  showLogin();
});

// ------------------------------------------------------------------ navigation drawer (narrow screens)

// Wide screens collapse the sidebar (remembered per browser); narrow screens open the drawer.
const PANEL_KEY = "panelCollapsed";
function readPanelCollapsed(storage) {
  try { return storage.getItem(PANEL_KEY) === "1"; } catch { return false; }
}
function writePanelCollapsed(storage, collapsed) {
  try { storage.setItem(PANEL_KEY, collapsed ? "1" : "0"); } catch { /* private mode: keep it for this page only */ }
}
const narrowScreen = window.matchMedia("(max-width: 800px)");
function syncNavToggles() {
  const collapsed = document.documentElement.classList.contains("panel-collapsed");
  const open = document.body.classList.contains("nav-open");
  const label = narrowScreen.matches ? tr("Menu") : tr(collapsed ? "Show panel" : "Hide panel");
  for (const button of document.querySelectorAll(".nav-toggle")) {
    button.title = label;
    button.setAttribute("aria-label", label);
    button.setAttribute("aria-expanded", String(narrowScreen.matches ? open : !collapsed));
  }
}
function setPanel(collapsed) {
  document.documentElement.classList.toggle("panel-collapsed", collapsed);
  writePanelCollapsed(localStorage, collapsed);
  syncNavToggles();
}
function setNav(open) {
  document.body.classList.toggle("nav-open", open);
  syncNavToggles();
}
for (const button of document.querySelectorAll(".nav-toggle")) {
  button.addEventListener("click", () => {
    if (narrowScreen.matches) setNav(!document.body.classList.contains("nav-open"));
    else setPanel(!document.documentElement.classList.contains("panel-collapsed"));
  });
}
narrowScreen.addEventListener("change", () => { setNav(false); });
syncNavToggles();
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

// Switching agent opens a fresh chat with it. If that cannot finish (a busy flag appeared, the new chat failed), go
// back to the agent and chat that were open instead of leaving the selector and the page disagreeing.
async function switchAgent(agentId) {
  const session = state.session;
  const token = ++state.agentSwitch;
  const previous = { agentId: state.agentId, chatId: state.chatId };
  const current = () => session === state.session && token === state.agentSwitch;
  let error = null;
  let opened = false;
  try {
    await savePreferences({ agent_id: agentId });
    if (!current()) return;
    await selectAgent(agentId);
    if (!current()) return;
    opened = await newChat();
  } catch (err) {
    error = err;
  }
  if (!current() || opened) return;
  try {
    if (state.agentId !== previous.agentId) {
      await savePreferences({ agent_id: previous.agentId });
      if (!current()) return;
      await selectAgent(previous.agentId);
    } else if (agentId !== previous.agentId) {
      await savePreferences({ agent_id: previous.agentId });
    }
    if (!current()) return;
    $("agent-select").value = state.agentId ?? "";
    if (previous.chatId && state.agentId === previous.agentId && !state.chatId) await openChat(previous.chatId);
  } catch (err) {
    error ??= err;
  }
  if (current()) {
    $("agent-select").value = state.agentId ?? "";
    if (error) addMessage("assistant").error(error);
  }
}

$("agent-select").addEventListener("change", (e) => {
  if (state.modelBusy || state.settingsBusy) {
    $("agent-select").value = state.agentId ?? "";
    return;
  }
  return switchAgent(e.target.value);
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

// Reopen the chat from the link or, failing that, the last one this user had open; false means start a new chat.
async function restoreChat(route) {
  const session = state.session;
  const target = parseChatHash(route) ?? readLastChat(localStorage, state.user.id);
  if (!target || !state.user.agents.some((a) => a.id === target.agentId)) return false;
  try {
    if (target.agentId !== state.agentId) {
      await savePreferences({ agent_id: target.agentId });
      if (session !== state.session) return true;
      await selectAgent(target.agentId);
    }
    await openChat(target.chatId);
    if (session !== state.session) return true;
    if (state.chatId === target.chatId && state.agentId === target.agentId) return true;
  } catch (err) {
    if (session !== state.session) return true;
  }
  // the chat or agent is gone: forget it and fall back to a new chat
  clearLastChat(localStorage, state.user.id);
  return false;
}

// Keep the address bar and storage pointing at the open chat; a chat without turns is only linked, never stored.
function rememberChat(agentId, chatId, hasTurns) {
  setDreamHash(chatHash(agentId, chatId));
  if (hasTurns && state.user) writeLastChat(localStorage, state.user.id, agentId, chatId);
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
  setDreamHash(state.agentId && state.chatId ? chatHash(state.agentId, state.chatId) : null);
  return Promise.resolve();
}

async function loadAgentConfigs() {
  const session = state.session;
  try {
    const [agents, toolGroups, mcpServers, defaults, levels] = await Promise.all([
      api("/api/agents"), api("/api/tool-groups"), api("/api/mcp"),
      api("/api/agents/defaults").catch(() => null), // only the default hints depend on it
      api("/api/levels").catch(() => null), // without it the form simply has no level selector
    ]);
    if (session !== state.session) return;
    state.levels = levels;
    state.agentConfigs = agents;
    state.toolGroupCatalog = toolGroups;
    state.agentDefaults = defaults;
    state.mcpServers = mcpServers;
    state.mcpTools.clear();
    renderAgentList();
    refreshRows();
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
      if (agent.level) name.append(" ", levelBadge(agent.level));
      button.append(name);
      if (agent.description) {
        const description = document.createElement("span");
        description.className = "agent-description";
        description.textContent = agent.description;
        button.title = agent.description;
        button.append(description);
      }
      if (agent.broken) {
        const warning = document.createElement("span");
        warning.className = "agent-description";
        warning.textContent = `⚠ ${tr("Invalid configuration")}`;
        button.title = agent.error || "";
        button.append(warning);
        button.disabled = true;
      } else {
        button.addEventListener("click", () => openAgent(agent.id));
      }
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
      keep_reasoning_in_memory: true, tools: true, associative_memory: "llm",
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
  state.agentForm.levelTouched = false;
  state.agentForm.initialLevel = state.agentForm.originalId ? agent.level ?? null : null;
  $("agent-name").value = agent.name;
  $("agent-description").value = agent.description ?? "";
  $("agent-avatar-free").checked = agent.avatar_style === "free";
  $("agent-avatar-error").textContent = "";
  state.agentForm.avatar = agent.avatar ?? null;
  renderAgentAvatarField();
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
      const badge = Object.assign(document.createElement("span"), { className: "level-badge", hidden: true });
      badge.dataset.group = group.id;
      label.append(checkbox, text, badge);
      if (group.id === "terminal") checkbox.addEventListener("change", updateTerminalBlock);
      if (group.id === "delegate") checkbox.addEventListener("change", updateDelegationBlock);
      return label;
    }),
  );
  renderTerminalBlock(agent);
  renderDelegationBlock(agent);
  $("agent-tool-groups").append(Object.assign(document.createElement("p"), {
    className: "model-note", textContent: tr("The folder groups need at least one folder, set in the Folders tab."),
  }));
  $("agent-associative").value = config.associative_memory;
  $("agent-associative-execution").value = config.associative_execution;
  $("agent-associative-execution").disabled = config.associative_memory === "off";
  $("agent-effort").value = config.reasoning_effort ?? "";
  $("agent-tokens-enabled").checked = config.reasoning_tokens != null;
  $("agent-reasoning-tokens").disabled = config.reasoning_tokens == null;
  setNumberValue($("agent-reasoning-tokens"), config.reasoning_tokens);
  setNumberValue($("agent-max-hops"), config.max_tool_hops);
  const compactionConfig = config.compaction ?? {};
  $("agent-compaction-enabled").checked = compactionConfig.enabled ?? true;
  $("agent-compaction-mode").value = compactionConfig.mode ?? "percent";
  setNumberValue($("agent-compaction-trigger"), compactionConfig.trigger ?? null);
  setNumberValue($("agent-compaction-keep"), compactionConfig.keep_recent ?? null);
  $("agent-compaction-model").value = compactionConfig.model ?? "";
  syncCompactionUnits();
  renderAgentMcp(config.mcp ?? {});
  renderFolders(agent.folders ?? []);
  resetIndexStatus();
  initAgentLevel();
  showAgentPanel("basic");
  refreshRows($("agent-form"));
}

// ---- permission level (agent form): the selector, the level the choices need, badges
function levelBadge(name, extraClass = "") {
  const badge = Object.assign(document.createElement("span"), {
    className: `level-badge ${extraClass}`.trim(), textContent: levelTitle(state.levels, name),
  });
  const description = levelDescriptionText(state.levels, name, translatedText);
  if (description) badge.title = description;
  return badge;
}

// The form's choices as the level rule sees them.
function agentLevelChoices() {
  const mine = state.user?.terminal_backends ?? [];
  return {
    toolGroups: [...document.querySelectorAll('input[name="agent-tool-group"]:checked')].map((input) => input.value),
    mcp: Object.keys(agentMcpBody()).length > 0,
    backends: readTerminalBackendFields().filter((field) => field.allowed).map((field) => ({
      name: field.name, min_level: mine.find((backend) => backend.name === field.name)?.min_level,
    })),
    delegationTargets: delegationGroupChecked() && $("agent-delegation-enabled").checked
      && readDelegationTargetFields().some((target) => target.allowed),
  };
}

function toolGroupName(id) {
  const group = state.toolGroupCatalog.find((item) => item.id === id);
  return group ? translatedText(group.name) : id;
}

// Builds the selector for the open agent: its level (or, for a new agent, the one its choices need) is selected.
function initAgentLevel() {
  const catalog = state.levels;
  $("agent-level-section").hidden = !catalog;
  if (!catalog) return;
  const max = state.user?.max_level ?? catalog.levels[catalog.levels.length - 1].name;
  $("agent-level").replaceChildren(...levelOptions(catalog, max).map((option) => Object.assign(
    document.createElement("option"), { value: option.value, textContent: option.label, disabled: option.disabled },
  )));
  if (!state.agentForm.initialLevel) state.agentForm.initialLevel = requiredLevelFor(catalog, agentLevelChoices()).level;
  $("agent-level").value = state.agentForm.initialLevel;
  updateLevelUi();
}

// Follows the form: the level the choices need, the message, the header badge and the badges of the tool groups.
function updateLevelUi() {
  const catalog = state.levels;
  if (!catalog || $("agent-level-section").hidden) return;
  const select = $("agent-level");
  const max = state.user?.max_level ?? catalog.levels[catalog.levels.length - 1].name;
  const choices = agentLevelChoices();
  const required = requiredLevelFor(catalog, choices);
  if (!state.agentForm.levelTouched) {
    // an agent the user did not set a level on follows what its choices need (never past their maximum)
    const target = Math.max(levelNumber(catalog, state.agentForm.initialLevel), Math.min(required.number, levelNumber(catalog, max)));
    select.value = catalog.levels[target].name;
  }
  const advice = levelAdvice(catalog, select.value, choices, max);
  $("agent-level-description").replaceChildren(...levelDescriptionItems(catalog, select.value, translatedText).map((item) => {
    const line = Object.assign(document.createElement("li"), { textContent: item.text });
    if (item.detail) line.title = item.detail;
    return line;
  }));
  $("agent-level-info").title = [
    tr("The level is a ceiling over what the agent may use: tools, MCP, terminal and delegation. Choose the lowest level that does the job. Only an administrator changes your maximum level."),
    tr("Required by the current choices: {level} ({service})", {
      level: levelTitle(catalog, advice.required.level), service: levelReasonText(advice.required.reason, toolGroupName),
    }),
  ].join("\n\n");
  $("agent-level-message").textContent = levelMessage(catalog, advice, select.value, max, toolGroupName);
  $("agent-level").classList.toggle("warning", advice.problem !== null);
  const badge = $("agent-form-level");
  badge.hidden = !state.agentForm.originalId;
  badge.textContent = levelTitle(catalog, state.agentForm.initialLevel ?? select.value);
  const badges = toolGroupBadges(catalog, select.value, max);
  for (const node of document.querySelectorAll("#agent-tool-groups .level-badge")) {
    const entry = badges[node.dataset.group];
    node.hidden = !entry;
    if (!entry) continue;
    const needed = levelTitle(catalog, entry.level);
    node.textContent = needed;
    node.classList.toggle("over-max", entry.overMax);
    node.title = entry.overMax
      ? tr("Needs the level {needed}, above your maximum level {max}. Ask an administrator to raise your maximum.", { needed, max: levelTitle(catalog, max) })
      : tr("Needs the level {needed} or higher; this agent is {level}. Raise the level to use it.", { needed, level: levelTitle(catalog, select.value) });
  }
}

// ---- approval rule editor (Terminal block): the agent's own hand-written rules, one editable row each
const RULE_VERDICTS = ["allow", "ask", "deny"];
const RULE_TOOLS = [["any", "Any action"], ["terminal", "Terminal commands"], ["file_read", "Read files"], ["file_write", "Write files"]];
const RULE_KINDS = [["", "Any kind"], ["sandbox", "sandbox"], ["machine", "machine"], ["local", "local"]];
const RULE_DANGEROUS = [["", "Dangerous or not"], ["yes", "Only dangerous actions"], ["no", "Only actions that are not dangerous"]];

function buildRuleRow(rule) {
  const fields = approvalRuleFields(rule);
  const item = document.createElement("li");
  item.className = "agent-rule";
  item.dataset.base = JSON.stringify(rule ?? {});
  const control = (tag, name, label, value) => {
    const element = document.createElement(tag);
    element.className = `rule-${name}`;
    element.setAttribute("aria-label", label);
    if (tag === "input") Object.assign(element, { value, placeholder: label, autocomplete: "off", maxLength: 1024 });
    return element;
  };
  const select = (name, label, options, value) => {
    const element = control("select", name, label);
    for (const [optionValue, text] of options) element.append(Object.assign(document.createElement("option"), { value: optionValue, textContent: text }));
    element.value = value;
    return element;
  };
  const field = (caption, element) => {
    const label = document.createElement("label");
    label.append(Object.assign(document.createElement("span"), { className: "model-note", textContent: caption }), element);
    return label;
  };
  const verdict = select("verdict", tr("Verdict"), RULE_VERDICTS.map((v) => [v, tr(v === "allow" ? "Allow" : v === "ask" ? "Ask" : "Deny")]), fields.verdict);
  const tool = select("tool", tr("Applies to"), RULE_TOOLS.map(([v, text]) => [v, tr(text)]), fields.tool);
  const command = control("input", "command", tr("Prefix or pattern, e.g. git *"), fields.command);
  const path = control("input", "path", tr("Pattern, e.g. /etc/*"), fields.path);
  const backend = control("input", "backend", tr("Name or pattern"), fields.backend);
  backend.setAttribute("list", "agent-rules-backends");
  const kind = select("kind", tr("Kind of backend"), RULE_KINDS.map(([v, text]) => [v, v ? text : tr(text)]), fields.backendKind);
  const dangerous = select("dangerous", tr("Danger"), RULE_DANGEROUS.map(([v, text]) => [v, tr(text)]), fields.dangerous);
  const exact = Object.assign(document.createElement("input"), { type: "checkbox", className: "rule-exact", checked: fields.exact });
  const exactLabel = document.createElement("label");
  exactLabel.className = "model-filter";
  exactLabel.append(exact, Object.assign(document.createElement("span"), { textContent: tr("Exact match") }));
  exactLabel.title = tr("The command or path must equal the text: no word prefix, no pattern.");
  const remove = Object.assign(document.createElement("button"), { type: "button", textContent: tr("Remove") });
  remove.addEventListener("click", () => {
    item.remove();
    ruleEditorChanged();
  });
  item.append(
    field(tr("Verdict"), verdict), field(tr("Applies to"), tool), field(tr("Command"), command), field(tr("Path"), path),
    field(tr("Backend"), backend), field(tr("Kind of backend"), kind), field(tr("Danger"), dangerous), exactLabel, remove,
  );
  return item;
}

function readRuleRow(item) {
  const value = (name) => item.querySelector(`.rule-${name}`).value;
  return buildApprovalRule(JSON.parse(item.dataset.base), {
    verdict: value("verdict"), tool: value("tool"), command: value("command"), path: value("path"), backend: value("backend"),
    backendKind: value("kind"), dangerous: value("dangerous"), exact: item.querySelector(".rule-exact").checked,
  });
}

function readRuleRows() {
  return [...document.querySelectorAll("#agent-rules-list .agent-rule")].map(readRuleRow);
}

function ruleEditorChanged() {
  state.agentForm.rulesDirty = true;
  $("agent-rules-empty").hidden = $("agent-rules-list").children.length > 0;
}

function renderRuleEditor(approvalRules) {
  state.agentForm.rulesDirty = false;
  const { manual, learned } = splitApprovalRules(approvalRules?.rules);
  state.agentForm.learned = learned;
  $("agent-rules-default").value = approvalRules?.default ?? "";
  $("agent-rules-list").replaceChildren(...manual.map(buildRuleRow));
  $("agent-rules-empty").hidden = manual.length > 0;
  $("agent-rules-error").textContent = "";
  $("agent-rules-backends").replaceChildren(...(state.user?.terminal_backends ?? []).map((backend) => Object.assign(
    document.createElement("option"), { value: backend.name },
  )));
}

// The `approval_rules` part of the save body: only when the editor was touched (else the stored rules are kept).
function ruleEditorPayload() {
  if (!state.agentForm.rulesDirty || !terminalGroupChecked()) return {};
  return { approval_rules: approvalRulesBody($("agent-rules-default").value, readRuleRows(), state.agentForm.learned ?? []) };
}

$("agent-rules-add").addEventListener("click", () => {
  const row = buildRuleRow(null);
  $("agent-rules-list").append(row);
  ruleEditorChanged();
  row.querySelector(".rule-command").focus();
});
$("agent-rules").addEventListener("input", () => { state.agentForm.rulesDirty = true; });
$("agent-rules").addEventListener("change", () => { state.agentForm.rulesDirty = true; });

// Read-only: what applies to the saved agent from the three layers.
async function loadEffectiveRules() {
  const agentId = state.agentForm.originalId;
  const details = $("agent-rules-effective");
  details.hidden = !agentId;
  if (!agentId || !details.open) return;
  const body = $("agent-rules-effective-body");
  try {
    const data = await api(`/api/agents/${encodeURIComponent(agentId)}/approval-rules/effective`);
    if (agentId !== state.agentForm.originalId) return;
    const names = { installation: tr("Installation (settings.yaml)"), user: tr("You (all your agents)"), agent: tr("This agent") };
    const nodes = [];
    for (const layer of data.layers) {
      const heading = `${names[layer.name] ?? layer.name}${layer.default ? ` — ${tr("default")}: ${layer.default}` : ""}`;
      const list = document.createElement("ul");
      list.className = "approval-rules-list";
      list.append(...layer.rules.map((entry) => Object.assign(document.createElement("li"), {
        className: "approval-rule-text", textContent: approvalRuleText(entry.rule),
      })));
      nodes.push(Object.assign(document.createElement("strong"), { textContent: heading }));
      nodes.push(layer.rules.length ? list : Object.assign(document.createElement("p"), { className: "model-note", textContent: tr("No rules.") }));
    }
    const shorthand = Object.entries(data.shorthand).map(([name, mode]) => `${name}: ${mode ?? tr("not set")}`).join(", ");
    if (shorthand) nodes.push(Object.assign(document.createElement("p"), { className: "model-note", textContent: `${tr("Approvals per backend")}: ${shorthand}` }));
    nodes.push(Object.assign(document.createElement("p"), { className: "model-note", textContent: `${tr("When no rule matches")}: ${data.default}` }));
    body.replaceChildren(...nodes);
  } catch (err) {
    if (agentId === state.agentForm.originalId) body.replaceChildren(Object.assign(document.createElement("p"), { className: "error", textContent: err.message }));
  }
}
$("agent-rules-effective").addEventListener("toggle", loadEffectiveRules);

// any change in the form can change the level its choices need
$("agent-form").addEventListener("change", (event) => {
  if (event.target.id === "agent-level") state.agentForm.levelTouched = true;
  if (!event.target.closest("#agent-rules")) updateLevelUi();
});

// The Terminal block follows the `terminal` tool group: shown (and validated) only while the group is ticked.
function terminalGroupChecked() {
  return Boolean(document.querySelector('input[name="agent-tool-group"][value="terminal"]:checked'));
}

function updateTerminalBlock() {
  const enabled = terminalGroupChecked();
  $("agent-terminal").hidden = !enabled;
  $("agent-terminal").disabled = !enabled;  // a disabled fieldset is not validated, so hidden fields never block saving
}

// One readable line for a rule of the agent's approval list: "allow terminal: git status [box] (exact)".
function approvalRuleText(rule) {
  const target = rule?.command ?? rule?.path ?? "";
  const kind = rule?.tool && rule.tool !== "any" ? `${rule.tool}: ` : "";
  const where = rule?.backend ? ` [${rule.backend}]` : "";
  return `${rule?.verdict ?? ""} ${kind}${target}${where}${rule?.exact ? " (exact)" : ""}`.trim();
}

let approvalRulesRequest = 0;

function drawApprovalRules(entries) {
  const list = $("agent-approval-rules-list");
  const rules = entries.filter((entry) => entry.learned); // the others are in the rule editor
  state.agentForm.learnedEntries = rules;
  state.agentForm.learned = rules.map((entry) => entry.rule);
  const open = chats.find((chat) => chat.id === state.chatId && chat.agent_id === state.agentId);
  if (open?.unread && !document.hidden) markChatRead(open.agent_id, open.id);
  list.replaceChildren(...rules.map((entry) => {
    const item = document.createElement("li");
    const text = Object.assign(document.createElement("span"), { className: "approval-rule-text", textContent: approvalRuleText(entry.rule) });
    const remove = Object.assign(document.createElement("button"), { type: "button", textContent: tr("Remove") });
    remove.addEventListener("click", () => changeApprovalRules("DELETE", `/${entry.index}`));
    item.append(text, remove);
    return item;
  }));
  $("agent-approval-rules-empty").hidden = rules.length > 0;
  $("agent-approval-rules-reset").hidden = rules.length === 0;
}

// Loads (no method) or changes the saved agent's approval rules; the form never edits them as part of its own save.
async function changeApprovalRules(method, suffix = "") {
  const agentId = state.agentForm.originalId;
  const block = $("agent-approval-rules");
  block.hidden = !agentId;
  if (!agentId) return;
  const request = ++approvalRulesRequest;
  $("agent-approval-rules-error").textContent = "";
  try {
    const rules = await api(`/api/agents/${encodeURIComponent(agentId)}/approval-rules${suffix}`, method ? { method } : undefined);
    if (request === approvalRulesRequest && agentId === state.agentForm.originalId) drawApprovalRules(rules);
  } catch (err) {
    if (request === approvalRulesRequest) $("agent-approval-rules-error").textContent = err.message;
  }
}

// Removes the learned approvals only (highest position first, so the others keep theirs); the hand-written rules stay.
async function resetLearnedApprovals() {
  const agentId = state.agentForm.originalId;
  const indexes = (state.agentForm.learnedEntries ?? []).map((entry) => entry.index).sort((a, b) => b - a);
  if (!agentId || !indexes.length) return;
  $("agent-approval-rules-error").textContent = "";
  try {
    let rules = null;
    for (const index of indexes) rules = await api(`/api/agents/${encodeURIComponent(agentId)}/approval-rules/${index}`, { method: "DELETE" });
    if (agentId === state.agentForm.originalId) drawApprovalRules(rules);
  } catch (err) {
    $("agent-approval-rules-error").textContent = err.message;
    changeApprovalRules();
  }
}

$("agent-approval-rules-reset").addEventListener("click", () => {
  if (confirm(tr("Remove every learned approval of this agent?"))) resetLearnedApprovals();
});

function renderTerminalBlock(agent) {
  const saved = agent.terminal ?? null;
  const rows = terminalBackendRows(state.user?.terminal_backends, saved);
  $("agent-terminal-no-backends").hidden = rows.length > 0;
  $("agent-terminal-backends").replaceChildren(...rows.map(buildTerminalBackendRow));
  setNumberValue($("agent-terminal-timeout"), saved?.timeout_seconds ?? agentDefault("terminal.timeout_seconds") ?? 60);
  $("agent-terminal-error").textContent = "";
  refreshTerminalDefault(saved?.default_backend);
  updateTerminalBlock();
  renderRuleEditor(agent.approval_rules);
  changeApprovalRules();
  loadEffectiveRules();
}

// The Delegation block follows the `delegate` tool group, like the Terminal block follows `terminal`.
function delegationGroupChecked() {
  return Boolean(document.querySelector('input[name="agent-tool-group"][value="delegate"]:checked'));
}

function updateDelegationBlock() {
  const enabled = delegationGroupChecked();
  $("agent-delegation").hidden = !enabled;
  $("agent-delegation").disabled = !enabled;  // a disabled fieldset is not validated, so hidden fields never block saving
}

function renderDelegationBlock(agent) {
  const saved = agent.delegation ?? null;
  $("agent-delegation-enabled").checked = saved?.enabled ?? agentDefault("delegation.enabled") ?? true;
  renderDelegationGroups(saved?.allowed_groups ?? DELEGATION_DEFAULT_GROUPS);
  setNumberValue($("agent-delegation-parallel"), saved?.max_parallel ?? null);
  setNumberValue($("agent-delegation-timeout"), saved?.timeout_seconds ?? null);
  setNumberValue($("agent-delegation-tokens"), saved?.max_tokens ?? null);
  $("agent-delegation-model").value = saved?.default_model ?? "";
  renderDelegationTargets(delegationTargetRows(state.agentConfigs, state.agentForm.originalId, saved));
  $("agent-delegation-error").textContent = "";
  updateDelegationBlock();
}

function renderDelegationGroups(selected) {
  const chosen = new Set(selected);
  const names = new Map(state.toolGroupCatalog.map((group) => [group.id, translatedText(group.name)]));
  $("agent-delegation-groups").replaceChildren(...DELEGATION_GRANTABLE_GROUPS.map((id) => {
    const label = document.createElement("label");
    label.className = "model-filter";
    const box = document.createElement("input");
    box.type = "checkbox";
    box.name = "agent-delegation-group";
    box.value = id;
    box.checked = chosen.has(id);
    const text = document.createElement("span");
    text.textContent = names.get(id) ?? id;
    label.append(box, text);
    return label;
  }));
}

function readDelegationGroups() {
  return [...document.querySelectorAll('input[name="agent-delegation-group"]:checked')].map((box) => box.value);
}

function renderDelegationTargets(rows) {
  $("agent-delegation-no-agents").hidden = rows.length > 0;
  $("agent-delegation-targets").replaceChildren(...rows.map((row) => {
    const box = document.createElement("div");
    box.className = "terminal-backend delegation-target";
    box.dataset.agent = row.id;
    const allow = document.createElement("input");
    allow.type = "checkbox";
    allow.className = "delegation-allow";
    allow.checked = row.allowed;
    const head = document.createElement("label");
    head.className = "model-filter";
    const name = document.createElement("span");
    name.textContent = row.missing ? tr("{name} (not defined)", { name: row.name }) : row.name;
    head.append(allow, name);
    const remember = document.createElement("input");
    remember.type = "checkbox";
    remember.className = "delegation-remember";
    remember.checked = row.remember;
    remember.disabled = !row.allowed;
    const memoryLabel = document.createElement("label");
    memoryLabel.className = "model-filter";
    const memoryText = document.createElement("span");
    memoryText.textContent = tr("Can write its own memory");
    memoryLabel.append(remember, memoryText);
    memoryLabel.hidden = !row.allowed;
    allow.addEventListener("change", () => {
      remember.disabled = !allow.checked;
      memoryLabel.hidden = !allow.checked;
      if (!allow.checked) remember.checked = false;
    });
    box.append(head, memoryLabel);
    return box;
  }));
}

// The form's target rows as plain data (also what buildDelegationConfig takes).
function readDelegationTargetFields() {
  return [...document.querySelectorAll("#agent-delegation-targets .delegation-target")].map((box) => ({
    id: box.dataset.agent,
    allowed: box.querySelector(".delegation-allow").checked,
    remember: box.querySelector(".delegation-remember").checked,
  }));
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
  const info = (state.user?.terminal_backends ?? []).find((backend) => backend.name === row.name);
  if (info?.kind) {  // read only: the kind and the level come from the user's configuration (user.yaml)
    box.append(make("p", "model-note", tr("Kind: {kind}. Needs the level {level}.", { kind: info.kind, level: levelTitle(state.levels, info.min_level) })));
  }
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
  const optionalNumber = numberValue;
  const name = $("agent-name").value.trim();
  const saved = state.agentConfigs.find((agent) => agent.id === state.agentForm.originalId);
  return {
    id: state.agentForm.originalId ?? agentIdFromName(name),
    name,
    description: $("agent-description").value.trim(),
    avatar_style: $("agent-avatar-free").checked ? "free" : "circle",
    // edited in the Dream tab; sent back unchanged so saving the agent does not reset it
    ...(saved ? { dream: saved.dream } : {}),
    folders: agentFoldersBody(),
    ...levelPayload($("agent-level").value, state.agentForm.initialLevel, state.agentForm.levelTouched),
    ...ruleEditorPayload(),
    terminal: buildTerminalConfig(saved?.terminal, terminalGroupChecked(), {
      backends: readTerminalBackendFields(), defaultBackend: $("agent-terminal-default").value,
      timeout: numberValue("agent-terminal-timeout"),
    }),
    // the saved block is sent back untouched while the group is off, and from the form while it is on
    delegation: buildDelegationConfig(saved?.delegation, delegationGroupChecked(), {
      enabled: $("agent-delegation-enabled").checked, allowedGroups: readDelegationGroups(),
      maxParallel: numberValue("agent-delegation-parallel"), timeout: numberValue("agent-delegation-timeout"),
      maxTokens: numberValue("agent-delegation-tokens"), defaultModel: $("agent-delegation-model").value,
      targets: readDelegationTargetFields(),
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
      max_tool_hops: optionalNumber("agent-max-hops"),
      compaction: compactionBody(),
      tools: $("agent-tools").checked,
      tool_groups: [...document.querySelectorAll('input[name="agent-tool-group"]:checked')].map((input) => input.value),
      mcp: agentMcpBody(),
      associative_memory: $("agent-associative").value,
      associative_execution: $("agent-associative-execution").value,
    },
  };
}

// the compaction block of the form; an empty number falls back to the default of the chosen units
function compactionBody() {
  const mode = $("agent-compaction-mode").value || "percent";
  const fallback = mode === "percent" ? { trigger: 50, keep_recent: 20 } : { trigger: 100_000, keep_recent: 20_000 };
  return {
    enabled: $("agent-compaction-enabled").checked,
    mode,
    trigger: numberValue("agent-compaction-trigger") ?? fallback.trigger,
    keep_recent: numberValue("agent-compaction-keep") ?? fallback.keep_recent,
    model: $("agent-compaction-model").value.trim() || null,
  };
}

function syncCompactionUnits() {
  const unit = $("agent-compaction-mode").value === "tokens" ? "tokens" : "percent";
  for (const id of ["agent-compaction-trigger", "agent-compaction-keep"]) $(id).dataset.unit = unit;
  const off = !$("agent-compaction-enabled").checked;
  for (const id of ["agent-compaction-mode", "agent-compaction-trigger", "agent-compaction-keep", "agent-compaction-model"]) $(id).disabled = off;
  refreshNumberFields();
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
  state.user.max_level = me.max_level ?? state.user.max_level;
  renderAgentOptions();
  renderAgentsEmpty();
  await loadAgentConfigs();
  renderAgentBanner();   // the saved picture style (circle or free) reaches the chat header at once
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

$("agent-compaction-enabled").addEventListener("change", syncCompactionUnits);
$("agent-compaction-mode").addEventListener("change", syncCompactionUnits);
$("agent-associative").addEventListener("change", () => {
  $("agent-associative-execution").disabled = $("agent-associative").value === "off";
});
$("agent-tokens-enabled").addEventListener("change", () => {
  const enabled = $("agent-tokens-enabled").checked;
  $("agent-reasoning-tokens").disabled = !enabled;
  if (enabled && !$("agent-reasoning-tokens").value) setNumberValue($("agent-reasoning-tokens"), 1024);
});

$("agent-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (state.agentForm.busy) return;
  const invalid = firstInvalidNumber($("agent-form"));
  if (invalid) { revealField(invalid); return; }
  const session = state.session;
  const previousId = state.agentForm.originalId;
  const body = JSON.stringify(agentFormBody());
  const soul = previousId ? "" : $("agent-prompt").value;
  setAgentBusy(true);
  $("agent-error").textContent = "";
  $("agent-terminal-error").textContent = "";
  $("agent-delegation-error").textContent = "";
  $("agent-rules-error").textContent = "";
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
      if (isLevelErrorCode(err.code)) {
        $("agent-level-message").textContent = err.message;
        $("agent-error").textContent = err.message;
        showAgentPanel("basic");
      } else if (terminalGroupChecked() && isRuleError(err)) {
        $("agent-rules-error").textContent = err.message;
        showAgentPanel("tools");
      } else if (terminalGroupChecked() && isTerminalError(err.message)) {
        $("agent-terminal-error").textContent = err.message;
        showAgentPanel("tools");
      } else if (delegationGroupChecked() && isDelegationError(err.message)) {
        $("agent-delegation-error").textContent = err.message;
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
  $("messages").classList.toggle("hide-agent-avatar", preferences.show_agent_avatar === false);
  translateInterface();
  updateModelControls();
}

async function applyServerPreferences(preferences, session, request, models = true) {
  if (preferences.language === "es" && !state.translations) {
    state.translations = await api("/api/localization?language=es");
  }
  if (session === state.session && request === state.preferencesRequest && state.user) {
    applyPreferences(preferences);
    if (models) await refreshChatModels(session, request);
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

// True when `err` only says the open chat no longer exists and that chat is still empty (nothing sent): the server
// prunes empty chats, so this is expected, not a failure. The page keeps the draft, the recording and the attachments
// as they are; sending answers in a new chat (`chat_replaced`), which `adoptReplacedChat` follows.
function vanishedEmptyChat(err, view) {
  const gone = err?.code === "unknown_chat" || err?.status === 404;
  // `history.total` is what the server had when the chat was drawn; the bubble of the message being sent does not count
  return Boolean(gone && view && view.history?.total === 0);
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
    // an empty chat pruned meanwhile must not make a send fail (and lose the draft): keep the models shown
    if (current() && !vanishedEmptyChat(err, state.view)) throw err;
  }
}

// `models: false` leaves the open chat's models alone (opening another chat reads the new chat's own with the chat itself).
async function refreshPreferences({ models = true } = {}) {
  if (state.settingsBusy && state.preferences) return state.preferences;
  const session = state.session;
  const request = ++state.preferencesRequest;
  const preferences = await api("/api/preferences", { cache: "no-store" });
  await applyServerPreferences(preferences, session, request, models);
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
      updateSettingsUi();
      updateBusy();
    }
  }
}

let settingsSavedTimer = null;

// What the application follows for a preference nobody set; `defaults` arrives with GET /api/preferences.
function prefDefault(key) {
  const entry = state.preferences?.defaults?.[key];
  return entry !== null && typeof entry === "object" && "default" in entry ? entry.default : entry;
}

const SETTINGS_KEYS = Object.values(SETTINGS_SECTIONS).flat();

// The effective values loaded into the dialog (a null in preferences.json shows as its default).
function settingsOriginalValues() {
  const saved = state.settingsOriginal;
  return {
    show_thinking: saved.show_thinking, show_agent_avatar: saved.show_agent_avatar, language: saved.language,
    default_llm_model: saved.default_llm_model, default_stt_model: saved.default_stt_model,
    stt_language: saved.stt_language ?? null,
    associative_max_matches: saved.associative_max_matches ?? prefDefault("associative_max_matches"),
    disposable_retention_days: saved.disposable_retention_days ?? prefDefault("disposable_retention_days"),
    orchestrator: saved.orchestrator ?? saved.orchestrator_default ?? false,
    core_agent_id: saved.core_agent_id ?? null,
    default_agent_id: saved.default_agent_id ?? null,
  };
}

function settingsCurrentValues() {
  const number = (id, key) => {
    const value = numberValue(id);
    return value === null ? prefDefault(key) : value; // an empty field means the default
  };
  return {
    show_thinking: $("settings-thinking").checked, show_agent_avatar: $("settings-agent-avatar").checked, language: $("settings-language").value,
    default_llm_model: state.settingsModelDraft.default_llm_model, default_stt_model: state.settingsModelDraft.default_stt_model,
    stt_language: $("settings-stt-language").value || null,
    associative_max_matches: number("settings-assoc-max", "associative_max_matches"),
    disposable_retention_days: number("settings-disposable-days", "disposable_retention_days"),
    orchestrator: $("settings-orchestrator").checked, core_agent_id: $("settings-core-agent").value || null,
    default_agent_id: $("settings-default-agent").value || null,
  };
}

function pendingSettings() {
  if (!state.settingsOriginal || !state.settingsModelDraft) return {};
  const defaults = Object.fromEntries(SETTINGS_KEYS.map((key) => [key, prefDefault(key)]));
  return settingsChanges(settingsCurrentValues(), settingsOriginalValues(), defaults);
}

// Pending-changes text, dots on the section tabs and the Save button follow the form.
function updateSettingsUi() {
  const changes = pendingSettings();
  const count = Object.keys(changes).length;
  $("settings-pending").textContent = count === 0 ? "" : count === 1 ? tr("1 unsaved change") : tr("{count} unsaved changes", { count });
  const counts = sectionChangeCounts(changes);
  for (const tab of $("settings-nav").querySelectorAll("button")) {
    const dot = tab.querySelector(".settings-dot");
    if (dot) dot.hidden = !counts[tab.dataset.section];
  }
  $("settings-save").disabled = state.settingsBusy || !state.settingsOriginal || count === 0;
  return changes;
}

function showSettingsSection(name) {
  const users = name === "users" && isAdministrator(state.user);  // Users and Providers sit outside the settings form, like Connections
  const providers = name === "providers";
  const outside = users || providers;
  const view = settingsSectionView(outside ? "connections" : name);
  for (const tab of $("settings-nav").querySelectorAll("button")) {
    tab.setAttribute("aria-pressed", String(outside ? tab.dataset.section === (users ? "users" : "providers") : view[tab.dataset.section]?.pressed === true));
  }
  for (const section of Object.keys(view)) {
    if (section === "connections") continue;
    $(`settings-section-${section}`).hidden = view[section].hidden;
  }
  $("users-admin").hidden = !users;
  $("providers-section").hidden = !providers;
  if (outside) {
    $("settings-form").hidden = true;
    $("connections").hidden = true;
    stopConnectionsPoll();
    if (users) loadUsersAdmin();
    else { loadClaudeLogin(); loadOpenRouterKey(); }
    return;
  }
  $("settings-form").hidden = !view.connections.hidden;
  $("connections").hidden = view.connections.hidden;
  if (view.connections.hidden) stopConnectionsPoll();
  else if (state.mcpServers.some((server) => server.pending)) startConnectionsPoll();
}

function fillSettings() {
  state.settingsOriginal = { ...state.preferences };
  state.settingsModelDraft = {
    default_llm_model: state.preferences.default_llm_model,
    default_stt_model: state.preferences.default_stt_model,
  };
  $("settings-thinking").checked = state.preferences.show_thinking;
  $("settings-agent-avatar").checked = state.preferences.show_agent_avatar;
  $("settings-language").value = state.preferences.language;
  const original = settingsOriginalValues();
  setNumberValue($("settings-assoc-max"), original.associative_max_matches);
  setNumberValue($("settings-disposable-days"), original.disposable_retention_days);
  $("settings-orchestrator").checked = original.orchestrator;
  const core = $("settings-core-agent");
  core.replaceChildren(new Option(tr("Same as default agent"), ""), ...(state.user?.agents ?? []).map((a) => new Option(a.name, a.id)));
  core.value = state.preferences.core_agent_id ?? "";
  const defaultAgent = $("settings-default-agent");
  defaultAgent.replaceChildren(new Option(tr("First agent in the list"), ""), ...(state.user?.agents ?? []).map((a) => new Option(a.name, a.id)));
  defaultAgent.value = state.preferences.default_agent_id ?? "";
  fillSttLanguage(state.preferences.stt_language ?? null);
  $("settings-theme").value = localStorage.getItem("theme") ?? "system";
  $("settings-avatar-error").textContent = "";
  renderUserAvatarField();
  renderSettingsModels();
}

// The language list is fixed (ISO-639-1); a region code or other value already configured is added so it still shows.
function fillSttLanguage(value) {
  const select = $("settings-stt-language");
  const known = new Set([...select.options].filter((option) => !option.dataset.extra).map((option) => option.value));
  for (const option of [...select.options]) if (option.dataset.extra) option.remove();
  for (const extra of [prefDefault("stt_language"), value]) {
    if (extra && !known.has(extra) && ![...select.options].some((option) => option.value === extra)) {
      const option = new Option(extra, extra);
      option.dataset.extra = "1";
      select.add(option);
    }
  }
  select.value = value ?? "";
}

function renderSettingsModels() {
  $("settings-llm").textContent = state.settingsModelDraft?.default_llm_model ?? "";
  $("settings-stt").textContent = state.settingsModelDraft?.default_stt_model ?? "";
  const disabled = !state.settingsOriginal || !state.user || state.settingsBusy;
  $("settings-llm").disabled = disabled;
  $("settings-stt").disabled = disabled;
  refreshRows($("settings-form"));
  updateSettingsUi();
}

$("settings-open").addEventListener("click", async () => {
  const session = state.session;
  state.settingsOriginal = null;
  state.settingsModelDraft = null;
  clearTimeout(settingsSavedTimer);
  $("settings-saved").textContent = "";
  $("settings-tab-users").hidden = !isAdministrator(state.user);
  showSettingsSection("general");
  renderSettingsModels();
  $("settings-error").textContent = "";
  $("settings-dialog").showModal();
  loadConnections();
  try {
    await refreshPreferences();
    if (session !== state.session) return;
    fillSettings();
  } catch (err) {
    if (session === state.session) $("settings-error").textContent = err.message;
  }
});

$("settings-nav").addEventListener("click", (event) => {
  const tab = event.target.closest("button[data-section]");
  if (tab) showSettingsSection(tab.dataset.section);
});
for (const type of ["input", "change"]) $("settings-form").addEventListener(type, updateSettingsUi);
$("settings-theme").addEventListener("change", (e) => applyTheme(e.target.value));
$("theme-toggle").addEventListener("click", () => {
  applyTheme(effectiveTheme() === "dark" ? "light" : "dark");
  $("settings-theme").value = localStorage.getItem("theme") ?? "system";
  refreshRows($("settings-form"));
});
// closing with unsaved changes asks first (the Close button, Escape)
function requestSettingsClose() {
  if (Object.keys(pendingSettings()).length && !window.confirm(tr("You have unsaved changes. Close and discard them?"))) return;
  $("settings-dialog").close();
}
$("settings-close").addEventListener("click", requestSettingsClose);
$("settings-dialog").addEventListener("cancel", (event) => { event.preventDefault(); requestSettingsClose(); });
$("settings-dialog").addEventListener("close", stopConnectionsPoll);

// ------------------------------------------------------------------ users (administrators only): maximum levels

// Only a user whose maximum level is admin sees the section; the server checks it again on every call.
function isAdministrator(user) {
  return user?.max_level === "admin";
}

async function loadUsersAdmin() {
  const session = state.session;
  $("users-error").textContent = "";
  try {
    const [users, levels] = await Promise.all([api("/api/admin/users"), state.levels ? state.levels : api("/api/levels").catch(() => null)]);
    if (session !== state.session) return;
    state.levels = levels;
    state.adminUsers = users;
    renderUsersAdmin();
  } catch (err) {
    if (session === state.session) $("users-error").textContent = err.message;
  }
}

function renderUsersAdmin() {
  $("users-list").replaceChildren(...state.adminUsers.map(userAdminItem));
}

function userAdminItem(user) {
  const item = document.createElement("li");
  item.className = "connection user-admin";
  const heading = document.createElement("div");
  heading.append(Object.assign(document.createElement("strong"), { textContent: user.name ?? user.id }), ` (${user.id}) `);
  if (user.error) {
    item.append(heading, Object.assign(document.createElement("p"), { className: "error", textContent: user.error }));
    return item;
  }
  heading.append(levelBadge(user.max_level));
  heading.append(` · ${tr("Agents: {count}", { count: user.agent_count })}`);
  item.append(heading);
  const agents = user.agents.map((agent) => `${agent.name} (${levelTitle(state.levels, agent.level)})`).join(", ");
  item.append(Object.assign(document.createElement("p"), { className: "model-note", textContent: agents || tr("No agents.") }));
  const row = document.createElement("div");
  row.className = "agent-actions";
  const select = document.createElement("select");
  select.setAttribute("aria-label", tr("Maximum level of {user}", { user: user.id }));
  select.append(...(state.levels?.levels ?? []).map((level) => Object.assign(
    document.createElement("option"), { value: level.name, textContent: `${level.title} (${level.number})` },
  )));
  select.value = user.max_level;
  const apply = Object.assign(document.createElement("button"), { type: "button", textContent: tr("Set maximum level"), disabled: true });
  const note = Object.assign(document.createElement("p"), { className: "error" });
  const refresh = () => {
    const above = agentsAboveCeiling(state.levels, user.agents, select.value);
    note.textContent = above.length
      ? tr("These agents would be above the new maximum: {agents}. Lower them first.", {
        agents: above.map((agent) => `${agent.name} (${levelTitle(state.levels, agent.level)})`).join(", "),
      })
      : "";
    apply.disabled = select.value === user.max_level || above.length > 0;
  };
  select.addEventListener("change", refresh);
  apply.addEventListener("click", () => setUserMaxLevel(user, select.value, apply, note));
  if (user.self) {
    select.disabled = true;
    row.append(select, Object.assign(document.createElement("span"), { className: "model-note", textContent: tr("You cannot change your own maximum level.") }));
  } else {
    row.append(select, apply);
  }
  item.append(row, note);
  return item;
}

async function setUserMaxLevel(user, level, button, note) {
  const session = state.session;
  button.disabled = true;
  note.textContent = "";
  $("users-status").textContent = "";
  try {
    const updated = await api(`/api/admin/users/${encodeURIComponent(user.id)}/max-level`, {
      method: "PUT", body: JSON.stringify({ max_level: level }),
    });
    if (session !== state.session) return;
    state.adminUsers = state.adminUsers.map((item) => (item.id === updated.id ? updated : item));
    renderUsersAdmin();
    $("users-status").textContent = tr("Maximum level of {user} set to {level}.", { user: user.id, level: levelTitle(state.levels, updated.max_level) });
  } catch (err) {
    if (session === state.session) {
      note.textContent = err.message;
      button.disabled = false;
    }
  }
}

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
    case "dream-worker-llm": return $("dream-delegate-model").value.trim() || undefined;
    case "agent-llm": return $("agent-model").value.trim() || state.preferences?.default_llm_model;
    case "agent-worker-llm": return $("agent-delegation-model").value.trim() || undefined;
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
      const experimental = tr("experimental");
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

// ---- OpenRouter key (Settings, Providers): the key goes to the server and is never shown again
function renderOpenRouterKey(report, error = "") {
  const box = $("openrouter-key");
  box.hidden = !report;
  $("openrouter-key-error").textContent = error;
  if (!report) return;
  $("openrouter-key-state").textContent = !report.configured ? tr("No key set")
    : report.own ? tr("Your own key is set") : tr("A key shared through the server is in use");
  $("openrouter-key-remove").hidden = !report.own;
}
async function loadOpenRouterKey(error = "") {
  const session = state.session;
  try {
    const report = await api("/api/openrouter/key", { cache: "no-store" });
    if (session === state.session) renderOpenRouterKey(report, error);
  } catch {
    if (session === state.session) renderOpenRouterKey(null);
  }
}
$("openrouter-key-save").addEventListener("click", async () => {
  const key = $("openrouter-key-input").value.trim();
  if (!key) return;
  $("openrouter-key-save").disabled = true;
  let error = "";
  try {
    await api("/api/openrouter/key", { method: "PUT", body: JSON.stringify({ key }) });
  } catch (err) {
    error = err.message;
  } finally {
    $("openrouter-key-save").disabled = false;
  }
  if (!error) $("openrouter-key-input").value = "";
  await loadOpenRouterKey(error);
});
$("openrouter-key-remove").addEventListener("click", async () => {
  let error = "";
  try { await api("/api/openrouter/key", { method: "DELETE" }); } catch (err) { error = err.message; }
  await loadOpenRouterKey(error);
});
$("openrouter-key-input").addEventListener("keydown", (event) => {
  if (event.key === "Enter") { event.preventDefault(); $("openrouter-key-save").click(); }
});
// ---- end OpenRouter key

// ---- Claude subscription sign-in (Settings, Connections)
let claudeLoginBusy = false;
function claudeLoginMessage(err) {
  return err?.message || tr("Claude Code login unknown");
}
function renderClaudeLogin(report, error = "") {
  const box = $("claude-login");
  box.hidden = !report?.available;
  $("claude-login-error").textContent = error;
  if (!report?.available) return;
  const step = !$("claude-login-step").hidden;
  let text = tr("Claude Code login unknown");
  if (report.logged_in === true) text = report.subscription ? tr("Connected ({plan})", { plan: report.subscription }) : tr("Connected");
  else if (report.logged_in === false) text = tr("Not signed in");
  $("claude-login-state").textContent = text;
  $("claude-login-start").textContent = report.logged_in ? tr("Sign in again") : tr("Sign in");
  $("claude-login-start").disabled = claudeLoginBusy || !report.cli_found;
  $("claude-login-actions").hidden = step;
}
async function loadClaudeLogin(error = "") {
  const session = state.session;
  try {
    const report = await api("/api/claude/login", { cache: "no-store" });
    if (session === state.session) renderClaudeLogin(report, error);
  } catch {
    if (session === state.session) renderClaudeLogin(null);
  }
}
function closeClaudeLoginStep() {
  $("claude-login-step").hidden = true;
  $("claude-login-code").value = "";
  $("claude-login-link").removeAttribute("href");
}
$("claude-login-start").addEventListener("click", async () => {
  claudeLoginBusy = true;
  $("claude-login-start").disabled = true;
  $("claude-login-error").textContent = "";
  try {
    const { url } = await api("/api/claude/login/start", { method: "POST" });
    $("claude-login-link").href = url;
    $("claude-login-step").hidden = false;
    $("claude-login-actions").hidden = true;
    $("claude-login-code").focus();
  } catch (err) {
    $("claude-login-error").textContent = claudeLoginMessage(err);
  } finally {
    claudeLoginBusy = false;
    $("claude-login-start").disabled = false;
  }
});
$("claude-login-send").addEventListener("click", async () => {
  const code = $("claude-login-code").value.trim();
  if (!code) return;
  $("claude-login-send").disabled = true;
  let error = "";
  try {
    await api("/api/claude/login/code", { method: "POST", body: JSON.stringify({ code }) });
  } catch (err) {
    error = claudeLoginMessage(err);
  } finally {
    $("claude-login-send").disabled = false;
  }
  closeClaudeLoginStep();
  await loadClaudeLogin(error);
  if (!error) { refreshClaudeUsage() }
});
$("claude-login-cancel").addEventListener("click", async () => {
  closeClaudeLoginStep();
  try { await api("/api/claude/login/start", { method: "DELETE" }); } catch { /* nothing waits any more */ }
  loadClaudeLogin();
});
$("claude-login-code").addEventListener("keydown", (event) => {
  if (event.key === "Enter") { event.preventDefault(); $("claude-login-send").click(); }
});
// ---- end Claude subscription sign-in

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
  if (server.auth_type === "token") {
    item.append(tokenConnection(server));
  } else if (server.requires_auth) {
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

// Servers reached over HTTP: the user pastes their own access token. It is sent once to
// RECALL, checked against the server and stored in the user's private folder; it is never
// shown again or put in a chat.
function tokenConnection(server) {
  const box = document.createElement("div");
  if (server.token_help) {
    box.append(Object.assign(document.createElement("p"), { className: "model-note", textContent: server.token_help }));
  }
  const form = document.createElement("form");
  form.className = "agent-actions mcp-token-form";
  const input = Object.assign(document.createElement("input"), {
    type: "password", autocomplete: "off", spellcheck: false, maxLength: 4096, required: true,
    placeholder: server.connected ? tr("Paste a new token to replace it") : (server.token_label || tr("Access token")),
  });
  input.setAttribute("aria-label", server.token_label || tr("Access token"));
  const save = Object.assign(document.createElement("button"), {
    type: "submit", textContent: tr(server.connected ? "Replace token" : "Connect"),
  });
  form.append(input, save);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const token = input.value.trim();
    input.value = "";
    if (token) changeConnection(server, "token", token);
  });
  box.append(form);
  if (server.connected) {
    const disconnect = Object.assign(document.createElement("button"), { type: "button", textContent: tr("Disconnect") });
    disconnect.addEventListener("click", () => {
      if (window.confirm(tr("Disconnect {server}? Your stored credentials for it are deleted.", { server: server.name }))) {
        changeConnection(server, "disconnect");
      }
    });
    const actions = document.createElement("div");
    actions.className = "agent-actions";
    actions.append(disconnect);
    box.append(actions);
  }
  return box;
}

async function changeConnection(server, action, token) {
  const session = state.session;
  $("connections-error").textContent = "";
  for (const button of $("connection-list").querySelectorAll("button")) button.disabled = true;
  try {
    const status = action === "token"
      ? await api(`/api/mcp/${server.id}/token`, { method: "PUT", body: JSON.stringify({ token }) })
      : await api(
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
  if (state.connectionsPoll || !$("settings-dialog").open || $("connections").hidden) return;
  state.connectionsPoll = setInterval(loadConnections, 3000);
}

function stopConnectionsPoll() {
  clearInterval(state.connectionsPoll);
  state.connectionsPoll = null;
}
$("settings-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (state.settingsBusy || !state.settingsOriginal) return;
  const invalid = firstInvalidNumber($("settings-form"));
  if (invalid) {
    $("settings-error").textContent = tr("Fix the highlighted values before saving.");
    revealField(invalid);
    return;
  }
  const session = state.session;
  const changes = pendingSettings();
  if (!Object.keys(changes).length) return;
  $("settings-error").textContent = "";
  try {
    await savePreferences(changes);
    if (session !== state.session) return;
    fillSettings();
    $("settings-saved").textContent = tr("Saved");
    clearTimeout(settingsSavedTimer);
    settingsSavedTimer = setTimeout(() => { $("settings-saved").textContent = ""; }, 4000);
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

// the open chat is being read: clear the server's unread mark (a reply that ends while it is open is read too)
function markChatRead(agentId, chatId) {
  api(`/api/agents/${encodeURIComponent(agentId)}/chats/${encodeURIComponent(chatId)}/read`, { method: "POST" }).catch(() => {});
}

async function refreshChats() {
  if (!state.user) return;
  const session = state.session;
  const keep = state.chatId ? `?keep=${encodeURIComponent(state.chatId)}` : "";
  const chats = await api(`/api/chats${keep}`);
  if (session !== state.session) return;
  const list = $("chat-list");
  let section = null;
  list.replaceChildren(
    ...chats.flatMap((chat) => {
      const items = [];
      const label = chatSection(chat.updated);
      if (label !== section) {
        section = label;
        const heading = document.createElement("li");
        heading.className = "chat-section";
        heading.setAttribute("role", "presentation");
        heading.textContent = tr(label);
        items.push(heading);
      }
      const li = document.createElement("li");
      li.classList.toggle("active", chat.id === state.chatId && chat.agent_id === state.agentId);
      const generating = isGenerating(chat.agent_id, chat.id) || Boolean(chat.active) || chat.processes > 0;
      li.dataset.active = String(Boolean(chat.active));
      li.classList.toggle("generating", generating);
      // dot before the title: blinking while the agent works, fixed when it waits for you (approval) or its answer is unread
      const here = chat.id === state.chatId && chat.agent_id === state.agentId;
      const dot = chat.pending_approvals > 0 && !here ? "approval"
        : chat.unread && !here ? "ready" : generating ? "working" : "";
      if (dot) li.dataset.dot = dot;
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
      li.addEventListener("click", async () => {
        if (state.modelBusy || state.settingsBusy) return;
        highlightChat(chat.agent_id, chat.id);  // at once: the list is redrawn only after the chat and a second request
        try {
          await openChatFromList(chat);
        } catch (err) {
          highlightChat(state.agentId, state.chatId);  // it did not open: the list goes back to the chat that is
          throw err;
        }
      });
      items.push(li);
      return items;
    })
  );
}

// Marks one chat of the sidebar as the open one without redrawing the list (a click shows at once, before the chat loads).
function highlightChat(agentId, chatId) {
  for (const li of $("chat-list").children) {
    const del = li.querySelector(".del");
    if (del) li.classList.toggle("active", del.dataset.agent === agentId && del.dataset.chat === chatId);
  }
}

// the date group a chat belongs to in the sidebar, from the day of its last activity (local time)
function chatSection(updated) {
  const when = new Date(updated);
  if (Number.isNaN(when.getTime())) return "Older";
  const startOfDay = (date) => new Date(date.getFullYear(), date.getMonth(), date.getDate());
  const days = Math.round((startOfDay(new Date()) - startOfDay(when)) / 86400000);
  if (days <= 0) return "Today";
  if (days === 1) return "Yesterday";
  if (days <= 7) return "Previous 7 days";
  if (days <= 30) return "Previous 30 days";
  return "Older";
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
  if (state.modelBusy || state.settingsBusy || !state.user) return false;
  const session = state.session;
  try {
    const preferences = await refreshPreferences();
    if (session !== state.session || state.modelBusy || state.settingsBusy) return false;
    if (!preferences.agent_id) {
      renderNoAgents();
      return false;
    }
    if (preferences.agent_id !== state.agentId) await selectAgent(preferences.agent_id);
    const agentId = state.agentId;
    const chat = await api(chatsUrl(), {
      method: "POST", ...(disposable ? { body: JSON.stringify({ disposable: true }) } : {}),
    });
    if (session !== state.session || agentId !== state.agentId) return false;
    // creating a chat discards the previous empty one; stop requesting its state while opening the new one
    state.chatId = null;
    state.reasoningEffort = null;
    await openChat(chat.id);
    return state.chatId === chat.id;
  } catch (err) {
    if (session === state.session) addMessage("assistant").error(err);
    return false;
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
  $("chat-make-disposable-text").textContent = tr("Make it disposable");
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
  const revision = ++state.chatModelRevision;
  // the preferences and the chat do not depend on each other, so they travel together (two round trips in series
  // before anything is drawn were most of a chat switch on a slow link); the chat carries its own models
  const [, data] = await Promise.all([refreshPreferences({ models: false }), api(chatsUrl(`/${chatId}${chatQuery()}`))]);
  if (session !== state.session || agentId !== state.agentId || revision !== state.chatModelRevision || state.modelBusy) return;
  state.chatId = chatId;
  state.chatInfo = data.chat;
  markChatRead(agentId, chatId);
  applyChatModels(data);
  if ($("agents-view").hidden) rememberChat(agentId, chatId, data.messages.length > 0);
  const key = chatKey(agentId, chatId);
  if (isGenerating(agentId, chatId) && state.views.has(key)) {
    activateView(state.views.get(key));
    syncUsage();
  } else {
    forgetApprovals(agentId, chatId);
    renderMessages(data.messages, data.window);
    // a reply started elsewhere (another tab, an earlier page load) is still running: replay it and follow it live
    if (data.chat.active) followReply(agentId, chatId);
  }
  updateBusy();
  syncApprovals(agentId, chatId);
  ensureTaskPolling();  // task cards and wake replies of this chat
  startProcessPolling();
  $("input").focus();
  await refreshChats();
}

async function deleteChat(chat) {
  if (isGenerating(chat.agent_id, chat.id) || state.modelBusy || !confirm(tr("Delete \"{title}\"?", { title: chat.title || tr("New chat") }))) return;
  await api(`/api/agents/${chat.agent_id}/chats/${chat.id}`, { method: "DELETE" });
  const remembered = readLastChat(localStorage, state.user.id);
  if (remembered && remembered.chatId === chat.id) clearLastChat(localStorage, state.user.id);
  if (state.chatId === chat.id && state.agentId === chat.agent_id) {
    if (parseChatHash(location.hash)?.chatId === chat.id) setDreamHash("");
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
  if (model.provider === "claude_subscription") return tr("Claude subscription");
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
  $("model-picker").title = [tr("Choose a model and routing policy for this chat"), ...parts.filter(Boolean)].join(" · ");
  updateReasoningEffortControls();
  renderDisposable();
  $("model-policy").classList.toggle("privacy-warning", openrouter && Boolean(state.model.require_zdr) && !state.model.zdr);
  $("model-refresh").disabled = state.modelLoading || state.modelBusy;
  $("model-close").disabled = state.modelBusy;
  const zdrApplies = state.modelProvider === "openrouter" || state.modelProvider === ANY_PROVIDER;
  $("model-zdr-label").hidden = !zdrApplies;
  $("model-zdr").disabled = state.modelBusy || state.modelLoading || state.modelTarget !== "chat-llm";
  const fieldTarget = ["dream-llm", "dream-worker-llm", "agent-llm", "agent-worker-llm"].includes(state.modelTarget);
  $("model-inherit").hidden = state.modelTarget.startsWith("default-");
  $("model-inherit").disabled = fieldTarget ? state.modelBusy : (!state.chatId || state.busy || state.modelBusy);
  // in a chat the entry falls back to the agent's default when it has one; elsewhere it empties the field
  $("model-inherit").textContent = tr(state.modelTarget === "chat-llm" && state.agentDefaultModel
    ? "Use agent default" : state.modelTarget === "agent-worker-llm" ? "Use chat model"
      : state.modelTarget === "dream-worker-llm" ? "Use the dream's model" : "Use user default");
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
    else if (state.modelTarget === "dream-worker-llm") selected = $("dream-delegate-model").value.trim();
    else if (state.modelTarget === "agent-llm") selected = $("agent-model").value.trim();
    else if (state.modelTarget === "agent-worker-llm") selected = $("agent-delegation-model").value.trim();
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
      : subscription ? tr("Experimental: uses your own Claude Code login.")
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
  if (target === "dream-llm" || target === "dream-worker-llm") {
    // only fills the Dream tab's field; Save dream settings stores it
    $(target === "dream-llm" ? "dream-model" : "dream-delegate-model").value = modelId ?? "";
    refreshRows($("dream-form"));
    $("model-dialog").close();
    return;
  }
  if (target === "agent-llm") {
    // only fills the agent form's field; saving the agent stores it
    $("agent-model").value = modelId ?? "";
    refreshRows($("agent-form"));
    $("model-dialog").close();
    return;
  }
  if (target === "agent-worker-llm") {
    // only fills the agent form's worker model field; saving the agent stores it
    $("agent-delegation-model").value = modelId ?? "";
    refreshRows($("agent-form"));
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
  if (target === "dream-llm" || target === "dream-worker-llm") {
    if (!state.dream?.agentId) return;
  } else if (target === "agent-llm" || target === "agent-worker-llm") {
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
  else if (target === "dream-worker-llm") subtitle = "Choose the model of the dream's reading workers. Use the dream's model clears the field.";
  else if (target === "agent-llm") subtitle = "Choose the default model for this agent. Use user default clears the field.";
  else if (target === "agent-worker-llm") subtitle = "Choose the default model of this agent's workers. Use chat model clears the field.";
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

// The voice recorder's state (see the voice message section below); declared first because setComposer reads it.
const voice = { recorder: null, stream: null, chunks: [], timer: null, startedAt: 0, discard: false };

function setComposer(enabled) {
  $("input").disabled = !enabled || state.modelBusy;
  $("attach").disabled = !enabled || state.modelBusy;
  $("mic").disabled = !voice.recorder && (!enabled || state.modelBusy || !voiceSupported());
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
    li.classList.toggle("generating", Boolean(del) && (isGenerating(del.dataset.agent, del.dataset.chat) || li.dataset.active === "true"));
  }
  setComposer(Boolean(state.chatId));
  updateEditButtons();
  updateModelControls();
  if ($("model-dialog").open) renderModelList();
  updateSendButton();
  renderAgentBanner();
}

// While a reply streams the button stops it; with text (or attachments) in the box it steers (sends) instead.
function sendMode() {
  if (!state.busy) return "send";
  return $("input").value.trim() || state.attachments.length ? "steer" : "stop";
}

function updateSendButton() {
  const mode = sendMode();
  const label = tr({ send: "Send", steer: "Steer", stop: "Stop" }[mode]);
  $("send").innerHTML = ICONS[mode];
  $("send").dataset.mode = mode;
  $("send").title = label;
  $("send").setAttribute("aria-label", label);
}
$("input").addEventListener("input", updateSendButton);

// Asks the server to stop the open chat's reply; its stream then ends with the partial answer kept.
async function stopReply() {
  const { agentId, chatId } = state;
  if (!agentId || !chatId) return;
  $("send").disabled = true;
  try {
    await api(`/api/agents/${agentId}/chats/${chatId}/stop`, { method: "POST" });
    beginStopping();
  } catch (err) {
    addMessage("assistant", state.view).error(err);
  } finally {
    setComposer(Boolean(state.chatId));
  }
}

// After Stop: running steps say "Stopping…"; if no `interrupted` event arrives within a few seconds, a notice says why.
const STOP_NOTICE_MS = 4000;
let stopNotice = null;
let stopTimer = null;

function beginStopping() {
  for (const node of runningSteps) {
    if (node._parts) continue;  // a task card has its own Stop
    node._stopping = true;
    node._status.textContent = tr("Stopping…");
  }
  clearTimeout(stopTimer);
  stopTimer = setTimeout(() => {
    stopTimer = null;
    if (stopNotice || !state.busy) return;
    stopNotice = document.createElement("div");
    stopNotice.className = "interruption";
    stopNotice.setAttribute("role", "status");
    stopNotice.textContent = tr("Still stopping: the action is still running and cannot be cut right now");
    (state.view ?? $("messages")).append(stopNotice);
  }, STOP_NOTICE_MS);
}

function endStopping() {
  clearTimeout(stopTimer);
  stopTimer = null;
  if (stopNotice) { stopNotice.remove(); stopNotice = null; }
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
  $("messages").classList.toggle("hide-agent-avatar", false);
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
  scrollDown(true);
}

// One attachment of a sent message. `data_url` (kept on the turn) lets the person see or hear what was sent.
function attachmentView(attachment) {
  const label = attachment.name || attachment.modality || "attachment";
  const kind = attachment.data_url && ["image", "audio", "video"].includes(attachment.modality) ? attachment.modality : null;
  const download = !kind && attachment.data_url && attachment.modality === "document";  // a file the agent sent
  const item = document.createElement(kind ? "div" : download ? "a" : "span");
  item.className = "attachment-name" + (kind ? ` attachment-${kind}` : "");
  if (download && (attachment.mime_type === "application/pdf" || /\.pdf$/i.test(label))) item.classList.add("attachment-pdf");
  item._attachment = attachment;
  if (!kind) {
    item.textContent = label;
    if (download) { item.href = attachment.data_url; item.download = label; item.title = tr("Download"); }
    return item;
  }
  const media = document.createElement(kind === "image" ? "img" : kind);
  media.src = attachment.data_url;
  if (kind === "image") { media.alt = label; media.loading = "lazy"; }
  else { media.controls = true; media.preload = "metadata"; }
  const caption = document.createElement("span");
  caption.className = "attachment-label";
  caption.textContent = label;
  item.append(media, caption);
  if (kind === "audio" && attachment.transcript) showTranscript(item, attachment.transcript);
  return item;
}

// The text speech-to-text heard, small, under a recording's player.
function showTranscript(item, transcript) {
  item._attachment.transcript = transcript;
  let heard = item.querySelector(":scope > .attachment-transcript");
  if (!heard) {
    heard = document.createElement("span");
    heard.className = "attachment-transcript";
    item.append(heard);
  }
  heard.textContent = transcript;
}

// The model reads a recording as "[AUDIO] Audio transcription:\n<text>" appended to the message; the person sees the text
// under the player instead, so those blocks are left out of the message text when the turn's recordings carry it.
function visibleUserText(text, media) {
  if (typeof text !== "string") return text;
  let shown = text;
  for (const item of media || []) {
    if (item.modality === "audio" && item.transcript) shown = shown.split(`[AUDIO] Audio transcription:\n${item.transcript}`).join("");
  }
  return shown === text ? text : shown.replace(/\n{3,}/g, "\n\n").trim();
}

// The viewer: an image opens large (click to zoom to full size), a PDF opens in the page; both can be downloaded.
let viewerUrl = null;

async function openViewer({ kind, src, name }) {
  const dialog = $("viewer-dialog");
  const body = $("viewer-body");
  body.classList.remove("zoomed");
  $("viewer-title").textContent = name || "";
  let href = src;
  if (kind === "pdf") {  // a data: URL is not allowed in a frame everywhere; a blob: URL of the same bytes is
    try { href = URL.createObjectURL(await (await fetch(src)).blob()); viewerUrl = href; } catch { href = src; }
    body.replaceChildren(element("iframe", { src: href, title: name || "PDF" }));
  } else {
    const image = element("img", { src, alt: name || "" });
    image.addEventListener("click", () => body.classList.toggle("zoomed"));
    body.replaceChildren(image);
  }
  const download = $("viewer-download");
  download.href = href;
  download.download = name || (kind === "pdf" ? "document.pdf" : "image");
  if (!dialog.open) dialog.showModal();
}

function closeViewer() {
  $("viewer-dialog").close();
}

$("viewer-close").addEventListener("click", closeViewer);
$("viewer-dialog").addEventListener("click", (event) => { if (event.target === $("viewer-dialog")) closeViewer(); });
$("viewer-dialog").addEventListener("close", () => {
  $("viewer-body").replaceChildren();
  if (viewerUrl) { URL.revokeObjectURL(viewerUrl); viewerUrl = null; }
});

// one listener for every picture and PDF in the messages (sent attachments, files from the agent, markdown images)
$("messages").addEventListener("click", (event) => {
  const image = event.target.closest(".attachment-image img, .msg .body img");
  if (image && !image.closest("a")) {
    event.preventDefault();
    openViewer({ kind: "image", src: image.currentSrc || image.src, name: image.alt || image.closest(".attachment-image")?.querySelector(".attachment-label")?.textContent });
    return;
  }
  const pdf = event.target.closest("a.attachment-pdf");
  if (pdf) {
    event.preventDefault();
    openViewer({ kind: "pdf", src: pdf.href, name: pdf.download });
  }
});

// How many messages a chat opens with. The rest waits behind a bar at the top ("load earlier").
const CHAT_PAGE = 25;
const CHAT_MORE = 25;

// `?limit=` for a chat request: ask for as many messages as the open view already shows (never fewer than a page).
function chatQuery(limit = CHAT_PAGE) {
  return `?limit=${Math.max(CHAT_PAGE, limit)}`;
}

// The bar above the first drawn message: how many are left out and buttons to bring them in.
function updateHistoryBar(container) {
  container.querySelector(":scope > .history-bar")?.remove();
  const { hidden, total } = container.history;
  if (hidden <= 0) return;
  const bar = document.createElement("div");
  bar.className = "history-bar";
  const label = document.createElement("span");
  label.textContent = tr("{hidden} earlier messages not shown (of {total})", { hidden, total });
  const more = document.createElement("button");
  more.type = "button";
  more.textContent = tr("Load {count} more", { count: Math.min(CHAT_MORE, hidden) });
  more.addEventListener("click", () => loadEarlier(container, CHAT_MORE));
  bar.append(label, more);
  if (hidden > CHAT_MORE) {
    const all = document.createElement("button");
    all.type = "button";
    all.textContent = tr("Load all");
    all.addEventListener("click", () => loadEarlier(container, 5000));
    bar.append(all);
  }
  container.prepend(bar);
}

// Loads older messages above the ones shown, keeping what the person is reading where it is.
async function loadEarlier(container, count) {
  const history = container.history;
  if (history.loading || history.hidden <= 0 || container !== state.view) return;
  history.loading = true;
  const bar = container.querySelector(":scope > .history-bar");
  bar?.querySelectorAll("button").forEach((button) => { button.disabled = true; });
  const { agentId, chatId } = { agentId: state.agentId, chatId: state.chatId };
  try {
    const data = await api(chatsUrl(`/${chatId}?limit=${count}&before=${history.firstIndex}`));
    if (container !== state.view || agentId !== state.agentId || chatId !== state.chatId) return;
    const box = $("messages");
    const keepFromBottom = box.scrollHeight - box.scrollTop;  // distance to the bottom stays: the new content grows above
    const older = document.createElement("div");
    older.className = "chat-view";
    drawMessages(older, data.messages, { usage: false });
    bar?.remove();
    container.prepend(...older.childNodes);
    history.hidden = data.window.hidden;
    history.firstIndex = data.window.first_index ?? history.firstIndex;
    history.total = data.window.total;
    container.historyLimit += data.messages.length;
    updateHistoryBar(container);
    stuckToBottom = false;  // the observer must not jump to the end for content added at the top
    box.scrollTop = box.scrollHeight - keepFromBottom;
    updateJumpLatest();
  } catch (err) {
    bar?.querySelectorAll("button").forEach((button) => { button.disabled = false; });
    addMessage("assistant", container).error(err);
  } finally {
    history.loading = false;
  }
}

// The end of a long chat is drawn first: `window` (from the server: {total, hidden, first_index}) says how many earlier
// messages are left out, and a bar above the first one loads them. `limit` is how many messages the view asks for again
// when the chat is reloaded (after a reply, an edit, a followed run), so a longer history the user opened stays open.
function renderMessages(messages, window = null, limit = CHAT_PAGE) {
  const container = document.createElement("div");
  container.className = "chat-view";
  container.historyLimit = limit;
  container.history = { hidden: window?.hidden ?? 0, total: window?.total ?? messages.length, firstIndex: window?.first_index ?? null, loading: false };
  state.view = container;
  $("messages").replaceChildren(container);
  $("usage").hidden = true;
  drawMessages(container, messages, { usage: true });
  // the stored history may carry no report (a reply still running, an older chat): keep this chat's last known one
  if ($("usage").hidden) syncUsage();
  updateHistoryBar(container);
  scrollDown(true);
}

// Draws stored messages into `parent`. `usage: false` (older messages loaded later) leaves the usage readout alone: it
// belongs to the latest reply.
function drawMessages(container, messages, { usage }) {
  for (const m of messages) {
    if (isWakeMessage(m)) {  // a subagent result, not something the user wrote
      container.append(wakeBubble(m));
      continue;
    }
    if (m.role === "compaction") {  // the older messages above reach the model only as a summary; they stay visible
      container.append(compactionDivider(m));
      continue;
    }
    const author = m.role === "assistant" && m.author_agent
      ? replyAuthor(m.author_agent, m.author_name, state.user?.agents, state.agentId) : null;
    const view = addMessage(m.role, container, author);
    if (m.role === "user" && m.index != null) setEditable(view.el, m.index);
    if (m.associative_skipped) view.memory({ state: "skipped" });
    else if (m.associative_matches != null) {
      view.memory({
        state: "done", matches: m.associative_matches,
        fragments: m.associative_fragments, query: m.associative_query,
      });
    }
    if (m.role === "assistant") {
      if (m.media?.length) view.agentFiles(m.media);
      for (const step of historySteps(m)) {
        if (step.type === "reasoning") view.thinking(step.text);
        else if (step.type === "tool") view.action(step.action);
        else view.text(step.text);
      }
    } else {
      view.text(visibleUserText(m.content, m.media));
      if (m.media?.length) view.attachments(m.media);
    }
    if (m.interrupted) view.interrupted(null, m.interrupted_by);
    if (!usage) continue;
    if (m.usage_report) showUsage(m.usage_report);
    else if (m.role === "assistant" && m.usage?.in != null) {
      const output = m.usage.out == null ? "unknown" : m.usage.out;
      showUsage(
        `in ${thousands(m.usage.in)} · out ${m.usage.out == null ? output : thousands(m.usage.out)}\n`
        + "Detailed context breakdown is unavailable for this older reply."
      );
    }
  }
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
    const attachments = [...el.querySelectorAll(".message-attachments .attachment-name")]
      .map((n) => n._attachment ?? { name: n.textContent });
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
  const data = await api(chatsUrl(`/${request.chatId}${chatQuery(state.view?.historyLimit)}`));
  if (!isCurrent(request) || isGenerating(request.agentId, request.chatId)) return;
  // pair user bubbles with the stored user messages in order; the assistant bubbles are left out of the count on
  // purpose: a stopped, steered or failed reply can leave more or fewer of them than stored turns, and a mismatch
  // there used to leave every edit button without its index until the page was reloaded
  const bubbles = [...state.view.querySelectorAll(":scope > .msg.user")];
  const stored = data.messages.filter((m) => m.role === "user" && !isWakeMessage(m));
  // the window leaves out the START of the chat only: when it holds fewer messages than are drawn (a reply added since),
  // the last ones still pair up, so count from the end
  const skip = Math.max(0, bubbles.length - stored.length);
  if (stored.length > bubbles.length || (data.window?.hidden === 0 && skip !== 0)) return;
  bubbles.slice(skip).forEach((el, position) => {
    if (stored[position].index != null) setEditable(el, stored[position].index);
  });
}

// 12345 -> "12.345": token counts are easier to read with thousands dots
function thousands(value) {
  return String(value).replace(/\B(?=(\d{3})+(?!\d))/g, ".");
}

// The report to show for a chat: its last known one, never another chat's (null when it has none).
function lastUsageReport(usages, key) {
  return key && usages.get(key) ? usages.get(key) : null;
}

// Shows the open chat's last known report, or hides the disclosure when it has none.
function syncUsage() {
  const report = lastUsageReport(state.usages, state.chatId ? chatKey(state.agentId, state.chatId) : null);
  if (report) showUsage(report);
  else $("usage").hidden = true;
}

// Fills the disclosure without touching its open state, so a refresh never folds it.
function showUsage(report) {
  if (state.chatId) state.usages.set(chatKey(state.agentId, state.chatId), report);
  $("usage").hidden = false;
  // the first line is the collapsed summary; the details below it do not repeat it
  const [summary, ...details] = report.split("\n");
  $("usage").querySelector("summary").textContent = summary;
  $("usage").querySelector("pre").textContent = details.join("\n") || summary;
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

// The approve buttons a card offers, from its `options` (approve scopes; a card without them offers `once` only).
// The first one is the primary button; "Approve" keeps its short label while it is the only choice.
function approvalScopeButtons(approval) {
  const offered = Array.isArray(approval?.options) && approval.options.length ? approval.options : ["once"];
  const labels = {
    once: offered.length > 1 ? tr("Approve once") : tr("Approve"),
    session: tr("Always in this session"),
    agent: tr("Always for this agent"),
  };
  return ["once", "session", "agent"].filter((scope) => offered.includes(scope)).map((scope) => ({ scope, label: labels[scope] }));
}

// " (this session)" / " (always for this agent)" after the outcome of an approval that was remembered; "" otherwise.
function approvalScopeSuffix(scope) {
  if (scope === "session") return ` (${tr("this session")})`;
  if (scope === "agent") return ` (${tr("always for this agent")})`;
  return "";
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

// ------------------------------------------------------------------ reply timeline
// A reply is an ordered list of segments: {type: "reasoning", text}, {type: "text", text} and {type: "tool", action}.
// The stored form (message.timeline) has {type: "tool", index} into message.actions instead of the action itself.
// Same rules as src/core/timeline.py: chunks of one kind merge, a tool step or the other kind starts a new segment,
// and a whitespace-only chunk never opens a segment.

// The kind a chunk lands in after `last`, or null when it is dropped (whitespace that would open a segment).
function timelineKind(last, kind, chunk) {
  if (kind === "tool") return kind;
  if (!String(chunk ?? "")) return null;
  if (last === kind) return kind;
  return String(chunk).trim() ? kind : null;
}

function timelineStep(segments, kind, payload) {
  const last = segments[segments.length - 1];
  if (timelineKind(last?.type, kind, payload) === null) return segments;
  if (kind === "tool") segments.push({ type: "tool", action: payload });
  else if (last?.type === kind) last.text += payload;
  else segments.push({ type: kind, text: String(payload) });
  return segments;
}

// Segments from a sequence of stream events [{kind, payload}] (reasoning/text chunks and tool actions).
function buildTimeline(events) {
  const segments = [];
  for (const { kind, payload } of events) {
    if (kind === "reasoning" || kind === "text" || kind === "tool") timelineStep(segments, kind, payload);
  }
  return segments;
}

// ---- live tool steps: a step exists from its `tool_start` event, the final `tool` event completes the SAME step.
// A step is {id, phase: "running" | "done" | "failed" | "stopped", action, startedAt}; the helpers are pure.
function toolStepStart(payload, nowMs = Date.now()) {
  const { id, name, arguments: args } = payload ?? {};
  return { id: id == null ? "" : String(id), phase: "running", action: { name, arguments: args ?? {}, pending: true, startedAt: nowMs }, startedAt: nowMs };
}

// Index of the step a final `tool` payload completes: same call id, else (no id) the oldest running step of that name.
function findToolStep(steps, payload) {
  if (payload?.id != null && payload.id !== "") {
    const index = steps.findIndex((step) => step.id === String(payload.id));
    if (index >= 0) return index;
  }
  if (payload?.id != null && payload.id !== "") return -1;
  return steps.findIndex((step) => step.phase === "running" && step.action?.name === payload?.name);
}

// The step after its final event: "failed" when the tool reported an error, else "done".
function toolStepFinish(step, payload) {
  const { id: _id, ...action } = payload ?? {};
  if (step.action?.taskId && !action.taskId) action.taskId = step.action.taskId;  // bound by a `task_started` event
  return { ...step, phase: payload?.error ? "failed" : "done", action };
}

// A `task_started` event ({task_id, goal}) names the task a still-running `task_start` step launched: binds it to the
// oldest running, unbound task_start step with the same goal (else the oldest unbound one). {steps, index}; index -1 = none.
function toolStepBindTask(steps, payload) {
  const taskId = payload?.task_id;
  if (typeof taskId !== "string" || !taskId) return { steps, index: -1 };
  if (steps.some((step) => step.action?.taskId === taskId)) return { steps, index: -1 };  // a replayed duplicate
  const goal = String(payload?.goal ?? "").replace(/\s+/g, " ").trim();
  const open = (step) => step.phase === "running" && step.action?.name === "task_start" && !step.action.taskId;
  const same = (step) => String(step.action?.arguments?.goal ?? "").replace(/\s+/g, " ").trim().slice(0, 300) === goal;
  let index = steps.findIndex((step) => open(step) && same(step));
  if (index < 0) index = steps.findIndex(open);
  if (index < 0) return { steps, index: -1 };
  return { steps: steps.map((step, i) => (i === index ? { ...step, action: { ...step.action, taskId } } : step)), index };
}

// A step still running when the stream ends or the turn is interrupted will not finish any more.
function toolStepStop(step) {
  return step.phase === "running" ? { ...step, phase: "stopped" } : step;
}

// Seconds a running step has been going ("" once it is not running), e.g. 7 -> "7s", 75 -> "1m 15s".
function toolStepElapsed(step, nowMs) {
  if (step.phase !== "running") return "";
  const total = Math.max(0, Math.floor((nowMs - step.startedAt) / 1000));
  return total < 60 ? `${total}s` : `${Math.floor(total / 60)}m ${String(total % 60).padStart(2, "0")}s`;
}

// Applies one stream event to the list of steps: returns {steps, index} (index of the touched step).
// A final `tool` without a matching start (older server, replies that are not streamed) becomes a finished step.
function toolStepsApply(steps, kind, payload, nowMs = Date.now()) {
  if (kind === "tool_start") {
    const existing = steps.findIndex((step) => step.id && step.id === String(payload?.id));
    if (existing >= 0) return { steps, index: existing };  // a replayed duplicate
    return { steps: [...steps, toolStepStart(payload, nowMs)], index: steps.length };
  }
  const index = findToolStep(steps, payload);
  if (index < 0) {
    const { id: _id, ...action } = payload ?? {};
    const step = { id: payload?.id == null ? "" : String(payload.id), phase: payload?.error ? "failed" : "done", action, startedAt: nowMs };
    return { steps: [...steps, step], index: steps.length };
  }
  return { steps: steps.map((step, i) => (i === index ? toolStepFinish(step, payload) : step)), index };
}

// What to draw for a stored message, in order. Messages saved before `timeline` existed keep the old layout:
// thinking, then every tool step, then the text.
function historySteps(message) {
  const actions = message.actions ?? [];
  const content = message.content;
  if (message.role !== "assistant" || !Array.isArray(message.timeline) || !message.timeline.length) {
    const steps = [];
    if (message.reasoning) steps.push({ type: "reasoning", text: message.reasoning });
    for (const action of actions) steps.push({ type: "tool", action });
    steps.push({ type: "text", text: content });
    return steps;
  }
  const steps = [];
  const shown = new Set();
  for (const segment of message.timeline) {
    if (segment.type === "tool") {
      if (actions[segment.index] && !shown.has(segment.index)) {
        shown.add(segment.index);
        steps.push({ type: "tool", action: actions[segment.index] });
      }
    } else if (segment.type === "reasoning" || segment.type === "text") {
      steps.push({ type: segment.type, text: segment.text ?? "" });
    }
  }
  actions.forEach((action, index) => { if (!shown.has(index)) steps.push({ type: "tool", action }); });
  return steps;
}

// First words of a thinking segment on one line, for its collapsed header.
function thinkingPreview(text, max = 90) {
  const line = String(text ?? "").replace(/\s+/g, " ").trim();
  return line.length > max ? `${line.slice(0, max)}…` : line;
}

// One-line view of a tool call: name, backend (terminal) and a short summary of the arguments.
// `line` is the first line of a command or a key=value list; `full` is the tooltip.
// An MCP tool is named `<server_id>__<tool>` for the model (no built-in tool has "__"). For display, split it
// into the server, shown as a highlighted chip with its catalog name when known, and the server's own tool name.
function mcpToolParts(name) {
  const cut = String(name ?? "").indexOf("__");
  if (cut <= 0) return null;
  const serverId = name.slice(0, cut);
  const tool = name.slice(cut + 2);
  if (!/^[a-z][a-z0-9_]{0,31}$/.test(serverId) || !tool) return null;
  const servers = typeof state !== "undefined" && Array.isArray(state.mcpServers) ? state.mcpServers : [];
  const server = servers.find((item) => item.id === serverId);
  return { serverId, server: server?.name || serverId, tool };
}

function actionSummary(action) {
  const args = action?.arguments && typeof action.arguments === "object" ? action.arguments : {};
  const backend = typeof args.backend === "string" ? args.backend : "";
  const one = (value) => String(value).replace(/\s+/g, " ").trim();
  let line;
  if (typeof args.command === "string") {
    line = one(args.command.split(/\r?\n/).find((part) => part.trim()) ?? "");
  } else {
    line = Object.entries(args).filter(([key]) => key !== "backend")
      .map(([key, value]) => `${key}=${one(typeof value === "string" ? value : JSON.stringify(value))}`).join(", ");
  }
  const full = `${action?.name ?? ""}${backend ? ` [${backend}]` : ""}\n${actionArgsText(action)}`.trim();
  const mcp = mcpToolParts(action?.name);
  return {
    name: mcp ? mcp.tool : String(action?.name ?? ""), mcp, backend, line,
    full: full.length > 2000 ? `${full.slice(0, 2000)}…` : full,
  };
}

// The arguments as a block: a lone command as it is (newlines kept), anything else as indented JSON.
function actionArgsText(action) {
  const args = action?.arguments ?? {};
  const keys = Object.keys(args).filter((key) => key !== "backend");
  if (keys.length === 1 && keys[0] === "command" && typeof args.command === "string") return args.command;
  return keys.length ? JSON.stringify(Object.fromEntries(keys.map((key) => [key, args[key]])), null, 2) : "";
}

// A collapsible thinking segment: one line (label + first words) when closed, a capped scrolling block when open.
function thinkingSegment() {
  const node = document.createElement("details");
  node.className = "thinking";
  node.open = prefs.thinkingOpen;
  const summary = document.createElement("summary");
  const preview = document.createElement("span");
  preview.className = "thinking-preview";
  summary.append(Object.assign(document.createElement("span"), { className: "thinking-label", textContent: tr("Thinking") }), preview);
  // a user click on the summary sets the preference for every thinking block
  summary.addEventListener("click", () => {
    prefs.thinkingOpen = !node.open;
    localStorage.setItem("thinkingOpen", prefs.thinkingOpen);
  });
  const body = document.createElement("div");
  body.className = "thinking-text";
  node.append(summary, body);
  return {
    node,
    set(text) {
      body.textContent = text;
      preview.textContent = text ? `· ${thinkingPreview(text)}` : "";
    },
  };
}

// ---- task cards: pure helpers (loaded without the DOM by tests/web/js/test_task_cards.cjs)
const TASK_FINAL = ["done", "failed", "cancelled", "timed_out"];
const TASK_STATUS_LABELS = { starting: "Starting", stopped: "Stopped", running: "Running", done: "Done", failed: "Failed", cancelled: "Cancelled", timed_out: "Timed out", awaiting_approval: "Awaiting approval", awaiting_approval_web: "Awaiting approval (answer it in the web)", awaiting_approval_telegram: "Awaiting approval (answer it in Telegram)" };

// The line above the command of an approval asked by a subagent task: "Task <title> (ephemeral|<agent>) wants to run:".
// `translate` (tr in the page) maps the English template and its values to the user's language; '' for a normal approval.
function approvalTaskLabel(approval, translate = (template, values) => template.replace(/\{(\w+)\}/g, (_, key) => values[key])) {
  const task = approval?.task;
  if (!task || typeof task !== "object" || !task.id) return "";
  return translate("Task {title} ({worker}) wants to run:", { title: String(task.title || task.id), worker: String(task.worker || "ephemeral") });
}

// The status a task card shows: a running task whose worker waits for the user's approval says so.
function taskDisplayStatus(record) {
  if (record?.status !== "running" || !(Number(record?.awaiting_approval) > 0)) return record?.status ?? "";
  // a task run by the other process (web or Telegram) asks there: approvals are never relayed across channels
  const elsewhere = record?.approval_elsewhere;
  return elsewhere === "web" || elsewhere === "telegram" ? `awaiting_approval_${elsewhere}` : "awaiting_approval";
}

function taskIsFinal(status) {
  return TASK_FINAL.includes(status);
}

// The English template of a status badge (callers pass it through tr()).
function taskStatusLabel(status) {
  return TASK_STATUS_LABELS[status] ?? "Unknown";
}

// 45 -> "45s", 185 -> "3m 05s", 3720 -> "1h 02m"; null when the time is unknown.
function formatElapsed(seconds) {
  if (typeof seconds !== "number" || !Number.isFinite(seconds) || seconds < 0) return "";
  const total = Math.floor(seconds);
  const two = (value) => String(value).padStart(2, "0");
  if (total < 60) return `${total}s`;
  if (total < 3600) return `${Math.floor(total / 60)}m ${two(total % 60)}s`;
  return `${Math.floor(total / 3600)}h ${two(Math.floor((total % 3600) / 60))}m`;
}

// Seconds a task ran: fixed once it ended, otherwise counted up from its start (the server value is the fallback).
function taskElapsed(record, nowMs) {
  if (!record) return null;
  if (!taskIsFinal(record.status) && record.started_at && Number.isFinite(Date.parse(record.started_at))) {
    return Math.max(0, Math.round((nowMs - Date.parse(record.started_at)) / 1000));
  }
  return typeof record.elapsed_s === "number" ? record.elapsed_s : null;
}

function taskResultJson(action) {
  const value = action?.result;
  if (value && typeof value === "object") return value;
  if (typeof value !== "string") return null;
  try {
    const parsed = JSON.parse(value);
    return parsed && typeof parsed === "object" ? parsed : null;
  } catch {
    // stored tool results are truncated, so a long `wait=true` result is no longer valid JSON: keep its id and status
    const id = /"id":\s*"([0-9a-f]{12})"/.exec(value)?.[1];
    if (!id) return null;
    const status = /"status":\s*"([a-z_]+)"/.exec(value)?.[1];
    return status ? { id, status } : { id };
  }
}

function isTaskStartAction(action) {
  return action?.name === "task_start";
}

// The task id a stored `task_start` step returned; null when the launch failed (no task exists).
function taskActionId(action) {
  const id = taskResultJson(action)?.id ?? action?.taskId;  // `taskId`: bound by `task_started` while the call still runs
  return typeof id === "string" && id ? id : null;
}

// Fallback for a running `task_start` step that has no id: the newest running task of this chat with the same goal
// (else title) that no other card shows. `records` are task records, `bound` the ids the other cards already hold.
function taskUnboundMatch(records, bound, action) {
  const args = action?.arguments && typeof action.arguments === "object" ? action.arguments : {};
  const norm = (value) => String(value ?? "").replace(/\s+/g, " ").trim();
  const goal = norm(args.goal);
  if (!goal) return null;
  const free = [...records].filter((r) => r && !bound.has(r.id) && !taskIsFinal(r.status));
  const same = free.filter((r) => norm(r.goal) === goal || (r.goal && goal.startsWith(norm(r.goal))) || norm(r.title) === norm(goal.length > 80 ? `${goal.slice(0, 79)}…` : goal));
  same.sort((a, b) => String(b.created_at ?? b.started_at ?? "").localeCompare(String(a.created_at ?? a.started_at ?? "")));
  return same[0] ?? null;
}

// What a card shows: from the task record when known, else from the step itself. `listed` says that the chat's task
// list was already fetched, so a task that is still missing there no longer exists.
function taskCardState(record, action, nowMs, listed = false) {
  const args = action?.arguments && typeof action.arguments === "object" ? action.arguments : {};
  const goal = String(args.goal ?? "").replace(/\s+/g, " ").trim();
  const pending = action?.pending === true;  // live step: `task_start` was called and has not returned yet
  const stopped = action?.stopped === true;   // the stream ended before it returned
  const launchFailed = !pending && !stopped && !taskActionId(action);
  let status = record?.status ?? (pending ? "starting" : stopped ? "stopped" : launchFailed ? "failed" : listed ? "unknown" : "running");
  const final = taskIsFinal(status) || status === "unknown" || status === "stopped";
  const kind = record?.target?.kind ?? (args.agent ? "agent" : "ephemeral");
  const agent = record?.target?.agent_id ?? args.agent ?? "";
  const running = status === "running";
  return {
    id: record?.id ?? taskActionId(action),
    title: record?.title || (goal.length > 80 ? `${goal.slice(0, 79)}…` : goal) || "task",
    target: kind === "agent" && agent ? agent : "",
    ephemeral: !(kind === "agent" && agent),
    status,
    statusLabel: taskStatusLabel(taskDisplayStatus(record) || status),
    awaitingApproval: taskDisplayStatus(record).startsWith("awaiting_approval"),
    elapsed: formatElapsed(record ? taskElapsed(record, nowMs) : pending && Number.isFinite(action.startedAt) ? Math.max(0, (nowMs - action.startedAt) / 1000) : null),
    tokens: (record?.usage?.in ?? 0) + (record?.usage?.out ?? 0),
    running,
    final,
    canStop: running,
    canSteer: running,
    canOpen: Boolean(record?.child?.chat_id),
    activity: running ? taskActivityLabel(record?.activity) : null,
    result: record?.result ?? record?.result_preview ?? "",
    error: record?.error ?? (launchFailed ? String(taskResultJson(action)?.error ?? action?.result ?? "") : ""),
    reason: record?.reason ?? "",
    model: record?.model ?? "",
  };
}

// A wake turn: a subagent result delivered to the parent, stored as a user turn but not written by the user.
function isWakeMessage(message) {
  return message?.role === "user" && (message?.source === "task" || message?.source === "process");
}

// The task titles and the readable body of a wake turn (the framing paragraphs for the model are cut off).
function wakeBubbleState(message) {
  const content = typeof message?.content === "string" ? message.content : "";
  const titles = [...content.matchAll(/^task (\w+) "(.*)" - /gm)].map((found) => ({ id: found[1], title: found[2] }));
  const parts = content.split("\n\n");
  if (parts[0]?.startsWith("[RECALL ")) parts.shift();
  if (parts.length && parts[parts.length - 1].startsWith("If the user's latest request")) parts.pop();
  return { ids: Array.isArray(message?.task_ids) ? message.task_ids : titles.map((t) => t.id), titles: titles.map((t) => t.title), body: parts.join("\n\n").trim() };
}

// ---- task panel helpers: the list of all the user's tasks (cross-chat), its filter and rows
// 950 -> "950", 12400 -> "12.4k", 1500000 -> "1.5M"
function formatTokenCount(count) {
  const value = Number(count);
  if (!Number.isFinite(value) || value < 0) return "0";
  if (value < 1000) return String(Math.round(value));
  if (value < 1_000_000) return `${(value / 1000).toFixed(value < 10_000 ? 1 : 0).replace(/\.0$/, "")}k`;
  return `${(value / 1_000_000).toFixed(1).replace(/\.0$/, "")}M`;
}

// "running" keeps the tasks that have not ended, "finished" the ended ones, anything else all of them.
function taskPanelFilter(tasks, filter) {
  const list = Array.isArray(tasks) ? tasks : [];
  if (filter === "running") return list.filter((task) => !taskIsFinal(task.status));
  if (filter === "finished") return list.filter((task) => taskIsFinal(task.status));
  return list.slice();
}

// Newest first by creation time (the server lists oldest first); stable for equal or missing times.
function taskPanelSort(tasks) {
  const stamp = (task) => Date.parse(task.created_at ?? task.started_at ?? "") || 0;
  return (Array.isArray(tasks) ? tasks : []).map((task, index) => ({ task, index }))
    .sort((a, b) => stamp(b.task) - stamp(a.task) || b.index - a.index).map((item) => item.task);
}

// What one row of the panel shows (the record is a `GET /api/tasks` entry, labelled with the parent's names).
function taskPanelRow(record, nowMs) {
  const kind = record?.target?.kind ?? "ephemeral";
  const worker = kind === "agent" ? record?.target?.name || record?.target?.agent_id || "" : "";
  const status = record?.status ?? "";
  const running = status === "running";
  const parentChat = record?.parent?.chat_id ?? "";
  return {
    id: record?.id ?? "",
    title: record?.title || record?.id || "task",
    status,
    statusLabel: taskStatusLabel(taskDisplayStatus(record) || status),
    awaitingApproval: taskDisplayStatus(record).startsWith("awaiting_approval"),
    ephemeral: !worker,
    target: worker,
    parentAgent: record?.parent?.agent_name || record?.parent?.agent_id || "",
    parentTitle: parentChat ? record?.parent?.title || "" : "",
    parentAgentId: record?.parent?.agent_id ?? "",
    parentChatId: parentChat,
    canOpenParent: Boolean(parentChat),
    scheduled: Boolean(record?.unattended),
    elapsed: formatElapsed(taskElapsed(record, nowMs)),
    tokens: (record?.usage?.in ?? 0) + (record?.usage?.out ?? 0),
    running,
    canStop: running,
    canSteer: running,
    canOpenChat: Boolean(record?.child?.chat_id),
  };
}

// The panel keeps refreshing while any listed task runs.
function taskPanelNeedsPolling(response) {
  return (response?.tasks ?? []).some((task) => !taskIsFinal(task.status));
}
// ---- end task panel helpers

// Pure: one row per running task of the chat for the tray above the message box (oldest first).
function taskTrayRows(records, agentId, chatId, nowMs) {
  return [...records]
    .filter((task) => task?.id && task?.status === "running" && task?.parent?.agent_id === agentId && task?.parent?.chat_id === chatId)
    .sort((a, b) => (Date.parse(a.created_at) || 0) - (Date.parse(b.created_at) || 0))
    .map((task) => ({
      id: task.id,
      title: String(task.title || task.goal || task.id).trim(),
      goal: String(task.goal || task.title || task.id).trim(),
      model: task.model || "",
      elapsed: formatElapsed(taskElapsed(task, nowMs)),
      startedMs: Date.parse(task.started_at) || null,
      canOpenChat: Boolean(task.child?.chat_id),
    }));
}

// Keep polling while a task of the chat runs or its result is still on the way to the parent, or (`expectWakeUntil`,
// ms) while the wake of a background process that just ended may still start.
function tasksNeedPolling(response, expectWakeUntil = 0, nowMs = Date.now()) {
  const tasks = response?.tasks ?? [];
  return tasks.some((task) => !taskIsFinal(task.status)) || Boolean(response?.pending_wake) || nowMs < expectWakeUntil;
}

// The ids of the previous process list that are not in the new one: processes that just finished.
function finishedProcessIds(previous, current) {
  const now = new Set(current ?? []);
  return (previous ?? []).filter((id) => !now.has(id));
}

// A wake reply started in the open chat without a request of this page: attach to it (never when one is attached).
function shouldAttachWake({ active, generating, current = true }) {
  return Boolean(active) && !generating && current;
}

// The muted "what is the worker doing" line of a running task card, from the `activity` of its record
// ({kind: reasoning | text | tool, tool, detail}): a [template, params] pair for `tr`, or null for nothing.
function taskActivityLabel(activity) {
  if (!activity || typeof activity !== "object") return null;
  if (activity.kind === "reasoning") return ["Thinking…", {}];
  if (activity.kind === "text") return ["Writing…", {}];
  if (activity.kind !== "tool" || !activity.tool) return null;
  const detail = String(activity.detail ?? "").split("\n")[0].trim();
  return detail ? ["Running {tool}: {detail}", { tool: activity.tool, detail }] : ["Running {tool}", { tool: activity.tool }];
}

// What the worker dialog does once a live stream of the worker's reply ended: "poll" (the live view is not
// available here, or it failed: refresh the saved chat every few seconds) or "reload" (show the saved chat, then attach
// again if the task still runs, which also covers a steer that started a new generation).
function taskLiveNext({ unavailable, failed }) {
  return unavailable || failed ? "poll" : "reload";
}

// The structured view of a worker chat's task message: `task_brief` / `task_message` metadata stored on the turn, else
// a parse of the known text layout (older chats). Returns {kind: "brief", taskId, from, user, goal, context,
// instructionsPresent}, {kind: "note", note: "followup"|"correction", text} or null for an ordinary message.
function taskBriefState(message) {
  if (message?.role !== "user") return null;
  const brief = message.task_brief;
  if (brief && typeof brief === "object") {
    return {
      kind: "brief", taskId: String(brief.task_id ?? ""), from: String(brief.from ?? ""), user: String(brief.user ?? ""),
      goal: String(brief.goal ?? ""), context: String(brief.context ?? ""), instructionsPresent: Boolean(brief.instructions_present),
    };
  }
  const note = message.task_message;
  if (note && typeof note === "object" && (note.kind === "followup" || note.kind === "correction")) {
    return { kind: "note", note: note.kind, text: String(note.text ?? "") };
  }
  const content = typeof message.content === "string" ? message.content : "";
  for (const [prefix, kind] of [["Follow-up from the parent agent:\n", "followup"], ["Correction from the parent agent:\n", "correction"]]) {
    if (content.startsWith(prefix)) return { kind: "note", note: kind, text: content.slice(prefix.length) };
  }
  const found = /^(?:Task from (.+?) \(([^()\n]*)\)\n\n|Scheduled task \(([^()\n]*)\) for ([^\n]*)\n\n)?Task (\w+)\n\nGoal:\n([\s\S]*)$/.exec(content);
  if (!found) return null;
  const [, from, askedBy, , scheduledFor, taskId, rest] = found;
  const cut = rest.indexOf("\n\nContext:\n");
  return {
    kind: "brief", taskId, from: from ?? "", user: askedBy ?? scheduledFor ?? "",
    goal: (cut < 0 ? rest : rest.slice(0, cut)).trim(), context: cut < 0 ? "" : rest.slice(cut + "\n\nContext:\n".length).trim(),
    instructionsPresent: false,
  };
}
// ---- end task cards

// A tool step: closed it is one line (icon, name, backend, summary); open it shows the arguments and the result.
function actionStep(action) {
  if (isTaskStartAction(action)) return taskCardStep(action);
  const info = actionSummary(action);
  const item = document.createElement("details");
  item.className = "action" + (action.error ? " failed" : "") + (action.pending ? " running" : "");
  const summary = document.createElement("summary");
  summary.title = info.full;
  if (info.mcp) {
    const chip = element("span", { className: "action-mcp", textContent: info.mcp.server });
    chip.title = tr("MCP server: {server}", { server: info.mcp.server });
    summary.append(chip);
  }
  summary.append(Object.assign(document.createElement("span"), { className: "action-name", textContent: info.name }));
  if (info.backend) summary.append(Object.assign(document.createElement("span"), { className: "action-backend", textContent: info.backend }));
  summary.append(Object.assign(document.createElement("span"), { className: "action-args", textContent: info.line }));
  if (action.pending) {
    // running indicator at the end of the line: pulsing dot, "Running" and the seconds so far
    const status = element("span", { className: "action-status" });
    status.setAttribute("role", "status");
    summary.append(status);
    item._status = status;
    item._startedAt = action.startedAt ?? Date.now();
    watchRunningStep(item);
  }
  item.append(summary);
  const argsText = actionArgsText(action);
  for (const [label, text] of [[tr("Arguments"), argsText], [tr("Result"), String(action.result ?? "")]]) {
    if (!text) continue;
    const block = document.createElement("div");
    block.className = "action-block";
    block.append(
      Object.assign(document.createElement("div"), { className: "action-label", textContent: label }),
      Object.assign(document.createElement("pre"), { textContent: text }),
    );
    item.append(block);
  }
  item.dataset.tool = action.name;
  item.dataset.command = String(action.arguments?.command ?? "");
  return item;
}

// Live steps (started, not finished): one shared 1 s timer repaints their elapsed seconds while any is on the page.
const runningSteps = new Set();
let runningTimer = null;

function watchRunningStep(node) {
  runningSteps.add(node);
  paintRunningStep(node);
  if (!runningTimer) runningTimer = setInterval(tickRunningSteps, 1000);
}

function paintRunningStep(node) {
  if (node._parts) refreshTaskCard(node);  // a task card shows "Starting" and the seconds in its badge
  else if (node._stopping) node._status.textContent = tr("Stopping…");
  else if (node._status) node._status.textContent = `${tr("Running")} · ${toolStepElapsed({ phase: "running", startedAt: node._startedAt }, Date.now())}`;
}

function tickRunningSteps() {
  for (const node of [...runningSteps]) {
    if (!node.isConnected) runningSteps.delete(node);
    else paintRunningStep(node);
  }
  if (!runningSteps.size) {
    clearInterval(runningTimer);
    runningTimer = null;
  }
}

function unwatchRunningStep(node) {
  runningSteps.delete(node);
}

// The step will never get its final event (stream ended, turn interrupted): it stops pulsing and says "Stopped".
function stopRunningStep(node) {
  runningSteps.delete(node);
  if (node._parts) {
    node._action.pending = false;
    node._action.stopped = true;
    refreshTaskCard(node);
    return;
  }
  node.classList.remove("running");
  node.classList.add("stopped");
  if (node._status) node._status.textContent = node._stopping ? tr("Stopped by you") : tr("Stopped");
}

// ---- task cards: DOM, polling and the worker-chat dialog
const taskRecords = new Map();  // task id -> last record fetched for the open chat
const taskCards = new Set();
let taskListed = null;          // chat key of the last fetched task list
let taskPollTimer = null;
let taskPollRunning = false;
let taskPollAgain = false;
let wakeAttaching = false;

function taskUrl(id, suffix = "") {
  return `/api/tasks/${encodeURIComponent(id)}${suffix}`;
}

// The card of a `task_start` step: one line closed (title, worker, status badge); open it holds the result and the buttons.
function taskCardStep(action) {
  const card = document.createElement("details");
  card.className = "action task-card";
  card.dataset.tool = "task_start";
  card.dataset.command = "";
  const summary = document.createElement("summary");
  const title = element("span", { className: "action-name task-title" });
  const target = element("span", { className: "action-backend task-target" });
  const badge = element("span", { className: "task-badge" });
  badge.setAttribute("role", "status");
  const tokens = element("span", { className: "action-args task-tokens" });
  const activity = element("span", { className: "task-activity", hidden: true });
  summary.append(title, target, badge, tokens, activity);
  const result = element("div", { className: "action-block task-result-block" });
  const resultLabel = element("div", { className: "action-label", textContent: tr("Result") });
  const resultText = element("pre");
  result.append(resultLabel, resultText);
  const error = element("div", { className: "task-error", hidden: true });
  error.setAttribute("role", "alert");
  const button = (text, className, handler) => {
    const node = element("button", { type: "button", className, textContent: text });
    node.addEventListener("click", handler);
    return node;
  };
  const stop = button(tr("Stop"), "secondary task-stop", () => taskStop(card, error));
  const steer = button(tr("Steer"), "secondary task-steer-toggle", () => {
    form.hidden = !form.hidden;
    if (!form.hidden) field.focus();
  });
  const open = button(tr("Open chat"), "secondary task-open", () => openTaskChat(card._taskId));
  const field = element("input", { type: "text", className: "task-steer-input", maxLength: 2000, placeholder: tr("Tell the worker what to change") });
  field.setAttribute("aria-label", tr("Steer"));
  const send = button(tr("Send"), "primary task-steer-send", () => taskSteer(card, field, form, error));
  field.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.isComposing) {
      event.preventDefault();
      send.click();
    }
  });
  const form = element("div", { className: "task-steer-form", hidden: true }, field, send);
  const buttons = element("div", { className: "task-actions" }, stop, steer, open);
  card.append(summary, result, buttons, form, error);
  card._parts = { title, target, badge, tokens, activity, resultText, result, stop, steer, open, form, error };
  card._action = action;
  card._taskId = taskActionId(action);
  taskCards.add(card);
  refreshTaskCard(card);
  if (action.pending) watchRunningStep(card);
  if (card._taskId || action.pending) ensureTaskPolling();  // a running step without an id is matched from the poll
  return card;
}

function refreshTaskCard(card) {
  const parts = card._parts;
  const listed = taskListed === chatKey(state.agentId, state.chatId);
  if (!card._taskId && card._action?.pending === true && listed) {  // no id yet: the newest running task of this chat with this goal
    const bound = new Set([...taskCards].map((other) => other._taskId).filter(Boolean));
    card._taskId = taskUnboundMatch(taskRecords.values(), bound, card._action)?.id ?? null;
  }
  const view = taskCardState(taskRecords.get(card._taskId), card._action, Date.now(), listed);
  card._taskId = view.id;
  card.dataset.status = view.status;
  card.classList.toggle("failed", view.status === "failed");
  parts.title.textContent = view.title;
  parts.target.textContent = view.ephemeral ? tr("ephemeral") : view.target;
  parts.badge.className = `task-badge ${view.awaitingApproval ? "awaiting_approval" : view.status}`;
  parts.badge.textContent = view.elapsed ? `${tr(view.statusLabel)} · ${view.elapsed}` : tr(view.statusLabel);
  parts.tokens.textContent = [view.model, view.tokens ? tr("{count} tokens", { count: view.tokens }) : ""].filter(Boolean).join(" · ");
  parts.activity.textContent = view.activity ? tr(view.activity[0], view.activity[1]) : "";
  parts.activity.hidden = !view.activity;
  const text = view.result || view.error || (view.running || view.status === "starting" ? "" : tr("No result"));
  parts.result.hidden = !text;
  parts.resultText.textContent = text;
  parts.stop.hidden = !view.canStop;
  parts.steer.hidden = !view.canSteer;
  parts.open.hidden = !view.canOpen;
  if (!view.canSteer) parts.form.hidden = true;
}

function refreshTaskCards() {
  for (const card of [...taskCards]) {
    if (!card.isConnected) taskCards.delete(card);
    else refreshTaskCard(card);
  }
  renderTaskTray();
}

function showTaskError(box, err) {
  box.textContent = err?.message ?? String(err);
  box.hidden = false;
}

async function taskStop(card, box) {
  box.hidden = true;
  try {
    await api(taskUrl(card._taskId, "/stop"), { method: "POST" });
  } catch (err) {
    showTaskError(box, err);
  }
  ensureTaskPolling();
}

async function taskSteer(card, field, form, box) {
  const text = field.value.trim();
  if (!text) return;
  box.hidden = true;
  try {
    await api(taskUrl(card._taskId, "/steer"), { method: "POST", body: JSON.stringify({ text }) });
    field.value = "";
    form.hidden = true;
  } catch (err) {
    showTaskError(box, err);
  }
  ensureTaskPolling();
}

// Polls the open chat's tasks every ~3 s while one runs or a result is on its way; a wake reply that started
// without a request of this page is then attached to like a reply that runs after a reload.
// ---- running subagent tasks of the open chat: a collapsible tray above the message box (fed by taskRecords)
const taskTray = { shown: "", key: null };

function renderTaskTray() {
  const box = $("tasks-tray");
  if (!box) return;
  const key = chatKey(state.agentId, state.chatId);
  if (taskTray.key !== key) {  // another chat: start from an empty tray
    taskTray.key = key;
    taskTray.shown = "";
    box.removeAttribute("open");
  }
  const rows = taskListed === key ? taskTrayRows(taskRecords.values(), state.agentId, state.chatId, Date.now()) : [];
  box.hidden = rows.length === 0;
  const summary = box.querySelector("summary");
  summary.textContent = rows.length === 1 ? tr("1 subagent running") : tr("{count} subagents running", { count: rows.length });
  const signature = JSON.stringify(rows.map((row) => row.id));
  if (signature === taskTray.shown) {  // same list: only the clocks change, so a half-typed steer is kept
    for (const row of rows) {
      const item = box.querySelector(`[data-task-id="${CSS.escape(row.id)}"]`);
      if (!item) continue;
      item.querySelector(".task-tray-time").textContent = row.elapsed;
      item.querySelector(".task-open").hidden = !row.canOpenChat;  // the worker chat appears a moment after the task starts
    }
    return;
  }
  taskTray.shown = signature;
  box.querySelector("ul").replaceChildren(...rows.map((row) => {
    const handle = { _taskId: row.id };
    const error = element("div", { className: "task-error", hidden: true });
    error.setAttribute("role", "alert");
    const button = (text, className, handler) => {
      const node = element("button", { type: "button", className, textContent: text });
      node.addEventListener("click", handler);
      return node;
    };
    const field = element("input", { type: "text", className: "task-steer-input", maxLength: 2000, placeholder: tr("Tell the worker what to change") });
    field.setAttribute("aria-label", tr("Steer"));
    const form = element("div", { className: "task-steer-form", hidden: true });
    const send = button(tr("Send"), "primary task-steer-send", () => taskSteer(handle, field, form, error));
    field.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.isComposing) {
        event.preventDefault();
        send.click();
      }
    });
    form.append(field, send);
    const stop = button(tr("Stop"), "secondary task-stop", () => taskStop(handle, error));
    const steer = button(tr("Steer"), "secondary task-steer-toggle", () => {
      form.hidden = !form.hidden;
      if (!form.hidden) field.focus();
    });
    const open = button(tr("Open chat"), "secondary task-open", () => openTaskChat(row.id));
    open.hidden = !row.canOpenChat;
    const time = element("span", { className: "process-time task-tray-time", textContent: row.elapsed });
    const item = element("li", { className: "task-tray-row" },
      element("span", { className: "task-tray-title", textContent: row.title, title: row.goal }),
      element("span", { className: "process-meta", textContent: row.model }),
      time,
      element("span", { className: "task-tray-actions" }, stop, steer, open),
      form, error);
    item.dataset.taskId = row.id;
    return item;
  }));
}

setInterval(() => { if (!document.hidden && !$("tasks-tray").hidden) renderTaskTray(); }, 1000);  // the clocks tick between polls

// ---- background processes of the open chat: a collapsible line above the message box, each with Stop
const PROCESS_POLL_MS = 3000;
const processPanel = { timer: null, key: null, shown: "", ids: [] };
const WAKE_GRACE_MS = 60000;  // after a process ends, the server needs a moment to start its wake reply
let expectWakeUntil = 0;

function processElapsed(started) {
  const seconds = Math.max(0, Math.floor(Date.now() / 1000 - started));
  const minutes = Math.floor(seconds / 60);
  return minutes >= 60 ? `${Math.floor(minutes / 60)}h${String(minutes % 60).padStart(2, "0")}m` : `${minutes}:${String(seconds % 60).padStart(2, "0")}`;
}

function renderProcesses(processes) {
  const box = $("processes");
  box.hidden = processes.length === 0;
  const signature = JSON.stringify(processes.map((p) => p.id));
  const summary = box.querySelector("summary");
  summary.textContent = processes.length === 1
    ? tr("{count} process running in the background", { count: 1 })
    : tr("{count} processes running in the background", { count: processes.length });
  if (signature === processPanel.shown) {  // same list: only the clocks change, so an open row is not rebuilt
    box.querySelectorAll("[data-started]").forEach((n) => { n.textContent = processElapsed(Number(n.dataset.started)); });
    return;
  }
  processPanel.shown = signature;
  box.querySelector("ul").replaceChildren(...processes.map((process) => {
    const stop = element("button", { type: "button", className: "process-stop", textContent: tr("Stop process") });
    stop.addEventListener("click", async () => {
      stop.disabled = true;
      try {
        await api(`/api/agents/${encodeURIComponent(state.agentId)}/chats/${encodeURIComponent(state.chatId)}/processes/${process.id}/stop`, { method: "POST" });
      } catch (err) {
        stop.disabled = false;
        addMessage("assistant").error(err);
      }
      pollProcesses();
    });
    const time = element("span", { className: "process-time", textContent: processElapsed(process.started) });
    time.dataset.started = process.started;
    return element("li", {},
      element("code", { className: "process-command", textContent: process.command, title: process.command }),
      element("span", { className: "process-meta", textContent: process.backend }),
      time, stop);
  }));
}

async function pollProcesses() {
  const { agentId, chatId, session } = state;
  if (!state.user || !agentId || !chatId || document.hidden) return;
  try {
    const data = await api(`/api/agents/${encodeURIComponent(agentId)}/chats/${encodeURIComponent(chatId)}/processes`);
    if (session === state.session && agentId === state.agentId && chatId === state.chatId) {
      renderProcesses(data.processes);
      const ids = data.processes.map((p) => p.id);
      const ended = finishedProcessIds(processPanel.ids, ids).length > 0;
      processPanel.ids = ids;
      if (ended) expectWakeUntil = Date.now() + WAKE_GRACE_MS;  // its wake reply is on the way: let the task poll find it
      if (ended || ids.length) ensureTaskPolling();
    }
  } catch {
    // the panel is a convenience: a failed poll keeps the last list
  }
}

function startProcessPolling() {
  const key = state.user && state.agentId && state.chatId ? chatKey(state.agentId, state.chatId) : null;
  if (key !== processPanel.key) {  // another chat: start from an empty panel
    processPanel.key = key;
    processPanel.shown = "";
    processPanel.ids = [];
    expectWakeUntil = 0;
    $("processes").hidden = true;
    $("processes").removeAttribute("open");
  }
  if (!key) return;
  pollProcesses();
  if (!processPanel.timer) processPanel.timer = setInterval(pollProcesses, PROCESS_POLL_MS);
}

// A tab that was hidden has throttled timers: look again at once when it comes back.
document.addEventListener("visibilitychange", () => {
  if (document.hidden || !state.user) return;
  pollProcesses();
  ensureTaskPolling();
});

function ensureTaskPolling() {
  if (!state.user) return;
  if (taskPollRunning) {
    taskPollAgain = true;
    return;
  }
  clearTimeout(taskPollTimer);
  taskPoll();
}

async function taskPoll() {
  taskPollRunning = true;
  taskPollAgain = false;
  const { agentId, chatId, session } = state;
  let again = false;
  try {
    if (agentId && chatId) {
      const data = await api(`/api/tasks?agent=${encodeURIComponent(agentId)}&chat=${encodeURIComponent(chatId)}`);
      const current = session === state.session && agentId === state.agentId && chatId === state.chatId;
      if (current) {
        for (const task of data.tasks) taskRecords.set(task.id, task);
        taskListed = chatKey(agentId, chatId);
        refreshTaskCards();
        // a worker with a terminal grant asks its approvals in this chat: list them so the cards appear (and close)
        if (data.tasks.some((task) => !taskIsFinal(task.status) && (task.backends?.length || task.awaiting_approval))) syncApprovals(agentId, chatId);
        again = tasksNeedPolling(data, expectWakeUntil) || (data.tasks.length > 0 && data.active);
        if (!wakeAttaching && shouldAttachWake({ active: data.active, generating: isGenerating(agentId, chatId), current })) {
          await attachWake(agentId, chatId);
        }
      }
    }
  } catch {
    again = false;
  }
  taskPollRunning = false;
  taskPollTimer = again || taskPollAgain ? setTimeout(taskPoll, taskPollAgain ? 0 : 3000) : null;
}

// A wake turn is being answered: draw the saved chat (it holds the wake bubble) and follow the reply live.
async function attachWake(agentId, chatId) {
  wakeAttaching = true;
  try {
    const data = await api(chatsUrl(`/${chatId}${chatQuery(state.view?.historyLimit)}`));
    const current = agentId === state.agentId && chatId === state.chatId && !isGenerating(agentId, chatId);
    if (!current || !data.chat.active) return;
    state.chatInfo = data.chat;
    forgetApprovals(agentId, chatId);
    renderMessages(data.messages, data.window, state.view?.historyLimit);
    followReply(agentId, chatId);
  } finally {
    wakeAttaching = false;
  }
}

// A wake turn in the history: a compact collapsible "Task result" note, not a user bubble.
function wakeBubble(message) {
  const info = wakeBubbleState(message);
  const node = document.createElement("details");
  node.className = "msg task-wake";
  node.dataset.taskIds = info.ids.join(",");
  const summary = element("summary", {}, element("span", { className: "task-wake-label", textContent: message.source === "process" ? tr("Process result") : tr("Task result") }));
  if (info.titles.length) summary.append(element("span", { className: "task-wake-titles", textContent: info.titles.join(" · ") }));
  node.append(summary, element("pre", { className: "task-wake-body", textContent: info.body || String(message.content ?? "") }));
  return node;
}

// A compaction in the history: a thin divider; everything above it reaches the model only as a summary.
function compactionDivider(message) {
  const before = Number.isFinite(message.tokens_before) ? thousands(message.tokens_before) : "?";
  const after = Number.isFinite(message.tokens_after) ? thousands(message.tokens_after) : "?";
  const label = message.manual
    ? tr("Context compacted with /compact: {before} → {after} tokens", { before, after })
    : tr("Context compacted: {before} → {after} tokens", { before, after });
  const kept = Number.isFinite(message.kept_percent) ? ` · ${tr("latest {kept} % kept", { kept: message.kept_percent })}` : "";
  const node = element("div", { className: "compaction-divider", textContent: label + kept });
  node.setAttribute("role", "separator");
  return node;
}

let taskDialogTimer = null;

// A worker chat's task message as a compact "Task brief" card (or a small follow-up / correction note).
function taskBriefNode(info) {
  const node = document.createElement("div");
  if (info.kind === "note") {
    node.className = "msg task-note";
    const agent = info.agent || tr("the parent agent");
    node.append(
      element("div", { className: "task-note-label", textContent: info.note === "correction" ? tr("Correction from {agent}", { agent }) : tr("Follow-up from {agent}", { agent }) }),
      markdown(info.text),
    );
    return node;
  }
  node.className = "msg task-brief";
  const head = element("div", { className: "task-brief-head" }, element("span", { className: "task-brief-title", textContent: tr("Task brief") }));
  if (info.from) head.append(element("span", { className: "task-brief-badge", textContent: tr("from {agent}", { agent: info.from }) }));
  if (info.taskId) head.append(element("span", { className: "task-brief-badge", textContent: `#${info.taskId.slice(0, 8)}` }));
  node.append(head, element("div", { className: "task-brief-label", textContent: tr("Goal") }), markdown(info.goal));
  node.lastChild.classList.add("task-brief-goal");
  if (info.context) {
    const long = info.context.split("\n").length > 6;
    const section = element("details", { className: "task-brief-context" }, element("summary", { textContent: tr("Context") }), markdown(info.context));
    section.open = !long;
    node.append(section);
  }
  return node;
}

function renderTaskChatMessages(container, messages) {
  container.replaceChildren();
  let agent = "";  // the parent agent's name, known from the brief
  for (const m of messages) {
    const brief = taskBriefState(m);
    if (brief) {
      if (brief.kind === "brief") agent = brief.from || agent;
      else brief.agent = agent;
      container.append(taskBriefNode(brief));
      continue;
    }
    if (isWakeMessage(m)) {
      container.append(wakeBubble(m));
      continue;
    }
    const view = addMessage(m.role, container);
    if (m.role === "assistant") {
      for (const step of historySteps(m)) {
        if (step.type === "reasoning") view.thinking(step.text);
        else if (step.type === "tool") view.action(step.action);
        else view.text(step.text);
      }
    } else {
      view.text(m.content);
    }
  }
}

// Read-only view of a task's hidden worker chat; refreshed every 3 s while the task runs.
async function openTaskChat(id) {
  if (!id) return;
  const dialog = $("task-dialog");
  const box = $("task-dialog-messages");
  stopTaskDialog();
  const run = taskDialogRun;
  const alive = () => run === taskDialogRun && dialog.open;
  const load = async () => {
    try {
      const data = await api(taskUrl(id, "/chat"));
      if (!alive()) return false;
      renderTaskChatMessages(box, data.messages);  // the stuck flag below keeps the bottom in view
      return data.chat.active;
    } catch (err) {
      if (alive()) box.replaceChildren(element("div", { className: "task-error", textContent: err.message }));
      return false;
    }
  };
  const record = taskRecords.get(id);
  $("task-dialog-title").textContent = tr("Worker chat: {title}", { title: record?.title || id });
  box.replaceChildren(element("p", { className: "model-note", textContent: tr("Loading…") }));
  taskDialogStuck = true;
  if (!dialog.open) dialog.showModal();
  // saved chat, then the live reply of the running worker; when that ends, the saved chat again and, if the task
  // still runs (a steer started a new generation), attach again. Without a live view: poll the saved chat.
  let pause = 0;
  while (alive()) {
    const active = await load();
    if (!alive() || !active) return;
    const outcome = await streamTaskReply(id, box, run);
    if (!alive()) return;
    pause = taskLiveNext(outcome) === "poll" ? 3000 : 500;
    await new Promise((resolve) => { taskDialogTimer = setTimeout(resolve, pause); });
  }
}

let taskDialogRun = 0;
let taskDialogAbort = null;
let taskDialogStuck = true;

// Ends whatever the dialog is doing: its live stream and its refresh timer.
function stopTaskDialog() {
  taskDialogRun++;
  clearTimeout(taskDialogTimer);
  taskDialogAbort?.abort();
  taskDialogAbort = null;
}

// Follows the worker's running reply read-only, drawn with the chat's own reply view in the dialog. Resolves with
// `{unavailable, failed}` when the stream ended (the reply ended, the other process runs the task, or it broke).
async function streamTaskReply(id, box, run) {
  const reply = addMessage("assistant", box);
  const request = {
    userId: state.user?.id, agentId: "", chatId: "", session: state.session, reply,
    generationId: null, done: false, unavailable: false,
  };
  const controller = new AbortController();
  taskDialogAbort = controller;
  let failed = false;
  try {
    const res = await fetch(taskUrl(id, "/stream"), {
      headers: { "Accept-Language": state.preferences?.language ?? "en" }, signal: controller.signal,
    });
    if (!res.ok) throw new Error(String(res.status));
    await readEvents(res, request);
  } catch (err) {
    failed = err?.name !== "AbortError";
  }
  if (taskDialogAbort === controller) taskDialogAbort = null;
  if (!reply.el.textContent.trim() && !reply.el.querySelector(".action, .thinking")) reply.el.remove();  // nothing was streamed
  if (run !== taskDialogRun) return { unavailable: false, failed: false };
  return { unavailable: request.unavailable, failed };
}

$("task-dialog-messages").addEventListener("scroll", () => {
  const box = $("task-dialog-messages");
  taskDialogStuck = nearBottom(box.scrollHeight, box.scrollTop, box.clientHeight);
}, { passive: true });
// streamed steps, thinking and text follow the bottom only while the user is within 30 px of it
new MutationObserver(() => {
  const box = $("task-dialog-messages");
  if (taskDialogStuck) box.scrollTop = box.scrollHeight;
}).observe($("task-dialog-messages"), { childList: true, subtree: true, characterData: true });

$("task-dialog-close").addEventListener("click", () => $("task-dialog").close());
$("task-dialog").addEventListener("close", stopTaskDialog);
// ---- task panel (DOM): every task of the user across chats and agents, newest first
const taskPanel = { filter: "all", tasks: [], rows: new Map(), timer: null, running: false, again: false, loaded: false };

function taskPanelRowNode(id) {
  const badge = element("span", { className: "task-badge" });
  badge.setAttribute("role", "status");
  const title = element("strong", { className: "tasks-row-title" });
  const meta = element("div", { className: "tasks-row-meta" });
  const error = element("div", { className: "task-error", hidden: true });
  error.setAttribute("role", "alert");
  const button = (text, handler) => {
    const node = element("button", { type: "button", className: "secondary", textContent: text });
    node.addEventListener("click", handler);
    return node;
  };
  const field = element("input", { type: "text", className: "task-steer-input", maxLength: 2000, placeholder: tr("Tell the worker what to change") });
  field.setAttribute("aria-label", tr("Steer"));
  const form = element("div", { className: "task-steer-form", hidden: true });
  const send = element("button", { type: "button", className: "primary", textContent: tr("Send") });
  form.append(field, send);
  const stop = button(tr("Stop"), async () => {
    error.hidden = true;
    try { await api(taskUrl(id, "/stop"), { method: "POST" }); } catch (err) { showTaskError(error, err); }
    taskPanelRefresh();
  });
  const steer = button(tr("Steer"), () => {
    form.hidden = !form.hidden;
    if (!form.hidden) field.focus();
  });
  const open = button(tr("Open chat"), () => openTaskChat(id));
  send.addEventListener("click", async () => {
    const text = field.value.trim();
    if (!text) return;
    error.hidden = true;
    try {
      await api(taskUrl(id, "/steer"), { method: "POST", body: JSON.stringify({ text }) });
      field.value = "";
      form.hidden = true;
    } catch (err) { showTaskError(error, err); }
    taskPanelRefresh();
  });
  field.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.isComposing) {
      event.preventDefault();
      send.click();
    }
  });
  const actions = element("div", { className: "task-actions" }, stop, steer, open);
  const head = element("div", { className: "tasks-row-head" }, badge, title);
  const node = element("li", { className: "tasks-row" }, head, meta, actions, form, error);
  node.dataset.id = id;
  node._parts = { badge, title, meta, stop, steer, open, form };
  return node;
}

function updateTaskPanelRow(node, record) {
  const view = taskPanelRow(record, Date.now());
  const parts = node._parts;
  node.dataset.status = view.status;
  parts.badge.className = `task-badge ${view.awaitingApproval ? "awaiting_approval" : view.status}`;
  parts.badge.textContent = tr(view.statusLabel);
  parts.title.textContent = view.title;
  parts.meta.replaceChildren();
  const bits = [view.ephemeral ? tr("ephemeral") : view.target];
  if (view.scheduled) bits.push(tr("scheduled"));
  if (view.elapsed) bits.push(view.elapsed);
  if (view.tokens) bits.push(tr("{count} tokens", { count: formatTokenCount(view.tokens) }));
  parts.meta.append(element("span", { textContent: bits.join(" · ") }));
  if (view.canOpenParent) {
    const link = element("button", {
      type: "button", className: "link-button tasks-parent",
      textContent: tr("from {agent} · {chat}", { agent: view.parentAgent, chat: view.parentTitle || tr("(untitled chat)") }),
    });
    link.addEventListener("click", async () => {
      $("tasks-dialog").close();
      await openChatFromList({ id: view.parentChatId, agent_id: view.parentAgentId });
    });
    parts.meta.append(link);
  } else {
    parts.meta.append(element("span", { textContent: tr("no parent chat") }));
  }
  parts.stop.hidden = !view.canStop;
  parts.steer.hidden = !view.canSteer;
  parts.open.hidden = !view.canOpenChat;
  if (!view.canSteer) parts.form.hidden = true;
}

function renderTaskPanel() {
  const list = $("tasks-list");
  const shown = taskPanelSort(taskPanelFilter(taskPanel.tasks, taskPanel.filter));
  const wanted = new Set(shown.map((task) => task.id));
  for (const [id, node] of [...taskPanel.rows]) {
    if (!wanted.has(id)) {
      node.remove();
      taskPanel.rows.delete(id);
    }
  }
  shown.forEach((task, index) => {
    let node = taskPanel.rows.get(task.id);
    if (!node) {
      node = taskPanelRowNode(task.id);
      taskPanel.rows.set(task.id, node);
    }
    updateTaskPanelRow(node, task);
    if (list.children[index] !== node) list.insertBefore(node, list.children[index] ?? null);
  });
  $("tasks-empty").hidden = shown.length > 0 || !taskPanel.loaded;
  for (const button of document.querySelectorAll("#tasks-filter button")) {
    const on = button.dataset.filter === taskPanel.filter;
    button.classList.toggle("active", on);
    button.setAttribute("aria-pressed", String(on));
  }
}

async function taskPanelRefresh() {
  if (!$("tasks-dialog").open || !state.user) return;
  if (taskPanel.running) {
    taskPanel.again = true;
    return;
  }
  clearTimeout(taskPanel.timer);
  taskPanel.running = true;
  taskPanel.again = false;
  let keep = false;
  try {
    const data = await api("/api/tasks?limit=100");
    taskPanel.tasks = data.tasks;
    taskPanel.loaded = true;
    for (const task of data.tasks) taskRecords.set(task.id, task);
    renderTaskPanel();
    $("tasks-error").hidden = true;
    keep = taskPanelNeedsPolling(data);
  } catch (err) {
    showTaskError($("tasks-error"), err);
  }
  taskPanel.running = false;
  if ($("tasks-dialog").open && (keep || taskPanel.again)) taskPanel.timer = setTimeout(taskPanelRefresh, taskPanel.again ? 0 : 3000);
}

$("tasks-open").addEventListener("click", () => {
  const dialog = $("tasks-dialog");
  if (!dialog.open) dialog.showModal();
  taskPanel.loaded = false;
  taskPanel.rows.clear();  // rebuilt: labels follow the interface language
  $("tasks-list").replaceChildren();
  taskPanelRefresh();
});
$("tasks-close").addEventListener("click", () => $("tasks-dialog").close());
$("assoc-close").addEventListener("click", () => $("assoc-dialog").close());
$("tasks-dialog").addEventListener("close", () => clearTimeout(taskPanel.timer));
$("tasks-filter").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-filter]");
  if (!button) return;
  taskPanel.filter = button.dataset.filter;
  renderTaskPanel();
});
// ---- end task panel (DOM)

// ---- automations panel: a second tab of the tasks dialog
const automationPanel = { items: [], loaded: false, editing: null, timer: null };

// Pure: the trigger sentence shown in a row, from a stored trigger.
function automationWhen(trigger) {
  if (!trigger) return "";
  if (trigger.kind === "every") {
    const minutes = trigger.minutes;
    return minutes % 60 === 0 ? tr("every {n} h", { n: minutes / 60 }) : tr("every {n} min", { n: minutes });
  }
  if (trigger.kind === "cron") return tr("cron {expr}", { expr: trigger.cron });
  if (trigger.kind === "at") return tr("once, {when}", { when: String(trigger.at).replace("T", " ").slice(0, 16) });
  return tr("when process {id} ends", { id: trigger.process });
}

// Pure: the status shown on the badge (a paused record keeps its stored status, `active`).
function automationBadge(record) {
  if (record.status === "active" && !record.enabled) return "paused";
  return record.status;
}

// Pure: the trigger object the API expects, from the form fields (the date-time input has no zone: it is Madrid time).
function automationTriggerFromForm(fields) {
  if (fields.kind === "every") return { kind: "every", minutes: Number(fields.minutes) || 0 };
  if (fields.kind === "cron") return { kind: "cron", cron: String(fields.cron || "").trim() };
  if (fields.kind === "at") return { kind: "at", at: String(fields.at || "") };
  return { kind: "process_end", process: String(fields.process || "").trim() };
}

function automationLocalTime(value) {
  if (!value) return "—";
  const when = new Date(value);
  return Number.isNaN(when.getTime()) ? value : when.toLocaleString(state.preferences?.language === "es" ? "es-ES" : undefined, { dateStyle: "short", timeStyle: "short" });
}

function automationRow(record) {
  const badge = element("span", { className: `task-badge ${automationBadge(record)}`, textContent: tr(automationBadge(record)) });
  const title = element("strong", { className: "tasks-row-title", textContent: record.title });
  const bits = [record.agent_name, automationWhen(record.trigger)];
  if (record.next_run && record.enabled && record.status === "active") bits.push(tr("next {when}", { when: automationLocalTime(record.next_run) }));
  if (record.last_run) bits.push(tr("last {when}", { when: automationLocalTime(record.last_run) }));
  bits.push(tr("{count} runs", { count: record.runs }));
  const meta = element("div", { className: "tasks-row-meta" }, element("span", { textContent: bits.join(" · ") }));
  const error = element("div", { className: "task-error", hidden: true });
  error.setAttribute("role", "alert");
  const button = (text, handler) => {
    const node = element("button", { type: "button", className: "secondary", textContent: text });
    node.addEventListener("click", handler);
    return node;
  };
  const act = (path, options = { method: "POST" }) => async () => {
    error.hidden = true;
    try { await api(`/api/automations/${record.id}${path}`, options); } catch (err) { showTaskError(error, err); return; }
    automationPanelRefresh();
  };
  const actions = element("div", { className: "task-actions" });
  if (record.status === "active") {
    actions.append(record.enabled ? button(tr("Pause"), act("/pause")) : button(tr("Resume"), act("/resume")));
  }
  actions.append(
    button(tr("Edit"), () => automationFormOpen(record)),
    button(tr("Delete"), () => { if (window.confirm(tr("Delete this automation?"))) act("", { method: "DELETE" })(); }),
  );
  const row = element("li", { className: "tasks-row automation-row" }, element("div", { className: "tasks-row-head" }, badge, title), meta, actions, error);
  row.dataset.enabled = String(record.enabled);
  row.dataset.id = record.id;
  if (record.history?.length) {
    const details = element("details", {}, element("summary", { textContent: tr("History") }));
    const list = element("ul", { className: "automation-history" });
    for (const entry of [...record.history].reverse()) {
      list.append(element("li", { textContent: `${automationLocalTime(entry.at)} · ${entry.outcome}` }));
    }
    details.append(list);
    row.append(details);
  }
  return row;
}

function renderAutomationPanel() {
  const list = $("automations-list");
  list.replaceChildren(...automationPanel.items.map(automationRow));
  $("automations-empty").hidden = automationPanel.items.length > 0 || !automationPanel.loaded;
}

async function automationPanelRefresh() {
  if (!$("tasks-dialog").open || !state.user || $("automations-pane").hidden) return;
  clearTimeout(automationPanel.timer);
  try {
    const data = await api("/api/automations");
    automationPanel.items = data.automations;
    automationPanel.loaded = true;
    renderAutomationPanel();
    $("tasks-error").hidden = true;
  } catch (err) {
    showTaskError($("tasks-error"), err);
  }
  if ($("tasks-dialog").open && !$("automations-pane").hidden) automationPanel.timer = setTimeout(automationPanelRefresh, 10000);
}

function automationKindChanged() {
  const kind = $("automation-kind").value;
  for (const name of ["every", "cron", "at", "process"]) {
    $(`automation-field-${name}`).hidden = name !== (kind === "process_end" ? "process" : kind);
  }
}

function automationFormOpen(record = null) {
  automationPanel.editing = record;
  const agents = state.user?.agents ?? [];
  $("automation-agent").replaceChildren(...agents.map((agent) => element("option", { value: agent.id, textContent: agent.name })));
  $("automation-agent").disabled = Boolean(record);
  $("automation-agent").value = record?.agent_id ?? state.agentId ?? agents[0]?.id ?? "";
  $("automation-title").value = record?.title ?? "";
  $("automation-instruction").value = record?.instruction ?? "";
  const trigger = record?.trigger ?? { kind: "every", minutes: 60 };
  $("automation-kind").value = trigger.kind;
  $("automation-minutes").value = trigger.minutes ?? 60;
  $("automation-cron").value = trigger.cron ?? "";
  $("automation-at").value = trigger.kind === "at" ? String(trigger.at).slice(0, 16) : "";
  $("automation-process").value = trigger.process ?? "";
  $("automation-error").hidden = true;
  automationKindChanged();
  $("automation-form").hidden = false;
  $("automation-instruction").focus();
}

function automationTabShow(tab) {
  const automations = tab === "automations";
  $("automations-pane").hidden = !automations;
  $("tasks-filter").hidden = automations;
  $("tasks-empty").hidden = automations || ($("tasks-empty").hidden && true);
  $("tasks-list").hidden = automations;
  for (const button of document.querySelectorAll("#tasks-tabs button")) {
    const on = button.dataset.tab === tab;
    button.classList.toggle("active", on);
    button.setAttribute("aria-pressed", String(on));
  }
  if (automations) {
    clearTimeout(taskPanel.timer);
    automationPanelRefresh();
  } else {
    clearTimeout(automationPanel.timer);
    taskPanelRefresh();
  }
}

$("tasks-tabs").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-tab]");
  if (button) automationTabShow(button.dataset.tab);
});
$("automation-kind").addEventListener("change", automationKindChanged);
$("automation-new").addEventListener("click", () => automationFormOpen(null));
$("automation-cancel").addEventListener("click", () => { $("automation-form").hidden = true; });
$("automation-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const fields = {
    kind: $("automation-kind").value, minutes: $("automation-minutes").value, cron: $("automation-cron").value,
    at: $("automation-at").value, process: $("automation-process").value,
  };
  const trigger = automationTriggerFromForm(fields);
  const record = automationPanel.editing;
  $("automation-error").hidden = true;
  try {
    if (record) {
      await api(`/api/automations/${record.id}`, {
        method: "PATCH",
        body: JSON.stringify({ instruction: $("automation-instruction").value, title: $("automation-title").value, trigger }),
      });
    } else {
      await api("/api/automations", {
        method: "POST",
        body: JSON.stringify({ agent_id: $("automation-agent").value, instruction: $("automation-instruction").value, title: $("automation-title").value, trigger }),
      });
    }
  } catch (err) {
    showTaskError($("automation-error"), err);
    return;
  }
  $("automation-form").hidden = true;
  automationPanelRefresh();
});
// the dialog always opens on the tasks tab, and closing it stops the automation refresh
$("tasks-open").addEventListener("click", () => automationTabShow("tasks"));
$("tasks-dialog").addEventListener("close", () => clearTimeout(automationPanel.timer));
// ---- end automations panel
// ---- end task cards: DOM

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
      return {
        phase: "resolved", kind: approvalOutcome(action.decision, action.reason), error: null, decision: current.decision,
        ...(action.scope ? { scope: action.scope } : {}),
      };
    case "answered":
      if (settled) return current;
      return {
        phase: "resolved", kind: approvalOutcome(action.status, null), error: null, decision: current.decision,
        ...(action.scope ? { scope: action.scope } : {}),
      };
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
  const attribution = approvalTaskLabel(approval, tr);
  const explanation = make("p", "approval-explanation", approval.explanation ?? "");
  explanation.hidden = !approval.explanation;
  if (attribution) detail.append(head, make("p", "approval-task", attribution), explanation, command);
  else detail.append(head, explanation, command);
  card.append(summary, detail);
  for (const [label, value] of [[tr("Working directory"), approval.cwd], [tr("Reason"), approval.reason]]) {
    if (!value) continue;
    const row = make("p", "approval-meta");
    row.append(make("strong", null, `${label}: `), document.createTextNode(value));
    detail.append(row);
  }
  const approveButtons = approvalScopeButtons(approval).map(({ scope, label }, index) => {
    const button = make("button", index === 0 ? "primary" : null, label);
    button.type = "button";
    button.dataset.scope = scope;
    button.addEventListener("click", () => answerApproval(entry, "approve", scope));
    return button;
  });
  const approve = approveButtons[0];
  const deny = make("button", null, tr("Deny"));
  deny.type = "button";
  deny.addEventListener("click", () => answerApproval(entry, "deny"));
  const actions = make("div", "approval-actions");
  actions.append(...approveButtons, deny);
  const status = make("p", "approval-status");
  status.setAttribute("role", "status");
  const error = make("p", "approval-error");
  error.setAttribute("role", "alert");
  detail.append(actions, status, error);
  entry.nodes = { card, explanation, summary, countdown, actions, approve, approveButtons, deny, status, error };
  return card;
}

function renderApproval(entry) {
  const { phase, kind, error, decision } = entry.state;
  const { card, summary, countdown, actions, approveButtons, deny, status, error: errorNode } = entry.nodes;
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
  for (const button of [...approveButtons, deny]) button.disabled = phase === "submitting";
  countdown.hidden = !active;
  countdown.textContent = formatCountdown(approvalRemaining(entry.data.expires_at, Date.now()));
  if (phase === "submitting") {
    status.textContent = tr(decision === "deny" ? "Denying…" : "Approving…");
  } else if (kind) {
    status.replaceChildren(Object.assign(document.createElement("span"), {
      className: `approval-badge ${kind}`, textContent: approvalOutcomeLabel(kind) + (kind === "approved" ? approvalScopeSuffix(entry.state.scope) : ""),
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

// The small model's sentence arrives after the card; a card already drawn is filled in, one drawn later gets it from its data.
function explainApproval({ id, text }) {
  const entry = approvalEntries.get(id);
  if (!entry || !text) return;
  entry.data.explanation = text;
  entry.nodes.explanation.textContent = text;
  entry.nodes.explanation.hidden = false;
}

function settleApproval({ id, decision, reason, scope }) {
  const entry = approvalEntries.get(id);
  if (entry) applyApproval(entry, { type: "resolved", decision, reason, scope });
}

async function answerApproval(entry, decision, scope = "once") {
  if (entry.state.phase !== "pending") return;
  applyApproval(entry, { type: "submit", decision });
  const { agent_id: agentId, chat_id: chatId, id } = entry.data;
  try {
    const result = await api(
      `/api/agents/${encodeURIComponent(agentId)}/chats/${encodeURIComponent(chatId)}/approvals/${encodeURIComponent(id)}`,
      { method: "POST", body: JSON.stringify({ decision, scope }) },
    );
    applyApproval(entry, { type: "answered", status: result?.status, scope: result?.scope });
    if (result?.error) {  // the approval event may have settled the card first: show the note either way
      entry.state = { ...entry.state, error: tr("Approved once only. The rule was not saved: {error}", { error: result.error }) };
      renderApproval(entry);
    }
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
  watchApprovalCards(pending.map((entry) => entry.nodes.card));
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
    $("messages-wrap").after(bar);
  }
  bar.hidden = pending.every((entry) => approvalCardsSeen.has(entry.nodes.card));  // nothing to jump to when it is already on screen
  const label = approvalPendingLabel(pending.length);
  if (bar.textContent !== label) bar.textContent = label;
}

// Which pending approval cards are inside the visible part of the chat (so the jump bar is only shown for hidden ones).
const approvalCardsSeen = new Set();
let approvalCardsObserver = null;
let approvalCardsWatched = new Set();

function watchApprovalCards(cards) {
  if (typeof IntersectionObserver === "undefined") return;
  approvalCardsObserver ??= new IntersectionObserver((entries) => {
    for (const entry of entries) {
      if (entry.isIntersecting) approvalCardsSeen.add(entry.target);
      else approvalCardsSeen.delete(entry.target);
    }
    updateApprovalIndicator();
  }, { root: $("messages"), threshold: 0.3 });
  const next = new Set(cards);
  for (const card of approvalCardsWatched) if (!next.has(card)) { approvalCardsObserver.unobserve(card); approvalCardsSeen.delete(card); }
  for (const card of next) if (!approvalCardsWatched.has(card)) approvalCardsObserver.observe(card);
  approvalCardsWatched = next;
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
  for (const approval of pending) if (approval.explanation) explainApproval({ id: approval.id, text: approval.explanation });
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
  const { backend: _old, workdir: _oldWorkdir, approvals: _oldApprovals, allow_in_dream: _oldDream, ...rest } = saved ?? {};
  return {
    ...rest,
    backends,
    default_backend: terminalDefaultBackend(allowed.map((field) => field.name), fields.defaultBackend),
    timeout_seconds: Number(fields.timeout),
  };
}

// ------------------------------------------------------------------ permission levels and approval rules (agent form)
// Pure helpers over the catalog of GET /api/levels (names, descriptions, the minimum level of every group); they mirror
// required_level_reason() in src/config/levels.py, and the catalog is built from the code that enforces the levels.

const LEARNED_RULE_REASON = "approved always for this agent"; // the `reason` of rules written from an approval card

function levelNumber(catalog, name) {
  const found = (catalog?.levels ?? []).find((level) => level.name === name);
  return found ? found.number : -1;
}

function levelTitle(catalog, name) {
  const found = (catalog?.levels ?? []).find((level) => level.name === name);
  return found ? found.title : String(name ?? "");
}

// The lowest level covering the form's choices and what sets it. `choices`: { toolGroups, mcp, backends: [{name,
// min_level}] (the terminal backends in use), delegationTargets } where mcp and delegationTargets are booleans.
function requiredLevelFor(catalog, choices) {
  let best = { number: 0, level: catalog.levels[0].name, reason: { kind: "none" } };
  const raise = (name, reason) => {
    const number = levelNumber(catalog, name);
    if (number > best.number) best = { number, level: name, reason };
  };
  const groups = choices.toolGroups ?? [];
  for (const group of groups) raise(catalog.groups[group] ?? catalog.levels[0].name, { kind: "group", name: group });
  if (choices.mcp) raise(catalog.mcp, { kind: "mcp" });
  if (groups.includes("terminal")) {
    for (const backend of choices.backends ?? []) {
      // only the classes above the plain ssh one (local, root) add to the level of the terminal group itself
      if (levelNumber(catalog, backend.min_level) >= levelNumber(catalog, catalog.services.terminal_root)) {
        raise(backend.min_level, { kind: "backend", name: backend.name });
      }
    }
  }
  if (groups.includes("delegate") && choices.delegationTargets) raise(catalog.delegation_targets, { kind: "delegation" });
  return best;
}

// How the selected level stands against the choices and the user's maximum: problem is null, "above_max" (the choices
// need more than the user may give: remove something or ask an administrator), "below_required" (raise the level) or
// "exceeds_max" (the selected level itself is above the maximum).
function levelAdvice(catalog, selectedName, choices, maxName) {
  const required = requiredLevelFor(catalog, choices);
  const selected = levelNumber(catalog, selectedName);
  const max = levelNumber(catalog, maxName);
  let problem = null;
  if (required.number > max) problem = "above_max";
  else if (required.number > selected) problem = "below_required";
  else if (selected > max) problem = "exceeds_max";
  return { required, problem };
}

// The select's options: one per level, those above the user's maximum disabled.
function levelOptions(catalog, maxName) {
  const max = levelNumber(catalog, maxName);
  return (catalog?.levels ?? []).map((level) => ({
    value: level.name, label: `${level.title} (${level.number})`, disabled: level.number > max,
  }));
}

// One line for a level, from the texts of its services (translate: the same function for every part).
function levelDescriptionText(catalog, name, translate) {
  const found = (catalog?.levels ?? []).find((level) => level.name === name);
  return found ? sentenceCase(found.parts.map(translate).join("; ")) : "";
}

function sentenceCase(text) {
  const chars = [...String(text ?? "")];
  return chars.length ? chars[0].toUpperCase() + chars.slice(1).join("") : "";
}

// The items of a level's description: the short text (capitalised) and, apart, the technical detail that the service text
// carries in parentheses (tool group names, when it asks), which the UI shows as a tooltip instead of in the line.
function levelDescriptionItems(catalog, name, translate) {
  const found = (catalog?.levels ?? []).find((level) => level.name === name);
  return (found?.parts ?? []).map((part) => {
    const text = String(translate(part));
    const match = text.match(/^([^(]*?)\s*\(([^)]*)\)\s*$/);
    return match ? { text: sentenceCase(match[1]), detail: sentenceCase(match[2]) } : { text: sentenceCase(text), detail: "" };
  });
}

// Badges for the Tools tab: the tool groups that need more than the selected level, and whether the user's maximum
// also falls short of it. Groups are never hidden.
function toolGroupBadges(catalog, selectedName, maxName) {
  const selected = levelNumber(catalog, selectedName);
  const max = levelNumber(catalog, maxName);
  const badges = {};
  for (const [group, level] of Object.entries(catalog?.groups ?? {})) {
    const number = levelNumber(catalog, level);
    if (number > selected) badges[group] = { level, overMax: number > max };
  }
  return badges;
}

// What the service that sets the required level is called in a message.
function levelReasonText(reason, groupName) {
  switch (reason?.kind) {
    case "group": return tr("tool group '{name}'", { name: groupName(reason.name) });
    case "mcp": return tr("MCP servers");
    case "backend": return tr("terminal backend '{name}'", { name: reason.name });
    case "delegation": return tr("delegation to other agents");
    default: return tr("no service");
  }
}

// The inline message under the selector ("" when everything fits).
function levelMessage(catalog, advice, selectedName, maxName, groupName) {
  const service = levelReasonText(advice.required.reason, groupName);
  const needed = levelTitle(catalog, advice.required.level);
  const max = levelTitle(catalog, maxName);
  switch (advice.problem) {
    case "above_max":
      return tr("{service} needs the level {needed}, above your maximum level {max}. Remove {service} or ask an administrator to raise your maximum.", { service, needed, max });
    case "below_required":
      return tr("{service} needs the level {needed}, but this agent is {level}. Raise the level to {needed} or remove {service}.", { service, needed, level: levelTitle(catalog, selectedName) });
    case "exceeds_max":
      return tr("The level {level} is above your maximum level {max}. Choose {max} or lower, or ask an administrator to raise your maximum.", { level: levelTitle(catalog, selectedName), max });
    default:
      return "";
  }
}

// The `level` to send: the selected one when the user chose it or the form moved it, else nothing (the stored level, or
// the one the configuration needs, stays as it is).
function levelPayload(selectedName, initialName, touched) {
  return selectedName && (touched || selectedName !== initialName) ? { level: selectedName } : {};
}

// Server errors about the level (HTTP 400, stable codes) show next to the selector.
function isLevelErrorCode(code) {
  return code === "level_exceeds_max" || code === "level_below_required" || code === "level_required_above_max";
}

// Errors about the approval rules (the model's validation, or the blanket-allow guardrail) show in the rule editor.
function isRuleError(error) {
  return error?.code === "approval_rule_not_allowed" || /approval_rules|approval rule/i.test(String(error?.message ?? ""));
}

// The agent's own rules split into the ones the editor shows (written by hand) and the learned ones (from approval cards).
function splitApprovalRules(rules) {
  const learned = (rules ?? []).filter((rule) => rule.reason === LEARNED_RULE_REASON);
  const manual = (rules ?? []).filter((rule) => rule.reason !== LEARNED_RULE_REASON);
  return { manual, learned };
}

// One rule from the editor's fields. `base` is the rule being edited: keys the editor does not show (id, reason) stay.
function buildApprovalRule(base, fields) {
  const rule = { ...(base ?? {}), verdict: fields.verdict, tool: fields.tool || "any" };
  for (const key of ["command", "path", "backend"]) {
    const value = String(fields[key] ?? "").trim();
    if (value) rule[key] = value;
    else delete rule[key];
  }
  if (fields.backendKind) rule.backend_kind = fields.backendKind;
  else delete rule.backend_kind;
  if (fields.dangerous === "yes") rule.dangerous = true;
  else if (fields.dangerous === "no") rule.dangerous = false;
  else delete rule.dangerous;
  if (fields.exact && (rule.command || rule.path)) rule.exact = true;
  else delete rule.exact;
  return rule;
}

// The editor's values as the fields of one row (the inverse of buildApprovalRule).
function approvalRuleFields(rule) {
  return {
    verdict: rule?.verdict ?? "ask", tool: rule?.tool ?? "any", command: rule?.command ?? "", path: rule?.path ?? "",
    backend: rule?.backend ?? "", backendKind: rule?.backend_kind ?? "",
    dangerous: rule?.dangerous === true ? "yes" : rule?.dangerous === false ? "no" : "", exact: Boolean(rule?.exact),
  };
}

// The `approval_rules` to save: the editor's rules, then the learned ones (the editor does not show them, saving must not drop them).
function approvalRulesBody(defaultVerdict, manualRules, learnedRules) {
  return { ...(defaultVerdict ? { default: defaultVerdict } : {}), rules: [...manualRules, ...learnedRules] };
}

// Administrator screen: the agents whose level is above a candidate maximum.
function agentsAboveCeiling(catalog, agents, ceilingName) {
  const ceiling = levelNumber(catalog, ceilingName);
  return (agents ?? []).filter((agent) => levelNumber(catalog, agent.level) > ceiling);
}

// ------------------------------------------------------------------ delegation settings (agent form)

// the groups a task may be given (DELEGATE_GRANTABLE_GROUPS in src/agent/tool_groups.py) and the default selection
const DELEGATION_GRANTABLE_GROUPS = ["web"];
const DELEGATION_DEFAULT_GROUPS = ["web"];

// One row per OTHER agent of the user (plus saved targets that vanished, so the error can be seen): allowed or not,
// and whether it may write its own memory while running the task.
function delegationTargetRows(agents, selfId, saved) {
  const used = new Map((saved?.targets ?? []).map((target) => [target.agent, Boolean(target.remember)]));
  const rows = (agents ?? []).filter((agent) => agent.id !== selfId).map((agent) => ({
    id: agent.id, name: agent.name || agent.id, allowed: used.has(agent.id), remember: used.get(agent.id) ?? false, missing: false,
  }));
  for (const [id, remember] of used) {
    if (!rows.some((row) => row.id === id)) rows.push({ id, name: id, allowed: true, remember, missing: true });
  }
  return rows;
}

// The `delegation` block to save: the form's fields while the `delegate` group is ticked, else the saved block
// untouched, so saving the agent never drops a customised block back to the defaults.
function buildDelegationConfig(saved, groupChecked, fields) {
  if (!groupChecked) return saved ?? null;
  return {
    ...(saved ?? {}),
    enabled: Boolean(fields.enabled),
    allowed_groups: [...(fields.allowedGroups ?? [])],
    max_parallel: fields.maxParallel ?? null,
    timeout_seconds: fields.timeout ?? null,
    max_tokens: fields.maxTokens ?? null,
    default_model: (fields.defaultModel ?? "").trim() || null,
    targets: (fields.targets ?? []).filter((row) => row.allowed).map((row) => ({ agent: row.id, remember: Boolean(row.remember) })),
  };
}

// Save errors about the delegation block belong next to it.
function isDelegationError(message) {
  return /delegation/i.test(String(message ?? ""));
}

// Save errors about the terminal block (missing backend, local disabled, bad fields) belong next to it.
function isTerminalError(message) {
  return /terminal/i.test(String(message ?? ""));
}

function addMessage(role, parent = state.view ?? $("messages"), author = null) {
  const el = document.createElement("div");
  el.className = `msg ${role}`;
  if (role === "assistant") el.dataset.author = author ? author.name : currentAgentName();
  else if (role === "command") el.dataset.author = "RECALL";
  let body = document.createElement("div");
  body.className = "body";
  let raw = "";
  // The reply is a timeline: text before a step (tool call, approval card) stays above it, later text opens a new body below it.
  const bodies = [body];
  const starts = [0];
  let thinking = null;  // the thinking segment that still takes chunks: nothing was added after it yet
  let thinkingRaw = "";
  let memoryIndicator = null;
  let workingLine = null;  // the "working…" line (see `working`), removed by the first text or error
  let actionCount = 0;
  let liveSteps = [];  // tool steps of this reply (state from toolStepsApply) and their DOM nodes, same indexes
  const liveNodes = [];
  let attachmentBox = null;
  let badge = null;
  function setPicture(avatar) {  // the big picture at the start of the reply (hidden by the show_agent_avatar setting)
    badge?.remove();
    badge = null;
    const picture = messageAvatar(avatar);
    if (!picture) return;
    const image = element("img", { alt: "", draggable: false });
    const own = element("span", { className: "msg-avatar" }, image);
    if (picture.free) own.dataset.avatarStyle = "free";
    image.addEventListener("error", () => own.remove());
    image.src = picture.src;
    el.prepend(own);
    badge = own;
  }
  if (role === "assistant") setPicture(author ? author.avatar : currentAgentAvatar());
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
  function addStep(node, keepThinking = false) {
    if (!keepThinking) thinking = null;
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
  // Attachments of a message: images, audio and video play right there; anything else shows its name.
  function renderAttachmentNames(attachments, replace = false) {
    if (!attachments?.length) return;
    if (!attachmentBox) {
      attachmentBox = document.createElement("div");
      attachmentBox.className = "message-attachments";
      el.insertBefore(attachmentBox, body);
    }
    const items = attachments.map((attachment) => attachmentView(attachment));
    if (replace) attachmentBox.replaceChildren(...items);
    else attachmentBox.append(...items);
  }
  // Files the agent hands over (`send_file`) sit in their own box at the very end of the turn: later steps and text are
  // added above it, so it never scrolls out of sight. A person's own attachments stay above the message instead.
  let agentFilesBox = null;
  function renderAgentFiles(files) {
    if (!files?.length) return;
    if (!agentFilesBox) {
      agentFilesBox = document.createElement("div");
      agentFilesBox.className = "message-attachments agent-files";
      el.append(agentFilesBox);
      new MutationObserver(() => { if (el.lastElementChild !== agentFilesBox) el.append(agentFilesBox); })
        .observe(el, { childList: true });
    }
    agentFilesBox.append(...files.map((file) => attachmentView(file)));
  }
  const view = {
    el,
    agentFiles: renderAgentFiles,
    text(chunk) {
      if (Array.isArray(chunk)) {
        const textParts = chunk.filter((part) => part.type === "text").map((part) => part.text);
        raw += textParts.join("");
        renderAttachmentNames(chunk.filter((part) => part.type !== "text").map((part) => (
          part.type === "image_url"
            ? { name: part.name || "image", modality: "image", data_url: part.image_url?.url }
            : { name: part.name || part.type }
        )), true);
        if (textParts.join("").trim()) thinking = null;
        renderBody();
        return;
      }
      raw += chunk;
      if (chunk.trim()) thinking = null;
      if (workingLine) { workingLine.remove(); workingLine = null; }
      renderBody();
    },
    attachments: (items) => renderAttachmentNames(items),
    // what speech-to-text heard, one text per recording of the message in order (the reply's first event)
    transcripts(texts) {
      const players = attachmentBox ? [...attachmentBox.querySelectorAll(":scope > .attachment-audio")] : [];
      players.forEach((item, position) => { if (texts[position]) showTranscript(item, texts[position]); });
    },
    // Every reasoning burst is its own segment at its place in the timeline; chunks that follow it directly join it.
    thinking(chunk) {
      if (!thinking) {
        if (!String(chunk).trim()) return;
        thinkingRaw = "";
        thinking = thinkingSegment();
        addStep(thinking.node, true);
      }
      // models end their reasoning with newlines; trimming keeps an empty line from showing under the block
      thinkingRaw += chunk;
      thinking.set(thinkingRaw.trimEnd());
    },
    // `working`: a pulsing line while RECALL does something that streams no text (a /compact); the first text replaces it.
    working(text) {
      if (workingLine) workingLine.remove();
      workingLine = document.createElement("div");
      workingLine.className = "working";
      workingLine.setAttribute("role", "status");
      workingLine.textContent = text;
      el.insertBefore(workingLine, body);
    },
    notice(text) {
      const note = document.createElement("div");
      note.className = "notice";
      note.setAttribute("role", "status");
      note.textContent = text;
      el.insertBefore(note, body);
    },
    memory({ state, matches = 0, fragments = null, query = null }) {
      if (!memoryIndicator) {
        memoryIndicator = document.createElement("div");
        memoryIndicator.setAttribute("role", "status");
        el.prepend(memoryIndicator);  // always the first element of the bubble, above any thinking
      }
      const outcome = state === "done" ? (matches > 0 ? "found" : "none") : state;
      memoryIndicator.className = `memory-indicator ${outcome}`;
      memoryIndicator.dataset.state = state;
      let summary;
      if (outcome === "searching") summary = tr("Associative memory: searching");
      else if (outcome === "skipped") summary = tr("Associative memory: skipped");
      else if (outcome === "none") summary = tr("Associative memory: no matches");
      else if (matches === 1) summary = tr("Associative memory: {count} match", { count: matches });
      else summary = tr("Associative memory: {count} matches", { count: matches });
      const shown = outcome === "found" ? fragments : null;
      const hasDetails = Boolean((Array.isArray(shown) && shown.length) || query);
      memoryIndicator.classList.toggle("expandable", hasDetails);
      if (hasDetails) {
        const button = Object.assign(document.createElement("button"), { type: "button", textContent: summary });
        button.setAttribute("aria-haspopup", "dialog");
        button.addEventListener("click", () => openMemoryDialog(summary, shown, query));
        memoryIndicator.replaceChildren(button);
      } else memoryIndicator.textContent = summary;
    },
    settleMemory() {
      // a search still running when the reply ends, fails or is interrupted was not used
      if (memoryIndicator?.dataset.state === "searching") this.memory({ state: "skipped" });
    },
    // `tool_start`: the step appears now, at its place in the timeline, with a running indicator.
    toolStart(p) {
      const { steps, index } = toolStepsApply(liveSteps, "tool_start", p);
      if (index < liveNodes.length) return;  // a replayed duplicate
      liveSteps = steps;
      liveNodes[index] = actionStep(steps[index].action);
      addStep(liveNodes[index]);
    },
    // The final `tool` event: completes the step started above in place (same position, same open/closed state).
    action(a) {
      actionCount++;
      if (a.files?.length) renderAgentFiles(a.files);  // `send_file`: the files the agent hands over
      const { steps, index } = toolStepsApply(liveSteps, "tool", a);
      const started = index < liveNodes.length;
      liveSteps = steps;
      const node = actionStep(a);
      if (started) {
        const old = liveNodes[index];
        node.open = old.open;
        unwatchRunningStep(old);
        old.replaceWith(node);
      } else {
        addStep(node);
      }
      liveNodes[index] = node;
    },
    // The stream ended or the turn was interrupted: a step without its final event will not get one.
    settleSteps() {
      endStopping();
      liveSteps = liveSteps.map((step, index) => {
        if (step.phase !== "running") return step;
        stopRunningStep(liveNodes[index]);
        return toolStepStop(step);
      });
    },
    // `task_started`: a task launched by a `task_start` that is still running (wait=true): bind its card to the id now.
    taskStarted(p) {
      const { steps, index } = toolStepBindTask(liveSteps, p);
      if (index < 0) return;
      liveSteps = steps;
      const node = liveNodes[index];
      if (node?._parts) {
        node._action = steps[index].action;
        node._taskId = p.task_id;
        refreshTaskCard(node);
        ensureTaskPolling();
      }
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
    interrupted(snapshot = null, reason = snapshot?.reason) {
      this.settleMemory();
      if (snapshot) {
        raw = snapshot.content;
        for (let i = starts.length - 1; i > 0; i--) if (starts[i] > raw.length) starts[i] = raw.length;
        renderBody(true);
        // the thinking segments already shown stay; the snapshot only fills in a view that saw none
        if (snapshot.reasoning && !el.querySelector(":scope > details.thinking")) this.thinking(snapshot.reasoning);
        // the step that was running when Stop came is completed in place by its final action (no second card)
        for (const action of snapshot.actions.slice(actionCount)) this.action(action);
      }
      this.settleSteps();
      if (!el.classList.contains("interrupted")) {
        el.classList.add("interrupted");
        const note = document.createElement("div");
        note.className = "interruption";
        note.setAttribute("role", "status");
        note.textContent = reason === "stopped" ? tr("Stopped by you") : reason === "replaced" ? tr("Interrupted by a new message") : tr("Interrupted");
        el.append(note);
      }
    },
    // another agent answers this reply (an @mention): its name and picture replace the chat agent's
    speaker(payload) {
      const author = replyAuthor(payload?.agent_id, payload?.name, state.user?.agents, state.agentId);
      el.dataset.author = author.name;
      setPicture(payload?.avatar ?? author.avatar);
    },
    error(source) {
      if (workingLine) { workingLine.remove(); workingLine = null; }
      this.settleMemory();
      this.settleSteps();
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
  } else if (action === "open-providers") {
    $("settings-open").click();
    showSettingsSection("providers");
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

// The chat follows streamed content only while the user is within the bottom margin (`stuck`). Scrolling up unsticks it
// and nothing moves the view until the user comes back near the bottom or clicks "Jump to latest". `force` (sending,
// opening a chat, rendering its history) always goes to the bottom and sticks.
let stuckToBottom = true;

function updateJumpLatest() {
  const box = $("messages");
  $("jump-latest").hidden = nearBottom(box.scrollHeight, box.scrollTop, box.clientHeight);
}

function scrollDown(force = false) {
  const box = $("messages");
  if (force) stuckToBottom = true;
  if (stuckToBottom) box.scrollTop = box.scrollHeight;
  updateJumpLatest();
}

// only the user's own scrolling changes `stuckToBottom`: growing content does not fire a scroll event
$("messages").addEventListener("scroll", () => {
  const box = $("messages");
  stuckToBottom = nearBottom(box.scrollHeight, box.scrollTop, box.clientHeight);
  updateJumpLatest();
}, { passive: true });
// tool steps, thinking and text streaming in follow the bottom while stuck, otherwise show the button
new MutationObserver(() => scrollDown()).observe($("messages"), { childList: true, subtree: true, characterData: true });
$("jump-latest").addEventListener("click", () => scrollDown(true));
// the box itself shrinks or grows when a tray above the composer (subagents, processes) opens or closes: stay at the end
if (typeof ResizeObserver !== "undefined") new ResizeObserver(() => scrollDown()).observe($("messages"));

// Phones: when the on-screen keyboard opens, some browsers shrink only the VISUAL viewport and scroll the page up, which
// pushes the top bar (menu button, agent name) out of sight. Size the app to the visual viewport instead, and keep the
// page scrolled to the top. `interactive-widget=resizes-content` in index.html does the same where it is supported.
function fitToVisualViewport() {
  const viewport = window.visualViewport;
  if (!viewport || !window.matchMedia("(max-width: 800px)").matches) {
    document.documentElement.style.removeProperty("--app-h");
    return;
  }
  if (viewport.scale > 1.01) return; // pinch-zoomed: leave the layout alone
  document.documentElement.style.setProperty("--app-h", `${Math.round(viewport.height)}px`);
  if (viewport.offsetTop > 0) window.scrollTo(0, 0);
}
if (window.visualViewport) {
  window.visualViewport.addEventListener("resize", fitToVisualViewport);
  window.visualViewport.addEventListener("scroll", fitToVisualViewport);
  window.addEventListener("orientationchange", fitToVisualViewport);
  fitToVisualViewport();
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

// The speech-to-text endpoint names MP4 audio (what Safari records) "m4a".
function audioFormat(mimeType) {
  return mimeType.split(";")[0] === "audio/mp4" ? "m4a" : null;
}

function readAttachment(file) {
  const pdf = isPdf(file);
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve({
      modality: attachmentModality(file),
      name: file.name,
      mime_type: pdf ? "application/pdf" : file.type,
      format: pdf ? "pdf" : audioFormat(file.type) ?? file.type.split("/")[1].split(";")[0],
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

// ---- voice messages: record in the browser, attach as audio; the server transcribes it like any audio file

// Browsers expose the microphone only on HTTPS or localhost.
function voiceSupported() {
  return Boolean(navigator.mediaDevices?.getUserMedia) && typeof MediaRecorder !== "undefined";
}

function voiceStopTracks() {
  voice.stream?.getTracks().forEach((track) => track.stop());
  clearInterval(voice.timer);
  voice.recorder = voice.stream = voice.timer = null;
  $("mic").classList.remove("recording");
  $("mic-time").hidden = true;
  $("mic-time").textContent = "";
  $("mic").title = tr("Record a voice message");
  $("mic").setAttribute("aria-label", tr("Record a voice message"));
}

function voiceTick() {
  const seconds = Math.floor((Date.now() - voice.startedAt) / 1000);
  const time = `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
  $("mic-time").textContent = time;
  $("mic").title = tr("Stop recording ({time})", { time });
}

async function voiceStart() {
  voice.stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  voice.chunks = [];
  voice.discard = false;
  voice.recorder = new MediaRecorder(voice.stream);
  voice.recorder.addEventListener("dataavailable", (event) => { if (event.data.size) voice.chunks.push(event.data); });
  voice.recorder.addEventListener("stop", async () => {
    const type = voice.recorder?.mimeType || "audio/webm";
    const chunks = voice.chunks;
    const discard = voice.discard;
    voiceStopTracks();
    if (discard || !chunks.length) return;
    try {
      const extension = type.includes("mp4") ? "m4a" : type.includes("ogg") ? "ogg" : "webm";
      const file = new File(chunks, `voice.${extension}`, { type });
      if (file.size > MAX_FILE_BYTES) throw new Error(tr("{name}: maximum size is 10 MB", { name: file.name }));
      const total = state.attachments.reduce((sum, item) => sum + item.size, 0) + file.size;
      if (total > MAX_TOTAL_FILE_BYTES) throw new Error(tr("Attachments exceed the 25 MB total limit"));
      state.attachments.push(await readAttachment(file));
      renderPendingAttachments();
      updateSendButton();
    } catch (err) {
      addMessage("assistant").error(err);
    }
  });
  voice.recorder.start();
  voice.startedAt = Date.now();
  $("mic").classList.add("recording");
  $("mic-time").hidden = false;
  $("mic").setAttribute("aria-label", tr("Stop recording"));
  voiceTick();
  voice.timer = setInterval(voiceTick, 1000);
}

$("mic").addEventListener("click", async () => {
  if (voice.recorder) { voice.recorder.stop(); return; }
  try {
    await voiceStart();
  } catch (err) {
    voiceStopTracks();
    addMessage("assistant").error(err?.name === "NotAllowedError" ? new Error(tr("Microphone access was denied")) : err);
  }
});
// Adds files to the message being written (the clip button, pasting and dropping share this): same type and size checks.
async function addFiles(files) {
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
    updateSendButton();
  } catch (err) {
    addMessage("assistant").error(err);
  }
}

$("file-input").addEventListener("change", async (event) => {
  const files = [...event.target.files];
  event.target.value = "";
  await addFiles(files);
});

// Ctrl+V of a screenshot or a copied file attaches it. Text always pastes as text: a copy from a spreadsheet
// carries both its text and a picture of it, and the text is what the person wants.
function pastedFiles(clipboard) {
  if (!clipboard || clipboard.getData("text/plain")) return [];
  const stamp = new Date().toISOString().slice(0, 19).replace(/[T:]/g, "-");
  return [...clipboard.items]
    .filter((item) => item.kind === "file")
    .map((item) => item.getAsFile())
    .filter(Boolean)
    // browsers call every pasted screenshot "image.png": give each its own name
    .map((file, index) => /^image\.\w+$/i.test(file.name)
      ? new File([file], `pasted-${stamp}${index ? `-${index + 1}` : ""}.${file.name.split(".").pop()}`, { type: file.type })
      : file);
}

$("input").addEventListener("paste", (event) => {
  const files = pastedFiles(event.clipboardData);
  if (!files.length) return;
  event.preventDefault();
  if ($("attach").disabled) return;  // no chat open, or the chat is switching models
  addFiles(files);
});

// Dragging files onto the chat attaches them, like the clip button and pasting. The chat area shows a frame
// while files are over it; text or links dragged around the page are left to the browser.
{
  const zone = $("chat-view");
  let depth = 0;  // dragenter/dragleave fire for every child: count them so the frame does not flicker
  const carriesFiles = (event) => [...(event.dataTransfer?.types ?? [])].includes("Files");
  const clear = () => { depth = 0; zone.classList.remove("drop-target"); };
  zone.addEventListener("dragenter", (event) => {
    if (!carriesFiles(event)) return;
    event.preventDefault();
    depth += 1;
    if (!$("attach").disabled) zone.classList.add("drop-target");
  });
  zone.addEventListener("dragover", (event) => {
    if (!carriesFiles(event)) return;
    event.preventDefault();  // without it the browser opens the file instead of dropping it here
    event.dataTransfer.dropEffect = $("attach").disabled ? "none" : "copy";
  });
  zone.addEventListener("dragleave", (event) => {
    if (!carriesFiles(event)) return;
    depth = Math.max(0, depth - 1);
    if (!depth) zone.classList.remove("drop-target");
  });
  zone.addEventListener("drop", (event) => {
    if (!carriesFiles(event)) return;
    event.preventDefault();
    clear();
    if ($("attach").disabled) return;  // no chat open, or the chat is switching models
    const files = [...event.dataTransfer.files];
    if (files.length) addFiles(files);
    $("input").focus();
  });
  window.addEventListener("dragend", clear);
}

$("composer").addEventListener("submit", async (e) => {
  e.preventDefault();
  const content = $("input").value.trim();
  if (sendMode() === "stop" && state.user && state.chatId && !state.modelBusy) { await stopReply(); return; }
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
  rememberChat(state.agentId, state.chatId, true);
  if (edit) {
    while (edit.el.nextElementSibling) edit.el.nextElementSibling.remove();
    edit.el.remove();
    state.usages.delete(key);  // the numbers of the removed replies no longer describe the chat
    $("usage").hidden = true;
  }
  const userMessage = addMessage("user", state.view);
  userMessage.text(content);
  userMessage.attachments(edit ? edit.shown : attachments);
  // /dream and /compact are answered by RECALL itself (not stored, not the model): shown as a system reply
  const command = !edit && !attachments.length && (DREAM_COMMAND.test(content) || COMPACT_COMMAND.test(content) || USAGE_COMMAND.test(content));
  const reply = addMessage(command ? "command" : "assistant", state.view);
  const request = {
    userId: state.user.id, agentId: state.agentId, chatId: state.chatId,
    session: state.session, controller: new AbortController(), reply,
    generationId: null, done: false, userMessage,
  };
  state.requests.add(request);
  state.latest = request;
  updateBusy();
  scrollDown(true);
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
        const finalKey = chatKey(request.agentId, request.chatId);  // the chat may have been replaced meanwhile
        state.views.delete(finalKey);
        state.usages.delete(finalKey);
      }
      updateBusy();
      if (!isGenerating(request.agentId, request.chatId)) {
        if (isCurrent(request)) $("input").focus();
        try {
          await refreshChats();
          if (isCurrent(request)) {
            if (request.reloadAfter) {
              forgetApprovals(request.agentId, request.chatId);
              const reloaded = await api(chatsUrl(`/${request.chatId}${chatQuery(state.view?.historyLimit)}`));
              renderMessages(reloaded.messages, reloaded.window, state.view?.historyLimit);
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

// Touch screens have no Shift key: there Enter writes a new line and the Send button sends.
const touchKeyboard = window.matchMedia("(pointer: coarse)");
function enterSends(event, touch = touchKeyboard.matches) {
  return event.key === "Enter" && !event.shiftKey && !event.isComposing && !touch;
}
$("input").addEventListener("keydown", (e) => {
  if (enterSends(e)) {
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

  await readEvents(res, request);
}

// Feeds the SSE frames of a response into the request's reply until the stream ends.
async function readEvents(res, request) {
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
    request.reply?.settleSteps?.();  // a step still running when the stream ends (error, abort) becomes "stopped"
  }
}

// What to do once a followed reply's stream ended: "stop" (nothing to redraw), "reload" (show the saved chat)
// or "follow" (a new message steered the reply and another run started).
function afterFollow({ outcome, current, generating, active, attempt }) {
  if (!current || generating || outcome === "failed" || outcome === "aborted") return "stop";
  return active && attempt < 5 ? "follow" : "reload";
}

// Attaches to a reply that runs without a request of this page (another tab, a page load, a reload): the server
// replays what it produced so far and then streams the rest. Leaving the chat never stops the reply or this reader.
async function followReply(agentId, chatId, attempt = 0) {
  const key = chatKey(agentId, chatId);
  const reply = addMessage("assistant", state.view);
  const note = element("div", { className: "generating-note", textContent: tr("Generating…") });
  note.setAttribute("role", "status");
  reply.el.append(note);
  const request = {
    userId: state.user.id, agentId, chatId, session: state.session, controller: new AbortController(), reply,
    generationId: null, done: false, follower: true,
  };
  state.requests.add(request);
  state.views.set(key, state.view);
  state.latest = request;
  updateBusy();
  let outcome = "ended";
  try {
    const res = await fetch(`/api/agents/${encodeURIComponent(agentId)}/chats/${encodeURIComponent(chatId)}/stream`, {
      headers: { "Accept-Language": state.preferences?.language ?? "en" }, signal: request.controller.signal,
    });
    if (res.status === 401) {
      if (sameSession(request)) showLogin();
      throw new Error(tr("not logged in"));
    }
    if (!res.ok) throw httpError(await res.json().catch(() => ({})), res.status);
    if (res.status !== 204) await readEvents(res, request);
  } catch (err) {
    request.controller.abort();
    if (err.name === "AbortError") outcome = "aborted";
    else {
      outcome = "failed";
      if (sameSession(request)) reply.error(err);
    }
  } finally {
    note.remove();
    state.requests.delete(request);
  }
  if (!sameSession(request) || outcome === "aborted") return;
  syncApprovals(agentId, chatId);
  if (isGenerating(agentId, chatId)) return;
  if (!isCurrent(request)) {
    state.views.delete(key);
    state.usages.delete(key);
  }
  updateBusy();
  try {
    await refreshChats();
    if (request.errored) return; // keep the error card visible
    if (afterFollow({ outcome, current: isCurrent(request), generating: false, active: false, attempt }) === "stop") return;
    // the run is over: show the saved chat, or follow the next run when a new message steered this one
    const data = await api(chatsUrl(`/${chatId}${chatQuery(state.view?.historyLimit)}`));
    const next = afterFollow({
      outcome, current: isCurrent(request), generating: isGenerating(agentId, chatId), active: data.chat.active, attempt,
    });
    if (next === "stop") return;
    forgetApprovals(agentId, chatId);
    renderMessages(data.messages, data.window, state.view?.historyLimit);
    if (next === "follow") followReply(agentId, chatId, attempt + 1);
    else {
      updateBusy();
      syncApprovals(agentId, chatId);
      $("input").focus();
    }
  } catch (err) {
    if (isCurrent(request)) reply.error(err);
  }
}

// The server answered in a new chat because the one this page wrote to (still empty) had been deleted:
// from here on the request, the open chat, the address bar and the sidebar follow the new one.
function adoptReplacedChat(request, chat) {
  if (!chat?.id) return;
  const wasCurrent = isCurrent(request);
  const oldKey = chatKey(request.agentId, request.chatId);
  const newKey = chatKey(request.agentId, chat.id);
  if (state.views.has(oldKey)) {
    state.views.set(newKey, state.views.get(oldKey));
    state.views.delete(oldKey);
  }
  request.chatId = chat.id;
  if (!wasCurrent) return;
  state.chatId = chat.id;
  state.chatInfo = chat;
  rememberChat(request.agentId, chat.id, true);
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
  else if (event === "tool_start") reply.toolStart(payload);
  else if (event === "tool") reply.action(payload);
  else if (event === "memory") reply.memory(payload);
  else if (event === "notice") reply.notice(payload);
  else if (event === "speaker") reply.speaker(payload);
  else if (event === "transcript") request.userMessage?.transcripts(payload.transcripts || []);
  else if (event === "working") reply.working(payload);
  else if (event === "chat_replaced") adoptReplacedChat(request, payload);
  else if (event === "approval") reply.approval({ agent_id: request.agentId, chat_id: request.chatId, ...payload });
  else if (event === "approval_resolved") settleApproval(payload);
  else if (event === "approval_explanation") explainApproval(payload);
  else if (event === "task_started") reply.taskStarted?.(payload);
  else if (event === "interrupted") reply.interrupted(payload);
  else if (event === "unavailable") request.unavailable = true;  // a task's live view: another process runs it
  else if (event === "error") {
    request.errored = true;
    reply.error(payload);
  }
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
const COMPACT_COMMAND = /^\/compact(?:@\w+)?(?:\s[\s\S]*)?$/i;
const USAGE_COMMAND = /^\/usage(?:@\w+)?\s*$/i;
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
  refreshNumberFields();
}

function fillDreamSettings(settings) {
  const dream = settings.dream;
  const mode = dream.every_hours != null ? "every" : dream.daily_at != null ? "daily" : "manual";
  $(`dream-mode-${mode}`).checked = true;
  setNumberValue($("dream-every-hours"), dream.every_hours);
  $("dream-daily-at").value = dream.daily_at ?? "";
  $("dream-enabled").checked = dream.enabled;
  $("dream-model").value = dream.model ?? "";
  $("dream-effort").value = dream.reasoning_effort ?? "";
  setNumberValue($("dream-max-tokens"), dream.max_tokens);
  setNumberValue($("dream-max-minutes"), dream.max_minutes);
  $("dream-tool-web").checked = dream.tools.includes("web");
  $("dream-tool-folders").checked = dream.tools.includes("folders");
  $("dream-prompt").value = dream.prompt;
  $("dream-prompt").placeholder = settings.default_prompt;
  $("dream-notify").checked = dream.notify_telegram;
  $("dream-delegate").checked = Boolean(dream.delegate);
  $("dream-delegate-model").value = dream.delegate_model ?? "";
  $("dream-model-browse").disabled = !state.user;
  $("dream-delegate-model-browse").disabled = !state.user;
  syncDreamScheduleInputs();
  refreshRows($("dream-form"));
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
      every_hours: mode === "every" ? numberValue("dream-every-hours") : null,
      daily_at: mode === "daily" ? $("dream-daily-at").value : null,
      model: $("dream-model").value.trim() || null,
      reasoning_effort: $("dream-effort").value || null,
      max_tokens: numberValue("dream-max-tokens"),
      max_minutes: numberValue("dream-max-minutes"),
      tools: ["web", "folders"].filter((tool) => $(`dream-tool-${tool}`).checked),
      prompt: $("dream-prompt").value.trim() ? $("dream-prompt").value : "",
      notify_telegram: $("dream-notify").checked,
      delegate: $("dream-delegate").checked,
      delegate_model: $("dream-delegate-model").value.trim() || null,
    },
  };
}

async function saveDreamSettings(event) {
  event.preventDefault();
  const invalid = firstInvalidNumber($("dream-form"));
  if (invalid) { revealField(invalid); return; }
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

// a dream summary: three lines, then "Show more" opens the whole text in place
function expandableSummary(text) {
  const box = element("span", { className: "dream-last-summary clamped", textContent: text });
  const wrap = element("span", { className: "dream-last-summary-wrap" }, box);
  requestAnimationFrame(() => {
    if (box.scrollHeight <= box.clientHeight + 1) { box.classList.remove("clamped"); return; }
    const more = element("button", { type: "button", className: "link dream-summary-toggle", textContent: tr("Show more") });
    more.addEventListener("click", () => {
      const open = box.classList.toggle("clamped") === false;
      more.textContent = open ? tr("Show less") : tr("Show more");
    });
    wrap.append(more);
  });
  return wrap;
}

function renderDreamStatus(status) {
  const last = status.last_session;
  let lastValue = tr("No dream sessions yet.");
  if (last) {
    const open = element("button", { type: "button", className: "link", textContent: `${sessionDate(last)} · ${dreamStatusLabel(last.status)}` });
    open.addEventListener("click", () => openDreamSession(last.id));
    lastValue = element("span", {}, open);
    const summary = (last.summary ?? "").split(/\s+/).join(" ").trim();
    if (summary) lastValue.append(expandableSummary(summary));
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

// the dream trace, grouped by step (one model call): reasoning and replies stay readable, tools are one-line rows
// that open to show their arguments and result; RECALL's notices stand out between the steps
function renderTrace(events) {
  const trace = element("div", { className: "dream-trace" });
  if (!events?.length) {
    trace.append(element("p", { className: "model-note", textContent: tr("The trace is empty.") }));
    return trace;
  }
  const toolsOnly = element("input", { type: "checkbox", checked: localStorage.getItem("dreamTraceTools") === "false" });
  const toggle = element("label", { className: "model-filter trace-toggle" }, toolsOnly, ` ${tr("Reasoning only (hide tools)")}`);
  const apply = () => trace.classList.toggle("hide-tools", toolsOnly.checked);
  toolsOnly.addEventListener("change", () => {
    localStorage.setItem("dreamTraceTools", toolsOnly.checked ? "false" : "true");
    apply();
  });
  trace.append(toggle);
  const results = new Map(events.filter((event) => event.type === "tool_result").map((event) => [event.id, event]));
  let card = null;
  let cardStep = null;
  for (const event of events) {
    if (event.type === "tool_result") continue;  // shown inside its call's row
    const inStep = event.step != null && ["usage", "reasoning", "assistant", "tool_call"].includes(event.type);
    if (inStep && event.step !== cardStep) {
      cardStep = event.step;
      card = element("section", { className: "trace-card" },
        element("header", { className: "trace-card-head" }, element("span", { textContent: tr("Step {step}", { step: event.step }) })));
      trace.append(card);
    }
    if (inStep) {
      if (event.type === "usage") {
        card.querySelector(".trace-card-head").append(element("span", { className: "trace-card-meta",
          textContent: `${event.ts ? `${formatDate(event.ts).slice(11)} · ` : ""}${tr("in {input} · out {output} tokens", {
            input: formatCount(event.input_tokens), output: formatCount(event.output_tokens) })}` }));
      } else if (event.type === "tool_call") {
        card.append(renderTraceTool(event, results.get(event.id)));
      } else {
        card.append(renderTraceEvent(event));
      }
      continue;
    }
    card = null;
    cardStep = null;
    trace.append(renderTraceEvent(event));
  }
  apply();
  return trace;
}

// one tool call as a compact row: name, a short hint of the arguments, size and outcome; opens to the details
function renderTraceTool(call, result) {
  const args = call.arguments ?? {};
  const hint = Object.values(args).filter((value) => typeof value === "string" || typeof value === "number")
    .map(String).join(" · ");
  const outcome = !result ? "…" : result.error ? "\u2717" : "\u2713";
  const size = result ? tr("{count} characters", { count: formatCount((result.result ?? "").length) }) : "";
  return element("details", { className: `trace-tool${result?.error ? " failed" : ""}` },
    element("summary", {},
      ...(mcpToolParts(call.name) ? [element("span", { className: "action-mcp", textContent: mcpToolParts(call.name).server })] : []),
      element("span", { className: "trace-tool-name", textContent: mcpToolParts(call.name)?.tool ?? call.name }),
      element("span", { className: "trace-tool-hint", textContent: hint.length > 90 ? `${hint.slice(0, 90)}…` : hint }),
      element("span", { className: "trace-tool-meta", textContent: [size, outcome].filter(Boolean).join(" ") })),
    element("div", { className: "trace-tool-label", textContent: tr("Arguments") }),
    ...expandablePre(JSON.stringify(args, null, 2)),
    ...(result ? [element("div", { className: "trace-tool-label", textContent: result.error ? tr("Failed") : tr("Result") }),
      ...expandablePre(result.result ?? "")] : []));
}

function renderTraceEvent(event) {
  const time = event.ts ? formatDate(event.ts).slice(11) : "";
  const note = (text) => element("div", { className: "trace-note", textContent: time ? `${time} · ${text}` : text });
  switch (event.type) {
    case "session_started":
      return note(tr("Session started ({trigger}) with {model}; {pending} pending chats; limits {tokens} tokens, {minutes} min.", {
        trigger: dreamTriggerLabel(event.trigger), model: event.model ?? "—", pending: formatCount(event.pending),
        tokens: event.max_tokens ? formatCount(event.max_tokens) : "∞", minutes: formatCount(event.max_minutes),
      }));
    case "usage":
      return note(tr("Tokens: input {input}, output {output}", {
        input: formatCount(event.input_tokens), output: formatCount(event.output_tokens),
      }));
    case "reasoning":
      return element("div", { className: "trace-reasoning" },
        element("div", { className: "trace-tool-label", textContent: tr("Reasoning") }),
        element("div", { className: "trace-reasoning-text", textContent: event.text ?? "" }));
    case "assistant":
      return element("div", { className: "trace-assistant" }, markdown(event.text));
    case "compaction_warning":
      return element("div", { className: "trace-stop" },
        element("div", { textContent: `${time ? `${time} · ` : ""}${tr("Context {percent}% full: the agent was told to save before compaction", {
          percent: formatCount(Math.round((event.context_tokens ?? 0) * 100 / (event.window || 1))) })}` }));
    case "compacted":
      return element("div", { className: "trace-stop" + (event.error ? " failed" : "") },
        element("div", { textContent: `${time ? `${time} · ` : ""}${tr("Context compacted: {before} → {after} tokens", {
          before: formatCount(event.tokens_before), after: formatCount(event.tokens_after) })}${event.error ? ` (${event.error})` : ""}` }));
    case "delegate_started":
      return note(tr("Workers started ({model}) for: {chats}", { model: event.model ?? "—", chats: (event.chats ?? []).join(", ") }));
    case "delegate_finished":
      return note(tr("Worker for {chat}: {state}, {steps} model calls, {tokens} tokens", {
        chat: event.chat_id, steps: formatCount(event.worker_steps), tokens: formatCount(event.tokens),
        state: event.finished ? tr("read to the end") : (event.error || tr("stopped early")),
      }));
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
    case "continue_nudge":
      return element("div", { className: "trace-stop" },
        element("div", { textContent: `${time ? `${time} · ` : ""}${tr("The agent replied without tool calls: told to go on or call dream_finish")}` }),
        ...expandablePre(event.text ?? ""));
    case "finish_requested":
      return note(tr("The agent ended the session with dream_finish"));
    case "idle_closed":
      return element("div", { className: "trace-stop", textContent: `${time ? `${time} · ` : ""}${tr("Closed: the agent replied {count} times in a row without tool calls and never called dream_finish.", { count: formatCount(event.replies) })}` });
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
$("dream-delegate-model-browse").addEventListener("click", () => openModelPicker("dream-worker-llm"));
$("agent-model-browse").addEventListener("click", () => openModelPicker("agent-llm"));
$("agent-delegation-model-browse").addEventListener("click", () => openModelPicker("agent-worker-llm"));
$("dream-form").addEventListener("submit", saveDreamSettings);
$("dream-start").addEventListener("click", startDream);
$("dream-cancel").addEventListener("click", cancelDream);
$("dream-session-back").addEventListener("click", showDreamList);

resetDream();

// ------------------------------------------------------------------ setting rows: defaults and reset

setupNumberFields();

// user settings (defaults come from GET /api/preferences > defaults)
checkboxRow("settings-thinking", () => prefDefault("show_thinking"));
checkboxRow("settings-agent-avatar", () => prefDefault("show_agent_avatar"));
registerRow("settings-theme", {
  getDefault: () => prefDefault("theme"), describe: (value) => optionText("settings-theme", value),
  read: () => $("settings-theme").value, write: (value) => { $("settings-theme").value = value; applyTheme(value); },
});
registerRow("settings-language", {
  getDefault: () => prefDefault("language"), describe: (value) => optionText("settings-language", value),
  read: () => $("settings-language").value || undefined, write: (value) => { $("settings-language").value = value; },
});
registerRow("settings-stt-language", {
  getDefault: () => prefDefault("stt_language") ?? null, describe: (value) => (value ? optionText("settings-stt-language", value) : tr("Auto-detect")),
  read: () => $("settings-stt-language").value || null, write: (value) => { fillSttLanguage(value); },
});
for (const [id, key] of [["settings-llm", "default_llm_model"], ["settings-stt", "default_stt_model"]]) {
  registerRow(id, {
    getDefault: () => prefDefault(key), describe: (value) => String(value),
    read: () => state.settingsModelDraft?.[key],
    write: (value) => { state.settingsModelDraft[key] = value; renderSettingsModels(); },
  });
}
numberRow("settings-assoc-max", () => state.preferences?.defaults?.associative_max_matches, () => "");
numberRow("settings-disposable-days", () => state.preferences?.defaults?.disposable_retention_days, () => "");
checkboxRow("settings-orchestrator", () => prefDefault("orchestrator"));
registerRow("settings-core-agent", {
  getDefault: () => prefDefault("core_agent_id"), describe: () => tr("Same as default agent"),
  read: () => $("settings-core-agent").value || null, write: (value) => { $("settings-core-agent").value = value ?? ""; },
});

registerRow("settings-default-agent", {
  getDefault: () => prefDefault("default_agent_id"), describe: () => tr("First agent in the list"),
  read: () => $("settings-default-agent").value || null, write: (value) => { $("settings-default-agent").value = value ?? ""; },
});

// agent form (defaults come from GET /api/agents/defaults, generated from the configuration models)
function agentDefaultSpec(path) {
  const [section, name] = path.split(".");
  return state.agentDefaults?.[section]?.[name];
}
const agentDefault = (path) => agentDefaultSpec(path)?.default;
const chatDefault = (name) => () => agentDefault(`chat_config.${name}`);
const dreamDefault = (name) => () => agentDefault(`dream.${name}`);

registerRow("agent-model", {
  getDefault: chatDefault("model"), describe: () => tr("your default model"),
  read: () => $("agent-model").value.trim() || null, write: (value) => { $("agent-model").value = value ?? ""; },
});
registerRow("agent-delegation-model", {
  getDefault: () => agentDefault("delegation.default_model"), describe: () => tr("the model of the chat"),
  read: () => $("agent-delegation-model").value.trim() || null, write: (value) => { $("agent-delegation-model").value = value ?? ""; },
});
selectRow("agent-effort", chatDefault("reasoning_effort"));
checkboxRow("agent-reasoning", chatDefault("reasoning"));
checkboxRow("agent-keep-reasoning", chatDefault("keep_reasoning_in_memory"));
numberSpecs.set("agent-reasoning-tokens", () => agentDefaultSpec("chat_config.reasoning_tokens"));
registerRow("agent-reasoning-tokens", {
  getDefault: chatDefault("reasoning_tokens"), describe: () => tr("No limit"),
  range: () => agentDefaultSpec("chat_config.reasoning_tokens") ?? {},
  read: () => ($("agent-tokens-enabled").checked ? numberValue("agent-reasoning-tokens") : null),
  write: (value) => {
    $("agent-tokens-enabled").checked = value != null;
    $("agent-reasoning-tokens").disabled = value == null;
    setNumberValue($("agent-reasoning-tokens"), value);
  },
});
checkboxRow("agent-stream", chatDefault("stream"));
checkboxRow("agent-memory", chatDefault("has_memory"));
checkboxRow("agent-recall-knowledge", chatDefault("recall_knowledge"));
selectRow("agent-associative", chatDefault("associative_memory"), () => {
  $("agent-associative-execution").disabled = $("agent-associative").value === "off";
});
selectRow("agent-associative-execution", chatDefault("associative_execution"));
numberRow("agent-max-hops", () => agentDefaultSpec("chat_config.max_tool_hops"), () => tr("Application value"));
checkboxRow("agent-tools", chatDefault("tools"));
const compactionDefault = (name) => () => agentDefault(`compaction.${name}`);
checkboxRow("agent-compaction-enabled", compactionDefault("enabled"), syncCompactionUnits);
selectRow("agent-compaction-mode", compactionDefault("mode"), syncCompactionUnits);
numberRow("agent-compaction-trigger", () => agentDefaultSpec("compaction.trigger"), () => "");
numberRow("agent-compaction-keep", () => agentDefaultSpec("compaction.keep_recent"), () => "");
registerRow("agent-compaction-model", {
  getDefault: compactionDefault("model"), describe: () => tr("the model of the chat"),
  read: () => $("agent-compaction-model").value.trim() || null, write: (value) => { $("agent-compaction-model").value = value ?? ""; },
});
numberRow("agent-terminal-timeout", () => agentDefaultSpec("terminal.timeout_seconds"), () => "");
// delegation block: limits can only lower the application's, so an empty value is the default
checkboxRow("agent-delegation-enabled", () => agentDefault("delegation.enabled"));
numberRow("agent-delegation-parallel", () => agentDefaultSpec("delegation.max_parallel"), () => tr("the application's limit"));
numberRow("agent-delegation-timeout", () => agentDefaultSpec("delegation.timeout_seconds"), () => tr("the application's limit"));
numberRow("agent-delegation-tokens", () => agentDefaultSpec("delegation.max_tokens"), () => tr("the application's limit"));
registerRow("agent-delegation-groups", {
  getDefault: () => DELEGATION_DEFAULT_GROUPS.join(","), describe: (value) => value || tr("none"),
  read: () => readDelegationGroups().join(","), write: (value) => renderDelegationGroups(value ? value.split(",") : []),
});
registerRow("agent-delegation-targets", {
  getDefault: () => "", describe: () => tr("none"),
  read: () => readDelegationTargetFields().filter((row) => row.allowed).map((row) => `${row.id}:${row.remember}`).join(","),
  write: () => renderDelegationTargets(delegationTargetRows(state.agentConfigs, state.agentForm.originalId, null)),
});

registerRow("dream-model", {
  getDefault: dreamDefault("model"), describe: () => tr("the agent's model, else yours"),
  read: () => $("dream-model").value.trim() || null, write: (value) => { $("dream-model").value = value ?? ""; },
});
registerRow("dream-delegate-model", {
  getDefault: dreamDefault("delegate_model"), describe: () => tr("the dream's model"),
  read: () => $("dream-delegate-model").value.trim() || null, write: (value) => { $("dream-delegate-model").value = value ?? ""; },
});
selectRow("dream-effort", dreamDefault("reasoning_effort"));
numberRow("dream-max-tokens", () => agentDefaultSpec("dream.max_tokens"), () => "");
numberRow("dream-max-minutes", () => agentDefaultSpec("dream.max_minutes"), () => "");
numberSpecs.set("dream-every-hours", () => agentDefaultSpec("dream.every_hours"));
checkboxRow("dream-notify", dreamDefault("notify_telegram"));
checkboxRow("dream-delegate", dreamDefault("delegate"));
registerRow("dream-schedule-row", {
  // manual-only is the default whenever the model has neither an interval nor a time of day
  getDefault: () => (state.agentDefaults && agentDefault("dream.every_hours") == null && agentDefault("dream.daily_at") == null
    ? "manual" : undefined),
  describe: () => tr("Manual only"), read: () => dreamScheduleMode(),
  write: () => {
    $("dream-mode-manual").checked = true;
    setNumberValue($("dream-every-hours"), null);
    $("dream-daily-at").value = "";
    $("dream-enabled").checked = false;
    syncDreamScheduleInputs();
  },
});

// ------------------------------------------------------------------ boot

api("/api/version").then((info) => { state.versionInfo = info; renderVersion(); }, () => {});
api("/api/me").then(showApp, showLogin);


// ---- resizable side panel: drag the edge (or use the arrow keys); the width is remembered in this browser
(() => {
  const handle = document.getElementById("sidebar-resizer");
  if (!handle) return;
  const MIN = 220, MAX = 560, KEY = "sidebarWidth";
  const root = document.documentElement;
  const apply = (px) => root.style.setProperty("--sidebar-w", `${Math.min(MAX, Math.max(MIN, Math.round(px)))}px`);
  const width = () => parseInt(getComputedStyle(root).getPropertyValue("--sidebar-w"), 10) || 300;
  const save = () => { try { localStorage.setItem(KEY, String(width())); } catch { /* storage may be blocked */ } };
  try { const saved = Number(localStorage.getItem(KEY)); if (saved) apply(saved); } catch { /* ignore */ }
  handle.addEventListener("pointerdown", (event) => {
    event.preventDefault();
    handle.setPointerCapture(event.pointerId);
    handle.classList.add("dragging");
    root.classList.add("resizing-sidebar");
  });
  handle.addEventListener("pointermove", (event) => {
    if (handle.hasPointerCapture(event.pointerId)) apply(event.clientX);
  });
  const stop = (event) => {
    if (!handle.hasPointerCapture(event.pointerId)) return;
    handle.releasePointerCapture(event.pointerId);
    handle.classList.remove("dragging");
    root.classList.remove("resizing-sidebar");
    save();
  };
  handle.addEventListener("pointerup", stop);
  handle.addEventListener("pointercancel", stop);
  handle.addEventListener("dblclick", () => { root.style.removeProperty("--sidebar-w"); try { localStorage.removeItem(KEY); } catch { /* ignore */ } });
  handle.addEventListener("keydown", (event) => {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    event.preventDefault();
    apply(width() + (event.key === "ArrowRight" ? 16 : -16));
    save();
  });
})();

// ---- composer: once the message needs more than one line it spans the box and the chat options move under it
(() => {
  const input = document.getElementById("input"), composer = document.getElementById("composer");
  if (!input || !composer || typeof ResizeObserver === "undefined") return;
  new ResizeObserver(() => {
    if (!input.value) composer.classList.remove("multiline");
    else if (input.clientHeight > 44) composer.classList.add("multiline");
  }).observe(input);
  input.addEventListener("input", () => { if (!input.value) composer.classList.remove("multiline"); });
})();


// ------------------------------------------------------------------ Claude plan usage

const USAGE_LABELS = { session: "Session (5 h)", week: "Week", week_opus: "Week (Opus)", week_sonnet: "Week (Sonnet)" };
const USAGE_REFRESH_MS = 120000;

// green below 50 %, then yellow, orange and red from 80 % and 95 %
function usageLevel(percent) {
  return percent >= 95 ? "crit" : percent >= 80 ? "high" : percent >= 50 ? "warn" : "ok";
}

// "2h 15m", "3d 4h" or "now" until an ISO time
function usageReset(iso, now = Date.now()) {
  const ms = Date.parse(iso) - now;
  if (!Number.isFinite(ms)) return "";
  if (ms <= 0) return tr("now");
  const minutes = Math.round(ms / 60000);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ${minutes % 60}m`;
  return `${Math.floor(hours / 24)}d ${hours % 24}h`;
}

function renderClaudeUsage(report) {
  const box = $("claude-usage");
  const windows = report?.available ? report.windows : [];
  box.hidden = !windows.length;
  if (!windows.length) return setClaudeUsageOpen(false);
  const body = $("claude-usage-body");
  body.replaceChildren();
  for (const dot of box.querySelectorAll(".usage-dot")) dot.dataset.level = "";
  for (const item of windows) {
    const percent = Math.round(item.percent);
    const level = usageLevel(item.percent);
    const dot = box.querySelector(`.usage-dot[data-window="${item.key}"]`);
    if (dot) {
      dot.dataset.level = level;
      dot.title = `${tr(USAGE_LABELS[item.key] ?? item.key)}: ${percent}%`;
    }
    const fill = element("span", {});
    fill.style.width = `${percent}%`;
    fill.dataset.level = level;
    const reset = item.resets_at ? tr("resets in {time}", { time: usageReset(item.resets_at) }) : "";
    body.append(element("div", { className: "usage-row" },
      element("div", { className: "usage-head" }, element("span", { textContent: tr(USAGE_LABELS[item.key] ?? item.key) }), element("span", { className: "usage-reset", textContent: reset }), element("strong", { textContent: `${percent}%` })),
      element("div", { className: "usage-bar" }, fill)));
  }
}

let usageTimer = null;
async function refreshClaudeUsage() {
  if (!state.user) return renderClaudeUsage(null);
  try {
    renderClaudeUsage(await api("/api/claude/usage"));
  } catch {
    renderClaudeUsage(null);
  }
}
// the dots open a floating panel above them; a click outside, Escape or the dots again close it
function setClaudeUsageOpen(open) {
  $("claude-usage-body").hidden = !open;
  $("claude-usage-toggle").setAttribute("aria-expanded", String(open));
}
$("claude-usage-toggle").addEventListener("click", () => setClaudeUsageOpen($("claude-usage-body").hidden));
document.addEventListener("pointerdown", (event) => {
  if (!$("claude-usage-body").hidden && !$("claude-usage").contains(event.target)) setClaudeUsageOpen(false);
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !$("claude-usage-body").hidden) { setClaudeUsageOpen(false); $("claude-usage-toggle").focus(); }
});
try { localStorage.removeItem("claude-usage-open"); } catch { /* storage may be blocked */ }
usageTimer = setInterval(() => { if (!document.hidden) refreshClaudeUsage(); }, USAGE_REFRESH_MS);

// the chat list shows which chats are working, waiting for an approval or have an unread answer: keep it current
setInterval(() => {
  if (document.hidden || !state.user || document.querySelector("#chat-list .rename-input")) return;
  refreshChats().catch(() => {});
}, 4000);
