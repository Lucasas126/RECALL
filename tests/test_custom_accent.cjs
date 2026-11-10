const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");

// appearance.js run against a fake document: the custom accent colour is derived per theme and kept readable
const source = readFileSync(path.join(__dirname, "..", "src", "web", "static", "appearance.js"), "utf8");
const css = readFileSync(path.join(__dirname, "..", "src", "web", "static", "style.css"), "utf8");

function load({ theme, accent, colour } = {}) {
  const store = { ...(accent ? { "recall.accent": accent } : {}), ...(colour ? { "recall.accentColor": colour } : {}) };
  const properties = {};
  const root = {
    dataset: theme ? { theme } : {},
    style: { setProperty: (name, value) => { properties[name] = value; }, removeProperty: (name) => { delete properties[name]; } },
  };
  const listeners = {};
  const context = vm.createContext({
    document: { documentElement: root, addEventListener: (name, fn) => { listeners[name] = fn; } },
    localStorage: { getItem: (key) => store[key] ?? null, setItem: (key, value) => { store[key] = value; } },
    matchMedia: () => ({ matches: false, addEventListener() {} }),
    MutationObserver: class { observe() {} },
  });
  vm.runInContext(source, context);
  return { root, properties, store, listeners };
}

const channel = (v) => { const c = v / 255; return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4; };
const luminance = (hex) => { const [r, g, b] = [1, 3, 5].map((i) => channel(parseInt(hex.slice(i, i + 2), 16))); return 0.2126 * r + 0.7152 * g + 0.0722 * b; };
const contrast = (a, b) => { const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x); return (hi + 0.05) / (lo + 0.05); };
const SURFACES = { dark: ["#111111", "#171717", "#1f1f1f"], light: ["#fafaf8", "#ffffff", "#ebebe6"] };

test("the surfaces the maths uses are the ones of the stylesheet", () => {
  for (const hex of [...SURFACES.dark, ...SURFACES.light]) assert.ok(css.toLowerCase().includes(hex), hex);
});

test("a custom colour is applied as inline variables and every pair stays at least AA in both themes", () => {
  for (const theme of ["dark", "light"]) {
    for (const colour of ["#ff6b9d", "#000000", "#ffffff", "#ffee00", "#0000ff", "#808080", "#12ab56", "#7a7a7a", "#3d3dff"]) {
      const { root, properties } = load({ theme, accent: "custom", colour });
      assert.equal(root.dataset.accent, "custom");
      for (const surface of SURFACES[theme]) assert.ok(contrast(properties["--accent"], surface) >= 4.5, `${colour} ${theme} accent on ${surface}`);
      assert.ok(contrast(properties["--on-accent"], properties["--accent"]) >= 4.5, `${colour} ${theme} button`);
      assert.ok(contrast(properties["--accent"], properties["--accent-soft"]) >= 3, `${colour} ${theme} on soft`);
    }
  }
});

test("a colour that already reads well is kept as it is", () => {
  assert.equal(load({ theme: "dark", accent: "custom", colour: "#ff6b9d" }).properties["--accent"], "#ff6b9d");
  assert.equal(load({ theme: "light", accent: "custom", colour: "#b0205e" }).properties["--accent"], "#b0205e");
});

test("a dark colour is lightened on the dark theme, a light one darkened on the light theme", () => {
  assert.notEqual(load({ theme: "dark", accent: "custom", colour: "#101040" }).properties["--accent"], "#101040");
  assert.notEqual(load({ theme: "light", accent: "custom", colour: "#ffee00" }).properties["--accent"], "#ffee00");
});

test("a named swatch sets no inline variables, a bad stored colour falls back, an unknown accent is pink", () => {
  assert.deepEqual(Object.keys(load({ accent: "blue" }).properties), []);
  assert.equal(load({ accent: "blue" }).root.dataset.accent, "blue");
  assert.equal(load({ theme: "dark", accent: "custom", colour: "javascript:1" }).properties["--accent"], "#ff6b9d");
  assert.equal(load({ accent: "nope" }).root.dataset.accent, "pink");
  assert.equal(load({}).root.dataset.accent, "pink");
});
