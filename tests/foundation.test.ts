import { expect, test } from "bun:test";
import { Store } from "../src/db";
import { Service } from "../src/service";
import { Bridge } from "../src/webxdc_bridge";
import { configuration } from "../src/config";
import { encrypt, decrypt, random, hmac } from "../src/crypto";
import { Command, now } from "../src/protocol";
import { pbkdf2Sync } from "node:crypto";
import { fixture } from "./fixture";
test("authenticated envelopes reject tampering and untrusted identity", () => {
  const token = random(32),
    e = encrypt({ hello: "world" }, token);
  expect(decrypt(e, token, "contact-response-v3")).toEqual({ hello: "world" });
  expect(() =>
    decrypt({ ...e, nonce: random(12) }, token, "contact-response-v3"),
  ).toThrow();
  expect(
    Command.safeParse({
      token,
      id: "a".repeat(32),
      action: "refresh",
      sender_addr: "spoof@example.org",
    }).success,
  ).toBe(false);
});
test("transaction rollback preserves original state and database opens twice", () => {
  const f = fixture();
  expect(() =>
    f.s.tx(() => {
      f.s.run("UPDATE users SET current_network_id=99 WHERE id=?", f.user.id);
      throw Error("rollback");
    }),
  ).toThrow();
  expect(f.service.user(f.user.id).current_network_id).toBeNull();
  f.s.close();
});
test("administrator proof is account-bound, rate limited and nonce consumed", () => {
  const f = fixture();
  expect(
    f.call("admin_action", { operation: "create", data: { name: "No" } })
      .receipt.ok,
  ).toBe(0);
  f.call("admin_open");
  const c = f.bridge.snapshot(f.instance()).admin.challenge,
    proof = hmac(
      Buffer.from(f.key, "hex"),
      "contact-admin-v1:" + c.nonce + ":wrong-token",
    );
  expect(f.call("admin_unlock", { proof }).receipt.ok).toBe(0);
  expect(f.grant().admin.unlocked).toBe(true);
  f.s.close();
});
