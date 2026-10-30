import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import {
  StdioDeltaChat,
  type RawClient,
  type T,
} from "@deltachat/jsonrpc-client";
import {
  mkdirSync,
  existsSync,
  mkdtempSync,
  readFileSync,
  writeFileSync,
  rmSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { zipSync, strToU8 } from "fflate";
import { type Config } from "./config";
import { Service, type Identity } from "./service";
import { Bridge, type Publisher } from "./webxdc_bridge";
import { type Row } from "./db";
import { random, hash } from "./crypto";
import { fingerprint } from "./identity";
import { now } from "./protocol";
export class DeltaAdapter implements Publisher {
  child!: ChildProcessWithoutNullStreams;
  client!: StdioDeltaChat;
  rpc!: RawClient;
  bridge!: Bridge;
  account = 0;
  online = false;
  stopping = false;
  private tail = Promise.resolve();
  private queued = 0;
  private maintenance = 0;
  private timer?: ReturnType<typeof setInterval>;
  private live = new Set<string>();
  constructor(
    readonly service: Service,
    readonly config: Config,
  ) {}
  enqueue(task: () => Promise<void>) {
    this.queued++;
    this.tail = this.tail
      .then(task)
      .catch((e) =>
        console.warn("Task deferred:", e instanceof Error ? e.name : "Error"),
      )
      .finally(() => {
        this.queued--;
      });
  }
  isLive(token: string) {
    return this.live.has(token);
  }
  async start() {
    mkdirSync(this.config.accounts, { recursive: true, mode: 0o700 });
    this.child = spawn(this.config.rpc, [], {
      env: {
        ...process.env,
        DC_ACCOUNTS_PATH: existsSync(
          join(this.config.accounts, "accounts", "accounts.toml"),
        )
          ? join(this.config.accounts, "accounts")
          : this.config.accounts,
      },
      stdio: ["pipe", "pipe", "pipe"],
    });
    this.child.stderr.on("data", () => {
      /* Core logs may contain private addresses. */
    });
    this.child.on("error", () => {
      this.online = false;
    });
    this.client = new StdioDeltaChat(
      this.child.stdin,
      this.child.stdout,
      false,
    );
    this.rpc = this.client.rpc;
    this.bridge = new Bridge(this.service, this);
    await this.rpc.getSystemInfo();
    const accounts = await this.rpc.getAllAccountIds();
    this.account = this.config.accountId || accounts[0];
    if (!this.account || !(await this.rpc.isConfigured(this.account)))
      throw Error("A configured bot account is required");
    await this.rpc.startIo(this.account);
    this.online = true;
    this.child.on("exit", () => {
      this.online = false;
      if (!this.stopping) {
        console.error("RPC process exited; container supervisor will restart");
        process.exit(1);
      }
    });
    this.enqueue(async () => {
      for (const row of this.service.s.all(
        "SELECT x.*,v.user_id FROM xdc_instances x JOIN v2_instances v ON v.token=x.token WHERE x.account_id=?",
        this.account,
      ))
        await this.consume(row);
      await this.deliver();
    });
    this.timer = setInterval(() => {
      if (this.queued > 5) return;
      this.enqueue(async () => {
        this.service.s.tx(() => this.service.expire());
        if (++this.maintenance % 4 === 0) {
          for (const i of this.service.s.all(
            "SELECT x.*,v.user_id FROM xdc_instances x JOIN v2_instances v ON v.token=x.token WHERE x.account_id=?",
            this.account,
          ))
            await this.consume(i);
        }
        await this.deliver();
        await this.bridge.publishAll();
      });
    }, 15000);
    void this.events();
  }
  async events() {
    try {
      while (!this.stopping) {
        const batch = await this.rpc.getNextEventBatch();
        for (const item of batch) {
          if (item.contextId !== this.account) continue;
          const e = item.event;
          if (e.kind === "IncomingMsg")
            this.enqueue(() => this.incoming(e.msgId));
          else if (e.kind === "WebxdcStatusUpdate")
            this.enqueue(async () => {
              const i = this.bridge.instance(this.account, e.msgId);
              if (i) {
                if (await this.consume(i)) {
                  await this.deliver();
                  await this.bridge.publishAll();
                }
              }
            });
          else if (e.kind === "WebxdcRealtimeData")
            this.enqueue(async () => {
              const i = this.bridge.instance(this.account, e.msgId);
              if (!i || !(await this.verifyInstance(i))) return;
              let data;
              try {
                data = JSON.parse(Buffer.from(e.data).toString());
              } catch {
                return;
              }
              if (data?.type === "contact-ping-v3") {
                this.live.add(i.token);
                await this.rpc.sendWebxdcRealtimeAdvertisement(
                  this.account,
                  e.msgId,
                );
                await this.bridge.publishRealtime(i);
                return;
              }
              if (this.bridge.handle(i, data)) {
                this.live.add(i.token);
                await this.bridge.publish(i, false, true);
                await this.deliver();
                await this.bridge.publishAll(undefined, i.token);
              }
            });
          else if (e.kind === "WebxdcRealtimeAdvertisementReceived")
            this.enqueue(async () => {
              const i = this.bridge.instance(this.account, e.msgId);
              if (
                i &&
                !this.live.has(i.token) &&
                (await this.verifyInstance(i))
              ) {
                await this.rpc.sendWebxdcRealtimeAdvertisement(
                  this.account,
                  e.msgId,
                );
                this.live.add(i.token);
                await this.bridge.publish(i, false, true);
              }
            });
        }
      }
    } catch (e) {
      this.online = false;
      if (!this.stopping) {
        console.error("RPC event loop disconnected");
        process.exit(1);
      }
    }
  }
  async identity(chatId: number, contactId: number): Promise<Identity> {
    const chat = await this.rpc.getBasicChatInfo(this.account, chatId);
    if (
      chat.chatType !== "Single" ||
      chat.isDeviceChat ||
      chat.isSelfTalk ||
      !chat.isEncrypted
    )
      throw Error("Encrypted private chat required");
    const peers = await this.rpc.getChatContacts(this.account, chatId);
    if (
      !peers.includes(contactId) ||
      peers.some((id) => id > 9 && id !== contactId)
    )
      throw Error("Chat identity mismatch");
    const contact = await this.rpc.getContact(this.account, contactId);
    if (this.config.requireVerified && !contact.isVerified)
      throw Error("Verification required");
    const parsed = await this.cardContact(contactId);
    if (
      parsed.addr.toLowerCase() !== contact.address.toLowerCase() ||
      !parsed.key
    )
      throw Error("Contact key unavailable");
    return {
      accountId: this.account,
      chatId,
      contactId,
      address: contact.address,
      fingerprint: fingerprint(parsed.key),
    };
  }
  async cardContact(contactId: number) {
    const folder = mkdtempSync(join(tmpdir(), "friend-vcard-"));
    try {
      const path = join(folder, "native.vcf");
      writeFileSync(path, await this.rpc.makeVcard(this.account, [contactId]), {
        mode: 0o600,
      });
      const contacts = await this.rpc.parseVcard(path);
      if (contacts.length !== 1) throw Error("Invalid contact");
      return contacts[0];
    } finally {
      rmSync(folder, { recursive: true, force: true });
    }
  }
  async verifyInstance(i: Row) {
    try {
      const u = this.service.user(i.user_id),
        identity = await this.identity(i.chat_id, u.contact_id);
      if (identity.fingerprint !== u.fingerprint) return false;
      this.service.identify(identity);
      return true;
    } catch {
      return false;
    }
  }
  async incoming(id: number) {
    const m = await this.rpc.getMessage(this.account, id);
    if (m.isInfo || m.isBot || m.fromId <= 9 || !m.showPadlock) return;
    let identity: Identity;
    try {
      identity = await this.identity(m.chatId, m.fromId);
    } catch {
      return;
    }
    const u = this.service.identify(identity);
    if (
      this.service.s.one(
        "SELECT 1 FROM incoming_messages WHERE account_id=? AND message_id=?",
        this.account,
        id,
      )
    )
      return;
    await this.offer(u, identity);
    this.service.s.tx(() =>
      this.service.s.run(
        "INSERT OR IGNORE INTO incoming_messages VALUES(?,?,?)",
        this.account,
        id,
        now(),
      ),
    );
  }
  async offer(user: Row, identity: Identity) {
    const token = random(32),
      folder = mkdtempSync(join(tmpdir(), "friend-xdc-"));
    try {
      const entries: Record<string, Uint8Array> = {};
      for (const name of [
        "index.html",
        "style.css",
        "app.js",
        "manifest.toml",
        "icon.png",
      ])
        entries[name] = new Uint8Array(
          readFileSync(join(this.config.assets, name)),
        );
      entries["config.js"] = strToU8(
        "window.CONTACT_CONFIG=" + JSON.stringify({ token, protocol: 3 }) + ";",
      );
      const file = join(folder, "好友助手.xdc");
      writeFileSync(file, zipSync(entries), { mode: 0o600 });
      const [messageId] = await this.rpc.miscSendMsg(
        this.account,
        identity.chatId,
        null,
        file,
        "好友助手.xdc",
        null,
        null,
      );
      this.service.s.tx(() => {
        this.service.s.run(
          "INSERT INTO xdc_instances(token,actor_key,account_id,chat_id,message_id,created_at,notifications_enabled) VALUES(?,?,?,?,?,?,1)",
          token,
          user.actor_key,
          this.account,
          identity.chatId,
          messageId,
          now(),
        );
        this.service.s.run(
          "INSERT INTO v2_instances(token,user_id) VALUES(?,?)",
          token,
          user.id,
        );
      });
      const i = this.bridge.instance(this.account, messageId)!;
      await this.bridge.publish(i, true);
      await this.rpc.sendWebxdcRealtimeAdvertisement(this.account, messageId);
    } finally {
      rmSync(folder, { recursive: true, force: true });
    }
  }
  async consume(i: Row) {
    if (!(await this.verifyInstance(i))) return;
    const serial = Number(
      this.service.s.one(
        "SELECT serial FROM xdc_cursors WHERE account_id=? AND message_id=?",
        i.account_id,
        i.message_id,
      )?.serial || 0,
    );
    let changed = false;
    const updates = JSON.parse(
      await this.rpc.getWebxdcStatusUpdates(i.account_id, i.message_id, serial),
    );
    for (const update of updates) {
      if (!Number.isInteger(update.serial) || update.serial <= serial) continue;
      if (update.payload?.type === "contact-action-v2") {
        if (this.bridge.handle(i, update.payload, update.serial))
          changed = true;
        else this.service.s.tx(() => this.bridge.cursor(i, update.serial));
      } else this.service.s.tx(() => this.bridge.cursor(i, update.serial));
    }
    return changed;
  }
  async status(
    account: number,
    message: number,
    payload: unknown,
    notify = false,
  ) {
    await this.rpc.sendWebxdcStatusUpdate(
      account,
      message,
      JSON.stringify({
        payload,
        summary: "\u00a0",
        ...(notify ? { notify: { "*": "好友助手有新消息，点击查看" } } : {}),
      }),
      "",
    );
  }
  async realtime(account: number, message: number, payload: unknown) {
    await this.rpc.sendWebxdcRealtimeData(account, message, [
      ...Buffer.from(JSON.stringify(payload)),
    ]);
  }
  validJob(j: Row) {
    const r = this.service.s.one(
      "SELECT * FROM contact_requests WHERE id=? AND status='processing' AND expires_at>?",
      j.request_id,
      now(),
    );
    if (!r) return null;
    const a = this.service.s.one(
        "SELECT m.*,n.status network_status FROM memberships m JOIN networks n ON n.id=m.network_id WHERE m.id=? AND m.status='active'",
        r.requester_membership_id,
      ),
      b = this.service.s.one(
        "SELECT * FROM memberships WHERE id=? AND status='active'",
        r.target_membership_id,
      );
    if (
      !a ||
      !b ||
      a.network_status !== "active" ||
      this.service.blocked(a, b) ||
      a.share_version !== r.requester_share_version ||
      b.share_version !== r.target_share_version ||
      a.share_contact !== r.requester_contact ||
      b.share_contact !== r.target_contact
    )
      return null;
    return { r, a, b };
  }
  async deliver() {
    for (const j of this.service.s.all(
      "SELECT * FROM v2_jobs WHERE status IN ('pending','sending','uncertain') ORDER BY id",
    )) {
      const token =
        this.service.s.one(
          "SELECT value FROM v2_runtime WHERE key='delivery_namespace'",
        )?.value || random(16);
      this.service.s.run(
        "INSERT OR IGNORE INTO v2_runtime VALUES('delivery_namespace',?)",
        token,
      );
      const name = `friend-card-${token}-${j.id}.vcf`;
      if (j.status !== "pending") {
        try {
          const u = this.service.user(j.recipient),
            ids = (
              await this.rpc.getMessageIds(
                this.account,
                u.chat_id,
                false,
                false,
              )
            ).slice(-500);
          let match = 0;
          for (const id of ids.reverse()) {
            if (id <= 9) continue;
            const m = await this.rpc.getMessage(this.account, id);
            if (m.fromId === 1 && m.fileName === name) {
              match = m.id;
              break;
            }
          }
          if (match)
            this.service.s.tx(() =>
              this.service.s.run(
                "UPDATE v2_jobs SET status='sent',core_message_id=? WHERE id=?",
                match,
                j.id,
              ),
            );
          else if (j.status === "sending")
            this.service.s.tx(() =>
              this.service.s.run(
                "UPDATE v2_jobs SET status='uncertain' WHERE id=?",
                j.id,
              ),
            );
        } catch {
          /* Never resend an ambiguous delivery. */
        }
        continue;
      }
      if (!this.validJob(j)) {
        this.service.s.tx(() =>
          this.service.s.run(
            "UPDATE v2_jobs SET status='cancelled' WHERE id=?",
            j.id,
          ),
        );
        continue;
      }
      const folder = mkdtempSync(join(tmpdir(), "friend-card-"));
      let started = false;
      try {
        const recipient = this.service.user(j.recipient),
          source = this.service.user(j.source),
          who = await this.identity(recipient.chat_id, recipient.contact_id),
          from = await this.identity(source.chat_id, source.contact_id);
        if (
          who.fingerprint !== recipient.fingerprint ||
          from.fingerprint !== source.fingerprint
        )
          throw Error("Identity changed");
        const valid = this.validJob(j);
        if (!valid) throw Error("Exchange no longer valid");
        const member = valid.a.user_id === source.id ? valid.a : valid.b,
          contact = await this.cardContact(source.contact_id),
          accountContact = await this.rpc.getContact(this.account, source.contact_id);
        if (
          contact.addr.toLowerCase() !== member.share_contact.toLowerCase() ||
          !contact.key
        )
          throw Error("Share identity mismatch");
        const esc = (v: string) =>
          v
            .replace(/\\/g, "\\\\")
            .replace(/\r?\n/g, "\\n")
            .replace(/;/g, "\\;")
            .replace(/,/g, "\\,");
        const file = join(folder, name);
        writeFileSync(
          file,
          `BEGIN:VCARD\r\nVERSION:4.0\r\nFN:${esc(accountContact.authName.trim() || contact.addr)}\r\nEMAIL:${esc(member.share_contact)}\r\nKEY;TYPE=PGP;ENCODING=b:${contact.key}\r\nEND:VCARD\r\n`,
          { mode: 0o600 },
        );
        this.service.s.tx(() => {
          if (!this.validJob(j)) throw Error("Exchange no longer valid");
          this.service.s.run(
            "UPDATE v2_jobs SET status='sending',started_at=? WHERE id=? AND status='pending'",
            now(),
            j.id,
          );
        });
        started = true;
        const [id] = await this.rpc.miscSendMsg(
          this.account,
          recipient.chat_id,
          null,
          file,
          name,
          null,
          null,
        );
        this.service.s.tx(() =>
          this.service.s.run(
            "UPDATE v2_jobs SET status='sent',core_message_id=? WHERE id=?",
            id,
            j.id,
          ),
        );
      } catch (e) {
        this.service.s.tx(() => {
          this.service.s.run(
            "UPDATE v2_jobs SET status=? WHERE id=?",
            started ? "uncertain" : "failed",
            j.id,
          );
          if (!started)
            this.service.s.run(
              "UPDATE contact_requests SET status='invalid',updated_at=? WHERE id=?",
              now(),
              j.request_id,
            );
        });
        console.warn(
          "Card delivery requires review:",
          started ? "uncertain" : "preflight",
        );
      } finally {
        rmSync(folder, { recursive: true, force: true });
      }
    }
    this.service.s.tx(() => {
      const ready = this.service.s.all(
        "SELECT r.id,a.user_id a,b.user_id b FROM contact_requests r JOIN memberships a ON a.id=r.requester_membership_id JOIN memberships b ON b.id=r.target_membership_id WHERE r.status='processing' AND EXISTS(SELECT 1 FROM v2_jobs j WHERE j.request_id=r.id) AND NOT EXISTS(SELECT 1 FROM v2_jobs j WHERE j.request_id=r.id AND j.status<>'sent')",
      );
      for (const r of ready) {
        this.service.s.run(
          "UPDATE contact_requests SET status='completed',completed_at=?,updated_at=? WHERE id=?",
          now(),
          now(),
          r.id,
        );
        this.service.notify(
          r.a,
          "名片已发送，请在与好友助手的聊天中添加好友。",
        );
        this.service.notify(
          r.b,
          "名片已发送，请在与好友助手的聊天中添加好友。",
        );
      }
    });
  }
  async stop() {
    this.stopping = true;
    this.online = false;
    if (this.timer) clearInterval(this.timer);
    await this.tail;
    try {
      await this.rpc.stopIo(this.account);
    } finally {
      this.child.kill();
    }
  }
}
