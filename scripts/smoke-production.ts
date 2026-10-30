import { spawn } from "node:child_process";
import { StdioDeltaChat } from "@deltachat/jsonrpc-client";
import { readFileSync } from "node:fs";
import { configuration } from "../src/config";
import { encrypt, decrypt } from "../src/crypto";
const config = configuration(),
  child = spawn("/app/rpc/deltachat-rpc-server", [], {
    env: { ...process.env, DC_ACCOUNTS_PATH: "/client-accounts" },
    stdio: ["pipe", "pipe", "pipe"],
  });
child.stderr.on("data", () => {});
const rpc = new StdioDeltaChat(child.stdin, child.stdout, false).rpc;
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
async function wait<T>(
  name: string,
  fn: () => Promise<T> | T,
  seconds = 90,
): Promise<NonNullable<T>> {
  const end = Date.now() + seconds * 1000;
  while (Date.now() < end) {
    try {
      const value = await fn();
      if (value) return value as NonNullable<T>;
    } catch {}
    await sleep(600);
  }
  throw Error("Timed out: " + name);
}
let stopped = false;
const live: any[] = [];
try {
  const auth =
      "Basic " + Buffer.from("admin:" + config.webPassword).toString("base64"),
    response = await fetch("http://127.0.0.1:8787/api/invite", {
      headers: { Authorization: auth },
    });
  if (!response.ok) throw Error("Authenticated invite unavailable");
  const invite = (await response.json()) as { link: string };
  const qr = invite.link,
    account = 2;
  await rpc.startIo(account);
  void (async () => {
    try {
      while (!stopped)
        for (const item of await rpc.getNextEventBatch()) {
          if (
            item.contextId === account &&
            item.event.kind === "WebxdcRealtimeData"
          )
            live.push(JSON.parse(Buffer.from(item.event.data).toString()));
        }
    } catch {}
  })();
  const chat = await rpc.secureJoin(account, qr);
  await wait("SecureJoin", async () => {
    const ids = await rpc.getChatContacts(account, chat);
    return ids.length && (await rpc.getContact(account, ids[0])).isVerified;
  });
  const before = new Set(await rpc.getMessageIds(account, chat, false, false));
  await rpc.miscSendTextMessage(account, chat, "小程序");
  const app = await wait("new application delivery", async () => {
    for (const id of (
      await rpc.getMessageIds(account, chat, false, false)
    ).reverse()) {
      if (before.has(id)) continue;
      const m = await rpc.getMessage(account, id);
      if (m.viewType === "Webxdc" && m.fromId !== 1 && m.showPadlock) return m;
    }
    return null;
  });
  const text = Buffer.from(
      await rpc.getWebxdcBlob(account, app.id, "config.js"),
      "base64",
    ).toString(),
    token = JSON.parse(
      text.slice(text.indexOf("=") + 1).replace(/;\s*$/, ""),
    ).token;
  async function snapshot() {
    let state: any = null,
      version: any = null;
    for (const u of JSON.parse(
      await rpc.getWebxdcStatusUpdates(account, app.id, 0),
    )) {
      try {
        const p = decrypt(u.payload, token, "contact-response-v3");
        if (p.state) {
          state = p.state;
          version = p.version;
        } else if (state && p.base === version) {
          state = { ...state, ...p.patch };
          version = p.version;
        }
      } catch {}
    }
    return state;
  }
  let current = await wait("initial authenticated state", snapshot);
  console.log("PRODUCTION app and authenticated state passed");
  async function command(action: string, extra: any = {}) {
    const id = Buffer.from(crypto.getRandomValues(new Uint8Array(16))).toString(
        "hex",
      ),
      payload = encrypt(
        {
          token,
          id,
          action,
          network: current.current_network,
          notification_version: 1,
          ...extra,
        },
        token,
        "contact-action-v2",
      );
    await rpc.sendWebxdcStatusUpdate(
      account,
      app.id,
      JSON.stringify({ payload }),
      "",
    );
    current = await wait("operation receipt", async () => {
      const s = await snapshot();
      return s?.receipt?.request_id === id ? s : null;
    });
    if (!current.receipt.ok) throw Error("Business action rejected: " + action);
    return current;
  }
  const access = readFileSync(
      "/production-data/initial-network-access.txt",
      "utf8",
    ),
    code = access
      .split("\n")
      .find((x) => x.startsWith("加入码："))
      ?.slice(4);
  if (!code) throw Error("Network credential unavailable");
  await command("join_network", {
    value: code,
    nickname: "上线验证账号",
    consent: true,
    use_current_account: true,
    discoverable: false,
    accepts_requests: false,
  });
  if (!current.profile) throw Error("Profile absent");
  console.log("PRODUCTION real status command and account binding passed");
  await rpc.sendWebxdcRealtimeAdvertisement(account, app.id);
  await wait(
    "realtime handshake",
    async () => {
      await rpc.sendWebxdcRealtimeData(account, app.id, [
        ...Buffer.from(JSON.stringify({ type: "contact-ping-v3" })),
      ]);
      return live.some((p) => {
        try {
          return decrypt(p, token, "contact-response-v3").role === "bot";
        } catch {
          return false;
        }
      });
    },
    60,
  );
  console.log("PRODUCTION authenticated realtime passed");
  await command("command", { command: "退出网络" });
  await command("reply", { step: current.pending.token, value: "1" });
  if (current.profile) throw Error("Leave failed");
  await command("command", { command: "删除我的数据" });
  await command("reply", { step: current.pending.token, value: "1" });
  console.log("PRODUCTION leave and deletion passed");
  console.log("PRODUCTION_SMOKE_ALL_PASS");
} catch (e) {
  console.error(
    "PRODUCTION_SMOKE_FAILED",
    e instanceof Error
      ? e.message
      : /qr|scheme|parse/i.test(String((e as any)?.message))
        ? "invalid-invitation"
        : /account|configured/i.test(String((e as any)?.message))
          ? "account-state-error"
          : "RPC error",
  );
  process.exitCode = 1;
} finally {
  stopped = true;
  child.kill();
  setTimeout(() => process.exit(process.exitCode || 0), 500);
}
