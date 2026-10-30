import { readFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { fixture } from "../tests/fixture";
import { encrypt } from "../src/crypto";
const f = fixture(),
  a = f.create("原网络"),
  b = f.create("第二网络");
f.call("join_network", {
  value: a.code,
  nickname: "测试成员",
  consent: true,
  use_current_account: true,
  discoverable: true,
  accepts_requests: true,
});
let updates: any[] = [];
async function publish(force = false) {
  const i = f.instance(),
    state = f.bridge.snapshot(i),
    sequence = Number(i.revision) + 1;
  f.s.run(
    "UPDATE v2_instances SET revision=? WHERE token=?",
    sequence,
    f.token,
  );
  updates.push({
    serial: sequence,
    payload: encrypt(
      { role: "bot", sequence, version: String(sequence), base: null, state },
      f.token,
    ),
  });
}
await publish(true);
Bun.serve({
  hostname: "127.0.0.1",
  port: 18794,
  async fetch(req) {
    const url = new URL(req.url);
    if (url.pathname === "/test-info") return Response.json({ second: b.code });
    if (url.pathname === "/updates") return Response.json(updates);
    if (url.pathname === "/send") {
      const { payload } = await req.json();
      f.bridge.handle(f.instance(), payload);
      await publish();
      return Response.json({ ok: true });
    }
    if (url.pathname === "/webxdc.js")
      return new Response(
        `window.webxdc={sendUpdate:async data=>{window.testSends=(window.testSends||0)+1;await fetch('/send',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(data)});await window.deliver();},setUpdateListener:async cb=>{window.updateCallback=cb;window.deliver=async()=>{const data=await(await fetch('/updates')).json();for(const u of data)cb(u);};await window.deliver();},sendToChat:async()=>{}};`,
        { headers: { "Content-Type": "text/javascript" } },
      );
    if (url.pathname === "/config.js")
      return new Response(
        "window.CONTACT_CONFIG=" +
          JSON.stringify({ token: f.token, protocol: 3 }) +
          ";",
        { headers: { "Content-Type": "text/javascript" } },
      );
    const name = url.pathname === "/" ? "index.html" : url.pathname.slice(1);
    if (!["index.html", "app.js", "style.css", "icon.png"].includes(name))
      return new Response("not found", { status: 404 });
    return new Response(Bun.file(resolve("webxdc", name)));
  },
});
console.log("UI fixture ready at 18794");
