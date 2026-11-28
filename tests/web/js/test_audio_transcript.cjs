// A voice message shows what was heard once, under its player: the raw "[AUDIO] Audio transcription:" block the model
// reads is left out of the visible text, and a live transcript is attached to the player already on screen.
const assert = require("assert");
const fs = require("fs");
const path = require("path");

const src = fs.readFileSync(path.join(__dirname, "..", "..", "..", "src", "web", "static", "app.js"), "utf8");
const start = src.indexOf("function visibleUserText(text, media)");
const end = src.indexOf("\n}\n", start) + 3;
assert(start > 0 && end > start, "visibleUserText not found in app.js");
const visibleUserText = new Function(src.slice(start, end) + "; return visibleUserText;")();
assert(src.includes("view.text(visibleUserText(m.content, m.media))"), "history must render the stripped text");
assert(src.includes('event === "transcript"'), "the transcript event must be handled");

const audio = [{ modality: "audio", transcript: "Hello there" }];
assert.strictEqual(visibleUserText("[AUDIO] Audio transcription:\nHello there", audio), "Hello there");
assert.strictEqual(visibleUserText("see this\n\n[AUDIO] Audio transcription:\nHello there", audio), "see this\n\nHello there");
assert.strictEqual(
  visibleUserText("a\n\n[AUDIO] Audio transcription:\nHello there\n\nb", audio), "a\n\nHello there\n\nb");
// without a stored transcript nothing is hidden, and plain text is untouched
assert.strictEqual(
  visibleUserText("[AUDIO] Audio transcription:\nHello there", [{ modality: "audio" }]),
  "[AUDIO] Audio transcription:\nHello there");
assert.strictEqual(visibleUserText("hi", audio), "hi");
const parts = [{ type: "text", text: "x" }];
assert.strictEqual(visibleUserText(parts, audio), parts);
// the "[FILE] Original of ..." lines for the agent are never shown, with or without a transcript
const note = "[FILE] Original of 'voice.webm' saved at /data/u/.uploads/c1/20261126-voice.webm (read-only source address: uploads:c1/20261126-voice.webm)";
assert.strictEqual(visibleUserText(`[AUDIO] Audio transcription:\nHello there\n\n${note}`, audio), "Hello there");
assert.strictEqual(visibleUserText(`look\n\n${note}\n${note.replace("voice.webm", "b.pdf")}`, []), "look");
assert.strictEqual(visibleUserText(note, [{ modality: "image" }]), "");
assert.strictEqual(visibleUserText("I wrote [FILE] Original of x", []), "I wrote [FILE] Original of x");
// a message with an image keeps its content as a list of parts: the text part is cleaned, the rest is untouched
const image = { type: "image_url", image_url: { url: "data:image/png;base64,AA" } };
const mixed = [{ type: "text", text: `[AUDIO] Audio transcription:\nHello there\n\n${note}` }, image];
const cleaned = visibleUserText(mixed, [{ modality: "image" }, ...audio]);
assert.strictEqual(cleaned[0].text, "Hello there");
assert.strictEqual(cleaned[1], image);
console.log("ok");
