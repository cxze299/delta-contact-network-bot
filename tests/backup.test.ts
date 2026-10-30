import { expect, test } from "bun:test";
import { mkdtempSync, writeFileSync, readdirSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { fixture } from "./fixture";
import { Store } from "../src/db";

test("online WAL backup encrypts and restores a valid consistent database", async () => {
  const f = fixture(),
    folder = mkdtempSync(join(tmpdir(), "friend-backup-")),
    path = join(folder, "db.sqlite3"),
    out = join(folder, "backups");
  let source: Store | undefined, restored: Store | undefined;
  try {
    writeFileSync(path, f.s.db.serialize());
    source = new Store(path);
    source.tx(() =>
      source!.run(
        "UPDATE users SET current_network_id=42 WHERE id=?",
        f.user.id,
      ),
    );
    const env = {
      ...process.env,
      CONTACTBOT_BUN_DB: path,
      CONTACTBOT_BACKUP_DIR: out,
      CONTACTBOT_BACKUP_PASSWORD: "isolated-backup-test-password",
    };
    let proc = Bun.spawn([process.execPath, resolve("scripts/backup-db.ts")], {
      env,
      stdout: "pipe",
      stderr: "pipe",
    });
    const error = await new Response(proc.stderr).text();
    expect(await proc.exited).toBe(0);
    expect(error).toBe("");
    const backup = join(out, readdirSync(out)[0]),
      target = join(folder, "restored.sqlite3");
    proc = Bun.spawn(
      [
        process.execPath,
        resolve("scripts/backup-crypto.ts"),
        "decrypt",
        backup,
        target,
      ],
      { env, stdout: "pipe", stderr: "pipe" },
    );
    expect(await proc.exited).toBe(0);
    restored = new Store(target);
    expect(restored.one("PRAGMA integrity_check")!.integrity_check).toBe("ok");
    expect(
      restored.one(
        "SELECT current_network_id FROM users WHERE id=?",
        f.user.id,
      )!.current_network_id,
    ).toBe(42);
  } finally {
    restored?.close();
    source?.close();
    f.s.close();
    rmSync(folder, { recursive: true, force: true });
  }
}, 30000);
