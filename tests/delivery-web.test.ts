import { readFileSync } from "node:fs";
import { expect, test } from "bun:test";
import { fixture } from "./fixture";
import { DeltaAdapter } from "../src/delta_adapter";
import { createAdmin } from "../src/admin_web";
import { now } from "../src/protocol";

test("send succeeds but receipt commit fails: reconcile without resend", async () => {
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
  const adapter = new DeltaAdapter(f.service, f.config);
  adapter.account = 1;
  const sent: any[] = [];
  let shouldFail = true;
  const original = f.s.run.bind(f.s);
  f.s.run = ((sql: string, ...args: any[]) => {
    if (shouldFail && sql.includes("SET status='sent',core_message_id")) {
      shouldFail = false;
      throw Error("simulated commit failure after send");
    }
    return original(sql, ...args);
  }) as typeof f.s.run;
  adapter.identity = async (chat, contact) => {
    const u = f.s.one(
      "SELECT u.id,v.address,v.fingerprint FROM users u JOIN v2_users v ON v.user_id=u.id WHERE u.chat_id=?",
      chat,
    )!;
    return {
      accountId: 1,
      chatId: chat,
      contactId: contact,
      address: u.address,
      fingerprint: u.fingerprint,
    };
  };
  adapter.cardContact = async (contact) => {
    const u = f.s.one(
      "SELECT v.address FROM users u JOIN v2_users v ON v.user_id=u.id WHERE u.contact_id=?",
      contact,
    )!;
    return { addr: u.address, key: "test-public-key" } as any;
  };
  adapter.rpc = {
    getContact: async (_account: number, id: number) => ({authName: id === 12 ? "Original Name" : ""}),
    miscSendMsg: async (
      _account: number,
      chat: number,
      _text: any,
      _file: string,
      name: string,
    ) => {
      const id = sent.length + 1000;
      sent.push({ id, chat, fromId: 1, fileName: name, card: readFileSync(_file, "utf8") });
      return [id, {}];
    },
    getMessageIds: async (_account: number, chat: number) =>
      sent.filter((x) => x.chat === chat).map((x) => x.id),
    getMessage: async (_account: number, id: number) =>
      sent.find((x) => x.id === id),
  } as any;
  await adapter.deliver();
  expect(sent.length).toBe(2);
  expect(sent.some(x => x.card.includes("FN:Original Name\r\n"))).toBe(true);
  expect(sent.some(x => x.card.includes("FN:two@example.org\r\n"))).toBe(true);
  expect(
    f.s.one("SELECT count(*) n FROM v2_jobs WHERE status='uncertain'")!.n,
  ).toBe(1);
  await adapter.deliver();
  expect(sent.length).toBe(2);
  expect(f.s.one("SELECT count(*) n FROM v2_jobs WHERE status='sent'")!.n).toBe(
    2,
  );
  expect(f.s.one("SELECT status FROM contact_requests")!.status).toBe(
    "completed",
  );
  f.s.close();
});
test("web authentication, CSRF and network administrator scope", async () => {
  const f = fixture(),
    n = f.create("A"),
    app = createAdmin(
      f.service,
      f.bridge.admin,
      () => true,
      () => {},
    ),
    auth =
      "Basic " +
      Buffer.from("admin:" + f.config.webPassword).toString("base64");
  expect((await app.request("/")).status).toBe(401);
  expect((await app.request("/health")).status).toBe(200);
  const response = await app.request("/", { headers: { Authorization: auth } });
  expect(response.status).toBe(200);
  const html = await response.text();
  expect(html).toContain("网络管理工作台");
  const denied = await app.request("/action", {
    method: "POST",
    headers: {
      Authorization: auth,
      "Content-Type": "application/x-www-form-urlencoded",
    },
    body: new URLSearchParams({
      action: "create",
      name: "Forbidden",
      csrf: "wrong",
    }),
  });
  expect(denied.status).toBe(400);
  expect(f.s.one("SELECT count(*) n FROM networks")!.n).toBe(1);
  const other = f.service.identify({
    accountId: 1,
    chatId: 44,
    contactId: 45,
    address: "admin-network@example.org",
    fingerprint: "c".repeat(40),
  });
  f.s.tx(() =>
    f.service.join(other, {
      token: f.token,
      id: "c".repeat(32),
      action: "join_network",
      value: n.code,
      nickname: "Scoped",
      consent: true,
      use_current_account: true,
      discoverable: true,
      accepts_requests: true,
    }),
  );
  f.s.run(
    "UPDATE memberships SET role='network_admin' WHERE user_id=?",
    other.id,
  );
  expect(() =>
    f.s.tx(() =>
      f.bridge.admin.action(f.service.user(other.id), n.network, "rename", {
        name: "Forbidden",
      }),
    ),
  ).toThrow();
  expect(f.s.one("SELECT name FROM networks WHERE id=?", n.network)!.name).toBe(
    "A",
  );
  f.s.close();
});
