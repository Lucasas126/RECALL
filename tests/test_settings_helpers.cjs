const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");

// the pure settings helpers (defaults, reset logic, number parsing, section state), loaded without the DOM
const source = readFileSync(path.join(__dirname, "..", "src", "web", "static", "app.js"), "utf8");
const code = source.match(/\/\/ ---- settings helpers[\s\S]*?\/\/ ---- end settings helpers/)[0];
const context = vm.createContext({});
vm.runInContext(`${code}
this.api = { SETTINGS_SECTIONS, settingsSectionView, settingsSectionOf, sectionChangeCounts, sameValue,
  differsFromDefault, parseNumberInput, clampNumber, stepNumber, formatNumber, rangeText, preferenceChange, settingsChanges };`, context);
const api = context.api;
const plain = (value) => JSON.parse(JSON.stringify(value));
const translate = (text, values = {}) => text.replace(/\{(\w+)\}/g, (_, key) => values[key]);

test("null, undefined and empty text are the same 'no value'; numbers compare numerically", () => {
  assert.equal(api.sameValue(null, ""), true);
  assert.equal(api.sameValue(undefined, null), true);
  assert.equal(api.sameValue(3, "3"), true);
  assert.equal(api.sameValue(3, 4), false);
  assert.equal(api.sameValue(false, null), false);
  assert.equal(api.sameValue("a", "a"), true);
});

test("a value differs from its default only when it is not the default", () => {
  assert.equal(api.differsFromDefault(3, 3), false);
  assert.equal(api.differsFromDefault(5, 3), true);
  assert.equal(api.differsFromDefault(null, null), false);
  assert.equal(api.differsFromDefault(30, null), true);
  assert.equal(api.differsFromDefault(Number.NaN, 3), true);
});

test("number text: empty, plain, thousands separators, and rejected formats", () => {
  const spec = { min: 1000, max: 10000000 };
  assert.deepEqual(plain(api.parseNumberInput("", spec)), { empty: true, value: null, error: null });
  assert.equal(api.parseNumberInput("500000", spec).value, 500000);
  for (const text of ["500,000", "500.000", "500 000", " 500000 "]) assert.equal(api.parseNumberInput(text, spec).value, 500000, text);
  for (const text of ["2.5", "abc", "-3", "1e3", "12,34"]) assert.equal(api.parseNumberInput(text, spec).error, "format", text);
});

test("bounds are reported with the number, so the caller can clamp", () => {
  const spec = { min: 1, max: 20 };
  assert.deepEqual(plain(api.parseNumberInput("0", spec)), { empty: false, value: 0, error: "min" });
  assert.deepEqual(plain(api.parseNumberInput("21", spec)), { empty: false, value: 21, error: "max" });
  assert.equal(api.parseNumberInput("20", spec).error, null);
  assert.equal(api.parseNumberInput("99999", {}).error, null); // no bounds known: anything numeric passes
  assert.equal(api.clampNumber(0, spec), 1);
  assert.equal(api.clampNumber(99, spec), 20);
  assert.equal(api.clampNumber(7, spec), 7);
  assert.equal(api.clampNumber(7, { min: 1 }), 7);
});

test("the stepper moves by the step, stops at the bounds and starts from the start, default or minimum", () => {
  const spec = { min: 1, max: 20, default: 3 };
  assert.equal(api.stepNumber(3, 1, spec), 4);
  assert.equal(api.stepNumber(20, 1, spec), 20);
  assert.equal(api.stepNumber(1, -1, spec), 1);
  assert.equal(api.stepNumber(500000, 1, { min: 1000, max: 10000000, step: 10000 }), 510000);
  assert.equal(api.stepNumber(null, 1, spec), 3); // empty shows the default first
  assert.equal(api.stepNumber(null, 1, { min: 1 }), 1);
  assert.equal(api.stepNumber(null, 1, { min: 1, start: 1024, step: 256 }), 1024);
  assert.equal(api.stepNumber(null, -1, {}), 0);
});

test("big numbers get thousands separators in the interface language", () => {
  assert.equal(api.formatNumber(500000, "en"), "500,000");
  assert.equal(api.formatNumber(500000, "es"), "500.000");
  assert.equal(api.formatNumber(30, "en"), "30");
  assert.equal(api.parseNumberInput(api.formatNumber(10000000, "es")).value, 10000000);
});

test("the allowed range hint depends on the bounds the backend reports", () => {
  assert.equal(api.rangeText({ min: 1, max: 20 }, translate), "Allowed: 1–20");
  assert.equal(api.rangeText({ min: 1, max: null }, translate), "Minimum: 1");
  assert.equal(api.rangeText({ min: null, max: 9 }, translate), "Maximum: 9");
  assert.equal(api.rangeText({}, translate), "");
  assert.equal(api.rangeText({ min: 1000, max: 10000000 }, translate), "Allowed: 1,000–10,000,000");
});

test("saving a nullable preference at its default resets it; other values are sent as they are", () => {
  assert.equal(api.preferenceChange("associative_max_matches", 3, 7, 3), null); // reset: follow the default
  assert.equal(api.preferenceChange("associative_max_matches", 5, 3, 3), 5);
  assert.equal(api.preferenceChange("associative_max_matches", 3, 3, 3), undefined); // unchanged
  assert.equal(api.preferenceChange("orchestrator", false, true, false), null);
  assert.equal(api.preferenceChange("core_agent_id", null, "helper", null), null);
  assert.equal(api.preferenceChange("show_thinking", true, false, true), true); // never null: it always holds a value
  assert.equal(api.preferenceChange("language", "es", "en", "es"), "es");
});

test("the settings PATCH holds only what changed", () => {
  const original = { show_thinking: true, language: "en", associative_max_matches: 3, disposable_retention_days: 10, orchestrator: false, core_agent_id: null };
  const defaults = { show_thinking: true, language: "en", associative_max_matches: 3, disposable_retention_days: 30, orchestrator: false, core_agent_id: null };
  assert.deepEqual(plain(api.settingsChanges({ ...original }, original, defaults)), {});
  assert.deepEqual(
    plain(api.settingsChanges({ ...original, associative_max_matches: 8, disposable_retention_days: 30, orchestrator: true }, original, defaults)),
    { associative_max_matches: 8, disposable_retention_days: null, orchestrator: true },
  );
});

test("section state: one section is visible, an unknown one falls back to the first", () => {
  const view = api.settingsSectionView("telegram");
  assert.deepEqual(Object.keys(plain(view)), ["general", "models", "chats", "telegram", "connections"]);
  assert.deepEqual(Object.keys(view).filter((name) => !view[name].hidden), ["telegram"]);
  assert.equal(view.telegram.pressed, true);
  assert.equal(api.settingsSectionView("nope").general.hidden, false);
});

test("changes are counted per section for the tab dots", () => {
  assert.equal(api.settingsSectionOf("disposable_retention_days"), "chats");
  assert.equal(api.settingsSectionOf("nothing"), null);
  assert.deepEqual(
    plain(api.sectionChangeCounts({ language: "es", associative_max_matches: 4, disposable_retention_days: null })),
    { general: 1, models: 0, chats: 2, telegram: 0, connections: 0 },
  );
});
