const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");

// pure error helpers of the web UI, loaded without the DOM
const source = readFileSync(path.join(__dirname, "..", "src", "web", "static", "app.js"), "utf8");
function helpers() {
  const pick = (re) => source.match(re)[0];
  const code = [
    pick(/function formatApiError\(detail\) \{[\s\S]*?\r?\n\}\r?\n/),
    pick(/function normalizeError\([\s\S]*?\r?\n\}\r?\n/),
    pick(/function httpError\([\s\S]*?\r?\n\}\r?\n/),
    pick(/function errorInfo\([\s\S]*?\r?\n\}\r?\n/),
  ].join("\n");
  const context = vm.createContext({ tr: (text, values = {}) => text.replace(/\{(\w+)\}/g, (_, key) => values[key]) });
  vm.runInContext(`${code}\nthis.normalizeError = normalizeError; this.httpError = httpError; this.errorInfo = errorInfo;`, context);
  return context;
}

test("normalizeError accepts strings, Errors and SSE payload objects", () => {
  const { normalizeError } = helpers();
  assert.deepEqual({ ...normalizeError("boom") }, { message: "boom", code: null });
  assert.deepEqual({ ...normalizeError({ message: "m", code: "no_agents" }) }, { message: "m", code: "no_agents" });
  assert.deepEqual({ ...normalizeError(Object.assign(new Error("e"), { code: "rate_limited" })) }, { message: "e", code: "rate_limited" });
  assert.deepEqual({ ...normalizeError({ message: "m", code: 5 }) }, { message: "m", code: null });
  assert.deepEqual({ ...normalizeError(null) }, { message: "", code: null });
});

test("httpError keeps the server message and code, with an HTTP fallback", () => {
  const { httpError } = helpers();
  const coded = httpError({ detail: "No agents.", code: "no_agents" }, 404);
  assert.equal(coded.message, "No agents.");
  assert.equal(coded.code, "no_agents");
  const bare = httpError({}, 500);
  assert.equal(bare.message, "HTTP 500");
  assert.equal(bare.code, null);
});

test("errorInfo maps codes to actions and never invents one for unknown codes", () => {
  const { errorInfo } = helpers();
  assert.equal(errorInfo("no_agents").action, "create-agent");
  assert.equal(errorInfo("no_agents").label, "Create an agent");
  for (const code of ["missing_api_key", "provider_unreachable", "claude_not_installed", "claude_no_own_login", "provider_unavailable", "model_unavailable", "rate_limited"]) {
    assert.equal(errorInfo(code).action, "choose-model", code);
  }
  const loggedOut = errorInfo("claude_not_logged_in");
  assert.equal(loggedOut.command, "claude auth login");
  assert.equal(loggedOut.action, "choose-model");  // a command is shown, never run by a button
  assert.equal(errorInfo("claude_no_own_login").command, undefined);  // nothing the user can run: the administrator sets it up
  assert.match(errorInfo("claude_no_own_login").hint, /administrator/);
  assert.equal(errorInfo("agent_busy").action, undefined);
  assert.equal(errorInfo("unknown_chat").action, "new-chat");
  assert.equal(errorInfo("mcp_not_connected").action, "open-settings");
  assert.equal(errorInfo("something_new"), null);
  assert.equal(errorInfo(null), null);
});
