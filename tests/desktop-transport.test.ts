import { expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { runInNewContext } from "node:vm";
import { webcrypto } from "node:crypto";
import { encrypt } from "../src/crypto";

test("read-only desktop Webxdc API delivers decrypted initial and subsequent state", async () => {
  const token = "desktop-test-token";
  const first = encrypt({ role: "bot", sequence: 1, version: "1", state: { profile: { nickname: "测试" } } }, token);
  let deliver!: (update: unknown) => void;
  const api = Object.freeze({
    setUpdateListener: async (callback: (update: unknown) => void) => {
      deliver = callback;
      callback({ payload: first });
    },
    sendUpdate: async () => {},
  });
  const window: any = { webxdc: api, CONTACT_CONFIG: { token } };
  runInNewContext(readFileSync("transport.js", "utf8"), {
    window, crypto: webcrypto, TextEncoder, TextDecoder, Uint8Array,
    atob, btoa, setInterval, clearInterval,
  });
  const received: any[] = [];
  await window.XdcTransportListen((update: any) => received.push(update));
  expect(received.at(-1).payload.profile.nickname).toBe("测试");
  const next = encrypt({ role: "bot", sequence: 2, version: "2", base: "1", patch: { profile: { nickname: "更新" } } }, token);
  deliver({ payload: next });
  await new Promise((resolve) => setTimeout(resolve, 20));
  expect(received.at(-1).payload.profile.nickname).toBe("更新");
  expect(window.webxdc).toBe(api);
});
