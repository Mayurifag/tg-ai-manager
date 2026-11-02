const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const context = {window: {}};
vm.createContext(context);
vm.runInContext(fs.readFileSync("static/libs/twemoji/twemoji.min.js", "utf8"), context);
vm.runInContext(fs.readFileSync("static/js/apple-emoji-map.js", "utf8"), context);
const mapping = context.window.TG_APPLE_EMOJI_ASSETS;

for (const [emoji, filename] of [
  ["❤️", "2764-fe0f.png"], ["☀️", "2600-fe0f.png"], ["⚙️", "2699-fe0f.png"],
  ["✏️", "270f-fe0f.png"], ["🗑️", "1f5d1-fe0f.png"], ["👍", "1f44d.png"],
  ["1️⃣", "0031-fe0f-20e3.png"], ["👩🏽‍💻", "1f469-1f3fd-200d-1f4bb.png"],
  ["🇺🇦", "1f1fa-1f1e6.png"],
]) {
  let mapped;
  context.twemoji.parse(emoji, {callback(key) { mapped = mapping[key]; return null; }});
  assert.equal(mapped, filename, emoji);
  assert.ok(fs.existsSync(`static/emoji/apple/${mapped}`), emoji);
}

for (const filename of Object.values(mapping)) {
  assert.ok(fs.existsSync(`static/emoji/apple/${filename}`), filename);
}

// A missing local image must not turn native Unicode into a broken image.
const original = "❤️";
assert.equal(context.twemoji.parse(original, {callback() { return null; }}), original);
