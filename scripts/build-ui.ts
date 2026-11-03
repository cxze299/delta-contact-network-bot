import { readFileSync, writeFileSync } from "node:fs";
const bundle = readFileSync("transport.js", "utf8") + "\n" + readFileSync("webxdc/ui.js", "utf8");
// Validate without executing. Never replace a working package with invalid JS.
new Function(bundle);
if (bundle.includes("\uFFFD")) throw new Error("UI source contains invalid UTF-8 replacement characters");
writeFileSync("webxdc/app.js", bundle, "utf8");
