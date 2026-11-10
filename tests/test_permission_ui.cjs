const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");

// the pure helpers of the permission level selector, the approval rule editor and the Users section, without the DOM
const source = readFileSync(path.join(__dirname, "..", "src", "web", "static", "app.js"), "utf8");
const take = (name) => source.match(new RegExp(`function ${name}\\([\\s\\S]*?\\r?\\n\\}\\r?\\n`))[0];
const names = [
  "levelNumber", "levelTitle", "requiredLevelFor", "levelAdvice", "levelOptions", "levelDescriptionText", "levelDescriptionItems", "sentenceCase", "toolGroupBadges",
  "levelReasonText", "levelMessage", "levelPayload", "isLevelErrorCode", "isRuleError", "splitApprovalRules",
  "buildApprovalRule", "approvalRuleFields", "approvalRulesBody", "agentsAboveCeiling", "isAdministrator",
];
const context = vm.createContext({ tr: (text, values = {}) => text.replace(/\{(\w+)\}/g, (_, key) => values[key]) });
vm.runInContext(
  `const LEARNED_RULE_REASON = "approved always for this agent";\n${names.map(take).join("\n")}\n${names.map((n) => `this.${n} = ${n};`).join(" ")}`,
  context,
);
const f = context;
const plain = (value) => JSON.parse(JSON.stringify(value));

// the shape of GET /api/levels (levels_catalog in src/config/levels_help.py)
const TITLES = ["Minimal", "Reader", "Assistant", "Collaborator", "Operator", "Admin"];
const catalog = {
  levels: TITLES.map((title, number) => ({
    name: title.toLowerCase(), number, title, parts: [`part of ${title}`, "second part"], description: `part of ${title}; second part`,
  })),
  groups: {
    memory_read: "reader", folders_read: "reader", memory_edit: "assistant", self: "assistant", web: "assistant",
    folders_edit: "collaborator", delegate: "collaborator", permissions: "operator", terminal: "operator",
  },
  services: { terminal_ssh: "operator", terminal_root: "admin", terminal_local: "admin" },
  mcp: "collaborator", delegation_targets: "operator",
  backend_rules: { local: "admin", ssh_root: "admin", ssh: "operator" },
};
const groupName = (id) => id.toUpperCase();
const nothing = { toolGroups: [], mcp: false, backends: [], delegationTargets: false };

test("the options above the user's maximum are disabled and the rest are enabled", () => {
  const options = plain(f.levelOptions(catalog, "collaborator"));
  assert.deepEqual(options.map((o) => o.value), TITLES.map((t) => t.toLowerCase()));
  assert.deepEqual(options.map((o) => o.disabled), [false, false, false, false, true, true]);
  assert.equal(options[2].label, "Assistant (2)");
  assert.deepEqual(plain(f.levelOptions(catalog, "admin")).map((o) => o.disabled), [false, false, false, false, false, false]);
  assert.deepEqual(plain(f.levelOptions(catalog, "minimal")).map((o) => o.disabled), [false, true, true, true, true, true]);
  assert.deepEqual(plain(f.levelOptions(null, "admin")), []);
});

test("the required level is the highest minimum of the choices, with what sets it", () => {
  assert.deepEqual(plain(f.requiredLevelFor(catalog, nothing)), { number: 0, level: "minimal", reason: { kind: "none" } });
  const web = plain(f.requiredLevelFor(catalog, { ...nothing, toolGroups: ["memory_read", "web"] }));
  assert.deepEqual([web.level, web.reason], ["assistant", { kind: "group", name: "web" }]);
  const mcp = plain(f.requiredLevelFor(catalog, { ...nothing, toolGroups: ["memory_read"], mcp: true }));
  assert.deepEqual([mcp.level, mcp.reason.kind], ["collaborator", "mcp"]);
  const terminal = plain(f.requiredLevelFor(catalog, { ...nothing, toolGroups: ["terminal"] }));
  assert.deepEqual([terminal.level, terminal.reason], ["operator", { kind: "group", name: "terminal" }]);
});

test("a local or root backend needs admin, other ssh backends stay at operator, only with the terminal group", () => {
  const backends = [{ name: "pc", min_level: "operator" }, { name: "rootbox", min_level: "admin" }];
  const root = plain(f.requiredLevelFor(catalog, { ...nothing, toolGroups: ["terminal"], backends }));
  assert.deepEqual([root.level, root.reason], ["admin", { kind: "backend", name: "rootbox" }]);
  const plainSsh = plain(f.requiredLevelFor(catalog, { ...nothing, toolGroups: ["terminal"], backends: [backends[0]] }));
  assert.equal(plainSsh.level, "operator");
  const noGroup = plain(f.requiredLevelFor(catalog, { ...nothing, toolGroups: ["memory_read"], backends }));
  assert.equal(noGroup.level, "reader");
  const unknown = plain(f.requiredLevelFor(catalog, { ...nothing, toolGroups: ["terminal"], backends: [{ name: "gone" }] }));
  assert.equal(unknown.level, "operator");
});

