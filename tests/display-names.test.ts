import { expect, test } from "bun:test";
import { fixture } from "./fixture";

test("ordered display names persist, validate, and remain network scoped", () => {
  const f = fixture();
  try {
    const a = f.create("A"), b = f.create("B");
    const join = (code: string, nickname: string) => f.call("join_network", {value: code, nickname, consent: true, use_current_account: true, discoverable: true, accepts_requests: true});
    join(a.code, "Original");
    const save = (value: string) => f.call("command", {command: "昵称显示排序", value});
    expect(save("First\nSecond\nThird").profile.display_names).toEqual(["First", "Second", "Third"]);
    expect(save("Second\nFirst").profile.nickname).toBe("Second");
    for (const value of ["", "x\nx", "a".repeat(41), Array.from({length:11}, (_,i) => String(i)).join("\n")]) expect(save(value).receipt.ok).toBe(0);
    expect(f.bridge.snapshot(f.instance()).profile.display_names).toEqual(["Second", "First"]);
    join(b.code, "PrivateOtherNetwork");
    expect(f.bridge.snapshot(f.instance()).profile.display_names).toEqual([]);
    const peer = f.service.identify({accountId:1,chatId:30,contactId:31,address:"peer@example.org",fingerprint:"b".repeat(40)});
    f.s.run("UPDATE memberships SET user_id=? WHERE user_id=? AND network_id=?", peer.id, f.user.id, a.network);
    join(a.code, "Viewer");
    let directory = f.bridge.snapshot(f.instance()).directory;
    expect(directory.members[0].display_names).toEqual(["Second", "First"]);
    expect(JSON.stringify(directory)).not.toContain("PrivateOtherNetwork");
    expect(directory.members[0].share_contact).toBeUndefined();
    f.s.run("UPDATE memberships SET discoverable=0 WHERE user_id=?", peer.id);
    expect(f.bridge.snapshot(f.instance()).directory.members).toEqual([]);
  } finally { f.s.close(); }
});
