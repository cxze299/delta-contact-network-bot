import { spawn } from "node:child_process";
import { StdioDeltaChat } from "@deltachat/jsonrpc-client";
import { mkdirSync, writeFileSync } from "node:fs";
import { configuration } from "../src/config";
import { Store } from "../src/db";
import { Service } from "../src/service";
import { DeltaAdapter } from "../src/delta_adapter";
import { Bridge } from "../src/webxdc_bridge";
import { encrypt, decrypt, random } from "../src/crypto";
import { pbkdf2Sync } from "node:crypto";
const root = process.env.E2E_ROOT || "/e2e";
mkdirSync(root, { recursive: true, mode: 0o700 });
const child = spawn("/app/rpc/deltachat-rpc-server", [], {
  env: { ...process.env, DC_ACCOUNTS_PATH: root + "/accounts" },
  stdio: ["pipe", "pipe", "pipe"],
});
child.stderr.on("data", () => {});
const rpc = new StdioDeltaChat(child.stdin, child.stdout, false).rpc;
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
async function wait<T>(
  label: string,
  fn: () => Promise<T> | T,
  seconds = 90,
): Promise<NonNullable<T>> {
  const end = Date.now() + seconds * 1000;
  while (Date.now() < end) {
    try {
      const x = await fn();
      if (x) return x as NonNullable<T>;
    } catch {}
    await sleep(700);
  }
  throw Error("Timeout: " + label);
}
let stopped = false;
const realtime: any[] = [];
let tail = Promise.resolve();
const salt = "ab".repeat(16),
  key = pbkdf2Sync(
    "integration-test-key",
    Buffer.from(salt, "hex"),
    250000,
    32,
    "sha256",
  ).toString("hex");
const cfg = configuration({
  CONTACTBOT_SECRET: "isolated-real-core-tests-0123456789abcdef",
  CONTACTBOT_XDC_ADMIN_VERIFIER: `250000:${salt}:${key}`,
  CONTACTBOT_ADMIN_WEB_TOKEN: "isolated-web-test-password",
  CONTACTBOT_ASSETS: "/app/webxdc",
});
const store = new Store(root + "/business-" + Date.now() + ".sqlite3"),
  service = new Service(store, cfg),
  adapter = new DeltaAdapter(service, cfg);
