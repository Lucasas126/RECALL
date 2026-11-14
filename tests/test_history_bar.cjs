const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");

// The "load earlier" bar and loadEarlier(), loaded without a browser: a tiny DOM that is enough for prepend/remove/query.
const source = readFileSync(path.join(__dirname, "..", "src", "web", "static", "app.js"), "utf8");
const grab = (name) => source.match(new RegExp(`(?:async )?function ${name}\\([\\s\\S]*?\\r?\\n\\}\\r?\\n`))[0];

class Node_ {
  constructor(tag = "div") { this.tag = tag; this.children = []; this.parent = null; this.className = ""; this.disabled = false; this.listeners = {}; this.textContent = ""; }
  get childNodes() { return [...this.children]; }
  append(...nodes) { for (const n of nodes) { n.parent?.remove?.call(n.parent, n); n.parent = this; this.children.push(n); } }
  prepend(...nodes) { for (const n of nodes) { n.parent?.children.splice(n.parent.children.indexOf(n), 1); n.parent = this; } this.children.unshift(...nodes); }
  remove(node) { if (!node) { this.parent?.children.splice(this.parent.children.indexOf(this), 1); this.parent = null; return; } this.children.splice(this.children.indexOf(node), 1); node.parent = null; }
  addEventListener(type, fn) { this.listeners[type] = fn; }
  querySelector(selector) { return this.querySelectorAll(selector)[0] ?? null; }
  querySelectorAll(selector) {
    const cls = selector.replace(":scope > ", "").replace(/^\./, "");
    const scoped = selector.startsWith(":scope >");
    const pool = scoped ? this.children : this.children.flatMap(function walk(c) { return [c, ...c.children.flatMap(walk)]; });
    return pool.filter((n) => (n.className || "").split(" ").includes(cls) || n.tag === cls);
  }
}

const msg = (n) => ({ role: "user", content: `m${n}` });

function harness({ hidden = 150, total = 210, pages = {} } = {}) {
  const calls = [];
  const box = { scrollHeight: 1000, scrollTop: 0 };
  const state = { view: null, agentId: "bot", chatId: "c1" };
  const context = vm.createContext({
    state, calls, box, document: { createElement: (tag) => new Node_(tag) },
    tr: (text, vars = {}) => text.replace(/\{(\w+)\}/g, (_, k) => vars[k]),
    chatsUrl: (suffix) => `/chats${suffix}`,
    $: () => box, updateJumpLatest: () => calls.push("jump"), stuckToBottom: true,
    addMessage: () => ({ error: (e) => calls.push(`error ${e.message}`) }),
    drawMessages: (container, messages) => { for (const m of messages) { const n = new Node_(); n.className = "msg"; n.textContent = m.content; container.append(n); } },
    grow: (count) => { box.scrollHeight += count * 100; },  // the box gets taller when the new messages are inserted, not before
    api: async (url) => {
      calls.push(url);
      const page = pages[url];
      if (!page) throw new Error(`no page for ${url}`);
      if (page.delay) await new Promise((r) => setTimeout(r, page.delay));
      return page;
    },
  });
  vm.runInContext(
    `const CHAT_PAGE = 25; const CHAT_MORE = 25;\n${grab("chatQuery")}\n${grab("updateHistoryBar")}\n${grab("loadEarlier")}\n`
    + "this.updateHistoryBar = updateHistoryBar; this.loadEarlier = loadEarlier;",
    context,
  );
  const view = new Node_();
  const realPrepend = view.prepend.bind(view);
  view.prepend = (...nodes) => { realPrepend(...nodes); box.scrollHeight += nodes.filter((n) => n.className === "msg").length * 100; };
  view.history = { hidden, total, firstIndex: 300, loading: false };
  view.historyLimit = 25;
  state.view = view;
  return { context, view, calls, box, state };
}

test("a chat with hidden messages gets a bar with the counts and both buttons", () => {
  const { context, view } = harness({ hidden: 150, total: 210 });
  context.updateHistoryBar(view);
  const bar = view.querySelector(":scope > .history-bar");
  assert.ok(bar);
  assert.equal(bar.children[0].textContent, "150 earlier messages not shown (of 210)");
  assert.deepEqual(bar.children.slice(1).map((b) => b.textContent), ["Load 25 more", "Load all"]);
});

