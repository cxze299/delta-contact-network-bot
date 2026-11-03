import { encrypt } from "../src/crypto";
import { expect, test } from "bun:test";
import { fixture } from "./fixture";
test("native notification is claimed once even after an ambiguous publish",async()=>{
 const f=fixture();try{
 await f.bridge.publish(f.instance(),true);
 f.s.run("INSERT INTO v2_notifications VALUES(?,?,?,?)","new",f.user.id,"new",new Date().toISOString());
 const status=f.bridge.publisher.status;
 f.bridge.publisher.status=async(...args)=>{await status(...args);throw Error("after-send failure");};
 await expect(f.bridge.publish(f.instance())).rejects.toThrow();
 f.bridge.publisher.status=status;
 await f.bridge.publish(f.instance());
 expect(f.published.filter(row=>row[3]===true).length).toBe(1);
 f.s.run("INSERT INTO v2_notifications VALUES(?,?,?,?)","old",f.user.id,"history","2020-01-01T00:00:00.000Z");
 await f.bridge.publish(f.instance());
 expect(f.published.at(-1)[3]).toBe(false);
 }finally{f.s.close();}
});
test("old app instances stay silent and already read notices stay silent",async()=>{
 const f=fixture();try{
 await f.bridge.publish(f.instance(),true);const i=f.instance(),token="q".repeat(32);
 f.s.run("INSERT INTO xdc_instances(token,actor_key,account_id,chat_id,message_id,created_at) VALUES(?,?,?,?,?,?)",token,i.actor_key,1,11,999,new Date().toISOString());
 f.s.run("INSERT INTO v2_instances(token,user_id) VALUES(?,?)",token,f.user.id);
 const latest=f.bridge.instance(1,999)!;await f.bridge.publish(latest,true);
 f.s.run("INSERT INTO v2_notifications VALUES(?,?,?,?)","fresh",f.user.id,"new",new Date().toISOString());
 await f.bridge.publish(f.instance());expect(f.published.at(-1)[3]).toBe(false);
 await f.bridge.publish(latest);expect(f.published.at(-1)[3]).toBe(true);
 f.s.run("INSERT INTO v2_notifications VALUES(?,?,?,?)","read",f.user.id,"read",new Date().toISOString());
 f.bridge.handle(latest,encrypt({token,id:"a".repeat(32),action:"read_notifications",value:JSON.stringify(["read"])},token,"contact-action-v2"));
 await f.bridge.publish(latest);expect(f.published.at(-1)[3]).toBe(false);
 }finally{f.s.close();}
});
