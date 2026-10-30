const $ = (id) => document.getElementById(id);
const state = {
  user: null, agentId: null, chatId: null, busy: false, session: 0,
  requests: new Set(), latest: null, sendQueue: Promise.resolve(),
};
// thinking blocks start open (streamed live); closing one keeps them closed from then on
const prefs = { thinkingOpen: localStorage.getItem("thinkingOpen") !== "false" };

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
    throw new Error(body.detail || `HTTP ${res.status}`);
  }
  return res.status === 204 ? null : res.json();
}

const chatsUrl = (suffix = "") => `/api/agents/${state.agentId}/chats${suffix}`;

// ------------------------------------------------------------------ login

function showLogin() {
  resetStreams();
  state.user = null;
  state.agentId = null;
  state.chatId = null;
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
  if (state.busy) return;
  state.agentId = agentId;
  state.chatId = null;
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
  if (state.busy || !state.agentId) return;
  const session = state.session;
  const agentId = state.agentId;
  const chat = await api(chatsUrl(), { method: "POST" });
  if (session !== state.session || agentId !== state.agentId) return;
  await openChat(chat.id);
}

$("new-chat").addEventListener("click", newChat);

async function openChat(chatId) {
  if (state.busy) return;
  const session = state.session;
  const agentId = state.agentId;
  const data = await api(chatsUrl(`/${chatId}`));
  if (session !== state.session || agentId !== state.agentId || state.busy) return;
  state.chatId = chatId;
  renderMessages(data.messages);
  $("usage").hidden = true;
  setComposer(true);
  await refreshChats();
  $("input").focus();
}

async function deleteChat(chat) {
  if (state.busy || !confirm(`Delete "${chat.title || "New chat"}"?`)) return;
  await api(chatsUrl(`/${chat.id}`), { method: "DELETE" });
  if (state.chatId === chat.id) {
    state.chatId = null;
    renderMessages([]);
    setComposer(false);
  }
  await refreshChats();
}

// ------------------------------------------------------------------ messages

function setComposer(enabled) {
  $("input").disabled = !enabled;
  $("send").disabled = !enabled;
}

function updateBusy() {
  state.busy = state.requests.size > 0;
  $("agent-select").disabled = state.busy;
  $("new-chat").disabled = state.busy;
  for (const button of $("chat-list").querySelectorAll(".del")) button.disabled = state.busy;
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
  $("usage").hidden = true;
  updateBusy();
}

function isCurrent(request) {
  return request.session === state.session && request.userId === state.user?.id
    && request.agentId === state.agentId && request.chatId === state.chatId;
}

function renderMessages(messages) {
  $("messages").replaceChildren();
  for (const m of messages) {
    const view = addMessage(m.role);
    if (m.reasoning) view.thinking(m.reasoning);
    for (const action of m.actions ?? []) view.action(action);
    view.text(m.content);
    if (m.interrupted) view.interrupted();
  }
  scrollDown();
}

function addMessage(role) {
  const el = document.createElement("div");
  el.className = `msg ${role}`;
  const body = document.createElement("div");
  body.className = "body";
  let raw = "";
  let thinking = null;
  let actionCount = 0;
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
  return {
    text(chunk) {
      raw += chunk;
      renderBody();
    },
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

$("composer").addEventListener("submit", async (e) => {
  e.preventDefault();
  const content = $("input").value.trim();
  if (!content || !state.user || !state.chatId) return;
  $("input").value = "";
  addMessage("user").text(content);
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
    await streamReply(content, request);
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

async function streamReply(content, request) {
  // Serialize acceptance, not generation, so rapid sends reach the service in order.
  const response = state.sendQueue.then(() => {
    if (!isCurrent(request)) throw new DOMException("Session changed", "AbortError");
    return fetch(`/api/agents/${request.agentId}/chats/${request.chatId}/messages`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content }),
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
    throw new Error(body.detail || `HTTP ${res.status}`);
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
  else if (event === "interrupted") reply.interrupted(payload);
  else if (event === "error") reply.error(payload);
  else if (event === "done") request.done = true;
  else if (event === "usage" && request === state.latest) {
    // the first line is the headline; the full breakdown stays folded until expanded
    $("usage").hidden = false;
    $("usage").querySelector("summary").textContent = payload.split("\n")[0];
    $("usage").querySelector("pre").textContent = payload;
  }
}

// ------------------------------------------------------------------ boot

api("/api/me").then(showApp, showLogin);
