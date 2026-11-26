const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");

// the code block helpers of app.js (copy button, syntax colours), run against a tiny fake DOM
const root = path.join(__dirname, "..", "..", "..", "src", "web", "static");
const source = readFileSync(path.join(root, "app.js"), "utf8");
const code = source.match(/\/\/ ---- code blocks[\s\S]*?\/\/ ---- end code blocks/)[0];

class FakeElement {
  constructor(tag) {
    this.tag = tag; this.children = []; this.parentElement = null; this.dataset = {}; this.attrs = {};
    this.className = ""; this.style = {}; this._text = ""; this.removed = false;
    const owner = this;
    this.classList = {
      contains: (name) => owner.className.split(/\s+/).includes(name),
      add: (name) => { if (!owner.classList.contains(name)) owner.className = `${owner.className} ${name}`.trim(); },
    };
  }
  setAttribute(name, value) { this.attrs[name] = value; }
  append(...nodes) { for (const n of nodes) { n.parentElement?.children.splice(n.parentElement.children.indexOf(n), 1); n.parentElement = this; this.children.push(n); } }
  before(node) { const list = this.parentElement.children; list.splice(list.indexOf(this), 0, node); node.parentElement = this.parentElement; }
  remove() { this.removed = true; const list = this.parentElement?.children; if (list) list.splice(list.indexOf(this), 1); }
  all() { return this.children.flatMap((c) => [c, ...c.all()]); }
  querySelectorAll(tag) { return this.all().filter((n) => n.tag === tag); }
  querySelector(tag) { return this.querySelectorAll(tag)[0] || null; }
  closest(selector) {
    const name = selector.replace(".", "");
    for (let n = this; n; n = n.parentElement) if (n.classList.contains(name)) return n;
    return null;
  }
  select() {} focus() {}
  get textContent() { return this.children.length ? this.children.map((c) => c.textContent).join("") : this._text; }
  set textContent(value) { this.children = []; this._text = String(value); }
  set innerHTML(html) { this.children = []; this._text = html.replace(/<[^>]+>/g, "").replace(/&quot;/g, '"').replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&amp;/g, "&"); this.html = html; }
}
function fakeDoc(execResult = true) {
  const doc = { body: new FakeElement("body"), activeElement: null, copied: null, execCommand(cmd) {
    const box = doc.body.querySelector("textarea");
    if (!box || cmd !== "copy") return false;
    doc.copied = box.value;
    return execResult;
  }, createElement: (tag) => {
    const el = new FakeElement(tag);
    if (tag === "textarea") Object.defineProperty(el, "value", { value: "", writable: true });
    return el;
  } };
  return doc;
}
function pre(text, language) {
  const p = new FakeElement("pre");
  const c = new FakeElement("code");
  if (language) c.className = `language-${language}`;
  c.textContent = text;
  p.append(c);
  return p;
}
const context = vm.createContext({ setTimeout, clearTimeout, Number, String, Boolean, Promise });
vm.runInContext(`${code}\nthis.api = { codeLanguage, endsInOpenFence, highlightCode, decorateCodeBlocks, copyToClipboard, copyCodeBlock };`, context);
const api = context.api;
const translate = (text) => text;
const hljsContext = vm.createContext({ window: {} });
vm.runInContext(readFileSync(path.join(root, "vendor", "highlight.min.js"), "utf8"), hljsContext);
const hljs = hljsContext.window.hljs;
const toHtml = (text, language) => (hljs.getLanguage(language) ? hljs.highlight(text, { language }).value : null);

test("the language comes from the class, empty when there is none", () => {
  assert.equal(api.codeLanguage(pre("x", "Python").children[0]), "python");
  assert.equal(api.codeLanguage(pre("x").children[0]), "");
});

