import {
  createCipheriv,
  createDecipheriv,
  randomBytes,
  pbkdf2Sync,
} from "node:crypto";
import { readFileSync, writeFileSync } from "node:fs";
const [mode, input, output] = process.argv.slice(2),
  password = process.env.CONTACTBOT_BACKUP_PASSWORD;
if (!password || password.length < 16)
  throw Error("CONTACTBOT_BACKUP_PASSWORD must contain at least 16 characters");
if (mode === "encrypt") {
  const salt = randomBytes(16),
    iv = randomBytes(12),
    key = pbkdf2Sync(password, salt, 250000, 32, "sha256"),
    cipher = createCipheriv("aes-256-gcm", key, iv),
    body = Buffer.concat([cipher.update(readFileSync(input)), cipher.final()]);
  writeFileSync(
    output,
    Buffer.concat([
      Buffer.from("FRIEND01"),
      salt,
      iv,
      cipher.getAuthTag(),
      body,
    ]),
    { mode: 0o600, flag: "wx" },
  );
} else if (mode === "decrypt") {
  const raw = readFileSync(input);
  if (raw.subarray(0, 8).toString() !== "FRIEND01")
    throw Error("Invalid backup header");
  const salt = raw.subarray(8, 24),
    iv = raw.subarray(24, 36),
    tag = raw.subarray(36, 52),
    key = pbkdf2Sync(password, salt, 250000, 32, "sha256"),
    decipher = createDecipheriv("aes-256-gcm", key, iv);
  decipher.setAuthTag(tag);
  const body = Buffer.concat([
    decipher.update(raw.subarray(52)),
    decipher.final(),
  ]);
  writeFileSync(output, body, { mode: 0o600, flag: "wx" });
} else throw Error("Unknown mode");