test("with fewer hidden messages than a page there is only the load button, with the real count", () => {
  const { context, view } = harness({ hidden: 12, total: 37 });
  context.updateHistoryBar(view);
  const bar = view.querySelector(":scope > .history-bar");
  assert.deepEqual(bar.children.slice(1).map((b) => b.textContent), ["Load 12 more"]);
});

test("nothing hidden: no bar, and an old bar is removed", () => {
  const { context, view } = harness({ hidden: 30, total: 90 });
  context.updateHistoryBar(view);
  view.history.hidden = 0;
  context.updateHistoryBar(view);
  assert.equal(view.querySelector(":scope > .history-bar"), null);
});

test("loading earlier asks for the page that ends before the first shown message and puts it on top, in order", async () => {
  const { context, view, calls } = harness({
    hidden: 150, total: 210,
    pages: { "/chats/c1?limit=25&before=300": { messages: [msg(1), msg(2), msg(3)], window: { hidden: 50, total: 210, first_index: 200 } } },
  });
  const shown = new Node_(); shown.className = "msg"; shown.textContent = "latest";
  view.append(shown);
  context.updateHistoryBar(view);
  await context.loadEarlier(view, 25);
  assert.deepEqual(calls.filter((c) => c.startsWith("/chats")), ["/chats/c1?limit=25&before=300"]);
  const order = view.children.filter((c) => c.className === "msg").map((c) => c.textContent);
  assert.deepEqual(order, ["m1", "m2", "m3", "latest"]);
  assert.equal(view.children[0].className, "history-bar", "the bar stays above everything, with the new counts");
  assert.equal(view.children[0].children[0].textContent, "50 earlier messages not shown (of 210)");
  assert.deepEqual([view.history.hidden, view.history.firstIndex, view.historyLimit], [50, 200, 28]);
});

test("the reader keeps their place: the distance to the bottom does not change", async () => {
  const { context, view, box } = harness({
    hidden: 150, total: 210,
    pages: { "/chats/c1?limit=25&before=300": { messages: [msg(1), msg(2)], window: { hidden: 148, total: 210, first_index: 290 } } },
  });
  box.scrollTop = 200;
  const before = box.scrollHeight - box.scrollTop;
  await context.loadEarlier(view, 25);
  assert.equal(box.scrollHeight - box.scrollTop, before);
});

test("two clicks at once load one page; a click after the view changed does nothing", async () => {
  const { context, view, calls, state } = harness({
    hidden: 150, total: 210,
    pages: { "/chats/c1?limit=25&before=300": { delay: 20, messages: [msg(1)], window: { hidden: 149, total: 210, first_index: 299 } } },
  });
  await Promise.all([context.loadEarlier(view, 25), context.loadEarlier(view, 25)]);
  assert.equal(calls.filter((c) => c.startsWith("/chats")).length, 1);
  state.view = new Node_();
  await context.loadEarlier(view, 25);
  assert.equal(calls.filter((c) => c.startsWith("/chats")).length, 1);
});

test("a chat that changed while the page was loading is left alone", async () => {
  const { context, view, state } = harness({
    hidden: 150, total: 210,
    pages: { "/chats/c1?limit=25&before=300": { delay: 20, messages: [msg(1)], window: { hidden: 149, total: 210, first_index: 299 } } },
  });
  const pending = context.loadEarlier(view, 25);
  state.chatId = "c2";
  await pending;
  assert.equal(view.children.filter((c) => c.className === "msg").length, 0);
  assert.equal(view.history.loading, false);
});

test("a failed load re-enables the buttons and says so", async () => {
  const { context, view, calls } = harness({ hidden: 150, total: 210, pages: {} });
  context.updateHistoryBar(view);
  await context.loadEarlier(view, 25);
  const bar = view.querySelector(":scope > .history-bar");
  assert.ok(bar.children.slice(1).every((b) => !b.disabled));
  assert.ok(calls.some((c) => c.startsWith("error")));
  assert.equal(view.history.loading, false);
});

test("the reload query asks for as many messages as the view shows, never fewer than a page", () => {
  const { context } = harness();
  assert.equal(vm.runInContext("chatQuery()", context), "?limit=25");
  assert.equal(vm.runInContext("chatQuery(10)", context), "?limit=25");
  assert.equal(vm.runInContext("chatQuery(40)", context), "?limit=40");
  assert.equal(vm.runInContext("chatQuery(260)", context), "?limit=260");
});
