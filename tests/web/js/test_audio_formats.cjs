// The speech-to-text endpoint needs the audio format name; Safari records MP4 (m4a), not webm. Runs the real code from app.js.
const assert = require("assert");
const fs = require("fs");
const path = require("path");

const src = fs.readFileSync(path.join(__dirname, "..", "..", "..", "src", "web", "static", "app.js"), "utf8");
const start = src.indexOf("// ---- audio formats");
const end = src.indexOf("// ---- end audio formats");
assert(start > 0 && end > start, "audio formats block not found in app.js");
const { audioFormat, recordingType, recordingExtension } = new Function(
  src.slice(start, end) + "; return { audioFormat, recordingType, recordingExtension };")();

assert.strictEqual(audioFormat("audio/mp4"), "m4a");
assert.strictEqual(audioFormat("audio/mp4;codecs=mp4a.40.2"), "m4a");
assert.strictEqual(audioFormat("audio/x-m4a"), "m4a");
assert.strictEqual(audioFormat("audio/aac"), "aac");
assert.strictEqual(audioFormat("audio/mpeg"), "mp3");
assert.strictEqual(audioFormat("audio/x-wav"), "wav");
assert.strictEqual(audioFormat("audio/webm;codecs=opus"), null);

assert.strictEqual(recordingExtension("audio/mp4"), "m4a");
assert.strictEqual(recordingExtension("audio/mp4;codecs=mp4a.40.2"), "m4a");
assert.strictEqual(recordingExtension("audio/webm;codecs=opus"), "webm");
assert.strictEqual(recordingExtension("audio/ogg;codecs=opus"), "ogg");
assert.strictEqual(recordingExtension("audio/aac"), "aac");

// Safari supports mp4 only; Chrome prefers webm/opus; an unknown browser lets the recorder choose
assert.strictEqual(recordingType((t) => t === "audio/mp4"), "audio/mp4");
assert.strictEqual(recordingType(() => true), "audio/webm;codecs=opus");
assert.strictEqual(recordingType(() => false), "");
console.log("ok");
