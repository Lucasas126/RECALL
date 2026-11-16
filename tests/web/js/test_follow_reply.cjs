const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");

// the decision after a followed reply ends, loaded without the DOM
const source = readFileSync(path.join(__dirname, "..", "..", "..", "src", "web", "static", "app.js"), "utf8");
const code = source.match(/function afterFollow\([\s\S]*?\r?\n\}\r?\n/)[0];
const context = vm.createContext({});
vm.runInContext(`${code}\nthis.afterFollow = afterFollow;`, context);
const decide = (changes) => context.afterFollow({
  outcome: "ended", current: true, generating: false, active: false, attempt: 0, ...changes,
});

test("a finished reply of the open chat shows the saved chat", () => {
  assert.equal(decide({}), "reload");
});

test("a steered reply is followed into the next run, a bounded number of times", () => {
  assert.equal(decide({ active: true }), "follow");
  assert.equal(decide({ active: true, attempt: 5 }), "reload");
});

test("nothing is redrawn when the user left, a new request owns the chat, or the stream failed or was aborted", () => {
  assert.equal(decide({ current: false }), "stop");
  assert.equal(decide({ generating: true }), "stop");
  assert.equal(decide({ outcome: "failed" }), "stop");
  assert.equal(decide({ outcome: "aborted" }), "stop");
});
