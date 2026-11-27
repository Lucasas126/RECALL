const { test } = require("node:test");
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

// startsAgentReply of app.js (with the mention items it uses), loaded without the page
const source = readFileSync(path.join(__dirname, "..", "..", "..", "src", "web", "static", "app.js"), "utf8");
const take = (name) => source.match(new RegExp(`function ${name}\\([\\s\\S]*?\\r?\\n\\}\\r?\\n`))[0];
const context = vm.createContext({});
vm.runInContext([take("composerMentionItems"), take("startsAgentReply"), "this.startsAgentReply = startsAgentReply;"].join("\n"), context);

const agents = [
  { id: "nova", shared: true, members: [{ id: "user-a", name: "Ana" }, { id: "user-b", name: "Ben" }] },
  { id: "sage" },
];
const run = (text, agentId = "nova") => context.startsAgentReply(text, agents, agentId, "user-a");

test("a plain message or one for an agent starts a reply", () => {
  assert.equal(run("hello"), true);
  assert.equal(run("@sage hello"), true);
  assert.equal(run("@nobody hello"), true);
  assert.equal(run("@user-a me"), true);  // oneself is not a valid mention
});

test("the first valid mention being a member only delivers the message", () => {
  assert.equal(run("@user-b are you there?"), false);
  assert.equal(run("hi @user-b."), false);
  assert.equal(run("@sage then @user-b"), true);  // the first valid mention decides
  assert.equal(run("@user-b then @sage"), false);
});

test("people are only mentionable in a shared agent's chat", () => {
  assert.equal(run("@user-b hi", "sage"), true);
});
