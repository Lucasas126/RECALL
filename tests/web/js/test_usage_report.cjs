const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");

const source = readFileSync(path.join(__dirname, "..", "..", "..", "src", "web", "static", "app.js"), "utf8");
const fn = source.match(/function lastUsageReport\([\s\S]*?\r?\n\}\r?\n/)[0];
const context = vm.createContext({});
vm.runInContext(`${fn}\nthis.lastUsageReport = lastUsageReport;`, context);

test("a chat gets its own last known report, never another chat's", () => {
  const usages = new Map([["a\u0000one", "report one"]]);
  assert.equal(context.lastUsageReport(usages, "a\u0000one"), "report one");
  assert.equal(context.lastUsageReport(usages, "a\u0000two"), null);
});

test("no chat open means no report", () => {
  assert.equal(context.lastUsageReport(new Map([["k", "r"]]), null), null);
});
