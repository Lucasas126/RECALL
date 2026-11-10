const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");

// the avatar helpers of app.js, loaded without the page: what to draw, which files may be uploaded, and the DOM it makes
const source = readFileSync(path.join(__dirname, "..", "src", "web", "static", "app.js"), "utf8");
const take = (name) => source.match(new RegExp(`function ${name}\\([\\s\\S]*?\\r?\\n\\}\\r?\\n`))[0];
const constant = (name) => source.match(new RegExp(`const ${name} = [^\\n]*;`))[0];

class FakeElement {
  constructor(tag) { this.tag = tag; this.dataset = {}; this.children = []; this.listeners = {}; this.alt = undefined; this.draggable = undefined; }
  addEventListener(name, fn) { this.listeners[name] = fn; }
  replaceChildren(...nodes) { this.children = nodes; }
}
const context = vm.createContext({ document: { createElement: (tag) => new FakeElement(tag) } });
vm.runInContext(
  [constant("AVATAR_MAX_BYTES"), constant("AVATAR_TYPES"), take("avatarView"), take("avatarFileProblem"), take("renderAvatar"),
    "this.avatarView = avatarView; this.avatarFileProblem = avatarFileProblem; this.renderAvatar = renderAvatar;"].join("\n"),
  context,
);
const plain = (value) => JSON.parse(JSON.stringify(value));

test("without an avatar the first letter of the name is drawn, upper-cased", () => {
  assert.deepEqual(plain(context.avatarView(null, "ana")), { src: null, free: false, letter: "A" });
  assert.deepEqual(plain(context.avatarView(undefined, "ñandú")), { src: null, free: false, letter: "Ñ" });
  assert.deepEqual(plain(context.avatarView(null, "")), { src: null, free: false, letter: "?" });
  assert.deepEqual(plain(context.avatarView(null, undefined)), { src: null, free: false, letter: "?" });
  assert.equal(context.avatarView(null, "😀 smile").letter, "😀");
});

test("the image url carries the version so a new upload replaces the cached one", () => {
  const view = context.avatarView({ url: "/api/agents/bot/avatar", version: "1a2b", style: "circle" }, "Bot");
  assert.deepEqual(plain(view), { src: "/api/agents/bot/avatar?v=1a2b", free: false, letter: "B" });
  assert.equal(context.avatarView({ url: "/api/me/avatar", version: "a b&c", style: "circle" }, "x").src, "/api/me/avatar?v=a%20b%26c");
  assert.equal(context.avatarView({ url: "/api/me/avatar" }, "x").src, "/api/me/avatar?v=");
});

test("the free style is reported only for style free", () => {
  assert.equal(context.avatarView({ url: "/api/me/avatar", version: "1", style: "free" }, "x").free, true);
  assert.equal(context.avatarView({ url: "/api/me/avatar", version: "1", style: "circle" }, "x").free, false);
  assert.equal(context.avatarView({ url: "/api/me/avatar", version: "1" }, "x").free, false);
});

test("a url outside the API is never used as an image source", () => {
  for (const url of ["https://example.com/x.png", "javascript:alert(1)", "data:image/png;base64,AA", "//example.com/x.png", 5, null]) {
    assert.equal(context.avatarView({ url, version: "1", style: "circle" }, "x").src, null, String(url));
  }
});

test("only png, jpeg and webp up to 2 MiB pass the client check", () => {
  for (const type of ["image/png", "image/jpeg", "image/webp"]) assert.equal(context.avatarFileProblem({ type, size: 1000 }), null);
  assert.equal(context.avatarFileProblem({ type: "image/png", size: 2 * 1024 * 1024 }), null);
  assert.deepEqual(plain(context.avatarFileProblem({ type: "image/png", size: 2 * 1024 * 1024 + 1 })),
    { template: "The image is larger than {limit} MiB.", values: { limit: 2 } });
  for (const type of ["image/gif", "image/svg+xml", "", "text/plain"]) {
    assert.deepEqual(plain(context.avatarFileProblem({ type, size: 10 })), { template: "Use a PNG, JPEG or WEBP image.", values: {} });
  }
});

test("renderAvatar draws a decorative, undraggable image, or the letter", () => {
  const element = new FakeElement("span");
  context.renderAvatar(element, { url: "/api/agents/bot/avatar", version: "7", style: "circle" }, "Bot");
  const [image] = element.children;
  assert.equal(image.tag, "img");
  assert.equal(image.alt, "");
  assert.equal(image.draggable, false);
  assert.equal(image.src, "/api/agents/bot/avatar?v=7");
  assert.equal(element.dataset.avatarStyle, undefined);
  context.renderAvatar(element, { url: "/api/agents/bot/avatar", version: "8", style: "free" }, "Bot");
  assert.equal(element.dataset.avatarStyle, "free");
  context.renderAvatar(element, null, "Bot");
  assert.deepEqual(plain(element.children), ["B"]);
  assert.equal(element.dataset.avatarStyle, undefined);
});

test("a picture that fails to load falls back to the letter, without the free style", () => {
  const element = new FakeElement("span");
  context.renderAvatar(element, { url: "/api/me/avatar", version: "1", style: "free" }, "Zoe");
  element.children[0].listeners.error();
  assert.deepEqual(plain(element.children), ["Z"]);
  assert.equal(element.dataset.avatarStyle, undefined);
});