adapter.rpc = rpc;
adapter.bridge = new Bridge(service, adapter);
try {
  await rpc.getSystemInfo();
  let accounts = await rpc.getAllAccountIds();
  while (accounts.length < 3) {
    accounts.push(await rpc.addAccount());
  }
  for (const id of accounts.slice(0, 3)) {
    console.log("E2E configuring test account", id);
    if (!(await rpc.isConfigured(id)))
      await rpc.addTransportFromQr(
        id,
        process.env.E2E_ACCOUNT_QR || "dcaccount:nine.testrun.org",
      );
    await rpc.startIo(id);
    console.log("E2E test account ready", id);
  }
  const [bot, a, b] = accounts;
  adapter.account = bot;
  adapter.online = true;
  console.log("E2E accounts configured");
  const events = (async () => {
    try {
      while (!stopped) {
        for (const { contextId, event: e } of await rpc.getNextEventBatch()) {
          if (e.kind === "WebxdcRealtimeData" && contextId !== bot) {
            realtime.push({
              account: contextId,
              message: e.msgId,
              data: JSON.parse(Buffer.from(e.data).toString()),
            });
            continue;
          }
          if (contextId !== bot) continue;
          tail = tail
            .then(async () => {
              if (e.kind === "IncomingMsg") await adapter.incoming(e.msgId);
              if (e.kind === "WebxdcStatusUpdate") {
                const i = adapter.bridge.instance(bot, e.msgId);
                if (i) {
                  await adapter.consume(i);
                  await adapter.deliver();
                  await adapter.bridge.publishAll();
                }
              }
              if (e.kind === "WebxdcRealtimeAdvertisementReceived") {
                const i = adapter.bridge.instance(bot, e.msgId);
                if (i) {
                  await rpc.sendWebxdcRealtimeAdvertisement(bot, e.msgId);
                  await adapter.bridge.publish(i, true, true);
                }
              }
              if (e.kind === "WebxdcRealtimeData") {
                const i = adapter.bridge.instance(bot, e.msgId);
                if (i && (await adapter.verifyInstance(i))) {
                  const payload = JSON.parse(Buffer.from(e.data).toString());
                  if (adapter.bridge.handle(i, payload)) {
                    await adapter.deliver();
                    await adapter.bridge.publish(i, true, true);
                  }
                }
              }
            })
            .catch((e) => console.log("E2E task:", e.name));
        }
      }
    } catch {}
  })();
  console.log("E2E requesting securejoin QR");
  const qr = await rpc.getChatSecurejoinQrCode(bot, null),
    clients: any[] = [];
  console.log("E2E securejoin QR ready");
  for (const account of [a, b]) {
    console.log("E2E joining test account", account);
    const chat = await rpc.secureJoin(account, qr);
    console.log("E2E securejoin started");
    await wait("encrypted securejoin", async () => {
      const c = await rpc.getBasicChatInfo(account, chat),
        peers = await rpc.getChatContacts(account, chat);
      if (!c.isEncrypted || !peers.length) return false;
      const peer = await rpc.getContact(account, peers[0]);
      return peer.isVerified;
    });
    const beforeIds = new Set(
      await rpc.getMessageIds(account, chat, false, false),
    );
    await rpc.miscSendTextMessage(account, chat, "integration app");
    const message = await wait("bot sends app", async () => {
      for (const id of (
        await rpc.getMessageIds(account, chat, false, false)
      ).reverse()) {
        if (beforeIds.has(id)) continue;
        const m = await rpc.getMessage(account, id);
        if (m.fromId !== 1 && m.viewType === "Webxdc") return m;
      }
      return null;
    });
    const config = Buffer.from(
        await rpc.getWebxdcBlob(account, message.id, "config.js"),
        "base64",
      ).toString(),
      token = JSON.parse(
        config.slice(config.indexOf("=") + 1).replace(/;\s*$/, ""),
      ).token;
    clients.push({ account, chat, message: message.id, token });
  }
  console.log("E2E encrypted app delivery passed");
  const first = store.one(
    "SELECT * FROM users WHERE contact_id>9 ORDER BY id LIMIT 1",
  )!;
  store.run(
    "INSERT OR REPLACE INTO xdc_admin_grants VALUES(?,?,?)",
    first.id,
    adapter.bridge.admin.version,
    new Date().toISOString(),
  );
  const result = store.tx(() =>
    adapter.bridge.admin.action(service.user(first.id), null, "create", {
      name: "Integration network",
    }),
  );
  const network = store.one("SELECT * FROM networks ORDER BY id DESC LIMIT 1")!,
    code = service.readCode(network.join_code_ciphertext);
  const send = async (client: any, action: string, extra: any = {}) => {
    const id = Buffer.from(crypto.getRandomValues(new Uint8Array(16))).toString(
        "hex",
      ),
      u = store.one(
        "SELECT v.user_id FROM v2_instances v WHERE token=?",
        client.token,
      )!,
      user = service.user(u.user_id),
      payload = encrypt(
        {
          token: client.token,
          id,
          action,
          network: user.current_network_id,
          notification_version: 1,
          ...extra,
        },
        client.token,
        "contact-action-v2",
      );
    await rpc.sendWebxdcStatusUpdate(
      client.account,
      client.message,
      JSON.stringify({ payload }),
      "",
    );
    await wait("request receipt", () =>
      store.one(
        "SELECT * FROM v2_results WHERE token=? AND request_id=?",
        client.token,
        id,
      ),
    );
    return { id, payload };
  };
  for (let i = 0; i < clients.length; i++)
    await send(clients[i], "join_network", {
      value: code,
      nickname: "Integration " + i,
      consent: true,
      use_current_account: true,
      discoverable: true,
      accepts_requests: true,
    });
  if (
    store.one("SELECT count(*) n FROM memberships WHERE status='active'")?.n !==
    2
  )
    throw Error("Join failed");
  console.log("E2E real status command join passed");
  const ia = store.one(
    "SELECT x.*,v.user_id FROM xdc_instances x JOIN v2_instances v ON v.token=x.token WHERE x.token=?",
    clients[0].token,
  )!;
  const snapshot = adapter.bridge.snapshot(ia);
  if (snapshot.directory.members.length !== 1)
    throw Error("Directory mismatch");
  const command = await send(clients[0], "exchange_now", {
    value: snapshot.directory.members[0].member_code,
  });
  await wait(
    "native cards delivered",
    () =>
      store.one("SELECT count(*) n FROM v2_jobs WHERE status='sent'")?.n === 2,
    120,
  );
  for (const client of clients) {
    await wait(
      "recipient receives native contact",
      async () => {
        for (const id of (
          await rpc.getMessageIds(client.account, client.chat, false, false)
        ).reverse()) {
          const m = await rpc.getMessage(client.account, id);
          if (
            m.fromId !== 1 &&
            m.viewType === "Vcard" &&
            m.showPadlock &&
            m.vcardContact?.addr
          )
            return true;
        }
        return false;
      },
      120,
    );
  }
  console.log("E2E native encrypted cards delivered both ways");
  await rpc.sendWebxdcStatusUpdate(
    clients[0].account,
    clients[0].message,
    JSON.stringify({ payload: command.payload }),
    "",
  );
  await sleep(3000);
  await tail;
  if (store.one("SELECT count(*) n FROM v2_jobs")?.n !== 2)
    throw Error("Duplicate delivery");
  console.log("E2E duplicate request did not send more cards");
  for (const c of clients)
    await rpc.sendWebxdcRealtimeAdvertisement(c.account, c.message);
  await rpc.sendWebxdcRealtimeAdvertisement(bot, ia.message_id);
  await wait(
    "realtime advertisement and response",
    () => realtime.some((x) => x.account === clients[0].account),
    60,
  );
  const refresh = {
    token: clients[0].token,
    id: random(16)
      .replace(/[^a-f0-9]/g, "a")
      .padEnd(32, "a")
      .slice(0, 32),
    action: "refresh",
    notification_version: 1,
  };
  await rpc.sendWebxdcRealtimeData(clients[0].account, clients[0].message, [
    ...Buffer.from(
      JSON.stringify(encrypt(refresh, clients[0].token, "contact-action-v2")),
    ),
  ]);
  await wait(
    "authenticated realtime receipt",
    () =>
      realtime.some((x) => {
        try {
          return (
            decrypt(x.data, clients[0].token, "contact-response-v3").state
              ?.receipt?.request_id === refresh.id
          );
        } catch {
          return false;
        }
      }),
    60,
  );
  console.log("E2E authenticated realtime command and reply passed");
  console.log("E2E_ALL_PASS");
} catch (e) {
  console.error(
    "E2E_FAILED",
    String((e as any)?.message || "Error")
      .replace(/[A-Z0-9._%+-]+@[A-Z0-9.-]+/gi, "[redacted-address]")
      .slice(0, 180),
    String((e as any)?.code || ""),
    JSON.stringify((e as any)?.data || {})
      .replace(/[A-Z0-9._%+-]+@[A-Z0-9.-]+/gi, "[redacted-address]")
      .slice(0, 200),
  );
  process.exitCode = 1;
} finally {
  stopped = true;
  adapter.online = false;
  await tail;
  child.kill();
  store.close();
  setTimeout(() => process.exit(process.exitCode || 0), 500);
}
