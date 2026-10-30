const $ = (id) => document.getElementById(id);
const state = {
  user: null, agentId: null, chatId: null, busy: false, session: 0,
  requests: new Set(), latest: null, sendQueue: Promise.resolve(),
  attachments: [],
  model: null, modelCatalog: null, modelCatalogZdr: null, modelFavorites: [],
  modelBusy: false, modelLoading: false, modelRequest: 0,
  modelFavoritesOnly: false,
};
// thinking blocks start open (streamed live); closing one keeps them closed from then on
const prefs = { thinkingOpen: localStorage.getItem("thinkingOpen") !== "false" };
const tokenPriceFormat = new Intl.NumberFormat("en-US", {
  style: "currency", currency: "USD", maximumSignificantDigits: 6,
});

function tokenPrice(value) {
  return value == null ? "unavailable" : `from ${tokenPriceFormat.format(value)}`;
}

async function api(path, options = {}) {
  const session = state.session;
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (res.status === 401) {
    if (session === state.session) showLogin();
    throw new Error("not logged in");
  }
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(formatApiError(body.detail) || `HTTP ${res.status}`);
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
  $("app").hidden = true;
  $("login").hidden = false;
}

async function showApp(user) {
  resetStreams();
  state.user = user;
  const session = state.session;
  $("login").hidden = true;
  $("app").hidden = false;
  $("user-name").textContent = user.name;
  const select = $("agent-select");
  select.replaceChildren(
    ...user.agents.map((a) => {
      const option = document.createElement("option");
      option.value = a.id;
      option.textContent = a.name;
      return option;
    })
  );
  try {
    await selectAgent(user.agents[0]?.id ?? null);
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

async function selectAgent(agentId) {
  if (state.busy || state.modelBusy) return;
  state.agentId = agentId;
  state.chatId = null;
  state.model = null;
  updateModelControls();
  $("agent-select").value = agentId ?? "";
  renderMessages([]);
  setComposer(false);
  await refreshChats();
}

$("agent-select").addEventListener("change", (e) => selectAgent(e.target.value));

async function refreshChats() {
  if (!state.agentId) return;
  const session = state.session;
  const agentId = state.agentId;
  const keep = state.chatId ? `?keep=${encodeURIComponent(state.chatId)}` : "";
  const chats = await api(chatsUrl(keep));
  if (session !== state.session || agentId !== state.agentId) return;
  const list = $("chat-list");
  list.replaceChildren(
    ...chats.map((chat) => {
      const li = document.createElement("li");
      li.classList.toggle("active", chat.id === state.chatId);
      const title = document.createElement("span");
      title.className = "title" + (chat.title ? "" : " empty");
      title.textContent = chat.title || "New chat";
      const del = document.createElement("button");
      del.className = "del";
      del.textContent = "✕";
      del.title = "Delete chat";
      del.disabled = state.busy;
      del.addEventListener("click", (e) => {
        e.stopPropagation();
        deleteChat(chat);
      });
      li.append(title, del);
      li.addEventListener("click", () => openChat(chat.id));
      return li;
    })
  );
}

async function newChat() {
  if (state.busy || state.modelBusy || !state.agentId) return;
  const session = state.session;
  const agentId = state.agentId;
  const chat = await api(chatsUrl(), { method: "POST" });
  if (session !== state.session || agentId !== state.agentId) return;
  await openChat(chat.id);
}

$("new-chat").addEventListener("click", newChat);

async function openChat(chatId) {
  if (state.busy || state.modelBusy) return;
  const session = state.session;
  const agentId = state.agentId;
  const data = await api(chatsUrl(`/${chatId}`));
  if (session !== state.session || agentId !== state.agentId || state.busy || state.modelBusy) return;
  state.chatId = chatId;
  state.model = data.model;
  updateModelControls();
  renderMessages(data.messages);
  setComposer(true);
  await refreshChats();
  $("input").focus();
}

async function deleteChat(chat) {
  if (state.busy || state.modelBusy || !confirm(`Delete "${chat.title || "New chat"}"?`)) return;
  await api(chatsUrl(`/${chat.id}`), { method: "DELETE" });
  if (state.chatId === chat.id) {
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
  $("model-policy").classList.toggle("privacy-warning", openrouter && !required);
  $("model-refresh").disabled = state.modelLoading || state.modelBusy;
  $("model-close").disabled = state.modelBusy;
  $("model-zdr").disabled = state.modelBusy || state.modelLoading;
  $("model-zdr-warning").hidden = $("model-zdr").checked;
  $("model-favorites-only").setAttribute("aria-pressed", String(state.modelFavoritesOnly));
  $("model-favorites-only").textContent = state.modelFavoritesOnly ? "\u2605" : "\u2606";
  $("model-favorites-only").title = state.modelFavoritesOnly ? "Show all models" : "Show favorites only";
  const activeFilters = ["model-tools", "model-images", "model-reasoning"]
    .filter((id) => $(id).checked).length;
  $("model-filters-label").textContent = activeFilters ? `Filters (${activeFilters})` : "Filters";
  $("model-filters").dataset.active = String(activeFilters > 0);
}

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
    $("model-status").textContent = "Loading current model catalog...";
    return;
  }
  if (!state.modelCatalog || state.modelCatalogZdr !== $("model-zdr").checked) {
    $("model-status").textContent = "Catalog unavailable. Use Refresh to retry.";
    return;
  }
  const saved = new Set(state.modelFavorites);
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
    && matchesCapabilities(model, capabilities)
    && (!query || model.id.toLocaleLowerCase().includes(query) || model.name.toLocaleLowerCase().includes(query))
  ).sort((a, b) => compareModels(a, b, $("model-sort").value, saved));
  $("model-status").textContent = matches.length
    ? `${matches.length} model${matches.length === 1 ? "" : "s"} - ${$("model-zdr").checked ? "ZDR only" : "all routes allowed"}`
    : "No models match. Try another search or relax the filters.";
  for (const model of matches) {
    const available = availableIds.has(model.id);
    const row = document.createElement("li");
    row.className = "model-row";
    const choose = document.createElement("button");
    choose.type = "button";
    choose.className = "model-choice";
    choose.disabled = !available || state.busy || state.modelBusy;
    choose.setAttribute("aria-pressed", String(
      model.id === state.model?.id && (state.model?.zdr !== false) === $("model-zdr").checked
    ));
    const name = document.createElement("strong");
    name.textContent = model.name;
    const id = document.createElement("small");
    id.textContent = model.id;
    const providers = document.createElement("small");
    providers.textContent = !available ? "Unavailable under the current filter"
      : model.zdr_providers.length ? `ZDR routes: ${model.zdr_providers.join(", ")}`
      : "No active ZDR routes";
    const pricing = document.createElement("small");
    pricing.textContent = `USD / 1M tokens: input ${tokenPrice(model.pricing?.prompt)}`
      + ` | output ${tokenPrice(model.pricing?.completion)}`;
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
    row.append(choose, star);
    list.append(row);
  }
}

