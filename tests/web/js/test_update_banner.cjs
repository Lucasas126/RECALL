const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");

// the new-version banner logic of app.js, loaded without the DOM
const source = readFileSync(path.join(__dirname, "..", "..", "..", "src", "web", "static", "app.js"), "utf8");
const take = (name) => source.match(new RegExp(`function ${name}\\([\\s\\S]*?\\r?\\n\\}\\r?\\n`))[0];

function elements() {
  const make = () => ({ hidden: true, disabled: false, textContent: "", attrs: {}, setAttribute(k, v) { this.attrs[k] = v; } });
  return { "update-banner": make(), "update-banner-text": make(), "update-reload": make(), "update-dismiss": make() };
}

function load({ controller = {} } = {}) {
  const els = elements();
  const listeners = {};
  const reloads = [];
  const context = vm.createContext({
    UPDATE_CHECK_INTERVAL_MS: 300000,
    $: (id) => els[id],
    tr: (text) => text,
    navigator: { serviceWorker: { controller, addEventListener: (name, fn) => { listeners[name] = fn; } } },
    location: { reload: () => reloads.push(1) },
  });
  vm.runInContext(
    `${take("updateCheckDue")}\n${take("showUpdateBanner")}\nthis.updateCheckDue = updateCheckDue; this.showUpdateBanner = showUpdateBanner;`,
    context,
  );
  return { context, els, listeners, reloads };
}

test("a check is due the first time and then at most once per interval", () => {
  const { context } = load();
  assert.equal(context.updateCheckDue(1000, null), true);
  assert.equal(context.updateCheckDue(1000 + 299999, 1000), false);
  assert.equal(context.updateCheckDue(1000 + 300000, 1000), true);
});

test("the banner shows the message, Reload asks the waiting worker to take over and reloads once it does", () => {
  const { context, els, listeners, reloads } = load();
  const messages = [];
  const worker = { postMessage: (message) => messages.push(message) };
  context.showUpdateBanner(worker);
  assert.equal(els["update-banner"].hidden, false);
  assert.equal(els["update-banner-text"].textContent, "A new version of RECALL is available.");
  els["update-reload"].onclick();
  assert.equal(JSON.stringify(messages), JSON.stringify([{ type: "SKIP_WAITING" }]));
  assert.equal(reloads.length, 0);   // only after the new worker took control
  listeners.controllerchange();
  assert.equal(reloads.length, 1);
});

test("dismiss hides the banner without touching the worker", () => {
  const { context, els } = load();
  const messages = [];
  context.showUpdateBanner({ postMessage: (m) => messages.push(m) });
  els["update-dismiss"].onclick();
  assert.equal(els["update-banner"].hidden, true);
  assert.equal(messages.length, 0);
});
