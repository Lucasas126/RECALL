const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");

// the version line helper, loaded without the DOM
const source = readFileSync(path.join(__dirname, "..", "src", "web", "static", "app.js"), "utf8");
const code = source.match(/\/\/ ---- version line[\s\S]*?\/\/ ---- end version line/)[0];
const context = vm.createContext({});
vm.runInContext(`${code}\nthis.versionText = versionText;`, context);
const tr = (template, values = {}) => template.replace(/\{(\w+)\}/g, (_, k) => values[k]);

test("shows the version, and the build only when set", () => {
  assert.equal(context.versionText({ version: "0.1.0", build: null }, tr), "RECALL v0.1.0");
  assert.equal(context.versionText({ version: "0.1.0", build: "abc1234" }, tr), "RECALL v0.1.0 (abc1234)");
});

test("shows nothing before the version is known", () => {
  assert.equal(context.versionText(null, tr), "");
  assert.equal(context.versionText({}, tr), "");
});
