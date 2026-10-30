const $ = (id) => document.getElementById(id);
const state = { user: null, agentId: null, chatId: null, busy: false };
// thinking blocks start open (streamed live); closing one keeps them closed from then on
const prefs = { thinkingOpen: localStorage.getItem("thinkingOpen") !== "false" };

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (res.status === 401) {
    showLogin();
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
  state.user = null;
  $("app").hidden = true;
  $("login").hidden = false;
}

function showApp(user) {
  state.user = user;
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
  selectAgent(user.agents[0]?.id ?? null);
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
  const keep = state.chatId ? `?keep=${encodeURIComponent(state.chatId)}` : "";
  const chats = await api(chatsUrl(keep));
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
  const chat = await api(chatsUrl(), { method: "POST" });
  await openChat(chat.id);
}

$("new-chat").addEventListener("click", newChat);

async function openChat(chatId) {
  if (state.busy) return;
  const data = await api(chatsUrl(`/${chatId}`));
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

function renderMessages(messages) {
  $("messages").replaceChildren();
  for (const m of messages) {
    const view = addMessage(m.role);
    if (m.reasoning) view.thinking(m.reasoning);
    for (const action of m.actions ?? []) view.action(action);
    view.text(m.content);
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
  el.append(body);
  $("messages").append(el);
  return {
    text(chunk) {
      raw += chunk;
      if (role === "assistant") {
        body.innerHTML = DOMPurify.sanitize(marked.parse(raw, { breaks: true }));
        for (const a of body.querySelectorAll("a")) {
          a.target = "_blank";
          a.rel = "noopener noreferrer";
        }
      } else {
        body.textContent = raw;
      }
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
  if (!content || state.busy || !state.chatId) return;
  $("input").value = "";
  state.busy = true;
  setComposer(false);

  addMessage("user").text(content);
  const reply = addMessage("assistant");
  scrollDown();
  try {
    await streamReply(content, reply);
  } catch (err) {
    reply.error(err.message);
  } finally {
    state.busy = false;
    setComposer(true);
    $("input").focus();
    refreshChats();
  }
});

$("input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    $("composer").requestSubmit();
  }
});

async function streamReply(content, reply) {
  const res = await fetch(chatsUrl(`/${state.chatId}/messages`), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ content }),
  });
  if (res.status === 401) {
    showLogin();
    throw new Error("not logged in");
  }
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `HTTP ${res.status}`);
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let end;
    while ((end = buffer.indexOf("\n\n")) !== -1) {
      handleEvent(buffer.slice(0, end), reply);
      buffer = buffer.slice(end + 2);
    }
    scrollDown();
  }
}

function handleEvent(raw, reply) {
  let event = "message";
  let data = "";
  for (const line of raw.split("\n")) {
    if (line.startsWith("event: ")) event = line.slice(7);
    else if (line.startsWith("data: ")) data += line.slice(6);
  }
  const payload = data ? JSON.parse(data) : "";
  if (event === "text") reply.text(payload);
  else if (event === "reasoning") reply.thinking(payload);
  else if (event === "tool") reply.action(payload);
  else if (event === "error") reply.error(payload);
  else if (event === "usage") {
    // the first line is the headline; the full breakdown stays folded until expanded
    $("usage").hidden = false;
    $("usage").querySelector("summary").textContent = payload.split("\n")[0];
    $("usage").querySelector("pre").textContent = payload;
  }
}

// ------------------------------------------------------------------ boot

api("/api/me").then(showApp, showLogin);
