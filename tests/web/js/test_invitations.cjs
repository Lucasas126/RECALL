const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");

const source = readFileSync(path.join(__dirname, "..", "..", "..", "src", "web", "static", "app.js"), "utf8");
const code = source.match(/\/\/ ---- settings helpers[\s\S]*?\/\/ ---- end settings helpers/)[0];
const context = vm.createContext({});
vm.runInContext(`${code}\nthis.api = { invitationText, invitationNote, memberRows, shareSignature, outgoingFor, navAttentionLevel };`, context);
const api = context.api;
const tr = (text, values = {}) => text.replace(/\{(\w+)\}/g, (_, key) => values[key]);  // like the page's tr()

test("the invitation text names the inviter and the agent, the note the billing user", () => {
  const invitation = { agent_id: "nova", agent_name: "Nova", inviter: "user-a", inviter_name: "User A", billing_user: "user-a" };
  assert.equal(api.invitationText(invitation, tr), "User A wants to share Nova with you");
  assert.equal(api.invitationNote(invitation, tr), "It uses the keys and Claude subscription of user-a. Every member sees all its chats.");
  assert.equal(api.invitationText({ agent_id: "nova", inviter: "user-a" }, tr), "user-a wants to share nova with you");
});

test("only the pending invitations of the open agent are listed", () => {
  const data = { outgoing: [
    { id: "1", agent_id: "nova", invitee: "user-b", status: "pending" },
    { id: "2", agent_id: "nova", invitee: "user-c", status: "declined" },
    { id: "3", agent_id: "sage", invitee: "user-b", status: "pending" },
  ] };
  assert.deepEqual(api.outgoingFor(data, "nova").map((i) => i.id), ["1"]);
  assert.equal(api.outgoingFor(null, "nova").length, 0);
});

test("the menu dot takes the strongest of chats and invitations", () => {
  assert.equal(api.navAttentionLevel("", 0), "");
  assert.equal(api.navAttentionLevel("", 2), "ready");
  assert.equal(api.navAttentionLevel("ready", 0), "ready");
  assert.equal(api.navAttentionLevel("approval", 1), "approval");
});

test("the members list puts (you) after the name and says whose providers are used", () => {
  const tr2 = (text, values = {}) => ({ "Only you": "Only you", "(you)": "(you)",
    "uses your providers and subscriptions": "uses your providers and subscriptions",
    "uses {user}'s providers and subscriptions": "uses {user}'s providers and subscriptions" })[text]?.replace(/\{(\w+)\}/g, (_, k) => values[k]) ?? text;
  const shared = { shared: true, billing_user: "user-a", members: [{ id: "user-a", name: "Ana" }, { id: "user-b", name: "Ben" }] };
  const rows = JSON.parse(JSON.stringify(api.memberRows(shared, "user-b", tr2)));
  assert.deepEqual(rows.map((r) => [r.id, r.name, r.you, r.suffix, r.note]), [
    ["user-a", "Ana", false, "", "uses Ana's providers and subscriptions"], ["user-b", "Ben", true, "(you)", ""]]);
  const own = api.memberRows({ shared: true, billing_user: "user-b", members: [{ id: "user-b", name: "" }] }, "user-b", tr2)[0];
  assert.equal(own.note, "uses your providers and subscriptions");
  const solo = api.memberRows({ id: "nova" }, "user-a", tr2);
  assert.equal(solo.length, 1);
  assert.equal(solo[0].name, "Only you");
});

test("the share signature changes when an outgoing invitation of the open agent is answered", () => {
  const before = { outgoing: [{ id: "i1", agent_id: "nova", status: "pending" }] };
  assert.equal(api.shareSignature(before, "nova"), "i1");
  assert.equal(api.shareSignature({ outgoing: [] }, "nova"), "");
  assert.equal(api.shareSignature(before, null), null);
});
