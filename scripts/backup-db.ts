import { Database } from "bun:sqlite";
import { createCipheriv, randomBytes, pbkdf2Sync } from "node:crypto";
import {
  mkdirSync,
  writeFileSync,
  readdirSync,
  statSync,
  unlinkSync,
} from "node:fs";
import { join } from "node:path";
process.umask(0o077);
const password = process.env.CONTACTBOT_BACKUP_PASSWORD;
if (!password || password.length < 16)
  throw Error("Independent backup password required");
const path =
    process.env.CONTACTBOT_BUN_DB || "/app/data/bun-contactbot.sqlite3",
  dir = process.env.CONTACTBOT_BACKUP_DIR || "/app/data/backups";
mkdirSync(dir, { recursive: true, mode: 0o700 });
const db = new Database(path, { readonly: true });
db.exec("PRAGMA busy_timeout=5000; BEGIN");
const statement = db.prepare("SELECT count(*) FROM sqlite_schema");
statement.get();
statement.finalize();
const snapshot = db.serialize();
db.exec("ROLLBACK");
db.close(true);
const salt = randomBytes(16),
  iv = randomBytes(12),
  key = pbkdf2Sync(password, salt, 250000, 32, "sha256"),
  cipher = createCipheriv("aes-256-gcm", key, iv),
  body = Buffer.concat([cipher.update(snapshot), cipher.final()]);
const stamp = new Date()
  .toISOString()
  .replace(/[-:]/g, "")
  .replace(/\.\d{3}Z$/, "Z");
writeFileSync(
  join(dir, "database-" + stamp + ".enc"),
  Buffer.concat([Buffer.from("FRIEND01"), salt, iv, cipher.getAuthTag(), body]),
  { mode: 0o600, flag: "wx" },
);
for (const name of readdirSync(dir)) {
  if (/^database-\d{8}T\d{6}Z\.enc$/.test(name)) {
    const file = join(dir, name);
    if (statSync(file).mtimeMs < Date.now() - 30 * 86400000) unlinkSync(file);
  }
}
console.log("Encrypted database backup created");
