import { expect, test } from "bun:test";
import { fixture } from "./fixture";
import { encrypt, random } from "../src/crypto";
import { now } from "../src/protocol";
const join = (f: ReturnType<typeof fixture>, code: string, nickname = "One") =>
  f.call("join_network", {
    value: code,
    nickname,
    consent: true,
    use_current_account: true,
    discoverable: true,
    accepts_requests: true,
  });

test("ordinary account joins five networks and duplicate join preserves profile", () => {
  const f = fixture();
  const nets = [];
  for (let i = 0; i < 5; i++) nets.push(f.create("network " + i));
  f.call("admin_lock");
  for (const n of nets) expect(join(f, n.code).receipt.ok).toBe(1);
  expect(f.bridge.snapshot(f.instance()).networks.length).toBe(5);
  expect(join(f, nets[0].code, "Overwrite").profile.nickname).toBe("One");
  expect(
    f.call("join_network", {
      value: "bad",
      nickname: "One",
      consent: true,
      use_current_account: true,
      discoverable: true,
      accepts_requests: true,
    }).receipt.ok,
  ).toBe(0);
  expect(f.bridge.snapshot(f.instance()).networks.length).toBe(5);
  f.s.close();
});
test("same request from two channels executes once and cursor advances atomically", () => {
  const f = fixture(),
    n = f.create("A"),
    cmd = {
      token: f.token,
      id: "f".repeat(32),
      action: "join_network",
      network: null,
      value: n.code,
      nickname: "One",
      consent: true,
      use_current_account: true,
      discoverable: true,
      accepts_requests: true,
    },
    envelope = encrypt(cmd, f.token, "contact-action-v2");
  f.bridge.handle(f.instance(), envelope, 2);
  f.bridge.handle(f.instance(), envelope, 3);
  expect(f.s.one("SELECT count(*) n FROM memberships")!.n).toBe(1);
  expect(f.s.one("SELECT serial FROM xdc_cursors")!.serial).toBe(3);
  f.s.close();
});
test("stale network mutation rejected, explicit join allowed, leave switches", () => {
  const f = fixture(),
    a = f.create("A"),
    b = f.create("B");
  join(f, a.code);
  join(f, b.code);
  expect(
    f.call("command", {
      network: a.network,
      command: "编辑昵称",
      value: "Wrong",
    }).receipt.ok,
  ).toBe(0);
  let state = f.call("command", { command: "退出网络" });
  state = f.call("reply", { step: state.pending.token, value: "1" });
  expect(state.current_network).toBe(a.network);
  expect(state.networks.length).toBe(1);
  f.s.close();
});
test("directory isolates networks and excludes hidden profiles and addresses", () => {
  const f = fixture(),
    a = f.create("A"),
    b = f.create("B");
  join(f, a.code);
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
      id: "b".repeat(32),
      action: "join_network",
      value: b.code,
      nickname: "Other",
      consent: true,
      use_current_account: true,
      discoverable: true,
      accepts_requests: true,
    }),
  );
  expect(f.bridge.snapshot(f.instance()).directory.members).toEqual([]);
  f.s.tx(() =>
    f.service.join(f.service.user(other.id), {
      token: f.token,
      id: "c".repeat(32),
      action: "join_network",
      value: a.code,
      nickname: "Visible",
      consent: true,
      use_current_account: true,
      discoverable: true,
      accepts_requests: true,
    }),
  );
  let directory = f.bridge.snapshot(f.instance()).directory;
  expect(directory.members.length).toBe(1);
  expect(JSON.stringify(directory)).not.toContain("@");
  f.s.run("UPDATE memberships SET discoverable=0 WHERE user_id=?", other.id);
  expect(f.bridge.snapshot(f.instance()).directory.members.length).toBe(0);
  f.s.close();
});
test("exchange queues exactly two native cards, block invalidates unsent delivery", () => {
  const f = fixture(),
    a = f.create("A");
  join(f, a.code);
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
      value: a.code,
      nickname: "Other",
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
  expect(f.call("exchange_now", { value: target.member_code }).receipt.ok).toBe(
    1,
  );
  expect(f.s.one("SELECT count(*) n FROM v2_jobs")!.n).toBe(2);
  expect(f.call("exchange_now", { value: target.member_code }).receipt.ok).toBe(
    0,
  );
  expect(
    f.call("command", { command: "屏蔽", value: target.member_code }).receipt
      .ok,
  ).toBe(1);
  expect(
    f.s.one("SELECT count(*) n FROM v2_jobs WHERE status='pending'")!.n,
  ).toBe(0);
  f.s.close();
});
test("deletion clears profile, grants, cached snapshot and pending jobs", () => {
  const f = fixture(),
    a = f.create("A");
  join(f, a.code);
  let state = f.call("command", { command: "删除我的数据" });
  state = f.call("reply", { step: state.pending.token, value: "1" });
  expect(state.profile).toBeNull();
  expect(state.networks).toEqual([]);
  expect(state.admin.unlocked).toBe(false);
  expect(
    f.s.one("SELECT share_contact FROM memberships")!.share_contact,
  ).toBeNull();
  f.s.close();
});
test("failed join attempts remain limited across action rollback", () => {
  const f = fixture();
  for (let i = 0; i < 11; i++)
    f.call("join_network", {
      value: "wrong",
      nickname: "One",
      consent: true,
      use_current_account: true,
      discoverable: true,
      accepts_requests: true,
    });
  expect(
    f.s.one("SELECT count(*) n FROM rate_events WHERE category='join'")!.n,
  ).toBe(10);
  expect(f.bridge.snapshot(f.instance()).messages[0]).toContain("过多");
  f.s.close();
});
test("delta publication stores baseline only after durable send", async () => {
  const f = fixture();
  await f.bridge.publish(f.instance(), true);
  const first = f.instance();
  expect(first.snapshot).toBeTruthy();
  const revision = first.revision;
  await f.bridge.publish(f.instance());
  expect(f.instance().revision).toBe(revision);
  f.s.close();
});
