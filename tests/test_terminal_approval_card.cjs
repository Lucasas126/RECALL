const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");

// the terminal approval card logic and the Terminal settings helpers, loaded without the DOM
const source = readFileSync(path.join(__dirname, "..", "src", "web", "static", "app.js"), "utf8");
const take = (name) => source.match(new RegExp(`function ${name}\\([\\s\\S]*?\\r?\\n\\}\\r?\\n`))[0];
const names = [
  "approvalTitle", "formatCountdown", "approvalRemaining", "approvalOutcome", "approvalOutcomeLabel",
  "approvalInitialState", "approvalActive", "approvalTransition", "approvalSummary", "approvalStepIndex", "approvalPendingLabel", "textSegments",
  "terminalApprovalHint", "terminalBackendOptions", "terminalBackendRows", "terminalDefaultBackend", "buildTerminalConfig", "isTerminalError",
];
const context = vm.createContext({ tr: (text, values = {}) => text.replace(/\{(\w+)\}/g, (_, key) => values[key]) });
vm.runInContext(`${names.map(take).join("\n")}\n${names.map((n) => `this.${n} = ${n};`).join(" ")}`, context);
const f = context;
const plain = (value) => JSON.parse(JSON.stringify(value));

test("the title tells a dangerous command from one that only needs approval", () => {
  assert.equal(f.approvalTitle({ risk: "dangerous" }), "Dangerous command");
  assert.equal(f.approvalTitle({ risk: "manual" }), "Approval required");
  assert.equal(f.approvalTitle(null), "Approval required");
});

test("the countdown shows m:ss and never goes negative", () => {
  assert.equal(f.formatCountdown(300), "5:00");
  assert.equal(f.formatCountdown(59), "0:59");
  assert.equal(f.formatCountdown(61.2), "1:02");
  assert.equal(f.formatCountdown(0), "0:00");
  assert.equal(f.formatCountdown(-5), "0:00");
  assert.equal(f.formatCountdown(undefined), "0:00");
});

test("remaining seconds come from expires_at and bottom out at zero", () => {
  assert.equal(f.approvalRemaining(1000, 940_000), 60);
  assert.equal(f.approvalRemaining(1000, 940_500), 60);
  assert.equal(f.approvalRemaining(1000, 1_000_000), 0);
  assert.equal(f.approvalRemaining(1000, 2_000_000), 0);
  assert.equal(f.approvalRemaining(undefined, 1), 0);
});

test("every resolution reason maps to an outcome and a label", () => {
  const cases = [
    ["approved", "approved", "approved", "Approved"],
    ["denied", "denied by user", "denied", "Denied"],
    ["denied", "timed out", "timed_out", "Timed out"],
    ["denied", "cancelled", "cancelled", "Cancelled"],
    ["denied", "service shutdown", "cancelled", "Cancelled"],
  ];
  for (const [decision, reason, kind, label] of cases) {
    assert.equal(f.approvalOutcome(decision, reason), kind, reason);
    assert.equal(f.approvalOutcomeLabel(kind), label);
  }
  assert.equal(f.approvalOutcomeLabel("expired"), "Expired");
  assert.equal(f.approvalOutcomeLabel("unknown"), "Resolved");
});

test("a card goes pending -> submitting -> resolved", () => {
  let s = f.approvalInitialState();
  assert.equal(f.approvalActive(s), true);
  s = plain(f.approvalTransition(s, { type: "submit", decision: "approved" }));
  assert.equal(s.phase, "submitting");
  assert.equal(f.approvalActive(s), true);
  s = plain(f.approvalTransition(s, { type: "resolved", decision: "approved", reason: "approved" }));
  assert.deepEqual([s.phase, s.kind], ["resolved", "approved"]);
  assert.equal(f.approvalActive(s), false);
});

