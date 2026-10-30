import { readFileSync, writeFileSync } from "node:fs";
writeFileSync(
  "webxdc/app.js",
  readFileSync("transport.js", "utf8") +
    "\n" +
    readFileSync("webxdc/ui.js", "utf8"),
);
