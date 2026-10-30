import { expect, test } from "bun:test";
import { fixture } from "./fixture";
test("recovery guide opens without a membership and reviewed recovery transfers only network data", () => {
  const f = fixture();
  expect(f.call("command", { command: "恢复账号" }).pending.action).toBe(
    "guide_recover_identity",
  );
  const n = f.create("A");
  f.call("join_network", {
    value: n.code,
    nickname: "Old",
    consent: true,
    use_current_account: true,
    discoverable: false,
    accepts_requests: true,
  });
  const fresh = f.service.identify({
    accountId: 1,
    chatId: 51,
    contactId: 52,
    address: "recovery@example.org",
    fingerprint: "d".repeat(40),
  });
  f.s.tx(() => {
    f.service.pending(fresh, "guide_recover_identity", {});
    f.service.recover(fresh, {
      token: f.token,
      id: "e".repeat(32),
      action: "recover_by_name",
      join_code: n.code,
      nickname: "Old",
      old_address: "one@example.org",
    });
  });
  expect(
    f.s.one("SELECT 1 FROM pending_actions WHERE user_id=?", fresh.id),
  ).toBeNull();
  const r = f.s.one("SELECT * FROM account_recoveries WHERE status='pending'")!;
  f.s.tx(() =>
    f.bridge.admin.action(
      f.service.user(f.user.id),
      n.network,
      "approve_recovery",
      { recovery_id: r.public_id, confirmed: "yes" },
    ),
  );
  expect(
    f.s.one(
      "SELECT user_id FROM memberships WHERE network_id=? AND status='active'",
      n.network,
    )!.user_id,
  ).toBe(fresh.id);
  expect(f.bridge.admin.system(f.service.user(fresh.id))).toBe(false);
  f.s.close();
});
