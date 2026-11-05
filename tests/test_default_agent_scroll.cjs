const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");

// defaultAgentId and nearBottom live in the pure settings-helpers block of app.js, loaded here without the DOM
const source = readFileSync(path.join(__dirname, "..", "src", "web", "static", "app.js"), "utf8");
const code = source.match(/\/\/ ---- settings helpers[\s\S]*?\/\/ ---- end settings helpers/)[0];
const context = vm.createContext({});
vm.runInContext(`${code}\nthis.api = { defaultAgentId, nearBottom, SETTINGS_SECTIONS, NULLABLE_PREFERENCES, settingsChanges };`, context);
const api = context.api;

const agents = [{ id: "alpha", name: "Alpha" }, { id: "beta", name: "Beta" }];

test("defaultAgentId returns the saved agent while it is still in the list", () => {
  assert.equal(api.defaultAgentId({ default_agent_id: "beta" }, agents), "beta");
});

test("defaultAgentId falls back to the first agent when none is saved or the saved one is gone", () => {
  assert.equal(api.defaultAgentId({ default_agent_id: null }, agents), "alpha");
  assert.equal(api.defaultAgentId({}, agents), "alpha");
  assert.equal(api.defaultAgentId({ default_agent_id: "deleted" }, agents), "alpha");
  assert.equal(api.defaultAgentId(null, agents), "alpha");
});

test("defaultAgentId is null without agents", () => {
  assert.equal(api.defaultAgentId({ default_agent_id: "alpha" }, []), null);
  assert.equal(api.defaultAgentId({}, undefined), null);
});

test("default_agent_id is a nullable General setting; picking the first-agent option resets it", () => {
  assert.ok(api.SETTINGS_SECTIONS.general.includes("default_agent_id"));
  assert.ok(api.NULLABLE_PREFERENCES.has("default_agent_id"));
  const changes = api.settingsChanges({ default_agent_id: null }, { default_agent_id: "beta" }, { default_agent_id: null });
  assert.deepEqual(JSON.parse(JSON.stringify(changes)), { default_agent_id: null });
});

test("nearBottom is true at the bottom and within the slack", () => {
  assert.equal(api.nearBottom(1000, 600, 400), true);
  assert.equal(api.nearBottom(1000, 530, 400), true);
  assert.equal(api.nearBottom(1000, 520, 400), true);
});

test("nearBottom is false once scrolled up past the slack", () => {
  assert.equal(api.nearBottom(1000, 519, 400), false);
  assert.equal(api.nearBottom(1000, 0, 400), false);
});

test("nearBottom accepts a custom slack and treats short content as at the bottom", () => {
  assert.equal(api.nearBottom(1000, 500, 400, 100), true);
  assert.equal(api.nearBottom(1000, 400, 400, 100), false);
  assert.equal(api.nearBottom(300, 0, 400), true);
});