test("every block is wrapped once, with its language and a Copy button", () => {
  const body = new FakeElement("div");
  body.append(pre("print(1)", "python"), pre("plain"), pre("a@example.com", "text"));
  const options = { doc: fakeDoc(), translate, toHtml };
  api.decorateCodeBlocks(body, options);
  api.decorateCodeBlocks(body, options);   // a re-render pass over the same nodes adds nothing
  const wrappers = body.querySelectorAll("div").filter((n) => n.classList.contains("code-block"));
  assert.equal(wrappers.length, 3);
  assert.deepEqual(body.querySelectorAll("span").map((n) => n.textContent), ["python", "text", "text"]);
  const buttons = body.querySelectorAll("button");
  assert.equal(buttons.length, 3);
  assert.equal(buttons[0].type, "button");
  assert.equal(buttons[0].textContent, "Copy");
  assert.equal(buttons[0].attrs["aria-label"], "Copy code");
});

test("inline code gets no button", () => {
  const body = new FakeElement("div");
  const p = new FakeElement("p");
  p.append(new FakeElement("code"));
  body.append(p);
  api.decorateCodeBlocks(body, { doc: fakeDoc(), translate, toHtml });
  assert.equal(body.querySelectorAll("button").length, 0);
});

test("only known languages are coloured, and only once", () => {
  const known = pre("def f():\n  return 1", "python").children[0];
  const unknown = pre("def f(): return 1", "madeup").children[0];
  const none = pre("def f(): return 1").children[0];
  assert.equal(api.highlightCode(known, toHtml), true);
  assert.match(known.html, /hljs-keyword/);
  assert.equal(api.highlightCode(known, toHtml), false);
  assert.equal(api.highlightCode(unknown, toHtml), false);
  assert.equal(api.highlightCode(none, toHtml), false);
  assert.equal(unknown.html, undefined);
  assert.equal(none.html, undefined);
});

test("colouring keeps the text and escapes markup", () => {
  const block = pre('<script>alert("x")</script>', "html").children[0];
  api.highlightCode(block, toHtml);
  assert.ok(!block.html.includes("<script>"));
  assert.equal(block.textContent, '<script>alert("x")</script>');
});

test("an unfinished last fence is left alone while streaming", () => {
  assert.equal(api.endsInOpenFence("text\n```python\nx = 1"), true);
  assert.equal(api.endsInOpenFence("```python\nx = 1\n```\nmore"), false);
  const body = new FakeElement("div");
  body.append(pre("x = 1", "python"), pre("y = 2", "python"));
  api.decorateCodeBlocks(body, { doc: fakeDoc(), translate, toHtml, holdLast: true });
  const [first, second] = body.querySelectorAll("code");
  assert.ok(first.html);
  assert.equal(second.html, undefined);
});

test("copy takes the code text only, not the header", async () => {
  const body = new FakeElement("div");
  body.append(pre("echo hi\nls", "bash"));
  const doc = fakeDoc();
  api.decorateCodeBlocks(body, { doc, translate, toHtml });
  const button = body.querySelector("button");
  let written = null;
  const timers = [];
  const env = { nav: { clipboard: { writeText: async (t) => { written = t; } } }, doc, secure: true, translate, later: (fn) => timers.push(fn) };
  assert.equal(await api.copyCodeBlock(button, env), true);
  assert.equal(written, "echo hi\nls");
  assert.equal(button.textContent, "Copied");
  timers[0]();
  assert.equal(button.textContent, "Copy");
});

test("without navigator.clipboard the hidden textarea is used and removed", async () => {
  const body = new FakeElement("div");
  body.append(pre("secret = 1", "python"));
  const doc = fakeDoc();
  api.decorateCodeBlocks(body, { doc, translate, toHtml });
  const button = body.querySelector("button");
  const env = { nav: {}, doc, secure: false, translate, later: () => 1 };
  assert.equal(await api.copyCodeBlock(button, env), true);
  assert.equal(doc.copied, "secret = 1");
  assert.equal(doc.body.querySelectorAll("textarea").length, 0);
});

test("a clipboard that refuses falls back, and a failed copy says so", async () => {
  const body = new FakeElement("div");
  body.append(pre("x", "python"));
  const doc = fakeDoc(false);
  api.decorateCodeBlocks(body, { doc, translate, toHtml });
  const button = body.querySelector("button");
  const nav = { clipboard: { writeText: async () => { throw new Error("denied"); } } };
  assert.equal(await api.copyCodeBlock(button, { nav, doc, secure: true, translate, later: () => 1 }), false);
  assert.equal(doc.copied, "x");
  assert.equal(button.textContent, "Failed to copy");
});
