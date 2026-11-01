import { expect, test } from "bun:test";
import { fixture } from "./fixture";
import { DeltaAdapter } from "../src/delta_adapter";
test("verified securejoin offers once without user message, including duplicate events and restarts", async () => {
  const f = fixture(); let sends = 0, verified = false;
  const create = () => {
    const a = new DeltaAdapter(f.service, f.config); a.account = 1;
    a.rpc = {getContact: async () => ({isVerified: verified,isBlocked:false}), createChatByContactId:async()=>44} as any;
    a.identity = async () => ({accountId:1,chatId:44,contactId:45,address:"new@example.org",fingerprint:"c".repeat(40)});
    a.offer = async () => {sends++;}; return a;
  };
  try {
    const a=create(); await a.onSecureJoin(45); expect(sends).toBe(0);
    verified=true; await a.onSecureJoin(45); await a.onSecureJoin(45); await create().onSecureJoin(45); expect(sends).toBe(1);
  } finally { f.s.close(); }
});
test("unencrypted or mismatched identity never receives automatic app",async()=>{
 const f=fixture(); const a=new DeltaAdapter(f.service,f.config);a.account=1;let sends=0;
 a.rpc={getContact:async()=>({isVerified:true,isBlocked:false}),createChatByContactId:async()=>44} as any;
 a.identity=async()=>{throw Error("Encrypted private chat required");};a.offer=async()=>{sends++;};
 try {await a.onSecureJoin(45);expect(sends).toBe(0);} finally {f.s.close();}
});
