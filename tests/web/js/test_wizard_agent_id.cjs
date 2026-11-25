const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");

// the agent id the onboarding wizard suggests for a template; it must always pass the backend id rule
const source = readFileSync(path.join(__dirname, "..", "..", "..", "src", "web", "static", "app.js"), "utf8");
const take = (name) => source.match(new RegExp(`function ${name}\\([\\s\\S]*?\\r?\\n\\}\\r?\\n`))[0];
const context = vm.createContext({});
vm.runInContext(`${take("suggestAgentId")}\nthis.suggestAgentId = suggestAgentId;`, context);
const ID = /^[A-Za-z0-9_-]{1,64}$/;

test("the default template suggests assistant", () => {
  assert.equal(context.suggestAgentId("default"), "assistant");
  assert.equal(context.suggestAgentId(""), "assistant");
  assert.equal(context.suggestAgentId(undefined), "assistant");
});

test("other templates keep their id", () => {
  assert.equal(context.suggestAgentId("guide"), "guide");
  assert.equal(context.suggestAgentId("permission-manager"), "permission-manager");
});

test("every suggestion passes the agent id rule", () => {
  for (const template of ["we ird/na.me", "ñandú", "---", "a".repeat(100), "  x  ", "!!!"]) {
    assert.match(context.suggestAgentId(template), ID, template);
  }
});
