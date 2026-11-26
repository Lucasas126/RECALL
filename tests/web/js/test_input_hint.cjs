// The composer prompt overlay: when does it scroll, and how fast (pure helpers of app.js).
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "..", "..", "..", "src", "web", "static", "app.js"), "utf8");
const shift = source.match(/function hintShift\([\s\S]*?\n}\n/);
const duration = source.match(/function hintDuration\([\s\S]*?\n}\n/);
assert.ok(shift && duration, "hintShift/hintDuration not found in app.js");
const context = vm.createContext({});
vm.runInContext(`${shift[0]}\n${duration[0]}\nthis.hintShift = hintShift; this.hintDuration = hintDuration;`, context);

test("text that fits does not scroll", () => {
  assert.equal(context.hintShift(100, 200, false), 0);
  assert.equal(context.hintShift(200, 200, false), 0);
  assert.equal(context.hintShift(200.5, 200, false), 0);   // sub-pixel rounding noise
});

test("wider text scrolls by its overflow", () => {
  assert.equal(context.hintShift(320, 200, false), 120);
  assert.equal(context.hintShift(200.4, 190, false), 11);
});

test("reduced motion and unmeasured boxes never scroll", () => {
  assert.equal(context.hintShift(320, 200, true), 0);
  assert.equal(context.hintShift(0, 200, false), 0);
  assert.equal(context.hintShift(320, 0, false), 0);
  assert.equal(context.hintShift(NaN, 200, false), 0);
});

test("the loop is slow and grows with the distance", () => {
  assert.equal(context.hintDuration(10), 8);
  assert.ok(context.hintDuration(500) > context.hintDuration(100));
});
