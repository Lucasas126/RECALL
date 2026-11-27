const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");

// the pure helpers of the in-app confirmation dialog, loaded without the DOM
const source = readFileSync(path.join(__dirname, "..", "..", "..", "src", "web", "static", "app.js"), "utf8");
const code = source.slice(source.indexOf("// ---- confirm dialog: pure helpers"), source.indexOf("// ---- end confirm dialog"));
const context = vm.createContext({});
vm.runInContext(`${code}\nObject.assign(this, { confirmSpec, confirmTextMatches, serialRunner });`, context);
const plain = (value) => JSON.parse(JSON.stringify(value));
const translate = (text) => text;

test("a plain message gets the default labels and is not dangerous", () => {
  assert.deepEqual(plain(context.confirmSpec("Sure?", translate)), {
    title: "", message: "Sure?", confirmLabel: "Confirm", cancelLabel: "Cancel", danger: false, requireText: "", inputLabel: "",
  });
});

test("named labels, title and danger are kept; labels go through the translator", () => {
  const spec = context.confirmSpec({ title: "T", message: "M", confirmLabel: "Delete chat", danger: 1 }, (text) => `<${text}>`);
  assert.deepEqual(plain(spec), { title: "T", message: "M", confirmLabel: "Delete chat", cancelLabel: "<Cancel>", danger: true, requireText: "", inputLabel: "" });
});

test("no options give empty texts rather than throwing", () => {
  assert.equal(context.confirmSpec(undefined, translate).message, "");
});

test("confirmations run one after the other, even when one fails", async () => {
  const run = context.serialRunner();
  const log = [];
  const slow = run(() => new Promise((resolve) => setTimeout(() => { log.push("a"); resolve(1); }, 20)));
  const failing = run(() => { log.push("b"); throw new Error("x"); });
  const last = run(() => { log.push("c"); return 3; });
  assert.equal(await slow, 1);
  await assert.rejects(failing);
  assert.equal(await last, 3);
  assert.deepEqual(log, ["a", "b", "c"]);
});

test("a required text enables the confirm button only when typed exactly", () => {
  assert.equal(context.confirmTextMatches("user-b", "user-b"), true);
  for (const typed of ["", "user-", "User-b", " user-b", "user-b "]) assert.equal(context.confirmTextMatches("user-b", typed), false);
  assert.equal(context.confirmTextMatches("", "anything"), true);
  assert.equal(context.confirmSpec({ message: "m", requireText: "user-b" }, translate).requireText, "user-b");
});
