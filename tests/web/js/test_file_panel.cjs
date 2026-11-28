const assert = require("node:assert/strict");
const path = require("node:path");
const { test } = require("node:test");

// the pure helpers of the file panel (no DOM): CSV parser, paths, tab store and the approval-aware API client
const fp = require(path.join(__dirname, "..", "..", "..", "src", "web", "static", "filepanel.js"));

const memory = () => ({ data: {}, getItem(k) { return k in this.data ? this.data[k] : null; }, setItem(k, v) { this.data[k] = String(v); } });

test("csv: quotes, doubled quotes, embedded newlines and CRLF", () => {
  const { rows } = fp.parseCsv('name,note\r\n"Ann, Jr.","said ""hi"""\r\nBob,"line1\nline2"\r\n');
  assert.deepEqual(rows, [["name", "note"], ["Ann, Jr.", 'said "hi"'], ["Bob", "line1\nline2"]]);
});

test("csv: semicolon and tab delimiters are detected, BOM dropped, last row without newline kept", () => {
  assert.deepEqual(fp.parseCsv("\uFEFFa;b\n1;2").rows, [["a", "b"], ["1", "2"]]);
  assert.deepEqual(fp.parseCsv("a\tb\n1\t2\n").rows, [["a", "b"], ["1", "2"]]);
  assert.deepEqual(fp.parseCsv("").rows, []);
});

test("csv: stops at the row limit and says so", () => {
  const text = Array.from({ length: 600 }, (_, i) => `r${i},x`).join("\n");
  const result = fp.parseCsv(text, fp.CSV_MAX_ROWS);
  assert.equal(result.rows.length, 500);
  assert.equal(result.truncated, true);
  assert.equal(fp.parseCsv("a,b\n1,2").truncated, false);
});

test("paths: normalise, join, parent and breadcrumbs", () => {
  assert.equal(fp.normalizePath("/a//b/./c/"), "a/b/c");
  assert.equal(fp.joinPath("", "x.md"), "x.md");
  assert.equal(fp.joinPath("a/b", "x.md"), "a/b/x.md");
  assert.equal(fp.parentPath("a/b/c"), "a/b");
  assert.equal(fp.parentPath("a"), "");
  assert.deepEqual(fp.breadcrumbs("Uploads", "a/b"), [
    { label: "Uploads", path: "" }, { label: "a", path: "a" }, { label: "b", path: "a/b" }]);
  assert.deepEqual(fp.breadcrumbs("Uploads", ""), [{ label: "Uploads", path: "" }]);
  assert.equal(fp.baseName("a/b/c.txt"), "c.txt");
});

test("sizes, kinds and entry order", () => {
  assert.equal(fp.formatSize(512), "512 B");
  assert.equal(fp.formatSize(1536), "1.5 KB");
  assert.equal(fp.formatSize(5 * 1024 * 1024), "5.0 MB");
  assert.equal(fp.guessKind("a.zip"), "binary");
  assert.equal(fp.guessKind("a.py"), "code");
  assert.equal(fp.guessKind("a.PDF"), "pdf");
  const sorted = fp.sortEntries([{ name: "b.txt", type: "file" }, { name: "z", type: "dir" }, { name: "a10", type: "file" }, { name: "a2", type: "file" }]);
  assert.deepEqual(sorted.map((e) => e.name), ["z", "a2", "a10", "b.txt"]);
});

test("tabs: open adds and focuses, opening again only focuses", () => {
  const store = fp.createTabStore(memory());
  const a = store.open({ root: "uploads", path: "a.pdf", name: "a.pdf", agent: "example_agent" });
  assert.equal(a.created, true);
  assert.equal(store.active, a.tab.id);
  const b = store.open({ root: "uploads", path: "b.md", name: "b.md", agent: "example_agent" });
  assert.equal(store.tabs.length, 2);
  const again = store.open({ root: "uploads", path: "a.pdf", name: "a.pdf", agent: "example_agent" });
  assert.equal(again.created, false);
  assert.equal(store.tabs.length, 2);
  assert.equal(store.active, a.tab.id);
  assert.notEqual(a.tab.id, b.tab.id);
});

test("tabs: closing the active tab focuses a neighbour, the last one falls back to Files", () => {
  const store = fp.createTabStore(memory());
  const a = store.open({ root: "uploads", path: "a" }).tab;
  const b = store.open({ root: "uploads", path: "b" }).tab;
  const c = store.open({ root: "uploads", path: "c" }).tab;
  store.focus(b.id);
  store.close(b.id);
  assert.equal(store.active, c.id);
  store.close(c.id);
  assert.equal(store.active, a.id);
  store.close(a.id);
  assert.equal(store.active, "files");
  assert.equal(store.close("nope"), false);
});

