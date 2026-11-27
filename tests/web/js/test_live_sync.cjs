const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");

// liveSyncAction and userAuthor of app.js, loaded without the page
const source = readFileSync(path.join(__dirname, "..", "..", "..", "src", "web", "static", "app.js"), "utf8");
const take = (name) => source.match(new RegExp(`function ${name}\\([\\s\\S]*?\\r?\\n\\}\\r?\\n`))[0];
const context = vm.createContext({ LIVE_SYNC_MS: 1500, LIVE_SYNC_SLOW_MS: 15000 });
vm.runInContext([take("liveSyncAction"), take("liveSyncDelay"), take("userAuthor"), "this.liveSyncAction = liveSyncAction; this.liveSyncDelay = liveSyncDelay; this.userAuthor = userAuthor;"].join("\n"), context);
const plain = (value) => JSON.parse(JSON.stringify(value));

test("no baseline or a page that is busy itself does nothing", () => {
  assert.equal(context.liveSyncAction(null, { turns: 2, active: false }, false), "none");
  assert.equal(context.liveSyncAction({ turns: 2, active: false }, { turns: 3, active: true }, true), "none");
});

test("a new turn or a reply that started reloads the chat", () => {
  assert.equal(context.liveSyncAction({ turns: 2, active: false }, { turns: 3, active: true }, false), "reload");
  assert.equal(context.liveSyncAction({ turns: 2, active: false }, { turns: 4, active: false }, false), "reload");
  assert.equal(context.liveSyncAction({ turns: 2, active: false }, { turns: 2, active: true }, false), "reload");
});

test("an unchanged chat or a reply that ended without new turns does nothing", () => {
  assert.equal(context.liveSyncAction({ turns: 2, active: false }, { turns: 2, active: false }, false), "none");
  assert.equal(context.liveSyncAction({ turns: 2, active: true }, { turns: 2, active: false }, false), "none");
});

test("author label: the logged-in person in a shared agent, nobody in an own agent", () => {
  assert.deepEqual(plain(context.userAuthor(true, { name: "Ben" })), { name: "Ben" });
  assert.equal(context.userAuthor(false, { name: "Ben" }), null);
  assert.equal(context.userAuthor(true, {}), null);
});

test("author label of a stored turn: the stored name, else the id, else none", () => {
  assert.deepEqual(plain(context.userAuthor(false, null, { author: "user-b", author_name: "Ben" })), { name: "Ben" });
  assert.deepEqual(plain(context.userAuthor(false, null, { author: "user-b" })), { name: "user-b" });
  assert.equal(context.userAuthor(false, null, { role: "user" }), null);
});

test("the summary poll is slow while the push stream is open and fast otherwise", () => {
  assert.equal(context.liveSyncDelay(true), 15000);
  assert.equal(context.liveSyncDelay(false), 1500);
});
