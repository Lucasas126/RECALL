const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");

// the stored panel preference helpers, loaded without the DOM
const source = readFileSync(path.join(__dirname, "..", "..", "..", "src", "web", "static", "app.js"), "utf8");
const code = source.match(/const PANEL_KEY[\s\S]*?function writePanelCollapsed[\s\S]*?\r?\n\}\r?\n/)[0];
const context = vm.createContext({});
vm.runInContext(`${code}\nthis.read = readPanelCollapsed; this.write = writePanelCollapsed;`, context);
const memory = (initial = {}) => ({ data: { ...initial }, getItem(k) { return k in this.data ? this.data[k] : null; }, setItem(k, v) { this.data[k] = String(v); } });

test("the panel is expanded by default", () => {
  assert.equal(context.read(memory()), false);
});

test("the choice round-trips through storage", () => {
  const storage = memory();
  context.write(storage, true);
  assert.equal(context.read(storage), true);
  context.write(storage, false);
  assert.equal(context.read(storage), false);
});

test("unavailable storage falls back to expanded without throwing", () => {
  const broken = { getItem() { throw new Error("denied"); }, setItem() { throw new Error("denied"); } };
  assert.equal(context.read(broken), false);
  assert.doesNotThrow(() => context.write(broken, true));
});
