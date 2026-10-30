import { readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { zipSync, strToU8 } from "fflate";
import { join } from "node:path";
mkdirSync("dist", { recursive: true });
const dest = process.argv[2] || "dist/好友助手.xdc";
const entries: Record<string, Uint8Array> = {};
for (const name of [
  "index.html",
  "app.js",
  "style.css",
  "manifest.toml",
  "icon.png",
])
  entries[name] = new Uint8Array(readFileSync(join("webxdc", name)));
entries["config.js"] = strToU8("window.CONTACT_CONFIG={};");
writeFileSync(dest, zipSync(entries), { mode: 0o600 });
console.log("Packaged Webxdc");
