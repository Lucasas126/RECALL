const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");

// pure helpers of the model picker's provider tabs, loaded without the DOM
const source = readFileSync(path.join(__dirname, "..", "src", "web", "static", "app.js"), "utf8");
function helpers() {
  const pick = (re) => source.match(re)[0];
  const code = [
    pick(/const ANY_PROVIDER = "any";/),
    pick(/function withAnyProvider\(rows\) \{[\s\S]*?\r?\n\}\r?\n/),
    pick(/function pickInitialProvider\([\s\S]*?\r?\n\}\r?\n/),
  ].join("\n");
  const context = vm.createContext({});
  vm.runInContext(`${code}\nthis.withAnyProvider = withAnyProvider; this.pickInitialProvider = pickInitialProvider;`, context);
  return context;
}

const rows = (...flags) => ["openrouter", "ollama", "claude_subscription"].map((id, i) => ({ id, available: flags[i] }));

test("Any is the first tab and is available when any provider is", () => {
  const { withAnyProvider } = helpers();
  assert.deepEqual(JSON.parse(JSON.stringify(withAnyProvider(rows(false, true, false)).map((r) => [r.id, r.available]))),
    [["any", true], ["openrouter", false], ["ollama", true], ["claude_subscription", false]]);
  assert.equal(withAnyProvider(rows(false, false, false))[0].available, false);
});

test("the picker opens on the selected model's provider unless the user last chose Any", () => {
  const { withAnyProvider, pickInitialProvider } = helpers();
  const all = withAnyProvider(rows(true, true, true));
  assert.equal(pickInitialProvider(all, "ollama", "openrouter", "openrouter"), "ollama");
  assert.equal(pickInitialProvider(all, "ollama", "any", "openrouter"), "any");
  assert.equal(pickInitialProvider(all, null, "claude_subscription", "openrouter"), "claude_subscription");
  assert.equal(pickInitialProvider(all, null, null, "openrouter"), "openrouter");
});

test("Any is ignored when unusable or as a fallback", () => {
  const { withAnyProvider, pickInitialProvider } = helpers();
  assert.equal(pickInitialProvider(withAnyProvider(rows(false, false, false)), null, "any", "openrouter"), null);
  assert.equal(pickInitialProvider(withAnyProvider(rows(false, true, false)), null, null, "openrouter"), "ollama");
  // speech-to-text rows have no Any tab, so a remembered Any falls through
  assert.equal(pickInitialProvider(rows(true, false, false).slice(0, 1), null, "any", "openrouter"), "openrouter");
});
