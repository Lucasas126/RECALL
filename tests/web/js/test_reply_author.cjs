const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");

// replyAuthor of app.js, loaded without the page: who a reply is shown as (the chat's agent or an @mentioned one)
const source = readFileSync(path.join(__dirname, "..", "..", "..", "src", "web", "static", "app.js"), "utf8");
const take = (name) => source.match(new RegExp(`function ${name}\\([\\s\\S]*?\\r?\\n\\}\\r?\\n`))[0];
const context = vm.createContext({});
vm.runInContext([take("replyAuthor"), "this.replyAuthor = replyAuthor;"].join("\n"), context);
const plain = (value) => JSON.parse(JSON.stringify(value));

const avatar = { url: "/api/agents/sage/avatar", version: "1", style: "circle" };
const agents = [{ id: "nova", name: "Nova", avatar: null }, { id: "sage", name: "Sage", avatar }];

test("a reply of a mentioned agent uses that agent's current name and picture", () => {
  assert.deepEqual(plain(context.replyAuthor("sage", "Old name", agents, "nova")), { id: "sage", name: "Sage", avatar });
});

test("without an author the chat's own agent is used", () => {
  assert.deepEqual(plain(context.replyAuthor(undefined, undefined, agents, "nova")), { id: "nova", name: "Nova", avatar: null });
});

test("a deleted agent falls back to the stored name, then the id, without a picture", () => {
  assert.deepEqual(plain(context.replyAuthor("atlas", "Atlas", agents, "nova")), { id: "atlas", name: "Atlas", avatar: null });
  assert.deepEqual(plain(context.replyAuthor("atlas", undefined, null, "nova")), { id: "atlas", name: "atlas", avatar: null });
});
