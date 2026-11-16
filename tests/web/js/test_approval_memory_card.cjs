const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");

// the approval card's scope buttons and the learned-rules line, loaded without the DOM
const source = readFileSync(path.join(__dirname, "..", "..", "..", "src", "web", "static", "app.js"), "utf8");
const take = (name) => source.match(new RegExp(`function ${name}\\([\\s\\S]*?\\r?\\n\\}\\r?\\n`))[0];
const names = [
  "approvalScopeButtons", "approvalScopeSuffix", "approvalOutcome", "approvalInitialState", "approvalActive", "approvalTransition",
  "approvalRuleText",
];
const context = vm.createContext({ tr: (text, values = {}) => text.replace(/\{(\w+)\}/g, (_, key) => values[key]) });
vm.runInContext(`${names.map(take).join("\n")}\n${names.map((n) => `this.${n} = ${n};`).join(" ")}`, context);
const f = context;
const plain = (value) => JSON.parse(JSON.stringify(value));

test("a card without options offers the single Approve button, as before", () => {
  assert.deepEqual(plain(f.approvalScopeButtons({})), [{ scope: "once", label: "Approve" }]);
  assert.deepEqual(plain(f.approvalScopeButtons({ options: [] })), [{ scope: "once", label: "Approve" }]);
  assert.deepEqual(plain(f.approvalScopeButtons({ options: ["once"] })), [{ scope: "once", label: "Approve" }]);
  assert.deepEqual(plain(f.approvalScopeButtons(null)), [{ scope: "once", label: "Approve" }]);
});

test("the offered scopes become buttons in a fixed order, the first one primary", () => {
  assert.deepEqual(
    plain(f.approvalScopeButtons({ options: ["agent", "once", "session"] })),
    [
      { scope: "once", label: "Approve once" },
      { scope: "session", label: "Always in this session" },
      { scope: "agent", label: "Always for this agent" },
    ],
  );
  assert.deepEqual(plain(f.approvalScopeButtons({ options: ["once", "session"] })).map((b) => b.scope), ["once", "session"]);
});

test("a scope the card did not offer gets no button, and unknown scopes are ignored", () => {
  assert.deepEqual(plain(f.approvalScopeButtons({ options: ["once", "forever"] })).map((b) => b.scope), ["once"]);
  assert.ok(!f.approvalScopeButtons({ options: ["once", "session"] }).some((b) => b.scope === "agent"));
});

test("a remembered approval says so in its outcome", () => {
  assert.equal(f.approvalScopeSuffix("session"), " (this session)");
  assert.equal(f.approvalScopeSuffix("agent"), " (always for this agent)");
  assert.equal(f.approvalScopeSuffix("once"), "");
  assert.equal(f.approvalScopeSuffix(undefined), "");
});

test("the state keeps the granted scope only when there is one", () => {
  const pending = f.approvalInitialState();
  const bare = f.approvalTransition(pending, { type: "answered", status: "approved" });
  assert.equal("scope" in bare, false);  // unchanged shape for a plain approval
  const session = f.approvalTransition(pending, { type: "answered", status: "approved", scope: "session" });
  assert.equal(session.scope, "session");
  assert.equal(session.kind, "approved");
  const fromEvent = f.approvalTransition(pending, { type: "resolved", decision: "approved", reason: "approved", scope: "agent" });
  assert.equal(fromEvent.scope, "agent");
  const denied = f.approvalTransition(pending, { type: "resolved", decision: "denied", reason: "denied by user" });
  assert.equal(denied.kind, "denied");
  assert.equal("scope" in denied, false);
});

test("a rule of the agent's list reads as one line", () => {
  assert.equal(
    f.approvalRuleText({ verdict: "allow", tool: "terminal", command: "git status", backend: "box", exact: true }),
    "allow terminal: git status [box] (exact)",
  );
  assert.equal(f.approvalRuleText({ verdict: "allow", tool: "file_write", path: "/srv/a.txt", backend: "box", exact: true }),
    "allow file_write: /srv/a.txt [box] (exact)");
  assert.equal(f.approvalRuleText({ verdict: "deny", command: "rm*" }), "deny rm*");
  assert.equal(f.approvalRuleText({}), "");
});
