// Enter in the composer: sends on desktop, writes a new line on touch screens (no Shift key there).
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "..", "src", "web", "static", "app.js"), "utf8");
const match = source.match(/function enterSends\([\s\S]*?\n}\n/);
assert.ok(match, "enterSends not found in app.js");
const context = vm.createContext({});
vm.runInContext(`${match[0]}\nthis.enterSends = enterSends;`, context);

test("desktop: Enter sends, Shift+Enter and IME composition do not", () => {
  assert.equal(context.enterSends({ key: "Enter" }, false), true);
  assert.equal(context.enterSends({ key: "Enter", shiftKey: true }, false), false);
  assert.equal(context.enterSends({ key: "Enter", isComposing: true }, false), false);
  assert.equal(context.enterSends({ key: "a" }, false), false);
});

test("touch screens: Enter never sends (the Send button does)", () => {
  assert.equal(context.enterSends({ key: "Enter" }, true), false);
});