test("delegation targets need operator only with the delegate group and at least one target", () => {
  const withTargets = plain(f.requiredLevelFor(catalog, { ...nothing, toolGroups: ["delegate"], delegationTargets: true }));
  assert.deepEqual([withTargets.level, withTargets.reason.kind], ["operator", "delegation"]);
  assert.equal(f.requiredLevelFor(catalog, { ...nothing, toolGroups: ["delegate"] }).level, "collaborator");
  assert.equal(f.requiredLevelFor(catalog, { ...nothing, toolGroups: ["memory_read"], delegationTargets: true }).level, "reader");
});

test("the advice tells raising the level from asking for a higher maximum", () => {
  const choices = { ...nothing, toolGroups: ["web"] };
  assert.equal(f.levelAdvice(catalog, "assistant", choices, "operator").problem, null);
  assert.equal(f.levelAdvice(catalog, "reader", choices, "operator").problem, "below_required");
  assert.equal(f.levelAdvice(catalog, "reader", choices, "reader").problem, "above_max");
  assert.equal(f.levelAdvice(catalog, "admin", choices, "operator").problem, "exceeds_max");
  assert.equal(f.levelAdvice(catalog, "minimal", nothing, "minimal").problem, null);
});

test("the inline messages say what to remove, raise or whom to ask", () => {
  const below = f.levelAdvice(catalog, "reader", { ...nothing, toolGroups: ["web"] }, "operator");
  assert.equal(
    f.levelMessage(catalog, below, "reader", "operator", groupName),
    "tool group 'WEB' needs the level Assistant, but this agent is Reader. Raise the level to Assistant or remove tool group 'WEB'.",
  );
  const above = f.levelAdvice(catalog, "reader", { ...nothing, toolGroups: ["folders_edit"] }, "assistant");
  assert.equal(
    f.levelMessage(catalog, above, "reader", "assistant", groupName),
    "tool group 'FOLDERS_EDIT' needs the level Collaborator, above your maximum level Assistant. Remove tool group 'FOLDERS_EDIT' or ask an administrator to raise your maximum.",
  );
  const exceeds = f.levelAdvice(catalog, "admin", nothing, "operator");
  assert.match(f.levelMessage(catalog, exceeds, "admin", "operator", groupName), /Admin is above your maximum level Operator.*ask an administrator/);
  const fine = f.levelAdvice(catalog, "assistant", { ...nothing, toolGroups: ["web"] }, "operator");
  assert.equal(f.levelMessage(catalog, fine, "assistant", "operator", groupName), "");
});

test("the reason names the service that sets the level", () => {
  assert.equal(f.levelReasonText({ kind: "mcp" }, groupName), "MCP servers");
  assert.equal(f.levelReasonText({ kind: "backend", name: "pc" }, groupName), "terminal backend 'pc'");
  assert.equal(f.levelReasonText({ kind: "delegation" }, groupName), "delegation to other agents");
  assert.equal(f.levelReasonText({ kind: "none" }, groupName), "no service");
});

test("groups that need more than the selected level get a badge, and are marked when the maximum falls short too", () => {
  const badges = plain(f.toolGroupBadges(catalog, "reader", "collaborator"));
  assert.equal(badges.memory_read, undefined);
  assert.deepEqual(badges.web, { level: "assistant", overMax: false });
  assert.deepEqual(badges.folders_edit, { level: "collaborator", overMax: false });
  assert.deepEqual(badges.terminal, { level: "operator", overMax: true });
  assert.deepEqual(Object.keys(plain(f.toolGroupBadges(catalog, "admin", "admin"))), []);
  assert.deepEqual(plain(f.toolGroupBadges(null, "admin", "admin")), {});
});

test("description items start with a capital and move the parenthesis to a detail", () => {
  const levels = { levels: [{ name: "x", parts: [
    "read the agent's own memory (tool group memory_read)", "search the web (tool group web; asks)", "use the `local` backend",
  ] }] };
  const items = plain(f.levelDescriptionItems(levels, "x", (part) => part));
  assert.deepEqual(items, [
    { text: "Read the agent's own memory", detail: "Tool group memory_read" },
    { text: "Search the web", detail: "Tool group web; asks" },
    { text: "Use the `local` backend", detail: "" },
  ]);
  assert.deepEqual(plain(f.levelDescriptionItems(levels, "nope", (part) => part)), []);
  assert.equal(f.sentenceCase("élan"), "Élan");
  assert.equal(f.sentenceCase(""), "");
});

test("a description joins the translated texts of the level's services", () => {
  assert.equal(f.levelDescriptionText(catalog, "reader", (part) => part.toUpperCase()), "PART OF READER; SECOND PART");
  assert.equal(f.levelDescriptionText(catalog, "nope", (part) => part), "");
  assert.equal(f.levelTitle(catalog, "operator"), "Operator");
  assert.equal(f.levelTitle(null, "operator"), "operator");
});