async function refreshModels(initial = false) {
  if (state.modelBusy) return;
  const session = state.session;
  const request = ++state.modelRequest;
  const zdr = $("model-zdr").checked;
  state.modelLoading = true;
  $("model-error").textContent = "";
  renderModelList();
  try {
    const [models, favorites] = await Promise.all([
      api(`/api/models?zdr=${zdr}`), api("/api/models/favorites"),
    ]);
    if (session !== state.session || request !== state.modelRequest) return;
    state.modelCatalog = models;
    state.modelCatalogZdr = zdr;
    state.modelFavorites = favorites;
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
  if (state.modelBusy || state.busy || state.modelLoading || !state.chatId) return;
  const session = state.session;
  const agentId = state.agentId;
  const chatId = state.chatId;
  const zdr = $("model-zdr").checked;
  state.modelBusy = true;
  $("model-error").textContent = "";
  updateBusy();
  try {
    const model = await api(chatsUrl(`/${chatId}/model`), {
      method: "PUT", body: JSON.stringify({ model_id: modelId, zdr }),
    });
    if (session !== state.session || agentId !== state.agentId || chatId !== state.chatId) return;
    state.model = model;
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

$("model-picker").addEventListener("click", () => {
  if ($("model-picker").disabled) return;
  $("model-search").value = "";
  $("model-error").textContent = "";
  $("model-zdr").checked = true;
  state.modelFavoritesOnly = state.modelFavorites.length > 0;
  $("model-filters").open = false;
  $("model-info").open = false;
  $("model-dialog").showModal();
  $("model-search").focus();
  if (!state.modelCatalog || state.modelCatalogZdr !== true || state.modelLoading) refreshModels(true);
  else renderModelList();
});
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
  state.busy = state.requests.size > 0;
  $("agent-select").disabled = state.busy || state.modelBusy;
  $("new-chat").disabled = state.busy || state.modelBusy;
  for (const button of $("chat-list").querySelectorAll(".del")) button.disabled = state.busy || state.modelBusy;
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
  state.latest = null;
  state.sendQueue = Promise.resolve();
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

function isCurrent(request) {
  return request.session === state.session && request.userId === state.user?.id
    && request.agentId === state.agentId && request.chatId === state.chatId;
}

function renderMessages(messages) {
  $("messages").replaceChildren();
  $("usage").hidden = true;
  for (const m of messages) {
    const view = addMessage(m.role);
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

function addMessage(role) {
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
  $("messages").append(el);
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
        note.textContent = "Interrupted by a new message";
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
      if (!attachmentModality(file)) throw new Error(`${file.name}: only image, video and audio files are supported`);
      if (file.size > MAX_FILE_BYTES) throw new Error(`${file.name}: maximum size is 10 MB`);
      return file;
    });
    const total = state.attachments.reduce((sum, item) => sum + item.size, 0)
      + valid.reduce((sum, file) => sum + file.size, 0);
    if (total > MAX_TOTAL_FILE_BYTES) throw new Error("Attachments exceed the 25 MB total limit");
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
  const userMessage = addMessage("user");
  userMessage.text(content);
  userMessage.attachments(attachments);
  const reply = addMessage("assistant");
  const request = {
    userId: state.user.id, agentId: state.agentId, chatId: state.chatId,
    session: state.session, controller: new AbortController(), reply,
    generationId: null, done: false,
  };
  state.requests.add(request);
  state.latest = request;
  $("usage").hidden = true;
  updateBusy();
  scrollDown();
  try {
    await streamReply(content, attachments, request);
  } catch (err) {
    request.controller.abort();
    if (isCurrent(request) && err.name !== "AbortError") reply.error(err.message);
  } finally {
    state.requests.delete(request);
    if (isCurrent(request)) {
      updateBusy();
      if (!state.busy) {
        $("input").focus();
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
  const response = state.sendQueue.then(() => {
    if (!isCurrent(request)) throw new DOMException("Session changed", "AbortError");
    return fetch(`/api/agents/${request.agentId}/chats/${request.chatId}/messages`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
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
    if (isCurrent(request)) showLogin();
    throw new Error("not logged in");
  }
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(formatApiError(body.detail) || `HTTP ${res.status}`);
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
    if (isCurrent(request) && !request.done) {
      throw new Error("Response stream ended before completion");
    }
  } finally {
    reader.releaseLock();
  }
}

function handleEvent(raw, request) {
  if (!isCurrent(request)) return;
  const reply = request.reply;
  let event = "message";
  let data = "";
  for (const line of raw.split("\n")) {
    if (line.startsWith("event: ")) event = line.slice(7);
    else if (line.startsWith("data: ")) data += line.slice(6);
    else if (line.startsWith("id: ")) {
      const id = line.slice(4);
      if (request.generationId && request.generationId !== id) {
        throw new Error("Generation changed within a response");
      }
      request.generationId = id;
    }
  }
  const payload = data ? JSON.parse(data) : "";
  if (event === "text") reply.text(payload);
  else if (event === "reasoning") reply.thinking(payload);
  else if (event === "tool") reply.action(payload);
  else if (event === "memory") reply.memoryMatches(payload.matches);
  else if (event === "interrupted") reply.interrupted(payload);
  else if (event === "error") reply.error(payload);
  else if (event === "done") request.done = true;
  else if (event === "usage" && request === state.latest) {
    // the first line is the headline; the full breakdown stays folded until expanded
    showUsage(payload);
  }
}

// ------------------------------------------------------------------ boot

api("/api/me").then(showApp, showLogin);
