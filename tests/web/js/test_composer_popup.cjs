// Composer popup: trigger detection at the caret, filtering and selection movement (DOM-free helpers of app.js).
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "..", "..", "..", "src", "web", "static", "app.js"), "utf8");
const names = ["composerTrigger", "composerFilter", "composerMove", "composerInsert"];
let code = "";
for (const name of names) {
  const match = source.match(new RegExp(`function ${name}\\([\\s\\S]*?\\n}\\n`));
  assert.ok(match, `${name} not found in app.js`);
  code += match[0] + `\nthis.${name} = ${name};\n`;
}
const commands = source.match(/const COMPOSER_COMMANDS = \[[\s\S]*?\n\];/);
assert.ok(commands, "COMPOSER_COMMANDS not found");
const context = vm.createContext({});
vm.runInContext(`${commands[0]}\nthis.COMPOSER_COMMANDS = COMPOSER_COMMANDS;\n${code}`, context);
const plain = (value) => JSON.parse(JSON.stringify(value));

test("slash opens only at the very start of the message", () => {
  assert.deepEqual(plain(context.composerTrigger("/", 1)), { kind: "command", query: "", start: 0, end: 1 });
  assert.deepEqual(plain(context.composerTrigger("/du", 3)), { kind: "command", query: "du", start: 0, end: 3 });
  assert.equal(context.composerTrigger("hello /du", 9), null);
  assert.equal(context.composerTrigger(" /du", 4), null);
  assert.equal(context.composerTrigger("/dream start", 12), null);
  assert.equal(context.composerTrigger("/dream", 3).query, "dr");
});

test("at-sign opens at the start or after a space or newline", () => {
  assert.deepEqual(plain(context.composerTrigger("@no", 3)), { kind: "agent", query: "no", start: 0, end: 3 });
  assert.deepEqual(plain(context.composerTrigger("hi @sa", 6)), { kind: "agent", query: "sa", start: 3, end: 6 });
  assert.equal(context.composerTrigger("hi\n@", 4).kind, "agent");
  assert.equal(context.composerTrigger("mail@sa", 7), null);
  assert.equal(context.composerTrigger("@nova hello", 11), null);
});

test("filter ranks prefix matches over substring ones and keeps order", () => {
  const items = [{ id: "atlas", name: "Atlas" }, { id: "nova", name: "Nova" }, { id: "x1", name: "Sage nova" }];
  assert.deepEqual(plain(context.composerFilter(items, "").map((i) => i.id)), ["atlas", "nova", "x1"]);
  assert.deepEqual(plain(context.composerFilter(items, "NO").map((i) => i.id)), ["nova", "x1"]);
  assert.deepEqual(plain(context.composerFilter(items, "sa").map((i) => i.id)), ["x1"]);
  assert.deepEqual(plain(context.composerFilter(items, "zzz")), []);
  assert.deepEqual(plain(context.composerFilter(context.COMPOSER_COMMANDS, "u").map((i) => i.id)), ["usage"]);
});

test("move wraps around and handles an empty list", () => {
  assert.equal(context.composerMove(0, 1, 3), 1);
  assert.equal(context.composerMove(2, 1, 3), 0);
  assert.equal(context.composerMove(0, -1, 3), 2);
  assert.equal(context.composerMove(0, 1, 0), -1);
});

test("insert replaces the token and adds one space", () => {
  const trigger = context.composerTrigger("hi @sa", 6);
  assert.deepEqual(plain(context.composerInsert("hi @sa", trigger, { id: "sage" })), { text: "hi @sage ", caret: 9 });
  const command = context.composerTrigger("/co", 3);
  assert.deepEqual(plain(context.composerInsert("/co", command, { id: "compact" })), { text: "/compact ", caret: 9 });
  const mid = context.composerTrigger("@no there", 3);
  assert.deepEqual(plain(context.composerInsert("@no there", mid, { id: "nova" })), { text: "@nova there", caret: 6 });
});

test("the command list is the three slash commands, each described", () => {
  assert.deepEqual(plain(context.COMPOSER_COMMANDS.map((c) => c.id)), ["dream", "compact", "usage"]);
  assert.ok(context.COMPOSER_COMMANDS.every((c) => c.description.length > 0));
});
