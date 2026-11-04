const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");

// the disposable-chat banner state and expiry date, loaded without the DOM
const source = readFileSync(path.join(__dirname, "..", "src", "web", "static", "app.js"), "utf8");
const take = (name) => source.match(new RegExp(`function ${name}\\([\\s\\S]*?\\r?\\n\\}\\r?\\n`))[0];
const context = vm.createContext({ tr: (text, values = {}) => text.replace(/\{(\w+)\}/g, (_, key) => values[key]) });
vm.runInContext(
  `${take("expiryDate")}\n${take("disposableView")}\nthis.expiryDate = expiryDate; this.disposableView = disposableView;`,
  context,
);

test("a disposable chat shows the banner with its deletion date and no make-disposable action", () => {
  const view = context.disposableView({ id: "c", disposable: true, expires_at: "2026-12-04T10:00:00+00:00" });
  assert.match(view.banner, /^Disposable chat: it will be deleted on .*2026.* and nothing here will be saved to the agent's memory\.$/);
  assert.equal(view.canMakeDisposable, false);
});

test("a normal chat has no banner and offers the make-disposable action", () => {
  assert.deepEqual({ ...context.disposableView({ id: "c", disposable: false, expires_at: null }) }, { banner: null, canMakeDisposable: true });
});

test("no open chat shows nothing; a bad date formats to an empty string", () => {
  assert.deepEqual({ ...context.disposableView(null) }, { banner: null, canMakeDisposable: false });
  assert.equal(context.expiryDate("not a date"), "");
});
