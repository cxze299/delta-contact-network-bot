import { expect, test } from "bun:test";
import { mkdtempSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fixture } from "./fixture";
import { Store } from "../src/db";
import { Service } from "../src/service";
import { DeltaAdapter } from "../src/delta_adapter";

test("restoring an old database reapplies external deletion ledger", () => {
  const f = fixture(),
    n = f.create("A");
  f.call("join_network", {
    value: n.code,
    nickname: "Private",
    consent: true,
    use_current_account: true,
    discoverable: true,
    accepts_requests: true,
  });
  const folder = mkdtempSync(join(tmpdir(), "friend-restore-")),
    path = join(folder, "db.sqlite3"),
    before = f.s.db.serialize();
  let opened: Store | undefined;
  try {
    writeFileSync(path, before);
    opened = new Store(path);
    let service = new Service(opened, f.config);
    opened.tx(() => service.erase(service.user(f.user.id)));
    opened.close();
    opened = undefined;
    writeFileSync(path, before);
    opened = new Store(path);
    service = new Service(opened, f.config);
    expect(
      opened.one("SELECT share_contact FROM memberships")!.share_contact,
    ).toBeNull();
    expect(opened.one("SELECT nickname FROM memberships")!.nickname).toBe(
      "已删除成员",
    );
    expect(
      opened.one("SELECT current_network_id FROM users WHERE id=?", f.user.id)!
        .current_network_id,
    ).toBeNull();
  } finally {
    opened?.close();
    f.s.close();
    try {
      rmSync(folder, { recursive: true, force: true });
    } catch {}
  }
}, 30000);
test("expired pending exchange cannot create or continue deliveries", () => {
  const f = fixture(),
    n = f.create("A");
  f.call("join_network", {
    value: n.code,
    nickname: "One",
    consent: true,
    use_current_account: true,
    discoverable: true,
    accepts_requests: true,
  });
  const other = f.service.identify({
    accountId: 1,
    chatId: 22,
    contactId: 23,
    address: "two@example.org",
    fingerprint: "b".repeat(40),
  });
  f.s.tx(() =>
    f.service.join(other, {
      token: f.token,
      id: "c".repeat(32),
      action: "join_network",
      value: n.code,
      nickname: "Two",
      consent: true,
      use_current_account: true,
      discoverable: true,
      accepts_requests: true,
    }),
  );
  const target = f.s.one(
    "SELECT * FROM memberships WHERE user_id=?",
    other.id,
  )!;
  f.call("exchange_now", { value: target.member_code });
  f.s.run("UPDATE contact_requests SET expires_at='2000-01-01T00:00:00.000Z'");
  f.call("refresh");
  expect(f.s.one("SELECT status FROM contact_requests")!.status).toBe(
    "expired",
  );
  expect(
    f.s.one("SELECT count(*) n FROM v2_jobs WHERE status='pending'")!.n,
  ).toBe(0);
  f.s.close();
});
test("same account with changed cryptographic identity is not silently rebound", () => {
  const f = fixture();
  expect(() =>
    f.service.identify({
      accountId: 1,
      chatId: 99,
      contactId: 98,
      address: "one@example.org",
      fingerprint: "f".repeat(40),
    }),
  ).toThrow();
  expect(f.service.user(f.user.id).fingerprint).toBe("a".repeat(40));
  f.s.close();
});
