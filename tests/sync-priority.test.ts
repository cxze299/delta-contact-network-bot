import { expect,test } from "bun:test";
import { fixture } from "./fixture";
import { DeltaAdapter } from "../src/delta_adapter";
test("broadcast skips inactive historical apps but keeps live and forced apps",async()=>{
 const f=fixture();try{
 const i=f.instance(),latest="l".repeat(32);
 f.s.run("INSERT INTO xdc_instances(token,actor_key,account_id,chat_id,message_id,created_at) VALUES(?,?,?,?,?,?)",latest,i.actor_key,1,11,999,new Date().toISOString());
 f.s.run("INSERT INTO v2_instances(token,user_id) VALUES(?,?)",latest,f.user.id);
 await f.bridge.publishAll();expect(f.published.map(row=>row[1])).toEqual([999]);
 f.published.length=0;await f.bridge.publishAll(i.token);expect(f.published.map(row=>row[1])).toEqual([100]);
 f.bridge.publisher.isLive=token=>token===i.token;
 const targets:string[]=[];f.bridge.publish=async row=>{targets.push(row.token);};
 await f.bridge.publishAll();expect(targets).toEqual([latest]);
 }finally{f.s.close();}
});
test("realtime presence expires so reconnection is not skipped forever",()=>{
 const f=fixture();try{
 const a=new DeltaAdapter(f.service,f.config);
 (a as any).live.set("old",Date.now()-46000);(a as any).live.set("new",Date.now());
 expect(a.isLive("old")).toBe(false);expect(a.isLive("new")).toBe(true);
 }finally{f.s.close();}
});
test("retired instances reject mutations but allow refresh and latest app requests",()=>{
 const f=fixture();try{
 const n=f.create("A");f.call("join_network",{value:n.code,nickname:"Original",consent:true,use_current_account:true,discoverable:true,accepts_requests:true});
 const i=f.instance(),token="r".repeat(32);
 f.s.run("INSERT INTO xdc_instances(token,actor_key,account_id,chat_id,message_id,created_at) VALUES(?,?,?,?,?,?)",token,i.actor_key,1,11,999,new Date().toISOString());
 f.s.run("INSERT INTO v2_instances(token,user_id) VALUES(?,?)",token,f.user.id);
 expect(f.call("command",{command:"编辑昵称",value:"Changed"}).receipt.ok).toBe(0);
 expect(f.bridge.snapshot(i).profile.nickname).toBe("Original");
 expect(f.call("refresh").instance_current).toBe(false);
 expect(f.call("get_latest_app").receipt.ok).toBe(1);
 expect(f.bridge.snapshot(f.bridge.instance(1,999)!).instance_current).toBe(true);
 }finally{f.s.close();}
});
