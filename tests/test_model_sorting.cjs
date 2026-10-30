const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");

function pickerHarness() {
  const elements = new Map();
  const element = () => ({
    children: [], attributes: {}, listeners: {}, dataset: {},
    checked: false, value: "", open: false, disabled: false,
    classList: { toggle() {} },
    addEventListener(name, callback) { this.listeners[name] = callback; },
    setAttribute(name, value) { this.attributes[name] = value; },
    append(...children) { this.children.push(...children); },
    replaceChildren(...children) { this.children = children; },
    querySelectorAll() { return []; },
    showModal() { this.open = true; },
    close() { this.open = false; },
    focus() { this.focused = true; },
  });
  const context = vm.createContext({
    document: {
      getElementById(id) {
        if (!elements.has(id)) elements.set(id, element());
        return elements.get(id);
      },
      createElement: element,
    },
    localStorage: { getItem() { return null; } },
    fetch: () => new Promise(() => {}),
    Intl,
  });
  vm.runInContext(
    readFileSync(path.join(__dirname, "..", "src", "web", "static", "app.js"), "utf8"),
    context,
  );
  elements.get("model-zdr").checked = true;
  elements.get("model-sort").value = "favorites";
  context.document.getElementById("model-info");
  context.document.getElementById("model-filters");
  return {
    elements, context,
    compareModels: vm.runInContext("compareModels", context),
    matchesCapabilities: vm.runInContext("matchesCapabilities", context),
    state: vm.runInContext("state", context),
    updateControls: vm.runInContext("updateModelControls", context),
    render: vm.runInContext("renderModelList", context),
    refresh: vm.runInContext("refreshModels", context),
    reset: vm.runInContext("resetStreams", context),
  };
}
const { compareModels, matchesCapabilities } = pickerHarness();
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

test("favorite star toggles filtering and exposes its pressed state", () => {
  const { elements, state, render } = pickerHarness();
  state.modelCatalog = models.map((model) => ({ ...model, zdr_providers: [] }));
  state.modelCatalogZdr = true;
  state.modelFavorites = ["vendor/a"];
  render();
  const star = elements.get("model-favorites-only");
  assert.equal(star.attributes["aria-pressed"], "false");
  assert.equal(elements.get("model-list").children.length, 5);
  star.listeners.click();
  assert.equal(star.attributes["aria-pressed"], "true");
  assert.equal(star.textContent, "\u2605");
  assert.equal(star.title, "Show all models");
  assert.equal(elements.get("model-list").children.length, 1);
  star.listeners.click();
  assert.equal(star.attributes["aria-pressed"], "false");
  assert.equal(star.textContent, "\u2606");
  assert.equal(elements.get("model-list").children.length, 5);
});

test("collapsed filters count active capabilities and still filter results", () => {
  const { elements, state, render } = pickerHarness();
  state.modelCatalog = [
    { ...models[0], zdr_providers: [], input_modalities: ["image"], parameter_routes: [["tools", "reasoning"]] },
    { ...models[1], zdr_providers: [], input_modalities: ["text"], parameter_routes: [] },
  ];
  state.modelCatalogZdr = true;
  render();
  assert.equal(elements.get("model-filters-label").textContent, "Filters");
  assert.equal(elements.get("model-filters").dataset.active, "false");
  for (const [index, id] of ["model-tools", "model-images", "model-reasoning"].entries()) {
    elements.get(id).checked = true;
    elements.get(id).listeners.change();
    assert.equal(elements.get("model-filters-label").textContent, `Filters (${index + 1})`);
  }
  assert.equal(elements.get("model-filters").open, false);
  assert.equal(elements.get("model-filters").dataset.active, "true");
  assert.equal(elements.get("model-list").children.length, 1);
  elements.get("model-search").value = "Alpha";
  elements.get("model-search").listeners.input();
  assert.equal(elements.get("model-list").children.length, 0);
});

test("opening closes disclosures, retains capabilities and restores safe defaults", () => {
  const { elements, state, updateControls } = pickerHarness();
  state.user = { provider: "openrouter" };
  state.chatId = "chat";
  state.modelCatalog = [];
  state.modelCatalogZdr = true;
  state.modelFavorites = ["vendor/a"];
  elements.get("model-zdr").checked = false;
  elements.get("model-tools").checked = true;
  elements.get("model-filters").open = true;
  elements.get("model-info").open = true;
  elements.get("model-search").value = "old query";
  elements.get("model-sort").value = "prompt-desc";
  updateControls();
  elements.get("model-picker").listeners.click();
  assert.equal(elements.get("model-dialog").open, true);
  assert.equal(elements.get("model-filters").open, false);
  assert.equal(elements.get("model-info").open, false);
  assert.equal(elements.get("model-tools").checked, true);
  assert.equal(elements.get("model-filters-label").textContent, "Filters (1)");
  assert.equal(elements.get("model-search").value, "");
  assert.equal(elements.get("model-search").focused, true);
  assert.equal(elements.get("model-sort").value, "prompt-desc");
  assert.equal(elements.get("model-zdr").checked, true);
  assert.equal(elements.get("model-zdr-warning").hidden, true);
  assert.equal(elements.get("model-favorites-only").attributes["aria-pressed"], "true");
});

test("non-ZDR warning remains visible independently of collapsed information", () => {
  const { elements, updateControls } = pickerHarness();
  elements.get("model-zdr").checked = false;
  updateControls();
  assert.equal(elements.get("model-info").open, false);
  assert.equal(elements.get("model-zdr-warning").hidden, false);
});

test("session reset clears favorite filter and capability choices", () => {
  const { elements, state, reset } = pickerHarness();
  state.modelFavorites = ["vendor/a"];
  state.modelFavoritesOnly = true;
  elements.get("model-sort").value = "prompt-asc";
  for (const id of ["model-tools", "model-images", "model-reasoning"]) elements.get(id).checked = true;
  elements.get("model-dialog").open = true;
  reset();
  assert.equal(elements.get("model-dialog").open, false);
  assert.equal(state.modelFavoritesOnly, false);
  assert.equal(state.modelFavorites.length, 0);
  assert.equal(elements.get("model-favorites-only").attributes["aria-pressed"], "false");
  assert.equal(elements.get("model-filters-label").textContent, "Filters");
  assert.equal(elements.get("model-sort").value, "favorites");
  for (const id of ["model-tools", "model-images", "model-reasoning"]) assert.equal(elements.get(id).checked, false);
});

test("initial catalog loading activates favorites only when saved IDs exist", async () => {
  for (const saved of [[], ["vendor/a"]]) {
    const { elements, context, state, refresh } = pickerHarness();
    context.fetch = async (url) => ({
      ok: true, status: 200,
      json: async () => url === "/api/models/favorites"
        ? saved : models.map((model) => ({ ...model, zdr_providers: [] })),
    });
    await refresh(true);
    assert.equal(state.modelLoading, false);
    assert.equal(elements.get("model-favorites-only").attributes["aria-pressed"], String(saved.length > 0));
    assert.equal(elements.get("model-list").children.length, saved.length ? 1 : 5);
  }
});

test("native dialog cancellation is blocked only while saving a selection", () => {
  const { elements, state } = pickerHarness();
  for (const busy of [false, true]) {
    state.modelBusy = busy;
    let prevented = false;
    elements.get("model-dialog").listeners.cancel({
      preventDefault() { prevented = true; },
    });
    assert.equal(prevented, busy);
  }
});
