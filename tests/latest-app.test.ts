import { expect, test } from "bun:test";
import { fixture } from "./fixture";
import { DeltaAdapter } from "../src/delta_adapter";
test("latest app request is deduplicated and uncertain sends never retry", async () => {
  const f=fixture(), a=new DeltaAdapter(f.service,f.config); a.account=1; let sends=0;
  a.identity=async()=>({accountId:1,chatId:11,contactId:12,address:"one@example.org",fingerprint:"a".repeat(40)});
  a.offer=async()=>{sends++;};
  try {
    const id="f".repeat(32);
    expect(f.call("get_latest_app",{},id).receipt.ok).toBe(1);
    f.call("get_latest_app",{},id);
    await a.deliverAppUpdates();await a.deliverAppUpdates();
    expect(sends).toBe(1);
    expect(f.s.one("SELECT status FROM v2_app_updates")?.status).toBe("sent");
    f.call("get_latest_app");a.offer=async()=>{sends++;throw Error("ambiguous send");};
    await a.deliverAppUpdates();await a.deliverAppUpdates();
    expect(sends).toBe(2);
    expect(f.s.one("SELECT count(*) n FROM v2_app_updates WHERE status='uncertain'")?.n).toBe(1);
    expect(f.call("get_latest_app").receipt.ok).toBe(0);
  } finally {f.s.close();}
});
test("latest app refuses an unencrypted chat",async()=>{
 const f=fixture(),a=new DeltaAdapter(f.service,f.config);a.account=1;let sends=0;
 a.identity=async()=>{throw Error("unencrypted");};a.offer=async()=>{sends++;};
 try{f.call("get_latest_app");await a.deliverAppUpdates();expect(sends).toBe(0);}finally{f.s.close();}
});
