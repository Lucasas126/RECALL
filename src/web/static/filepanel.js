// The file panel: a right-hand panel with a read-only file explorer and document tabs (markdown, text and code, images,
// CSV, HTML, PDF). Loaded before app.js; it reads a few app.js globals (tr, state, confirmDialog, renderMarkdown,
// codeToHtml) only when something happens, never at load time. The pure helpers (CSV parser, paths, tab store, API client)
// take what they touch as arguments so the Node tests run them without a browser.
"use strict";

(function (root) {
  // ------------------------------------------------------------------ pure helpers
  const CSV_MAX_ROWS = 500;
  const TEXT_MAX_CHARS = 1000000;
  const HIGHLIGHT_MAX_CHARS = 200000;

  // Parses CSV text (quotes, doubled quotes, CRLF, embedded newlines). The delimiter is guessed from the first line.
  function parseCsv(text, maxRows = CSV_MAX_ROWS) {
    const source = String(text ?? "").replace(/^\uFEFF/, "");
    const firstLine = source.split(/\r?\n/, 1)[0] || "";
    let delimiter = ",";
    let best = -1;
    for (const candidate of [",", ";", "\t"]) {
      const count = firstLine.split(candidate).length - 1;
      if (count > best) { best = count; delimiter = candidate; }
    }
    const rows = [];
    let row = [];
    let field = "";
    let quoted = false;
    let truncated = false;
    let i = 0;
    const endRow = () => { row.push(field); field = ""; rows.push(row); row = []; };
    while (i < source.length) {
      if (rows.length >= maxRows) { truncated = true; break; }
      const ch = source[i];
      if (quoted) {
        if (ch === '"') {
          if (source[i + 1] === '"') { field += '"'; i += 2; continue; }
          quoted = false;
        } else field += ch;
        i++;
        continue;
      }
      if (ch === '"' && field === "") quoted = true;
      else if (ch === delimiter) { row.push(field); field = ""; }
      else if (ch === "\r") { if (source[i + 1] === "\n") i++; endRow(); }
      else if (ch === "\n") endRow();
      else field += ch;
      i++;
    }
    if (!truncated && (field !== "" || row.length)) endRow();
    return { rows, truncated, delimiter };
  }

  // Paths are relative to a root, with "/" separators and no leading or trailing slash; "" is the root itself.
  function normalizePath(path) {
    return String(path ?? "").split("/").filter((part) => part && part !== ".").join("/");
  }
  function joinPath(base, name) {
    return normalizePath(`${normalizePath(base)}/${name}`);
  }
  function parentPath(path) {
    const parts = normalizePath(path).split("/").filter(Boolean);
    parts.pop();
    return parts.join("/");
  }
  // [{ label, path }] from the root down to `path`; the first crumb is the root itself.
  function breadcrumbs(rootLabel, path) {
    const crumbs = [{ label: rootLabel, path: "" }];
    let current = "";
    for (const part of normalizePath(path).split("/").filter(Boolean)) {
      current = joinPath(current, part);
      crumbs.push({ label: part, path: current });
    }
    return crumbs;
  }
  function baseName(path) {
    const parts = normalizePath(path).split("/").filter(Boolean);
    return parts.length ? parts[parts.length - 1] : "";
  }
  function formatSize(bytes) {
    const n = Number(bytes);
    if (!Number.isFinite(n) || n < 0) return "";
    if (n < 1024) return `${n} B`;
    const units = ["KB", "MB", "GB", "TB"];
    let value = n / 1024;
    let unit = 0;
    while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit++; }
    return `${value >= 10 ? Math.round(value) : value.toFixed(1)} ${units[unit]}`;
  }
  function formatDate(mtime) {
    const n = Number(mtime);
    if (!Number.isFinite(n) || n <= 0) return "";
    const date = new Date(n < 1e11 ? n * 1000 : n);  // seconds or milliseconds
    return Number.isNaN(date.getTime()) ? "" : date.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
  }
  // Folders first, then names in natural order.
  function sortEntries(entries) {
    return [...(entries || [])].sort((a, b) => {
      if ((a.type === "dir") !== (b.type === "dir")) return a.type === "dir" ? -1 : 1;
      return String(a.name).localeCompare(String(b.name), undefined, { numeric: true, sensitivity: "base" });
    });
  }

  const EXT_KIND = {
    md: "markdown", markdown: "markdown", txt: "text", log: "text", pdf: "pdf", csv: "csv", tsv: "csv",
    html: "html", htm: "html", png: "image", jpg: "image", jpeg: "image", gif: "image", webp: "image", svg: "image",
    bmp: "image", avif: "image", zip: "binary", gz: "binary", tar: "binary", exe: "binary", bin: "binary",
    docx: "binary", xlsx: "binary", pptx: "binary", mp3: "binary", mp4: "binary", wav: "binary", mov: "binary",
    sqlite: "binary", db: "binary", so: "binary", dll: "binary", iso: "binary", "7z": "binary",
  };
  const CODE_EXT = ["py", "js", "mjs", "cjs", "ts", "tsx", "jsx", "json", "yaml", "yml", "toml", "ini", "sh", "bash", "css",
    "xml", "sql", "go", "rs", "java", "c", "h", "cpp", "hpp", "rb", "php", "diff", "patch", "lua", "kt", "swift", "cs", "conf"];
  function extension(name) {
    const match = /\.([A-Za-z0-9+_-]+)$/.exec(String(name ?? ""));
    return match ? match[1].toLowerCase() : "";
  }
  // A cheap guess from the name, used by the explorer to offer Download for known binaries; the viewer asks the server.
  function guessKind(name) {
    const ext = extension(name);
    if (EXT_KIND[ext]) return EXT_KIND[ext];
    return CODE_EXT.includes(ext) ? "code" : "text";
  }
  const HLJS_ALIAS = { mjs: "javascript", cjs: "javascript", js: "javascript", jsx: "javascript", ts: "typescript", tsx: "typescript",
    py: "python", sh: "bash", yml: "yaml", rs: "rust", rb: "ruby", h: "c", hpp: "cpp", kt: "kotlin", cs: "csharp", conf: "ini",
    patch: "diff", html: "xml", json: "json", jsonl: "json", ndjson: "json", toml: "ini", cfg: "ini", env: "ini", log: "plaintext", txt: "plaintext", htm: "xml", svg: "xml", zsh: "bash", bash: "bash" };
  function codeLanguageFor(name) {
    const ext = extension(name);
    return HLJS_ALIAS[ext] || ext;
  }

  function tabKey(card) {
    return [card.agent || "", card.root || "", normalizePath(card.path)].join("|");
  }

  // ---- the tab store: which documents are open and which tab is shown. "files" is the fixed explorer tab.
  const TABS_KEY = "filePanelTabs";
  function createTabStore(storage) {
    let counter = 0;
    const store = {
      tabs: [],
      active: "files",
      find(card) { const key = tabKey(card); return store.tabs.find((tab) => tab.key === key) || null; },
      get(id) { return store.tabs.find((tab) => tab.id === id) || null; },
      // The person opens a document: the tab is added if new and shown.
      open(card, { focus = true } = {}) {
        let tab = store.find(card);
        const created = !tab;
        if (!tab) {
          tab = {
            id: `fp${++counter}`, key: tabKey(card), root: card.root, path: normalizePath(card.path),
            name: card.name || baseName(card.path) || card.path, kind: card.kind || null, agent: card.agent || "", dot: false,
          };
          store.tabs.push(tab);
        }
        if (focus) store.focus(tab.id);
        store.save();
        return { tab, created };
      },
      // An agent shows a file while the panel is up: the tab appears without taking the focus from the one in use.
      // `buttonDot` tells the caller to mark the button that opens a closed panel.
      receive(card, { panelOpen }) {
        const { tab, created } = store.open(card, { focus: false });
        if (store.active !== tab.id) tab.dot = true;
        store.save();
        return { tab, created, buttonDot: !panelOpen };
      },
      focus(id) {
        if (id !== "files" && !store.get(id)) return false;
        store.active = id;
        const tab = store.get(id);
        if (tab) tab.dot = false;
        store.save();
        return true;
      },
      close(id) {
        const index = store.tabs.findIndex((tab) => tab.id === id);
        if (index < 0) return false;
        store.tabs.splice(index, 1);
        if (store.active === id) store.active = (store.tabs[index] || store.tabs[index - 1])?.id || "files";
        store.save();
        return true;
      },
      save() {
        try {
          storage?.setItem(TABS_KEY, JSON.stringify({
            tabs: store.tabs.map(({ root, path, name, kind, agent }) => ({ root, path, name, kind, agent })),
            active: store.active === "files" ? -1 : store.tabs.findIndex((tab) => tab.id === store.active),
          }));
        } catch { /* storage may be blocked */ }
      },
      load() {
        try {
          const saved = JSON.parse(storage?.getItem(TABS_KEY) || "null");
          if (!saved || !Array.isArray(saved.tabs)) return;
          for (const item of saved.tabs) {
            if (item && typeof item.root === "string" && typeof item.path === "string") store.open(item, { focus: false });
          }
          store.active = store.tabs[saved.active]?.id || "files";
        } catch { /* a damaged value is ignored */ }
      },
    };
    return store;
  }

  // ---- the API client. A backend that is not open answers 403 { error: "approval_required", backend }: the person is
  // asked once, the grant is posted and the request retried. Concurrent requests for one backend share one question.
  class FilesError extends Error {
    constructor(message, status, code, backend) { super(message); this.status = status; this.code = code; this.backend = backend; }
  }
  function createFilesClient({ fetchFn, confirm, t = (text, values) => String(text).replace(/\{(\w+)\}/g, (_, key) => values?.[key] ?? "") }) {
    const asking = new Map();
    function query(params) {
      const search = new URLSearchParams();
      for (const [key, value] of Object.entries(params || {})) if (value !== undefined && value !== null && value !== "") search.set(key, String(value));
      return search.toString();
    }
    const url = (endpoint, params) => `/api/files/${endpoint}?${query(params)}`;
    async function failure(response) {
      const body = await response.json().catch(() => ({}));
      return new FilesError(typeof body.error === "string" ? body.error : `HTTP ${response.status}`, response.status, body.error, body.backend);
    }
    async function grant(backend, agent) {
      if (!asking.has(backend)) {
        const run = (async () => {
          const ok = await confirm({
            title: t("Allow browsing"),
            message: t("Allow read-only browsing of {name} for this session?", { name: backend }),
            confirmLabel: t("Allow"),
          });
          if (!ok) throw new FilesError(t("Browsing of {name} was not allowed", { name: backend }), 403, "declined", backend);
          const response = await fetchFn(`/api/files/grant?${query({ agent })}`, {
            method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ backend }),
          });
          if (!response.ok) throw await failure(response);
        })();
        asking.set(backend, run);
        run.then(() => asking.delete(backend), () => asking.delete(backend));
      }
      return asking.get(backend);
    }
    // Returns the Response of a successful request.
    async function request(endpoint, params) {
      for (let attempt = 0; ; attempt++) {
        const response = await fetchFn(url(endpoint, params), { credentials: "same-origin" });
        if (response.ok) return response;
        const error = await failure(response);
        if (error.status === 403 && error.code === "approval_required" && error.backend && attempt < 1) {
          await grant(error.backend, params && params.agent);
          continue;
        }
        throw error;
      }
    }
    return {
      url,
      grant,
      request,
      async json(endpoint, params) { return (await request(endpoint, params)).json(); },
      async text(params) { return (await request("content", params)).text(); },
      roots: (agent) => request("roots", { agent }).then((r) => r.json()),
      list: (agent, rootId, path) => request("list", { agent, root: rootId, path }).then((r) => r.json()),
      info: (agent, rootId, path) => request("info", { agent, root: rootId, path }).then((r) => r.json()),
      contentUrl: (agent, rootId, path, extra = {}) => url("content", { agent, root: rootId, path, ...extra }),
      pdfPageUrl: (agent, rootId, path, page, width) => url("pdf-page", { agent, root: rootId, path, page, width }),
    };
  }

  const api = {
    parseCsv, normalizePath, joinPath, parentPath, breadcrumbs, baseName, formatSize, formatDate, sortEntries, guessKind,
    codeLanguageFor, extension, tabKey, createTabStore, createFilesClient, FilesError, CSV_MAX_ROWS, TEXT_MAX_CHARS,
  };

  // ------------------------------------------------------------------ the panel (browser only)
  if (typeof document !== "undefined") {
    const OPEN_KEY = "filePanelOpen";
    const WIDTH_KEY = "filePanelWidth";
    const WIDTH_MIN = 300;
    const WIDTH_DEFAULT = 440;
    const SVG = {
      files: '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
      folder: '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
      file: '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/>',
      lock: '<rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/>',
      download: '<path d="M12 4v11m0 0-4-4m4 4 4-4M5 20h14"/>',
      refresh: '<path d="M20 11a8 8 0 1 0-2.3 5.7"/><path d="M20 4v7h-7"/>',
      close: '<path d="M18 6 6 18M6 6l12 12"/>',
      back: '<path d="m15 18-6-6 6-6"/>',
      wrap: '<path d="M4 6h16M4 12h13a3 3 0 0 1 0 6h-4m0 0 2-2m-2 2 2 2M4 18h5"/>',
    };
    const icon = (name) => {
      const span = document.createElement("span");
      span.className = "fp-icon";
      span.innerHTML = `<svg class="icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false">${SVG[name]}</svg>`;  // constant markup above
      return span;
    };
    const el = (tag, className, text) => {
      const node = document.createElement(tag);
      if (className) node.className = className;
      if (text != null) node.textContent = text;
      return node;
    };
    const iconButton = (name, label, onClick) => {
      const button = el("button", "icon-btn fp-btn");
      button.type = "button";
      button.title = label;
      button.setAttribute("aria-label", label);
      button.append(icon(name));
      if (onClick) button.addEventListener("click", onClick);
      return button;
    };
    // Translations come from app.js; a text without a Spanish entry stays English instead of throwing.
    const t = (text, values) => {
      try { if (typeof tr === "function") return tr(text, values); } catch { /* fall through */ }
      return String(text).replace(/\{(\w+)\}/g, (_, key) => values?.[key] ?? "");
    };
    const currentAgent = () => (typeof state !== "undefined" && state.agentId) || "";

    let storageLocal = null;
    let storageSession = null;
    try { storageLocal = window.localStorage; storageSession = window.sessionStorage; } catch { /* blocked */ }

    const store = createTabStore(storageSession);
    const client = createFilesClient({
      fetchFn: (...args) => fetch(...args),
      confirm: (options) => (typeof confirmDialog === "function" ? confirmDialog(options) : Promise.resolve(window.confirm(options.message))),
      t,
    });
    const panels = new Map();  // tab id -> { panel, tabButton, wrap, loaded, render }
    const explorer = { agent: null, roots: [], root: null, path: "", entries: [], truncated: false, state: "idle", error: "", token: 0 };
    let panelEl = null, tabbar = null, tablist = null, body = null, toggle = null, filesPanel = null, filesBody = null;

    const isOpen = () => document.documentElement.classList.contains("filepanel-open");
    const isNarrow = () => window.matchMedia("(max-width: 800px)").matches;

    function setOpen(open, { remember = true } = {}) {
      document.documentElement.classList.toggle("filepanel-open", open);
      if (panelEl) panelEl.hidden = !open;
      if (toggle) {
        toggle.setAttribute("aria-expanded", String(open));
        if (open) delete toggle.dataset.dot;
      }
      if (remember) { try { storageLocal?.setItem(OPEN_KEY, open ? "1" : "0"); } catch { /* ignore */ } }
      if (open) { ensureExplorer(); renderTabs(); }
    }

    // ---- width: a handle on the left edge, like the side panel's
    function widthMax() { return Math.max(WIDTH_MIN, Math.min(900, Math.round(window.innerWidth * 0.7))); }
    function applyWidth(px) {
      const value = Math.min(widthMax(), Math.max(WIDTH_MIN, Math.round(px)));
      document.documentElement.style.setProperty("--filepanel-w", `${value}px`);
      return value;
    }
    function saveWidth(px) { try { storageLocal?.setItem(WIDTH_KEY, String(px)); } catch { /* ignore */ } }
    function currentWidth() { return panelEl?.getBoundingClientRect().width || WIDTH_DEFAULT; }
    function wireResizer(handle) {
      const root = document.documentElement;
      handle.addEventListener("pointerdown", (event) => {
        event.preventDefault();
        handle.setPointerCapture(event.pointerId);
        handle.classList.add("dragging");
        root.classList.add("resizing-filepanel");
      });
      handle.addEventListener("pointermove", (event) => {
        if (handle.hasPointerCapture(event.pointerId)) applyWidth(window.innerWidth - event.clientX);
      });
      const stop = (event) => {
        if (!handle.hasPointerCapture(event.pointerId)) return;
        handle.releasePointerCapture(event.pointerId);
        handle.classList.remove("dragging");
        root.classList.remove("resizing-filepanel");
        saveWidth(Math.round(currentWidth()));
      };
      handle.addEventListener("pointerup", stop);
      handle.addEventListener("pointercancel", stop);
      handle.addEventListener("dblclick", () => { root.style.removeProperty("--filepanel-w"); try { storageLocal?.removeItem(WIDTH_KEY); } catch { /* ignore */ } });
      handle.addEventListener("keydown", (event) => {
        if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
        event.preventDefault();
        saveWidth(applyWidth(currentWidth() + (event.key === "ArrowLeft" ? 16 : -16)));
      });
    }

    // ---- tabs
    function renderTabs() {
      if (!tablist) return;
      const items = [{ id: "files", name: t("Files"), fixed: true }, ...store.tabs];
      tablist.replaceChildren(...items.map((item) => {
        const wrap = el("div", "fp-tabwrap");
        wrap.setAttribute("role", "presentation");
        const button = el("button", "fp-tab");
        button.type = "button";
        button.id = `fp-tab-${item.id}`;
        button.setAttribute("role", "tab");
        button.setAttribute("aria-selected", String(store.active === item.id));
        button.setAttribute("aria-controls", `fp-panel-${item.id}`);
        button.tabIndex = store.active === item.id ? 0 : -1;
        button.dataset.tab = item.id;
        if (item.dot) button.dataset.dot = "1";
        if (item.fixed) button.append(icon("files"));
        const label = el("span", "fp-tab-label", item.name);
        button.append(label);
        if (!item.fixed) button.title = `${item.root}:${item.path}`;
        button.addEventListener("click", () => activate(item.id));
        button.addEventListener("auxclick", (event) => { if (event.button === 1 && !item.fixed) { event.preventDefault(); closeTab(item.id); } });
        button.addEventListener("mousedown", (event) => { if (event.button === 1) event.preventDefault(); });
        wrap.append(button);
        if (!item.fixed) {
          const close = el("button", "fp-tabclose");
          close.type = "button";
          close.tabIndex = -1;
          close.title = t("Close {name}", { name: item.name });
          close.setAttribute("aria-label", close.title);
          close.append(icon("close"));
          close.addEventListener("click", (event) => { event.stopPropagation(); closeTab(item.id); });
          wrap.append(close);
        }
        return wrap;
      }));
      syncPanels();
      tablist.querySelector('[aria-selected="true"]')?.scrollIntoView?.({ block: "nearest", inline: "nearest" });
    }
    function syncPanels() {
      filesPanel.hidden = store.active !== "files";
      for (const [id, entry] of panels) entry.panel.hidden = store.active !== id;
      const entry = panels.get(store.active);
      if (entry && !entry.loaded) { entry.loaded = true; entry.render(); }
    }
    function activate(id, { focusTab = false } = {}) {
      if (!store.focus(id)) return;
      if (id === "files") ensureExplorer();  // the agent may have changed since it was last shown
      if (id !== "files" && !panels.has(id)) buildDocPanel(store.get(id));
      renderTabs();
      if (focusTab) tablist.querySelector(`[data-tab="${id}"]`)?.focus();
    }
    function closeTab(id) {
      const entry = panels.get(id);
      entry?.dispose?.();
      entry?.panel.remove();
      panels.delete(id);
      store.close(id);
      if (store.active !== "files" && !panels.has(store.active)) buildDocPanel(store.get(store.active));
      renderTabs();
    }
    function wireTabKeys() {
      tablist.addEventListener("keydown", (event) => {
        const ids = ["files", ...store.tabs.map((tab) => tab.id)];
        const at = ids.indexOf(document.activeElement?.dataset?.tab || store.active);
        let target = null;
        if (event.key === "ArrowRight") target = ids[(at + 1) % ids.length];
        else if (event.key === "ArrowLeft") target = ids[(at - 1 + ids.length) % ids.length];
        else if (event.key === "Home") target = ids[0];
        else if (event.key === "End") target = ids[ids.length - 1];
        else if ((event.key === "Delete" || event.key === "Backspace") && ids[at] !== "files") {
          event.preventDefault();
          closeTab(ids[at]);
          tablist.querySelector('[aria-selected="true"]')?.focus();
          return;
        }
        if (target) { event.preventDefault(); activate(target, { focusTab: true }); }
      });
    }

    // ---- explorer
    function ensureExplorer() {
      const agent = currentAgent();
      if (!agent) return;
      if (explorer.state === "idle" || (agent && explorer.agent !== agent)) loadRoots(agent);
    }
    async function loadRoots(agent) {
      const token = ++explorer.token;
      explorer.agent = agent;
      explorer.state = "loading";
      renderExplorer();
      try {
        const data = await client.roots(agent);
        if (token !== explorer.token) return;
        explorer.roots = data.roots || [];
        const kept = explorer.root && explorer.roots.some((r) => r.id === explorer.root);
        const saved = kept ? explorer.root : explorer.roots[0]?.id || null;
        explorer.root = saved;
        if (!kept) explorer.path = "";
        if (!saved) { explorer.state = "ready"; explorer.entries = []; renderExplorer(); return; }
        await loadList(explorer.root, explorer.path);
      } catch (error) {
        if (token !== explorer.token) return;
        explorer.state = "error";
        explorer.error = error.message;
        renderExplorer();
      }
    }
    async function loadList(rootId, path) {
      const token = ++explorer.token;
      explorer.root = rootId;
      explorer.path = normalizePath(path);
      explorer.state = "loading";
      renderExplorer();
      try {
        const data = await client.list(explorer.agent, rootId, explorer.path);
        if (token !== explorer.token) return;
        explorer.entries = sortEntries(data.entries);
        explorer.truncated = Boolean(data.truncated);
        explorer.state = "ready";
        refreshRootsQuietly();  // a backend that was just allowed loses its lock
      } catch (error) {
        if (token !== explorer.token) return;
        explorer.state = "error";
        explorer.error = error.code === "declined" ? t("Browsing was not allowed.") : error.message;
        explorer.entries = [];
      }
      renderExplorer();
    }
    async function refreshRootsQuietly() {
      try {
        const data = await client.roots(explorer.agent);
        const before = JSON.stringify(explorer.roots);
        explorer.roots = data.roots || explorer.roots;
        if (JSON.stringify(explorer.roots) !== before) renderExplorer();
      } catch { /* the lock icon stays as it was */ }
    }
    const ROOT_NAMES = { mind_palace: "Mind palace", conversations: "Conversations", agent: "Agent files", uploads: "Uploads", recall_docs: "RECALL docs" };
    const AGENT_ROOTS = new Set(["mind_palace", "conversations", "agent"]);  // what belongs to one agent (uploads are the user's)
    const nameOfRoot = (r) => (r.kind === "source" && ROOT_NAMES[r.id] ? t(ROOT_NAMES[r.id]) : r.label || r.id);
    const rootLabel = (rootId) => { const r = explorer.roots.find((x) => x.id === rootId); return r ? nameOfRoot(r) : rootId || ""; };
    // The display name of an agent: sources such as the mind palace belong to one agent, so the panel says which.
    const agentName = (id) => (typeof state !== "undefined" && state.user?.agents?.find((a) => a.id === id)?.name) || id || "";
    const isLocked = (r) => r.kind === "backend" && !(r.open || r.granted);

    function renderExplorer() {
      if (!filesBody) return;
      const nodes = [];
      const rootsBar = el("div", "fp-roots");
      rootsBar.setAttribute("role", "toolbar");
      rootsBar.setAttribute("aria-label", t("Locations"));
      for (const r of explorer.roots) {
        const button = el("button", "fp-root");
        button.type = "button";
        button.setAttribute("aria-pressed", String(r.id === explorer.root));
        if (isLocked(r)) { button.dataset.locked = "1"; button.append(icon("lock")); button.title = t("Needs your approval"); }
        button.append(el("span", "", nameOfRoot(r)));
        button.addEventListener("click", () => loadList(r.id, ""));
        rootsBar.append(button);
      }
      nodes.unshift(el("p", "fp-agent", t("Files of {name}", { name: agentName(explorer.agent) })));
      nodes.push(rootsBar);
      if (explorer.root) {
        const nav = el("nav", "fp-crumbs");
        nav.setAttribute("aria-label", t("Path"));
        breadcrumbs(rootLabel(explorer.root), explorer.path).forEach((crumb, index, all) => {
          if (index) nav.append(el("span", "fp-sep", "/"));
          const last = index === all.length - 1;
          const button = el("button", "fp-crumb", crumb.label);
          button.type = "button";
          if (last) button.setAttribute("aria-current", "page");
          else button.addEventListener("click", () => loadList(explorer.root, crumb.path));
          nav.append(button);
        });
        nav.append(iconButton("refresh", t("Refresh"), () => loadList(explorer.root, explorer.path)));
        nodes.push(nav);
      }
      const list = el("div", "fp-list");
      list.setAttribute("aria-busy", String(explorer.state === "loading"));
      if (explorer.state === "loading") list.append(el("p", "fp-note", t("Loading…")));
      else if (explorer.state === "error") {
        const note = el("p", "fp-note fp-error", explorer.error);
        note.setAttribute("role", "alert");
        list.append(note);
      } else if (!explorer.roots.length) list.append(el("p", "fp-note", t("No locations are available.")));
      else {
        const ul = el("ul", "fp-entries");
        if (explorer.path) {
          const li = el("li");
          const up = el("button", "fp-entry");
          up.type = "button";
          up.append(icon("folder"), el("span", "fp-name", ".."), el("span", "fp-size"), el("span", "fp-date"));
          up.addEventListener("click", () => loadList(explorer.root, parentPath(explorer.path)));
          li.append(up);
          ul.append(li);
        }
        for (const entry of explorer.entries) ul.append(entryRow(entry));
        if (!explorer.entries.length) list.append(el("p", "fp-note", t("This folder is empty.")));
        list.append(ul);
        if (explorer.truncated) list.append(el("p", "fp-note", t("Only the first entries are shown.")));
      }
      nodes.push(list);
      filesBody.replaceChildren(...nodes);
    }
    function entryRow(entry) {
      const li = el("li");
      const isDir = entry.type === "dir";
      const binary = !isDir && guessKind(entry.name) === "binary";
      const row = el("div", "fp-row");
      const main = el("button", "fp-entry");
      main.type = "button";
      main.append(icon(isDir ? "folder" : "file"), el("span", "fp-name", entry.name),
        el("span", "fp-size", isDir ? "" : formatSize(entry.size)), el("span", "fp-date", formatDate(entry.mtime)));
      main.addEventListener("click", () => {
        if (isDir) loadList(explorer.root, joinPath(explorer.path, entry.name));
        else if (binary) download(explorer.root, joinPath(explorer.path, entry.name), explorer.agent);
        else openCard({ root: explorer.root, path: joinPath(explorer.path, entry.name), name: entry.name, agent: explorer.agent });
      });
      row.append(main);
      if (binary) {
        const link = iconButton("download", t("Download {name}", { name: entry.name }));
        link.addEventListener("click", () => download(explorer.root, joinPath(explorer.path, entry.name), explorer.agent));
        row.append(link);
      }
      li.append(row);
      return li;
    }
    function download(rootId, path, agent) {
      const link = document.createElement("a");
      link.href = client.contentUrl(agent, rootId, path, { download: 1 });
      link.download = baseName(path);
      link.rel = "noopener";
      document.body.append(link);
      link.click();
      link.remove();
    }

    // ---- document tabs
    function buildDocPanel(tab) {
      if (!tab || panels.has(tab.id)) return;
      const panel = el("div", "fp-doc");
      panel.id = `fp-panel-${tab.id}`;
      panel.setAttribute("role", "tabpanel");
      panel.setAttribute("aria-labelledby", `fp-tab-${tab.id}`);
      panel.hidden = true;
      const header = el("div", "fp-dochead");
      const title = el("div", "fp-doctitle");
      title.append(el("strong", "fp-docname", tab.name), el("span", "fp-docpath", `${AGENT_ROOTS.has(tab.root) ? `${agentName(tab.agent)} · ` : ""}${tab.root}:${tab.path}`));
      const actions = el("div", "fp-docactions");
      const docBody = el("div", "fp-docbody");
      const entry = { panel, loaded: false, docBody, info: null, dispose: null, render: null };
      const dl = iconButton("download", t("Download"), () => download(tab.root, tab.path, tab.agent));
      const refresh = iconButton("refresh", t("Refresh"), () => entry.render());
      entry.actions = actions;
      actions.append(dl, refresh);
      header.append(title, actions);
      panel.append(header, docBody);
      entry.render = () => renderDocument(tab, entry);
      panels.set(tab.id, entry);
      body.append(panel);
    }
    function note(text, error = false) {
      const p = el("p", error ? "fp-note fp-error" : "fp-note", text);
      if (error) p.setAttribute("role", "alert");
      return p;
    }
    async function renderDocument(tab, entry) {
      entry.dispose?.();
      entry.dispose = null;
      const target = entry.docBody;
      target.replaceChildren(note(t("Loading…")));
      target.setAttribute("aria-busy", "true");
      try {
        const info = await client.info(tab.agent, tab.root, tab.path);
        entry.info = info;
        tab.kind = info.kind;
        const stamp = Date.now();
        const view = await viewerFor(tab, info, entry, stamp);
        target.replaceChildren(view);
      } catch (error) {
        target.replaceChildren(note(error.code === "declined" ? t("Browsing was not allowed.") : error.message, true));
      } finally {
        target.removeAttribute("aria-busy");
      }
    }
    async function viewerFor(tab, info, entry, stamp) {
      const url = client.contentUrl(tab.agent, tab.root, tab.path, { _: stamp });
      switch (info.kind) {
        case "markdown": {
          const text = await client.text({ agent: tab.agent, root: tab.root, path: tab.path });
          const wrap = el("div", "fp-markdown");
          const holder = el("div", "body");
          if (typeof renderMarkdown === "function") renderMarkdown(holder, text);  // marked + DOMPurify, like a chat message
          else holder.textContent = text;
          for (const a of holder.querySelectorAll("a")) { a.target = "_blank"; a.rel = "noopener noreferrer"; }
          wrap.append(holder);
          return wrap;
        }
        case "text":
        case "code": {
          const text = await client.text({ agent: tab.agent, root: tab.root, path: tab.path });
          return textView(tab, text, entry);
        }
        case "csv": {
          const text = await client.text({ agent: tab.agent, root: tab.root, path: tab.path });
          return csvView(text);
        }
        case "image": return imageView(tab, url);
        case "html": return htmlView(tab, url);
        case "pdf": return pdfView(tab, info, entry);
        default: return binaryView(tab, info);
      }
    }
    function textView(tab, text, entry) {
      const wrap = el("div", "fp-textwrap");
      const clipped = text.length > TEXT_MAX_CHARS;
      const shown = clipped ? text.slice(0, TEXT_MAX_CHARS) : text;
      const bar = el("div", "fp-toolbar");
      const wrapButton = iconButton("wrap", t("Wrap lines"));
      wrapButton.setAttribute("aria-pressed", "true");
      bar.append(wrapButton);
      const pre = el("pre", "fp-text wrap");
      const code = el("code");
      let html = null;
      if (shown.length <= HIGHLIGHT_MAX_CHARS && typeof codeToHtml === "function") {
        const language = codeLanguageFor(tab.name);
        // JSON Lines: every line is its own JSON document, so each one is highlighted on its own
        html = /\.(jsonl|ndjson)$/i.test(tab.name)
          ? shown.split("\n").map((line) => codeToHtml(line, "json") ?? line.replace(/&/g, "&amp;").replace(/</g, "&lt;")).join("\n")
          : codeToHtml(shown, language);
      }
      if (html) code.innerHTML = html;  // hljs output, sanitised by codeToHtml with DOMPurify
      else code.textContent = shown;
      pre.append(code);
      wrapButton.addEventListener("click", () => {
        const on = wrapButton.getAttribute("aria-pressed") !== "true";
        wrapButton.setAttribute("aria-pressed", String(on));
        pre.classList.toggle("wrap", on);
      });
      wrap.append(bar, pre);
      if (clipped) wrap.append(note(t("The file is long: only the first part is shown. Download it to read the rest.")));
      return wrap;
    }
    function csvView(text) {
      const { rows, truncated } = parseCsv(text);
      const wrap = el("div", "fp-csv");
      if (!rows.length) return note(t("The file is empty."));
      const table = el("table");
      const width = Math.max(...rows.map((r) => r.length));
      const [head, ...rest] = rows;
      const thead = el("thead");
      const headRow = el("tr");
      for (let i = 0; i < width; i++) { const th = el("th", "", head[i] ?? ""); th.scope = "col"; headRow.append(th); }
      thead.append(headRow);
      const tbody = el("tbody");
      for (const r of rest) {
        const tr_ = el("tr");
        for (let i = 0; i < width; i++) tr_.append(el("td", "", r[i] ?? ""));
        tbody.append(tr_);
      }
      table.append(thead, tbody);
      const scroller = el("div", "fp-csv-scroll");
      scroller.tabIndex = 0;
      scroller.setAttribute("role", "region");
      scroller.setAttribute("aria-label", t("Table"));
      scroller.append(table);
      wrap.append(scroller);
      if (truncated) wrap.append(note(t("Only the first {count} rows are shown.", { count: CSV_MAX_ROWS })));
      return wrap;
    }
    function imageView(tab, url) {
      const wrap = el("div", "fp-image");
      const img = el("img");
      img.alt = tab.name;
      img.src = url;
      img.addEventListener("click", () => wrap.classList.toggle("actual"));
      img.addEventListener("error", () => wrap.replaceChildren(note(t("The image could not be loaded."), true)));
      wrap.title = t("Click to toggle the actual size");
      wrap.append(img);
      return wrap;
    }
    function htmlView(tab, url) {
      const frame = el("iframe", "fp-html");
      frame.setAttribute("sandbox", "");  // no scripts, no forms, no same-origin access
      frame.referrerPolicy = "no-referrer";
      frame.title = tab.name;
      frame.src = url;
      return frame;
    }
    function binaryView(tab, info) {
      const card = el("div", "fp-binary");
      card.append(icon("file"), el("strong", "", tab.name), el("span", "fp-muted", [info.mime, formatSize(info.size)].filter(Boolean).join(" · ")),
        el("p", "fp-muted", t("This file cannot be shown here.")));
      const button = el("button", "primary", t("Download"));
      button.type = "button";
      button.addEventListener("click", () => download(tab.root, tab.path, tab.agent));
      card.append(button);
      return card;
    }
    function pdfView(tab, info, entry) {
      const pages = Math.max(1, Number(info.pages) || 1);
      const wrap = el("div", "fp-pdf");
      const indicator = el("div", "fp-pageind", t("Page {page} of {pages}", { page: 1, pages }));
      indicator.setAttribute("role", "status");
      const scroller = el("div", "fp-pdf-scroll");
      scroller.tabIndex = 0;
      const stamp = Date.now();
      const slots = [];
      const pageWidth = () => Math.max(200, Math.round((scroller.clientWidth || panelEl?.clientWidth || WIDTH_DEFAULT) * (window.devicePixelRatio > 1 ? 1.5 : 1)));
      for (let page = 1; page <= pages; page++) {
        const slot = el("div", "fp-page");
        slot.dataset.page = String(page);
        slot.setAttribute("aria-label", t("Page {page} of {pages}", { page, pages }));
        slot.append(el("span", "fp-page-n", String(page)));
        scroller.append(slot);
        slots.push(slot);
      }
      const load = (slot) => {
        if (slot.dataset.loaded) return;
        slot.dataset.loaded = "1";
        const img = el("img");
        img.alt = t("Page {page} of {pages}", { page: slot.dataset.page, pages });
        img.addEventListener("load", () => { slot.style.minHeight = ""; slot.classList.add("ready"); });
        img.addEventListener("error", () => { slot.textContent = t("The page could not be loaded."); slot.classList.add("failed"); });
        img.src = `${client.pdfPageUrl(tab.agent, tab.root, tab.path, slot.dataset.page, pageWidth())}&_=${stamp}`;
        slot.append(img);
      };
      if (typeof IntersectionObserver === "function") {
        const visible = new Map();
        const lazy = new IntersectionObserver((items) => {
          for (const item of items) {
            if (item.isIntersecting) load(item.target);
            visible.set(item.target, item.intersectionRatio);
          }
          let best = null;
          for (const [slot, ratio] of visible) if (ratio > 0 && (!best || ratio > best.ratio)) best = { slot, ratio };
          if (best) indicator.textContent = t("Page {page} of {pages}", { page: best.slot.dataset.page, pages });
        }, { root: scroller, rootMargin: "300px 0px", threshold: [0, 0.25, 0.5, 0.75, 1] });
        for (const slot of slots) lazy.observe(slot);
        entry.dispose = () => lazy.disconnect();
      } else slots.slice(0, 3).forEach(load);
      wrap.append(indicator, scroller);
      return wrap;
    }

    // ---- opening documents
    function openCard(card, { focus = true } = {}) {
      if (!isOpen()) setOpen(true);
      const { tab } = store.open({ ...card, agent: card.agent || currentAgent() }, { focus });
      if (!panels.has(tab.id) && focus) buildDocPanel(tab);
      renderTabs();
      if (focus && isNarrow()) panelEl?.scrollIntoView?.({ block: "nearest" });
      return tab;
    }
    // A file card that arrives live from the agent: no focus change; a dot says there is something new.
    function receiveCard(card) {
      const { tab, buttonDot } = store.receive({ ...card, agent: card.agent || currentAgent() }, { panelOpen: isOpen() });
      if (buttonDot && toggle) toggle.dataset.dot = "1";
      if (isOpen()) renderTabs();
      return tab;
    }

    function cardElement(card) {
      const button = el("button", "file-card");
      button.type = "button";
      button.title = t("Open in the file panel");
      const kind = card.kind || guessKind(card.name || card.path);
      button.append(icon("file"));
      const text = el("span", "file-card-text");
      text.append(el("span", "file-card-name", card.name || baseName(card.path)), el("span", "file-card-path", `${card.root}:${normalizePath(card.path)}`));
      button.append(text, el("span", "file-card-kind", String(kind).toUpperCase()));
      button.addEventListener("click", () => openCard(card));
      return button;
    }
    // The chat attachment's small button: it opens the upload in the panel (root `uploads`, `<chat id>/<stored file>`, the
    // stored file being `<timestamp>-<name>`, what the server kept on the turn).
    function attachmentButton(attachment, chatId) {
      if (!attachment?.name || !attachment?.file || !chatId) return null;
      const button = el("button", "attachment-open");
      button.type = "button";
      button.title = t("Open in the file panel");
      button.setAttribute("aria-label", t("Open {name} in the file panel", { name: attachment.name }));
      button.append(icon("files"));
      button.addEventListener("click", (event) => {
        event.preventDefault();
        event.stopPropagation();
        openCard({ root: "uploads", path: `${chatId}/${attachment.file}`, name: attachment.name });
      });
      return button;
    }

    // ---- start-up
    function init() {
      panelEl = document.getElementById("file-panel");
      if (!panelEl) return;
      tabbar = panelEl.querySelector(".fp-tabbar");
      tablist = document.getElementById("fp-tablist");
      body = document.getElementById("fp-body");
      filesPanel = document.getElementById("fp-panel-files");
      filesBody = document.getElementById("fp-files-body");
      toggle = document.getElementById("filepanel-toggle");
      wireTabKeys();
      wireResizer(document.getElementById("filepanel-resizer"));
      document.getElementById("fp-close").addEventListener("click", () => setOpen(false));
      toggle?.addEventListener("click", () => setOpen(!isOpen()));
      try { const saved = Number(storageLocal?.getItem(WIDTH_KEY)); if (saved) applyWidth(saved); } catch { /* ignore */ }
      store.load();
      let open = false;
      try { open = storageLocal?.getItem(OPEN_KEY) === "1"; } catch { /* ignore */ }
      if (store.active !== "files") buildDocPanel(store.get(store.active));
      for (const tab of store.tabs) tab.dot = false;
      setOpen(open && !isNarrow(), { remember: false });
      renderTabs();
      window.addEventListener("resize", () => { if (isOpen()) applyWidth(currentWidth()); });
    }

    api.openCard = openCard;
    api.receiveCard = receiveCard;
    api.cardElement = cardElement;
    api.attachmentButton = attachmentButton;
    api.setOpen = setOpen;
    api.sync = () => { if (isOpen()) ensureExplorer(); };
    api.store = store;
    api.client = client;
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
    else init();
  }

  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.FilePanel = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
