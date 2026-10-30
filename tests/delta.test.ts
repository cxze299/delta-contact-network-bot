import { expect, test } from "bun:test";
import { fixture } from "./fixture";
import { decrypt } from "../src/crypto";
test("cold updates use explicit deltas and refresh repairs missing baselines", async () => {
  const f = fixture();
  await f.bridge.publish(f.instance(), true);
  f.call("admin_open");
  await f.bridge.publish(f.instance());
  let packet = decrypt(f.published.at(-1)[2], f.token, "contact-response-v3");
  expect(packet.patch).toBeDefined();
  expect(packet.state).toBeUndefined();
  f.call("refresh");
  await f.bridge.publish(f.instance());
  packet = decrypt(f.published.at(-1)[2], f.token, "contact-response-v3");
  expect(packet.state).toBeDefined();
  expect(packet.base).toBeNull();
  f.s.close();
});
