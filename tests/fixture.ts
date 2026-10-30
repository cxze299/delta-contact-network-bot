import { Store } from "../src/db";
import { Service } from "../src/service";
import { Bridge } from "../src/webxdc_bridge";
import { configuration } from "../src/config";
import { encrypt, decrypt, random, hmac } from "../src/crypto";
import { Command, now } from "../src/protocol";
import { pbkdf2Sync } from "node:crypto";
export function fixture() {
  const salt = "ab".repeat(16),
    key = pbkdf2Sync(
      "test-admin-key",
      Buffer.from(salt, "hex"),
      250000,
      32,
      "sha256",
    ).toString("hex"),
    config = configuration({
      CONTACTBOT_SECRET: "secret-for-isolated-tests-0123456789abcdef",
      CONTACTBOT_XDC_ADMIN_VERIFIER: `250000:${salt}:${key}`,
      CONTACTBOT_ADMIN_WEB_TOKEN: "independent-web-test-password",
    }),
    s = new Store(":memory:"),
    service = new Service(s, config),
    published: any[] = [],
    bridge = new Bridge(service, {
      status: async (...args) => {
        published.push(args);
      },
      realtime: async () => {},
    });
  const user = service.identify({
      accountId: 1,
      chatId: 11,
      contactId: 12,
      address: "one@example.org",
      fingerprint: "a".repeat(40),
    }),
    token = random(32);
  s.run(
    "INSERT INTO xdc_instances(token,actor_key,account_id,chat_id,message_id,created_at) VALUES(?,?,?,?,?,?)",
    token,
    user.actor_key,
    1,
    11,
    100,
    now(),
  );
  s.run("INSERT INTO v2_instances(token,user_id) VALUES(?,?)", token, user.id);
  let counter = 0;
  const instance = () => bridge.instance(1, 100)!;
  const call = (action: string, extra: any = {}, id?: string) => {
    const c = {
      token,
      id: id || (++counter).toString(16).padStart(32, "0"),
      action,
      network: service.user(user.id).current_network_id,
      ...extra,
    };
    bridge.handle(instance(), encrypt(c, token, "contact-action-v2"));
    return bridge.snapshot(instance());
  };
  const grant = () => {
    call("admin_open");
    const challenge = bridge.snapshot(instance()).admin.challenge;
    return call("admin_unlock", {
      proof: hmac(
        Buffer.from(key, "hex"),
        "contact-admin-v1:" + challenge.nonce + ":" + token,
      ),
    });
  };
  const create = (name: string) => {
    grantIfNeeded();
    const state = call("admin_action", { operation: "create", data: { name } }),
      network = state.admin.current,
      n = s.one("SELECT * FROM networks WHERE id=?", network)!;
    return { network, code: service.readCode(n.join_code_ciphertext) };
  };
  const grantIfNeeded = () => {
    if (!bridge.admin.system(service.user(user.id))) grant();
  };
  return {
    s,
    service,
    bridge,
    config,
    user,
    token,
    key,
    call,
    grant,
    create,
    instance,
    published,
  };
}
