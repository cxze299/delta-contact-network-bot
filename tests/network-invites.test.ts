import { expect,test } from "bun:test";
import { fixture } from "./fixture";
import { NetworkInvites } from "../src/network_invites";
import { createAdmin } from "../src/admin_web";
test("network invitation has limits, isolation, revocation and idempotent membership",()=>{
 const f=fixture();try{
 f.config.publicUrl="https://bot.example.org";const a=f.create("A"),b=f.create("B"),invites=new NetworkInvites(f.service);
 invites.create(a.network,f.user.id);const invitation=invites.list(a.network)[0];
 const join=(value:string)=>f.call("join_network",{value,nickname:"Member",consent:true,use_current_account:true,discoverable:true,accepts_requests:true});
 expect(join(invitation.link).receipt.ok).toBe(1);
 expect(join(invitation.link).receipt.ok).toBe(1);
 expect(invites.list(a.network)[0].uses).toBe(1);
 expect(join(b.code).networks.length).toBe(2);
 expect(join(invitation.link).current_network).toBe(a.network);
 f.call("admin_lock");
 expect(f.call("network_invite_create",{value:"1"}).receipt.ok).toBe(1);
 expect(f.call("network_invite_create",{value:"forever"}).receipt.ok).toBe(1);
 expect(invites.list(a.network)[0].expires_at.startsWith("9999")).toBe(true);
 expect(f.call("admin_action",{operation:"invite_create",admin_network:a.network}).receipt.ok).toBe(0);
 f.s.run("UPDATE v2_network_invites SET revoked=1 WHERE id=?",invitation.id);
 expect(join(invitation.link).receipt.ok).toBe(0);
 invites.create(a.network,f.user.id);const second=invites.list(a.network)[0];
 f.s.run("UPDATE v2_network_invites SET uses=100000 WHERE id=?",second.id);
 expect(join(second.link).receipt.ok).toBe(1);
 invites.create(a.network,f.user.id);const third=invites.list(a.network)[0];
 f.s.run("UPDATE v2_network_invites SET expires_at='2000-01-01' WHERE id=?",third.id);
 expect(join(third.link).receipt.ok).toBe(0);
 invites.create(a.network,f.user.id);const fourth=invites.list(a.network)[0];
 f.s.run("UPDATE networks SET join_enabled=0 WHERE id=?",a.network);
 expect(join(fourth.link).receipt.ok).toBe(0);
 }finally{f.s.close();}
});
test("public invite page needs no admin login and returns no private network data",async()=>{
 const f=fixture();try{
 f.config.publicUrl="https://bot.example.org";const n=f.create("Invite test"),invites=new NetworkInvites(f.service);invites.create(n.network,f.user.id);
 const token=invites.token(invites.list(n.network)[0].link)!;
 const app=createAdmin(f.service,f.bridge.admin,()=>true,()=>{},async()=>"https://i.delta.chat/#test");
 expect((await app.request("/join")).status).toBe(200);
 const response=await app.request("/api/public-invite",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({token})});
 expect(response.status).toBe(200);expect(await response.json()).toEqual({name:"Invite test",bot:"https://i.delta.chat/#test"});
 expect((await app.request("/api/public-invite",{method:"POST",body:JSON.stringify({token:"invalid"})})).status).toBe(400);
 expect((await app.request("/api/invite")).status).toBe(401);
 }finally{f.s.close();}
});
test("ordinary members can revoke only their own invitation and leaving revokes their links",()=>{
 const f=fixture();try{
 f.config.publicUrl="https://bot.example.org";const n=f.create("A");
 f.call("join_network",{value:n.code,nickname:"One",consent:true,use_current_account:true,discoverable:true,accepts_requests:true});
 f.call("admin_lock");
 expect(f.call("network_invite_create",{value:"1"}).receipt.ok).toBe(1);
 const invites=new NetworkInvites(f.service),own=invites.list(n.network,f.user.id)[0];
 const peer=f.service.identify({accountId:1,chatId:32,contactId:33,address:"peer@example.org",fingerprint:"b".repeat(40)});
 invites.create(n.network,peer.id,"forever");const foreign=invites.list(n.network,peer.id)[0];
 expect(f.call("network_invite_revoke",{value:String(foreign.id)}).receipt.ok).toBe(0);
 expect(f.call("network_invite_revoke",{value:String(own.id)}).receipt.ok).toBe(1);
 expect(()=>invites.find(own.link)).toThrow();
 f.call("network_invite_create",{value:"7"});const next=invites.list(n.network,f.user.id)[0];
 f.service.invalidate(f.service.member(f.service.user(f.user.id)).id,"left");
 expect(()=>invites.find(next.link)).toThrow();
 }finally{f.s.close();}
});

