import { Database, type Statement } from "bun:sqlite";
import { mkdirSync, readFileSync } from "node:fs";
import { dirname } from "node:path";
export type Row = Record<string, any>;
export class Store {
  readonly db: Database;
  private statements = new Map<string, Statement>();
  constructor(readonly path: string) {
    if (path !== ":memory:")
      mkdirSync(dirname(path), { recursive: true, mode: 0o700 });
    this.db = new Database(path, { create: true });
    this.db.exec(
      readFileSync(new URL("../schema.sql", import.meta.url), "utf8"),
    );
    this.db.exec(`PRAGMA busy_timeout=5000;
      CREATE TABLE IF NOT EXISTS v2_users(user_id INTEGER PRIMARY KEY REFERENCES users(id),address TEXT NOT NULL,fingerprint TEXT NOT NULL DEFAULT '',deleted_at TEXT);
      CREATE TABLE IF NOT EXISTS v2_instances(token TEXT PRIMARY KEY REFERENCES xdc_instances(token) ON DELETE CASCADE,user_id INTEGER NOT NULL REFERENCES users(id),page INTEGER NOT NULL DEFAULT 0,admin_network INTEGER,admin_page INTEGER NOT NULL DEFAULT 0,revision INTEGER NOT NULL DEFAULT 0,snapshot TEXT);
      CREATE TABLE IF NOT EXISTS v2_results(token TEXT NOT NULL REFERENCES v2_instances(token) ON DELETE CASCADE,request_id TEXT NOT NULL,digest TEXT NOT NULL,ok INTEGER NOT NULL,error TEXT,message TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL,PRIMARY KEY(token,request_id));
      CREATE TABLE IF NOT EXISTS v2_jobs(id INTEGER PRIMARY KEY,request_id INTEGER REFERENCES contact_requests(id),recipient INTEGER NOT NULL REFERENCES users(id),source INTEGER NOT NULL REFERENCES users(id),status TEXT NOT NULL DEFAULT 'pending',core_message_id INTEGER,started_at TEXT,UNIQUE(request_id,recipient));
      CREATE TABLE IF NOT EXISTS v2_tombstones(actor_key TEXT PRIMARY KEY,deleted_at TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS v2_app_updates(token TEXT NOT NULL REFERENCES xdc_instances(token) ON DELETE CASCADE,request_id TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',PRIMARY KEY(token,request_id));
      CREATE TABLE IF NOT EXISTS v2_network_invites(id INTEGER PRIMARY KEY,network_id INTEGER NOT NULL REFERENCES networks(id),creator_id INTEGER NOT NULL REFERENCES users(id),token_hash TEXT NOT NULL UNIQUE,token_cipher TEXT NOT NULL,expires_at TEXT NOT NULL,created_at TEXT NOT NULL,uses INTEGER NOT NULL DEFAULT 0,revoked INTEGER NOT NULL DEFAULT 0);
      CREATE TABLE IF NOT EXISTS v2_runtime(key TEXT PRIMARY KEY,value TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS v2_notifications(id TEXT PRIMARY KEY,user_id INTEGER NOT NULL REFERENCES users(id),body TEXT NOT NULL,created_at TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS v2_native_notices(notification_id TEXT PRIMARY KEY REFERENCES v2_notifications(id) ON DELETE CASCADE);
      CREATE TABLE IF NOT EXISTS v2_notification_reads(user_id INTEGER NOT NULL REFERENCES users(id),notification_id TEXT NOT NULL REFERENCES v2_notifications(id) ON DELETE CASCADE,PRIMARY KEY(user_id,notification_id));
      CREATE TABLE IF NOT EXISTS v2_name_choices(user_id INTEGER NOT NULL REFERENCES users(id),membership_id INTEGER NOT NULL REFERENCES memberships(id) ON DELETE CASCADE,name TEXT NOT NULL,PRIMARY KEY(user_id,membership_id));
      CREATE TABLE IF NOT EXISTS v2_aliases(actor_key TEXT PRIMARY KEY,user_id INTEGER NOT NULL REFERENCES users(id));`);
    // Existing messages are history after startup; never replay native alerts.
    this.db.exec("INSERT OR IGNORE INTO v2_native_notices SELECT id FROM v2_notifications");
    if (!this.db.query("PRAGMA table_info(memberships)").all().some((column: any) => column.name === "display_names")) {
      this.db.exec("ALTER TABLE memberships ADD COLUMN display_names TEXT NOT NULL DEFAULT '[]'");
    }
  }
  one(sql: string, ...p: any[]): Row | undefined {
    return this.statement(sql).get(...p) as Row | undefined;
  }
  all(sql: string, ...p: any[]): Row[] {
    return this.statement(sql).all(...p) as Row[];
  }
  run(sql: string, ...p: any[]) {
    return this.statement(sql).run(...p);
  }
  private statement(sql: string): Statement {
    let q = this.statements.get(sql);
    if (!q) {
      q = this.db.prepare(sql);
      this.statements.set(sql, q);
    }
    return q;
  }
  tx<T>(fn: () => T): T {
    this.db.exec("BEGIN IMMEDIATE");
    try {
      const result = fn();
      if (result && typeof (result as any).then === "function")
        throw Error("Async work is not permitted inside SQLite transactions");
      this.db.exec("COMMIT");
      return result;
    } catch (e) {
      this.db.exec("ROLLBACK");
      throw e;
    }
  }
  close() {
    this.db.exec("PRAGMA wal_checkpoint(TRUNCATE)");
    for (const q of this.statements.values()) q.finalize();
    this.statements.clear();
    this.db.close(true);
  }
}
