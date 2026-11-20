const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");

// the stored header height helpers, loaded without the DOM
const source = readFileSync(path.join(__dirname, "..", "..", "..", "src", "web", "static", "app.js"), "utf8");
const code = source.match(/const HEADER_KEY[\s\S]*?function writeHeaderHeight[\s\S]*?\r?\n\}\r?\n/)[0];
const context = vm.createContext({});
vm.runInContext(`${code}\nthis.clamp = clampHeaderHeight; this.read = readHeaderHeight; this.write = writeHeaderHeight;`, context);
const memory = (initial = {}) => ({ data: { ...initial }, getItem(k) { return k in this.data ? this.data[k] : null; }, setItem(k, v) { this.data[k] = String(v); }, removeItem(k) { delete this.data[k]; } });

test("the height is clamped between today's height and the maximum", () => {
  assert.equal(context.clamp(10), 60);
  assert.equal(context.clamp(500), 220);
  assert.equal(context.clamp(100.4), 100);
  assert.equal(context.clamp("abc"), 60);
});

test("nothing is stored by default", () => {
  assert.equal(context.read(memory()), null);
});

test("the height round-trips through storage, clamped, and can be reset", () => {
  const storage = memory();
  context.write(storage, 140);
  assert.equal(context.read(storage), 140);
  context.write(storage, 900);
  assert.equal(context.read(storage), 220);
  context.write(storage, null);
  assert.equal(context.read(storage), null);
});

test("unavailable storage does not throw", () => {
  const broken = { getItem() { throw new Error("denied"); }, setItem() { throw new Error("denied"); }, removeItem() { throw new Error("denied"); } };
  assert.equal(context.read(broken), null);
  assert.doesNotThrow(() => context.write(broken, 100));
  assert.doesNotThrow(() => context.write(broken, null));
});
