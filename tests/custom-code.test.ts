import { expect, test } from "bun:test";
import { fixture } from "./fixture";
import { hmac } from "../src/crypto";

test("custom join code replaces old code and rejects invalid or duplicate codes", () => {
  const f = fixture();
  try {
    const a = f.create("A"), b = f.create("B");
    const rotate = (code: string) => f.s.tx(() => f.bridge.admin.action(f.service.user(f.user.id), a.network, "rotate", { code }));
    rotate("xhsybyx");
    expect(f.s.one("SELECT id FROM networks WHERE join_code_hash=?", hmac(f.config.secret, "xhsybyx"))?.id).toBe(a.network);
    expect(f.s.one("SELECT id FROM networks WHERE join_code_hash=?", hmac(f.config.secret, a.code))).toBeNull();
    expect(() => rotate("short")).toThrow();
    expect(() => rotate("has space")).toThrow();
    expect(() => rotate("")).toThrow();
    expect(() => rotate(b.code)).toThrow();
    expect(f.service.readCode(f.s.one("SELECT join_code_ciphertext FROM networks WHERE id=?", a.network)!.join_code_ciphertext)).toBe("xhsybyx");
  } finally { f.s.close(); }
});