test("invited network preview and auto-selection across bridge and user lifecycle", async () => {
  const f = fixture();
  try {
    f.config.publicUrl = "https://bot.example.org";
    const n = f.create("Invite Preselection Test");
    const invites = new NetworkInvites(f.service);
    invites.create(n.network, f.user.id);
    const invitation = invites.list(n.network)[0];
    const token = invites.token(invitation.link)!;

    // 1. Calling /api/public-invite records the preview
    const app = createAdmin(f.service, f.bridge.admin, () => true, () => {}, async () => "https://i.delta.chat/#test");
    const response = await app.request("/api/public-invite", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token })
    });
    expect(response.status).toBe(200);

    const preview = f.service.latestInvitePreview(60000);
    expect(preview).not.toBeNull();
    expect(preview?.token).toBe(token);
    expect(preview?.network_name).toBe("Invite Preselection Test");

    // 2. Set user invited network for a user without profile
    f.service.setUserInvitedNetwork(f.user.id, preview!.network_id, preview!.token, preview!.network_name);

    const invited = f.service.getUserInvitedNetwork(f.user.id);
    expect(invited).not.toBeNull();
    expect(invited?.name).toBe("Invite Preselection Test");
    expect(invited?.code).toBe(token);

    // 3. Instance snapshot includes invited_network
    const snap = f.bridge.snapshot(f.instance());
    expect(snap.invited_network).toEqual({
      network_id: preview!.network_id,
      code: preview!.token,
      name: preview!.network_name
    });

    // 4. Action dismiss_invited_network clears it
    f.call("dismiss_invited_network");
    expect(f.service.getUserInvitedNetwork(f.user.id)).toBeNull();
    expect(f.bridge.snapshot(f.instance()).invited_network).toBeNull();

    // 5. Re-set and verify joining clears invited_network
    f.service.setUserInvitedNetwork(f.user.id, preview!.network_id, preview!.token, preview!.network_name);
    expect(f.service.getUserInvitedNetwork(f.user.id)).not.toBeNull();

    const joinRes = f.call("join_network", {
      value: token,
      nickname: "Joined User",
      consent: true,
      use_current_account: true,
      discoverable: true,
      accepts_requests: true
    });
    expect(joinRes.receipt.ok).toBe(1);
    expect(f.service.getUserInvitedNetwork(f.user.id)).toBeNull();
    expect(f.bridge.snapshot(f.instance()).invited_network).toBeNull();

    // 6. User already has active profile in Network A; receives invitation for Network B
    const netB = f.create("Network B");
    invites.create(netB.network, f.user.id);
    const tokenB = invites.token(invites.list(netB.network)[0].link)!;
    f.service.setUserInvitedNetwork(f.user.id, netB.network, tokenB, "Network B");

    const snapWithProfile = f.bridge.snapshot(f.instance());
    expect(snapWithProfile.profile).not.toBeNull();
    expect(snapWithProfile.invited_network).toEqual({
      network_id: netB.network,
      code: tokenB,
      name: "Network B"
    });

    // 7. Joining Network B clears invited_network and switches current network
    const joinB = f.call("join_network", {
      value: tokenB,
      nickname: "Joined User B",
      consent: true,
      use_current_account: true,
      discoverable: true,
      accepts_requests: true
    });
    expect(joinB.receipt.ok).toBe(1);
    expect(f.bridge.snapshot(f.instance()).invited_network).toBeNull();
    expect(f.bridge.snapshot(f.instance()).current_network).toBe(netB.network);
  } finally {
    f.s.close();
  }
});
