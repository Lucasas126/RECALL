const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");

// the turn-timeline and compact tool-step helpers, loaded without the DOM
const source = readFileSync(path.join(__dirname, "..", "src", "web", "static", "app.js"), "utf8");
const code = source.slice(source.indexOf("function timelineKind("), source.indexOf("// A collapsible thinking segment"));
const context = vm.createContext({});
vm.runInContext(
  `${code}\nthis.buildTimeline = buildTimeline; this.historySteps = historySteps; this.thinkingPreview = thinkingPreview; this.actionSummary = actionSummary; this.actionArgsText = actionArgsText;`,
  context,
);
const plain = (value) => JSON.parse(JSON.stringify(value));

test("reasoning bursts are interleaved with tool steps and text", () => {
  const tool = { name: "terminal", arguments: { command: "ls" } };
  const segments = context.buildTimeline([
    { kind: "reasoning", payload: "Let me " }, { kind: "reasoning", payload: "look." },
    { kind: "tool", payload: tool },
    { kind: "reasoning", payload: "Found it." },
    { kind: "text", payload: "Done" }, { kind: "text", payload: "!" },
  ]);
  assert.deepEqual(plain(segments).map((s) => s.type), ["reasoning", "tool", "reasoning", "text"]);
  assert.equal(segments[0].text, "Let me look.");
  assert.equal(segments[3].text, "Done!");
});

test("whitespace-only chunks never open a segment", () => {
  const segments = context.buildTimeline([{ kind: "text", payload: "\n" }, { kind: "reasoning", payload: "  " }, { kind: "text", payload: "Hi" }]);
  assert.deepEqual(plain(segments), [{ type: "text", text: "Hi" }]);
});

test("stored history follows the timeline and keeps unlisted tools", () => {
  const actions = [{ name: "a" }, { name: "b" }];
  const steps = context.historySteps({
    role: "assistant", content: "answer", actions,
    timeline: [{ type: "reasoning", text: "think" }, { type: "tool", index: 0 }, { type: "text", text: "answer" }],
  });
  assert.deepEqual(plain(steps).map((s) => s.type + (s.action ? `:${s.action.name}` : "")), ["reasoning", "tool:a", "text", "tool:b"]);
});

test("old messages without a timeline keep the old layout", () => {
  const steps = context.historySteps({ role: "assistant", content: "x", reasoning: "r", actions: [{ name: "t" }] });
  assert.deepEqual(plain(steps).map((s) => s.type), ["reasoning", "tool", "text"]);
});

test("a long multi-line command collapses to its first line", () => {
  const info = context.actionSummary({ name: "terminal", arguments: { command: "\n  cd /app &&\n  python -m src.admin agent list\n", backend: "local" } });
  assert.equal(info.name, "terminal");
  assert.equal(info.backend, "local");
  assert.equal(info.line, "cd /app &&");
  assert.match(info.full, /python -m src\.admin agent list/);
});

test("other arguments become a key=value line and the block is JSON", () => {
  const action = { name: "sandbox_read", arguments: { path: "a.txt", offset: 1 } };
  assert.equal(context.actionSummary(action).line, "path=a.txt, offset=1");
  assert.deepEqual(JSON.parse(context.actionArgsText(action)), { path: "a.txt", offset: 1 });
});

test("thinking preview is one trimmed line", () => {
  assert.equal(context.thinkingPreview("  a\n b  "), "a b");
  assert.equal(context.thinkingPreview("x".repeat(100), 10), "xxxxxxxxxx…");
});