test("a failed answer returns to pending with the message; a 404 means expired", () => {
  const submitting = plain(f.approvalTransition(f.approvalInitialState(), { type: "submit", decision: "denied" }));
  const failed = plain(f.approvalTransition(submitting, { type: "failed", status: 500, message: "boom" }));
  assert.deepEqual([failed.phase, failed.error], ["pending", "boom"]);
  const gone = plain(f.approvalTransition(submitting, { type: "failed", status: 404 }));
  assert.deepEqual([gone.phase, gone.kind], ["expired", "expired"]);
});

test("the server's resolved event overrides an expired mark, and a resolved card never goes back", () => {
  const expired = plain(f.approvalTransition(f.approvalInitialState(), { type: "failed", status: 404 }));
  const resolved = plain(f.approvalTransition(expired, { type: "resolved", decision: "denied", reason: "timed out" }));
  assert.deepEqual([resolved.phase, resolved.kind], ["resolved", "timed_out"]);
  for (const action of [
    { type: "submit", decision: "approved" },
    { type: "failed", status: 500, message: "x" },
    { type: "failed", status: 404 },
    { type: "answered", status: "denied" },
    { type: "gone", expired: true },
    { type: "resolved", decision: "approved", reason: "approved" },
  ]) {
    assert.deepEqual(plain(f.approvalTransition(resolved, action)), resolved, action.type);
  }
});

test("an answer or a vanished approval settles a pending card", () => {
  const answered = plain(f.approvalTransition(f.approvalInitialState(), { type: "answered", status: "approved" }));
  assert.deepEqual([answered.phase, answered.kind], ["resolved", "approved"]);
  const gone = plain(f.approvalTransition(f.approvalInitialState(), { type: "gone", expired: true }));
  assert.equal(gone.kind, "timed_out");
  assert.equal(plain(f.approvalTransition(f.approvalInitialState(), { type: "nope" })).phase, "pending");
});

test("backend options show name, type and description only; none defined gives an empty list for the help state", () => {
  assert.deepEqual(plain(f.terminalBackendOptions([], [])), []);
  assert.deepEqual(plain(f.terminalBackendOptions(undefined, null)), []);
  assert.deepEqual(
    plain(f.terminalBackendOptions([{ name: "box", type: "ssh", description: "sandbox" }], ["box"])),
    [{ value: "box", label: "box (ssh)", description: "sandbox" }],
  );
  assert.deepEqual(
    plain(f.terminalBackendOptions([{ name: "box", type: "ssh" }], ["old"])).map((o) => o.value),
    ["box", "old"],
  );
  assert.equal(f.terminalBackendOptions([], ["old"])[0].label, "old (not defined)");
});

test("one row per backend: allowed ones carry the saved settings, the rest default to manual and ~", () => {
  const backends = [{ name: "pc", type: "ssh", description: "my PC" }, { name: "sandbox", type: "ssh", description: "" }];
  const saved = { backends: { sandbox: { workdir: "~/workspace", approvals: "smart" }, gone: { workdir: "~", approvals: "off" } } };
  const rows = plain(f.terminalBackendRows(backends, saved));
  assert.deepEqual(rows.map((r) => [r.name, r.allowed, r.workdir, r.approvals]), [
    ["pc", false, "~", "manual"], ["sandbox", true, "~/workspace", "smart"], ["gone", true, "~", "off"],
  ]);
  assert.equal(rows[0].description, "my PC");
  assert.equal(f.terminalBackendRows([], null).length, 0);
});

test("the default backend must be an allowed one", () => {
  assert.equal(f.terminalDefaultBackend(["a", "b"], "b"), "b");
  assert.equal(f.terminalDefaultBackend(["a", "b"], "c"), "a");
  assert.equal(f.terminalDefaultBackend([], "c"), "");
});

test("the approvals hint follows the mode", () => {
  assert.match(f.terminalApprovalHint("smart"), /dangerous/);
  assert.match(f.terminalApprovalHint("off"), /hardline/);
  assert.match(f.terminalApprovalHint("manual"), /Every command/);
});

