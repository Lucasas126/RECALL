const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");

const elements = new Map();
const context = vm.createContext({
  document: {
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, { addEventListener() {} });
      return elements.get(id);
    },
  },
  localStorage: { getItem() { return null; } },
  fetch: () => new Promise(() => {}),
  Intl,
});
vm.runInContext(
  readFileSync(path.join(__dirname, "..", "src", "web", "static", "app.js"), "utf8"),
  context,
);
const compareModels = vm.runInContext("compareModels", context);
const matchesCapabilities = vm.runInContext("matchesCapabilities", context);
const models = [
  { id: "vendor/z", name: "Zulu", pricing: { prompt: "2", completion: "0" } },
  { id: "vendor/a", name: "Alpha", pricing: { prompt: "10", completion: "0.5" } },
  { id: "vendor/free", name: "Free", pricing: { prompt: "0", completion: "0" } },
  { id: "vendor/unknown", name: "Unknown", pricing: { prompt: null, completion: null } },
  { id: "vendor/retired", name: "Retired" },
];
const favorites = new Set(["vendor/a"]);
const sortedIds = (order, input = models) =>
  [...input].sort((a, b) => compareModels(a, b, order, favorites)).map((model) => model.id);

test("input prices sort numerically with unknowns last in both directions", () => {
  assert.deepEqual(sortedIds("prompt-asc"), [
    "vendor/free", "vendor/z", "vendor/a", "vendor/retired", "vendor/unknown",
  ]);
  assert.deepEqual(sortedIds("prompt-desc"), [
    "vendor/a", "vendor/z", "vendor/free", "vendor/retired", "vendor/unknown",
  ]);
});

test("output ordering uses output prices and preserves valid zero prices", () => {
  assert.deepEqual(sortedIds("completion-asc"), [
    "vendor/free", "vendor/z", "vendor/a", "vendor/retired", "vendor/unknown",
  ]);
  assert.deepEqual(sortedIds("completion-desc"), [
    "vendor/a", "vendor/free", "vendor/z", "vendor/retired", "vendor/unknown",
  ]);
});

test("favorites are pinned only in favorites-first mode", () => {
  assert.deepEqual(sortedIds("favorites"), [
    "vendor/a", "vendor/free", "vendor/retired", "vendor/unknown", "vendor/z",
  ]);
  const input = [
    { id: "vendor/z", name: "Zulu" },
    { id: "vendor/a", name: "Alpha" },
  ];
  assert.deepEqual(sortedIds("name", input), ["vendor/a", "vendor/z"]);
  const saved = new Set(["vendor/z"]);
  assert.equal(compareModels(input[0], input[1], "name", saved) > 0, true);
});

test("price ties break by name then ID", () => {
  const input = [
    { id: "vendor/b", name: "Same", pricing: { prompt: "0.1" } },
    { id: "vendor/a", name: "Same", pricing: { prompt: "0.1" } },
    { id: "vendor/c", name: "Earlier", pricing: { prompt: "0.1" } },
  ];
  assert.deepEqual(sortedIds("prompt-desc", input), ["vendor/c", "vendor/a", "vendor/b"]);
});

test("ordering uses unrounded prices and does not mutate the source catalog", () => {
  const input = [
    { id: "vendor/a", name: "Alpha", pricing: { prompt: "0.12345679" } },
    { id: "vendor/z", name: "Zulu", pricing: { prompt: "0.12345678" } },
  ];
  assert.deepEqual(sortedIds("prompt-asc", input), ["vendor/z", "vendor/a"]);
  assert.deepEqual(input.map((model) => model.id), ["vendor/a", "vendor/z"]);
  assert.deepEqual(sortedIds("prompt-asc", []), []);
});

test("capability filters leave all models visible when disabled", () => {
  assert.equal(matchesCapabilities({}, {}), true);
});

test("tools and reasoning must be supported by the same route", () => {
  const split = { parameter_routes: [["tools"], ["reasoning"]] };
  assert.equal(matchesCapabilities(split, { tools: true }), true);
  assert.equal(matchesCapabilities(split, { reasoning: true }), true);
  assert.equal(matchesCapabilities(split, { tools: true, reasoning: true }), false);
  const combined = { parameter_routes: [["tools", "reasoning_effort"]] };
  assert.equal(matchesCapabilities(combined, { tools: true, reasoning: true }), true);
});

test("image input composes with parameter filters and excludes unknown capabilities", () => {
  const vision = { input_modalities: ["text", "image"], parameter_routes: [["tools", "reasoning"]] };
  assert.equal(matchesCapabilities(vision, { tools: true, images: true, reasoning: true }), true);
  assert.equal(matchesCapabilities({ ...vision, input_modalities: ["text"] }, { images: true }), false);
  for (const filter of ["tools", "images", "reasoning"]) {
    assert.equal(matchesCapabilities({}, { [filter]: true }), false);
  }
  assert.equal(matchesCapabilities({ parameter_routes: [["include_reasoning"]] }, { reasoning: true }), false);
});

test("capability filters compose with price sorting without pinning favorites", () => {
  const input = [
    { ...models[0], parameter_routes: [["tools"]] },
    { ...models[1], parameter_routes: [["tools"]] },
    { ...models[2], parameter_routes: [] },
  ];
  assert.deepEqual(sortedIds("prompt-asc", input.filter((model) =>
    matchesCapabilities(model, { tools: true })
  )), ["vendor/z", "vendor/a"]);
});
