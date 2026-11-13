// Ctrl+V of an image or file attaches it; pasted text always stays text. Runs the real function from app.js.
const assert = require("assert");
const fs = require("fs");
const path = require("path");

const src = fs.readFileSync(path.join(__dirname, "..", "src", "web", "static", "app.js"), "utf8");
const start = src.indexOf("function pastedFiles(clipboard)");
const end = src.indexOf('$("input").addEventListener("paste"');
assert(start > 0 && end > start, "pastedFiles not found in app.js");
const pastedFiles = new Function("File", src.slice(start, end) + "; return pastedFiles;")(File);

const clip = (text, files) => ({
  getData: (type) => (type === "text/plain" ? text : ""),
  items: files.map((file) => ({ kind: "file", getAsFile: () => file })),
});
const shot = () => new File(["x"], "image.png", { type: "image/png" });

const [one] = pastedFiles(clip("", [shot()]));
assert(/^pasted-\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}\.png$/.test(one.name), "a screenshot gets a unique name");
assert.strictEqual(one.type, "image/png");

const two = pastedFiles(clip("", [shot(), shot()]));
assert.notStrictEqual(two[0].name, two[1].name, "two screenshots do not share a name");

const pdf = new File(["x"], "report.pdf", { type: "application/pdf" });
assert.strictEqual(pastedFiles(clip("", [pdf]))[0].name, "report.pdf", "a named file keeps its name");
assert.strictEqual(pastedFiles(clip("a copied cell", [shot()])).length, 0, "text wins over its picture");
assert.strictEqual(pastedFiles(null).length, 0);
assert.strictEqual(pastedFiles(clip("", [])).length, 0);
console.log("ok");
