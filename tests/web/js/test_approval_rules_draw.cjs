const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");

const source = readFileSync(path.join(__dirname, "..", "..", "..", "src", "web", "static", "app.js"), "utf8");
const code = source.match(/function drawApprovalRules\(entries\) \{[\s\S]*?\n\}\n/)[0];

// The agent editor draws the learned approval rules of whatever agent it opens; it used to read a `chats` variable that
// only exists inside refreshChats, so opening an editor threw "chats is not defined" and the error stayed on screen.
test("drawing the approval rules needs nothing outside its own inputs", () => {
  const elements = {};
  const element = () => ({ hidden: false, replaceChildren() {}, append() {}, addEventListener() {} });
  const context = vm.createContext({
    state: { agentForm: {}, chatId: "c1", agentId: "nova" },
    document: { hidden: false, createElement: element },
    $: (id) => (elements[id] ??= element()),
    tr: (text) => text,
    approvalRuleText: () => "",
    changeApprovalRules: () => {},
    updateRuleCounts: () => {},
  });
  vm.runInContext(code, context);
  assert.doesNotThrow(() => context.drawApprovalRules([{ learned: true, rule: { verdict: "allow" }, index: 0 }]));
  assert.equal(context.state.agentForm.learned.length, 1);
});