test("tabs: the list survives a reload through the storage", () => {
  const storage = memory();
  const first = fp.createTabStore(storage);
  first.open({ root: "uploads", path: "a.pdf", name: "a.pdf", agent: "nova" });
  const second = first.open({ root: "palace", path: "n/b.md", name: "b.md", agent: "nova" }).tab;
  first.focus(second.id);
  const next = fp.createTabStore(storage);
  next.load();
  assert.deepEqual(next.tabs.map((t) => t.path), ["a.pdf", "n/b.md"]);
  assert.equal(next.get(next.active).path, "n/b.md");
  const broken = fp.createTabStore({ getItem() { return "{not json"; }, setItem() { throw new Error("denied"); } });
  assert.doesNotThrow(() => broken.load());
  assert.doesNotThrow(() => broken.open({ root: "uploads", path: "x" }));
});

test("file card while the panel is open: a tab is added, the active tab keeps the focus, the new tab has a dot", () => {
  const store = fp.createTabStore(memory());
  const reading = store.open({ root: "uploads", path: "a.md", agent: "sage" }).tab;
  const result = store.receive({ root: "uploads", path: "x.pdf", name: "x.pdf", kind: "pdf", agent: "sage" }, { panelOpen: true });
  assert.equal(result.created, true);
  assert.equal(store.active, reading.id);
  assert.equal(result.tab.dot, true);
  assert.equal(result.buttonDot, false);
  store.focus(result.tab.id);
  assert.equal(result.tab.dot, false);
});

test("file card while the panel is closed: the button gets the dot; the tab is still kept", () => {
  const store = fp.createTabStore(memory());
  const result = store.receive({ root: "uploads", path: "x.pdf", agent: "sage" }, { panelOpen: false });
  assert.equal(result.buttonDot, true);
  assert.equal(store.tabs.length, 1);
  assert.equal(store.active, "files");
});

test("a card for the tab already shown adds no dot", () => {
  const store = fp.createTabStore(memory());
  const tab = store.open({ root: "uploads", path: "a.md", agent: "sage" }).tab;
  store.receive({ root: "uploads", path: "a.md", agent: "sage" }, { panelOpen: true });
  assert.equal(tab.dot, false);
});

function response(status, body) {
  return { ok: status < 400, status, json: async () => body, text: async () => JSON.stringify(body) };
}

test("approval_required: asks once, posts the grant and retries", async () => {
  const calls = [];
  let granted = false;
  const fetchFn = async (url, options = {}) => {
    calls.push(`${options.method || "GET"} ${url}`);
    if (url === "/api/files/grant?agent=example_agent") { granted = true; assert.equal(options.body, JSON.stringify({ backend: "orion" })); return response(200, { ok: true }); }
    if (!granted) return response(403, { error: "approval_required", backend: "orion" });
    return response(200, { root: "backend:orion", path: "", entries: [], truncated: false });
  };
  const questions = [];
  const client = fp.createFilesClient({ fetchFn, confirm: async (o) => { questions.push(o.message); return true; } });
  const data = await client.list("example_agent", "backend:orion", "");
  assert.equal(data.truncated, false);
  assert.deepEqual(questions, ["Allow read-only browsing of orion for this session?"]);
  assert.equal(calls.length, 3);
  assert.equal(calls[1], "POST /api/files/grant?agent=example_agent");
  assert.match(calls[0], /root=backend%3Aorion/);
});

test("approval_required: declining does not post a grant and surfaces an error", async () => {
  const calls = [];
  const fetchFn = async (url) => { calls.push(url); return response(403, { error: "approval_required", backend: "orion" }); };
  const client = fp.createFilesClient({ fetchFn, confirm: async () => false });
  await assert.rejects(client.list("a", "backend:orion", ""), (e) => e.code === "declined" && e.status === 403);
  assert.equal(calls.length, 1);
});

test("approval_required: parallel requests share one question; a second refusal does not loop", async () => {
  let granted = false;
  let asked = 0;
  const fetchFn = async (url) => {
    if (url === "/api/files/grant?agent=a") { granted = true; return response(200, {}); }
    return granted ? response(200, { entries: [] }) : response(403, { error: "approval_required", backend: "atlas" });
  };
  const client = fp.createFilesClient({ fetchFn, confirm: async () => { asked++; return true; } });
  await Promise.all([client.list("a", "backend:atlas", ""), client.info("a", "backend:atlas", "x")]);
  assert.equal(asked, 1);
  const stubborn = fp.createFilesClient({
    fetchFn: async (url) => (url === "/api/files/grant?agent=a" ? response(200, {}) : response(403, { error: "approval_required", backend: "atlas" })),
    confirm: async () => true,
  });
  await assert.rejects(stubborn.list("a", "backend:atlas", ""), (e) => e.code === "approval_required");
});

test("other errors keep their message and status", async () => {
  const client = fp.createFilesClient({ fetchFn: async () => response(404, { error: "not found" }), confirm: async () => true });
  await assert.rejects(client.info("a", "uploads", "x"), (e) => e.status === 404 && e.message === "not found");
});

test("urls carry agent, root, path and the download flag", () => {
  const client = fp.createFilesClient({ fetchFn: async () => response(200, {}), confirm: async () => true });
  const url = client.contentUrl("nova", "uploads", "a b/c.pdf", { download: 1 });
  assert.equal(url, "/api/files/content?agent=nova&root=uploads&path=a+b%2Fc.pdf&download=1");
  assert.match(client.pdfPageUrl("nova", "uploads", "c.pdf", 2, 600), /page=2&width=600/);
});
