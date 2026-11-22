const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");

// The viewer's pure logic and its controls, loaded from app.js without a browser: fake elements and a fake Panzoom.
const source = readFileSync(path.join(__dirname, "..", "..", "..", "src", "web", "static", "app.js"), "utf8");
const grab = (name) => source.match(new RegExp(`(?:async )?function ${name}\\([\\s\\S]*?\\r?\\n\\}\\r?\\n`))[0];
const NAMES = ["collectMessageImages", "viewerImageInfo", "stepViewerIndex", "viewerHasArrows", "viewerKeyAction", "viewerWheelAction",
  "resetViewerZoom", "destroyViewerPanzoom", "zoomViewer", "updateViewerControls", "showViewerImage", "stepViewer"];

function harness(images = []) {
  const els = {};
  const get = (id) => (els[id] ??= { id, hidden: false, disabled: false, textContent: "", replaceChildren() {} });
  const panzooms = [];
  const context = vm.createContext({
    $: get, panzooms,
    element: (tag, props) => ({ tag, ...props, addEventListener() {} }),
    Panzoom: () => { const p = { destroyed: false, calls: [], destroy() { this.destroyed = true; }, reset() { this.calls.push("reset"); }, zoomIn() { this.calls.push("in"); }, zoomOut() { this.calls.push("out"); }, getScale: () => 1 }; panzooms.push(p); return p; },
  });
  vm.runInContext(
    `const VIEWER_IMAGE_SELECTOR = "img"; const VIEWER_MIN_SCALE = 1; const VIEWER_MAX_SCALE = 8; const VIEWER_DRAG_PX = 4;
let viewerPanzoom = null; let viewerKind = "image"; let viewerImages = []; let viewerIndex = 0; let viewerPress = null;
${NAMES.map(grab).join("\n")}
this.api = { collectMessageImages, stepViewerIndex, viewerHasArrows, viewerKeyAction, viewerWheelAction, updateViewerControls, showViewerImage, stepViewer, zoomViewer,
  open(list, index = 0, kind = "image") { viewerKind = kind; viewerImages = list; viewerIndex = index; showViewerImage(list[index] || {src: "x"}); updateViewerControls(); },
  get index() { return viewerIndex; }, get panzoom() { return viewerPanzoom; }, destroy: destroyViewerPanzoom };`,
    context,
  );
  return { api: context.api, els, panzooms };
}

const imgs = (n) => Array.from({ length: n }, (_, i) => ({ src: `s${i}`, name: `n${i}` }));

test("index navigation stops at both ends", () => {
  const { api } = harness();
  assert.equal(api.stepViewerIndex(0, 3, -1), 0);
  assert.equal(api.stepViewerIndex(0, 3, 1), 1);
  assert.equal(api.stepViewerIndex(2, 3, 1), 2);
  assert.equal(api.stepViewerIndex(5, 3, 0), 2);
  assert.equal(api.stepViewerIndex(0, 0, 1), 0);
});

test("one image: no arrows, no counter, arrow keys ignored", () => {
  const { api, els } = harness();
  api.open(imgs(1));
  assert.equal(els["viewer-prev"].hidden, true);
  assert.equal(els["viewer-next"].hidden, true);
  assert.equal(els["viewer-count"].hidden, true);
  assert.equal(api.viewerKeyAction("ArrowLeft", { count: 1 }), null);
  assert.equal(api.viewerKeyAction("ArrowRight", { count: 1 }), null);
});

test("several images: counter, arrows, and the buttons are disabled at the ends", () => {
  const { api, els } = harness();
  api.open(imgs(3));
  assert.equal(els["viewer-count"].textContent, "1 / 3");
  assert.equal(els["viewer-prev"].hidden, false);
  assert.equal(els["viewer-prev"].disabled, true);
  assert.equal(els["viewer-next"].disabled, false);
  api.stepViewer(1);
  api.stepViewer(1);
  api.stepViewer(1);  // past the end: stays
  assert.equal(api.index, 2);
  assert.equal(els["viewer-count"].textContent, "3 / 3");
  assert.equal(els["viewer-next"].disabled, true);
  assert.equal(els["viewer-title"].textContent, "n2");
});

test("key handling", () => {
  const { api } = harness();
  const k = (key, extra = {}) => api.viewerKeyAction(key, { count: 3, ...extra });
  assert.equal(k("ArrowLeft"), "prev");
  assert.equal(k("ArrowRight"), "next");
  assert.equal(k("+"), "zoom-in");
  assert.equal(k("="), "zoom-in");
  assert.equal(k("-"), "zoom-out");
  assert.equal(k("0"), "reset");
  assert.equal(k("a"), null);
  assert.equal(k("ArrowLeft", { typing: true }), null);
  assert.equal(k("0", { modifier: true }), null);
  assert.equal(k("ArrowRight", { kind: "pdf" }), null);
  assert.equal(k("+", { kind: "pdf" }), null);
});

test("wheel: only ctrl (and pinch) is taken, a plain wheel is not", () => {
  const { api } = harness();
  assert.equal(api.viewerWheelAction({ ctrlKey: true }), "zoom");
  assert.equal(api.viewerWheelAction({ ctrlKey: false }), null);
  assert.equal(api.viewerWheelAction({ ctrlKey: true }, "pdf"), null);
});

test("showing another image destroys the old Panzoom and starts a new one; destroy clears it", () => {
  const { api, panzooms } = harness();
  api.open(imgs(2));
  api.stepViewer(1);
  assert.equal(panzooms.length, 2);
  assert.equal(panzooms[0].destroyed, true);
  assert.equal(panzooms[1].destroyed, false);
  api.destroy();
  assert.equal(panzooms[1].destroyed, true);
  assert.equal(api.panzoom, null);
});

test("zoom buttons are hidden for a PDF; zoomViewer drives Panzoom", () => {
  const { api, els, panzooms } = harness();
  api.open(imgs(1));
  assert.equal(els["viewer-zoom-in"].hidden, false);
  api.zoomViewer("zoom-in");
  api.zoomViewer("zoom-out");
  api.zoomViewer("reset");
  assert.deepEqual([...panzooms[0].calls], ["in", "out", "reset"]);
  api.open([], 0, "pdf");
  for (const id of ["viewer-zoom-in", "viewer-zoom-out", "viewer-zoom-reset"]) assert.equal(els[id].hidden, true);
});

test("collectMessageImages: DOM order, linked images skipped, other messages not mixed in", () => {
  const { api } = harness();
  const image = (src, linked = false) => ({ currentSrc: "", src, alt: src, closest: (s) => (s === "a" ? (linked ? {} : null) : null) });
  const message = { querySelectorAll: () => [image("a"), image("b", true), image("c")] };
  assert.deepEqual([...api.collectMessageImages(message)].map((i) => i.src), ["a", "c"]);
  assert.equal(api.collectMessageImages(null).length, 0);
});