test("the level is sent only when the user chose it or the form moved it", () => {
  assert.deepEqual(plain(f.levelPayload("assistant", "assistant", false)), {});
  assert.deepEqual(plain(f.levelPayload("assistant", "assistant", true)), { level: "assistant" });
  assert.deepEqual(plain(f.levelPayload("operator", "assistant", false)), { level: "operator" });
  assert.deepEqual(plain(f.levelPayload("", "assistant", true)), {});
});

test("server errors about levels and rules are told apart", () => {
  for (const code of ["level_exceeds_max", "level_below_required", "level_required_above_max"]) assert.equal(f.isLevelErrorCode(code), true);
  assert.equal(f.isLevelErrorCode("approval_rule_not_allowed"), false);
  assert.equal(f.isRuleError({ code: "approval_rule_not_allowed", message: "x" }), true);
  assert.equal(f.isRuleError({ message: "body.approval_rules.rules.0.verdict: Input should be 'allow'" }), true);
  assert.equal(f.isRuleError({ code: null, message: "The terminal backend is missing" }), false);
});

test("learned rules are kept apart from the ones the editor shows", () => {
  const rules = [
    { verdict: "deny", command: "rm *" },
    { verdict: "allow", command: "ls", exact: true, reason: "approved always for this agent" },
    { verdict: "ask", tool: "file_write", path: "/etc/*" },
  ];
  const { manual, learned } = plain(f.splitApprovalRules(rules));
  assert.deepEqual(manual.map((r) => r.command ?? r.path), ["rm *", "/etc/*"]);
  assert.deepEqual(learned.map((r) => r.command), ["ls"]);
  assert.deepEqual(plain(f.splitApprovalRules(undefined)), { manual: [], learned: [] });
});

test("a rule row becomes a rule: empty fields are left out and unknown keys of the edited rule stay", () => {
  const fields = { verdict: "allow", tool: "terminal", command: " git status ", path: "", backend: "pc", backendKind: "", dangerous: "", exact: true };
  assert.deepEqual(plain(f.buildApprovalRule(null, fields)), { verdict: "allow", tool: "terminal", command: "git status", backend: "pc", exact: true });
  const edited = plain(f.buildApprovalRule({ id: "my-rule", reason: "why", path: "/old", dangerous: true }, {
    verdict: "deny", tool: "file_write", command: "", path: "/etc/*", backend: "", backendKind: "machine", dangerous: "no", exact: false,
  }));
  assert.deepEqual(edited, { id: "my-rule", reason: "why", verdict: "deny", tool: "file_write", path: "/etc/*", backend_kind: "machine", dangerous: false });
  // exact needs a command or a path
  assert.equal("exact" in f.buildApprovalRule(null, { verdict: "ask", tool: "any", exact: true }), false);
  assert.equal(f.buildApprovalRule(null, { verdict: "ask" }).tool, "any");
  assert.equal(f.buildApprovalRule(null, { verdict: "ask", dangerous: "yes" }).dangerous, true);
});

test("the fields of a row are the inverse of building the rule", () => {
  const rule = { verdict: "ask", tool: "file_read", path: "/tmp/*", backend: "box", backend_kind: "sandbox", dangerous: false, exact: true };
  const fields = plain(f.approvalRuleFields(rule));
  assert.deepEqual(fields, { verdict: "ask", tool: "file_read", command: "", path: "/tmp/*", backend: "box", backendKind: "sandbox", dangerous: "no", exact: true });
  assert.deepEqual(plain(f.buildApprovalRule(rule, fields)), rule);
  assert.deepEqual(plain(f.approvalRuleFields(null)), {
    verdict: "ask", tool: "any", command: "", path: "", backend: "", backendKind: "", dangerous: "", exact: false,
  });
});

test("the saved list is the editor's rules then the learned ones, with the default only when chosen", () => {
  const manual = [{ verdict: "deny", command: "rm *" }];
  const learned = [{ verdict: "allow", command: "ls", exact: true, reason: "approved always for this agent" }];
  assert.deepEqual(plain(f.approvalRulesBody("", manual, learned)), { rules: [...manual, ...learned] });
  assert.deepEqual(plain(f.approvalRulesBody("ask", [], [])), { default: "ask", rules: [] });
});

test("the administrator screen lists the agents a lower ceiling would leave above it", () => {
  const agents = [{ id: "a", level: "reader" }, { id: "b", level: "operator" }, { id: "c", level: "admin" }];
  assert.deepEqual(plain(f.agentsAboveCeiling(catalog, agents, "collaborator")).map((a) => a.id), ["b", "c"]);
  assert.deepEqual(plain(f.agentsAboveCeiling(catalog, agents, "admin")), []);
  assert.deepEqual(plain(f.agentsAboveCeiling(catalog, undefined, "reader")), []);
});

test("only a user whose maximum is admin gets the Users section", () => {
  assert.equal(f.isAdministrator({ max_level: "admin" }), true);
  assert.equal(f.isAdministrator({ max_level: "operator" }), false);
  assert.equal(f.isAdministrator(null), false);
});
