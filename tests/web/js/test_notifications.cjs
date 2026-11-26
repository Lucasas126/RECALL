const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");

// the pure notification helpers, loaded without the DOM
const source = readFileSync(path.join(__dirname, "..", "..", "..", "src", "web", "static", "app.js"), "utf8");
const code = source.slice(source.indexOf("// ---- notifications: pure helpers"), source.indexOf("// ---- end notifications: pure helpers"));
const context = vm.createContext({});
vm.runInContext(`${code}\nObject.assign(this, { notifySupported, notifyBellOn, notifyBellClick, notifyShouldShow, notifyContent, notifyChatChanges });`, context);
const plain = (value) => JSON.parse(JSON.stringify(value));
const base = { kind: "reply", bell: true, supported: true, permission: "granted", hidden: true, focused: false };

test("support is detected from the window", () => {
  assert.equal(context.notifySupported({ Notification: function () {} }), true);
  assert.equal(context.notifySupported({}), false);
  assert.equal(context.notifySupported(undefined), false);
});

test("the bell is on only with the stored choice, the permission and support", () => {
  assert.equal(context.notifyBellOn("on", "granted", true), true);
  assert.equal(context.notifyBellOn(null, "granted", true), false);
  assert.equal(context.notifyBellOn("off", "granted", true), false);
  assert.equal(context.notifyBellOn("on", "denied", true), false);
  assert.equal(context.notifyBellOn("on", "granted", false), false);
});

test("a click asks, explains the block, or toggles", () => {
  assert.equal(context.notifyBellClick("default", false), "request");
  assert.equal(context.notifyBellClick("denied", false), "denied");
  assert.equal(context.notifyBellClick("granted", false), "on");
  assert.equal(context.notifyBellClick("granted", true), "off");
});

test("it shows only for the three triggers, when allowed and away", () => {
  for (const kind of ["reply", "approval", "automation"]) assert.equal(context.notifyShouldShow({ ...base, kind }), true);
  assert.equal(context.notifyShouldShow({ ...base, kind: "other" }), false);
  assert.equal(context.notifyShouldShow({ ...base, bell: false }), false);
  assert.equal(context.notifyShouldShow({ ...base, permission: "default" }), false);
  assert.equal(context.notifyShouldShow({ ...base, supported: false }), false);
  assert.equal(context.notifyShouldShow({ ...base, hidden: false, focused: true }), false);  // looking at the app
  assert.equal(context.notifyShouldShow({ ...base, hidden: false, focused: false }), true);  // visible but another window has focus
  assert.equal(context.notifyShouldShow({ ...base, hidden: true, focused: true }), true);
});

test("the text is generic, with the title only, and tagged per chat", () => {
  const tr = (text) => `T:${text}`;
  assert.deepEqual(plain(context.notifyContent("reply", "My chat", tr, "a1", "c1")), {
    title: "T:Reply ready", body: "My chat", tag: "chat:a1:c1", data: { kind: "reply", agent_id: "a1", id: "c1" },
  });
  assert.equal(context.notifyContent("approval", "My chat", tr, "a1", "c1").tag, "chat:a1:c1");
  assert.equal(context.notifyContent("automation", "Daily", tr, "", "x9").tag, "automation:x9");
  assert.equal(context.notifyContent("automation", "Daily", tr, "", "x9").title, "T:Automation finished");
  assert.equal(context.notifyContent("reply", "My chat", tr, "a1", "c1", "Atlas").title, "Atlas · T:Reply ready");
  assert.equal(context.notifyContent("reply", "My chat", tr, "a1", "c1", "Atlas").body, "My chat");
});

test("chat changes: nothing on the first look, then replies and approvals once", () => {
  const chat = (extra) => ({ agent_id: "a", id: "c", title: "T", unread: false, pending_approvals: 0, ...extra });
  const first = context.notifyChatChanges(null, [chat({ unread: true })]);
  assert.deepEqual(plain(first.events), []);
  const quiet = context.notifyChatChanges(plain(first.next), [chat({ unread: true })]);
  assert.deepEqual(plain(quiet.events), []);
  const reply = context.notifyChatChanges({ "a/c": { unread: false, approvals: 0 } }, [chat({ unread: true })]);
  assert.deepEqual(plain(reply.events), [{ kind: "reply", agent_id: "a", id: "c", title: "T" }]);
  const approval = context.notifyChatChanges({ "a/c": { unread: false, approvals: 0 } }, [chat({ pending_approvals: 1 })]);
  assert.deepEqual(plain(approval.events).map((e) => e.kind), ["approval"]);
  const fresh = context.notifyChatChanges({}, [chat({ unread: true })]);
  assert.equal(fresh.events.length, 0);  // a chat that just entered the list is not news
});
