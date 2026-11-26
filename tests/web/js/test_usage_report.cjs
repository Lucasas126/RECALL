const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");

const source = readFileSync(path.join(__dirname, "..", "..", "..", "src", "web", "static", "app.js"), "utf8");
const fn = source.match(/function lastUsageReport\([\s\S]*?\r?\n\}\r?\n/)[0];
const header = source.match(/function usageHeader\([\s\S]*?\r?\n\}\r?\n/)[0];
const providers = source.match(/function usageProviders\([\s\S]*?\r?\n\}\r?\n/)[0];
const context = vm.createContext({ tr: (text) => text });
vm.runInContext(`${fn}\n${header}\n${providers}\nthis.lastUsageReport = lastUsageReport; this.usageHeader = usageHeader; this.usageProviders = usageProviders;`, context);

test("a chat gets its own last known report, never another chat's", () => {
  const usages = new Map([["a\u0000one", "report one"]]);
  assert.equal(context.lastUsageReport(usages, "a\u0000one"), "report one");
  assert.equal(context.lastUsageReport(usages, "a\u0000two"), null);
});

test("no chat open means no report", () => {
  assert.equal(context.lastUsageReport(new Map([["k", "r"]]), null), null);
});

test("the usage header names the subscription, the account and the plan, leaving out what is missing", () => {
  const full = { label: "Claude subscription", account_email: "user@example.com", plan: "pro" };
  assert.equal(context.usageHeader(full), "Claude subscription · user@example.com · Pro");
  assert.equal(context.usageHeader({ label: "Claude subscription" }), "Claude subscription");
  assert.equal(context.usageHeader({ windows: [] }), "");
});

test("only an available report with windows is listed as a provider", () => {
  const report = { available: true, windows: [{ key: "week", percent: 1 }] };
  assert.equal(context.usageProviders(report).length, 1);
  assert.equal(context.usageProviders({ available: false, reason: "expired" }).length, 0);
  assert.equal(context.usageProviders(null).length, 0);
});
