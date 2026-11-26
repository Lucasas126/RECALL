const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");

const source = readFileSync(path.join(__dirname, "..", "..", "..", "src", "web", "static", "app.js"), "utf8");
const code = source.match(/\/\/ ---- settings helpers[\s\S]*?\/\/ ---- end settings helpers/)[0];
const context = vm.createContext({});
vm.runInContext(`${code}\nthis.api = { invitationText, invitationNote, outgoingFor, setNavDotSource };`, context);
const api = context.api;
const tr = (text) => text;

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

test("the nav dot shows while any source wants it", () => {
  const sources = new Set();
  assert.equal(api.setNavDotSource(sources, "invitations", true), true);
  assert.equal(api.setNavDotSource(sources, "chats", true), true);
  assert.equal(api.setNavDotSource(sources, "invitations", false), true);
  assert.equal(api.setNavDotSource(sources, "chats", false), false);
});
