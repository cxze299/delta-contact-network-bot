import { expect, test } from "bun:test";
import { fixture } from "./fixture";
test("account preferences persist to fresh instances and reject foreign notifications and aliases",()=>{
 const f=fixture();try{
 const a=f.create("A"),b=f.create("B");
 f.call("join_network",{value:a.code,nickname:"One",consent:true,use_current_account:true,discoverable:true,accepts_requests:true});
 const profile=f.call("command",{command:"昵称显示排序",value:"One\nTwo"}).profile;
 expect(f.call("select_alias",{value:profile.member_code,nickname:"Two"}).receipt.ok).toBe(1);
 expect(f.call("select_alias",{value:profile.member_code,nickname:"Unknown"}).receipt.ok).toBe(0);
 f.s.run("INSERT INTO v2_notifications VALUES(?,?,?,?)","notice-1",f.user.id,"hello",new Date().toISOString());
 expect(f.call("read_notifications",{value:JSON.stringify(["notice-1","foreign"])}).receipt.ok).toBe(1);
 const token="z".repeat(32),i=f.instance();
 f.s.run("INSERT INTO xdc_instances(token,actor_key,account_id,chat_id,message_id,created_at) VALUES(?,?,?,?,?,?)",token,i.actor_key,1,11,999,new Date().toISOString());
 f.s.run("INSERT INTO v2_instances(token,user_id) VALUES(?,?)",token,f.user.id);
 const fresh=f.bridge.snapshot(f.bridge.instance(1,999)!);
 expect(fresh.read_notifications).toEqual(["notice-1"]);
 expect(fresh.alias_choices[profile.member_code]).toBe("Two");
 f.call("join_network",{value:b.code,nickname:"Elsewhere",consent:true,use_current_account:true,discoverable:true,accepts_requests:true});
 expect(f.bridge.snapshot(f.instance()).alias_choices).toEqual({});
 expect(f.call("select_alias",{value:profile.member_code,nickname:"One"}).receipt.ok).toBe(0);
 f.service.erase(f.service.user(f.user.id));
 expect(f.s.one("SELECT count(*) n FROM v2_name_choices")?.n).toBe(0);
 expect(f.s.one("SELECT count(*) n FROM v2_notification_reads")?.n).toBe(0);
 }finally{f.s.close();}
});