test("buildTerminalConfig round-trips the form and leaves a disabled group alone", () => {
  const saved = {
    backends: { sandbox: { workdir: "~/w", approvals: "manual", timeout_seconds: 30 } },
    default_backend: "sandbox", timeout_seconds: 60, allow_in_dream: false, extra: 1,
  };
  const fields = {
    backends: [
      { name: "sandbox", allowed: true, workdir: "  ~/other ", approvals: "smart" },
      { name: "pc", allowed: true, workdir: " ", approvals: "manual" },
      { name: "nas", allowed: false, workdir: "~", approvals: "off" },
    ],
    defaultBackend: "pc", timeout: "90", allowInDream: true,
  };
  assert.deepEqual(plain(f.buildTerminalConfig(saved, true, fields)), {
    backends: {
      sandbox: { workdir: "~/other", approvals: "smart", timeout_seconds: 30 },
      pc: { workdir: "~", approvals: "manual" },
    },
    default_backend: "pc", timeout_seconds: 90, allow_in_dream: true, extra: 1,
  });
  assert.deepEqual(plain(f.buildTerminalConfig(saved, false, fields)), saved);
  assert.equal(f.buildTerminalConfig(undefined, false, fields), null);
  assert.equal(f.buildTerminalConfig(saved, true, { ...fields, backends: [{ ...fields.backends[2] }] }), null);
  const stale = f.buildTerminalConfig(null, true, { ...fields, defaultBackend: "nas" });
  assert.equal(stale.default_backend, "sandbox");  // a default that is no longer allowed falls back to the first allowed
});

test("isTerminalError spots save errors about the terminal block", () => {
  assert.equal(f.isTerminalError("terminal.backends is unknown"), true);
  assert.equal(f.isTerminalError("Terminal backend missing"), true);
  assert.equal(f.isTerminalError("name is too long"), false);
  assert.equal(f.isTerminalError(null), false);
});

test("a settled card summarises as one line with the outcome, tool, backend and the first 60 characters", () => {
  assert.equal(f.approvalSummary({ tool: "terminal", backend: "local", command: "ls -la" }, "approved"), "Approved: terminal [local] ls -la");
  const long = f.approvalSummary({ tool: "terminal", backend: "ssh", command: `echo   ${"x".repeat(100)}\nnext` }, "denied");
  assert.equal(long, `Denied: terminal [ssh] echo ${"x".repeat(55)}…`);
  assert.equal(f.approvalSummary({ tool: "terminal", command: "pwd" }, "timed_out"), "Timed out: terminal pwd");
});

test("a restored approval matches the tool step with the same tool and command, else none", () => {
  const steps = [{ name: "memory", command: "" }, { name: "terminal", command: "ls" }, { name: "terminal", command: "rm -rf /tmp/x" }];
  assert.equal(f.approvalStepIndex(steps, { tool: "terminal", command: "rm -rf /tmp/x" }), 2);
  assert.equal(f.approvalStepIndex(steps, { tool: "terminal", command: "ls" }), 1);
  assert.equal(f.approvalStepIndex(steps, { tool: "terminal", command: "cat" }), -1);
  assert.equal(f.approvalStepIndex(steps, { tool: "files", command: "ls" }), -1);
  assert.equal(f.approvalStepIndex([], { tool: "terminal", command: "ls" }), -1);
});

test("the pending indicator counts approvals", () => {
  assert.equal(f.approvalPendingLabel(1), "1 approval pending, jump to it");
  assert.equal(f.approvalPendingLabel(3), "3 approvals pending, jump to it");
});

test("text is split at the steps so later text renders below them", () => {
  assert.deepEqual(plain(f.textSegments("before after more", [0, 7, 13])), ["before ", "after ", "more"]);
  assert.deepEqual(plain(f.textSegments("all", [0])), ["all"]);
  assert.deepEqual(plain(f.textSegments("ab", [0, 2])), ["ab", ""]);
});
