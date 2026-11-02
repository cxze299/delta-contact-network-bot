import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { join } from "node:path";
export function uiVersion(assets: string): string {
  const hash = createHash("sha256");
  for (const name of ["index.html", "style.css", "app.js", "manifest.toml", "icon.png"]) {
    const data = readFileSync(join(assets, name));
    hash.update(name + ":" + data.length + ":").update(data);
  }
  return hash.digest("hex");
}
