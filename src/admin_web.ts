import { invitationLink } from "./invite";
import { Hono } from "hono";
import { readFileSync } from "node:fs";
import { pbkdf2Sync } from "node:crypto";
import { type Row } from "./db";
import { Service } from "./service";
import { Admin } from "./xdc_admin";
import { equal, hmac, random } from "./crypto";
import { UserError, now } from "./protocol";
const baseStyle = readFileSync(
  new URL("../admin-base.css", import.meta.url),
  "utf8",
);
const esc = (value: unknown) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ]!,
  );
export function createAdmin(
  service: Service,
  admin: Admin,
  healthy: () => boolean,
  changed: () => void,
  invite?: () => Promise<string>,
) {
  const s = service.s,
    app = new Hono<{ Variables: { actor: Row } }>();
  const actorKey = hmac(service.config.secret, "web-system-administrator");
  let webUser = s.one("SELECT id FROM users WHERE actor_key=?", actorKey);
  if (!webUser) {
    const id = Number(
      s.run(
        "INSERT INTO users(actor_key,contact_id,chat_id,created_at,updated_at) VALUES(?,-1,-1,?,?)",
        actorKey,
        now(),
        now(),
      ).lastInsertRowid,
    );
    s.run("INSERT INTO v2_users(user_id,address) VALUES(?,'')", id);
    webUser = { id };
  }
  s.run(
    "INSERT OR REPLACE INTO xdc_admin_grants VALUES(?,?,?)",
    webUser.id,
    admin.version,
    now(),
  );
  const csrf = (u: Row) =>
    hmac(service.config.webPassword, "contactbot-csrf:" + u.id);
  function authenticate(header: string) {
    try {
      const raw = Buffer.from(
          header.replace(/^Basic /, ""),
          "base64",
        ).toString(),
        at = raw.indexOf(":"),
        name = raw.slice(0, at),
        password = raw.slice(at + 1);
      if (at < 0) return null;
      if (equal(name, "admin") && equal(password, service.config.webPassword))
        return service.user(webUser!.id);
      if (/^user-\d+$/.test(name)) {
        const id = Number(name.slice(5)),
          row = s.one(
            "SELECT password_hash FROM web_admin_logins WHERE user_id=?",
            id,
          );
        if (!row) return null;
        const [salt, digest] = row.password_hash.split(":");
        if (
          equal(
            pbkdf2Sync(password, salt, 200000, 32, "sha256").toString("hex"),
            digest,
          )
        ) {
          const u = service.user(id);
          return admin.networks(u).length ? u : null;
        }
      }
    } catch {}
    return null;
  }
  app.use("*", async (c, next) => {
    c.header("Cache-Control", "no-store");
    c.header("X-Content-Type-Options", "nosniff");
    c.header("Referrer-Policy", "no-referrer");
    c.header(
      "Content-Security-Policy",
      "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'",
    );
    if (["/health", "/api/health"].includes(c.req.path)) return next();
    const u = authenticate(c.req.header("authorization") || "");
    if (!u) {
      c.header("WWW-Authenticate", 'Basic realm="ContactBot Local Admin"');
      return c.text("需要管理员身份验证", 401);
    }
    c.set("actor", u);
    await next();
  });
  app.get("/api/invite", async (c) => {
    const u = c.get("actor");
    admin.require(u, null, true);
    if (!invite) return c.text("Core unavailable", 503);
    const qr = await invite();
    return c.json({
      link: invitationLink(qr),
    });
  });
  app.get("/health", (c) =>
    c.text(healthy() ? "ok" : "starting", healthy() ? 200 : 503),
  );
  app.get("/api/health", (c) =>
    c.json({ ok: healthy(), runtime: "bun" }, healthy() ? 200 : 503),
  );
  app.get("/admin.css", (c) =>
    c.body(
      readFileSync(new URL("../admin.css", import.meta.url), "utf8"),
      200,
      { "Content-Type": "text/css" },
    ),
  );
  app.get("/admin.js", (c) =>
    c.body(readFileSync(new URL("../admin.js", import.meta.url), "utf8"), 200, {
      "Content-Type": "text/javascript",
    }),
  );
  app.get("/favicon.ico", (c) => c.body(null, 204));
  function page(
    u: Row,
    networkId: number | null,
    message = "",
    q = "",
    pageNo = 1,
  ) {
    const system = admin.system(u),
      networks = admin.networks(u),
      n = networks.find((n) => n.id === networkId) || networks[0],
      token = csrf(u);
    pageNo = Math.max(1, pageNo);
    const form = (
      op: string,
      label: string,
      fields = "",
      network = String(n?.id || ""),
    ) =>
      `<form method="post" action="/action"><input type="hidden" name="csrf" value="${token}"><input type="hidden" name="network_id" value="${esc(network)}"><input type="hidden" name="action" value="${op}">${fields}${["remove", "disable", "approve_recovery", "reject_recovery", "delivery_close"].includes(op) ? '<label class="confirm"><input type="checkbox" name="confirmed" value="yes" required>已核实并确认操作</label>' : ""}<button>${label}</button></form>`;
    let detail =
        '<section class="card" id="settings"><p>请创建网络。</p></section>',
      summary = "";
    if (n) {
      const count = Number(
          s.one(
            "SELECT count(*) n FROM contact_requests WHERE network_id=?",
            n.id,
          )?.n,
        ),
        reports = s.all(
          "SELECT * FROM reports WHERE network_id=? ORDER BY id DESC LIMIT 30",
          n.id,
        ),
        reportCount = Number(
          s.one(
            "SELECT count(*) n FROM reports WHERE network_id=? AND status='open'",
            n.id,
          )?.n,
        ),
        total = Number(
          s.one(
            "SELECT count(*) n FROM memberships WHERE network_id=? AND status='active' AND (instr(nickname,?)>0 OR instr(member_code,?)>0)",
            n.id,
            q,
            q,
          )?.n,
        );
      pageNo = Math.min(pageNo, Math.max(1, Math.ceil(total / 20)));
      const members = s.all(
        "SELECT * FROM memberships WHERE network_id=? AND status='active' AND (instr(nickname,?)>0 OR instr(member_code,?)>0) ORDER BY nickname,id LIMIT 20 OFFSET ?",
        n.id,
        q,
        q,
        (pageNo - 1) * 20,
      );
      summary = `<div class="workspace-heading"><div><p class="eyebrow">联系网络 / 管理工作台</p><h2>${esc(n.name)}</h2><p class="muted">管理成员与联系申请，让每一次连接都有明确的同意。</p></div><span class="network-state">${n.status === "active" ? "正常运行" : "已停用"}</span></div><div class="metrics"><article><span>活跃成员</span><strong>${n.member_count}</strong><small>当前网络</small></article><article><span>联系申请</span><strong>${count}</strong><small>累计申请</small></article><article><span>待处理举报</span><strong>${reportCount}</strong><small>需要关注</small></article></div>`;
      detail = `<section class="card" id="settings"><h2>${esc(n.name)}</h2><p class="muted">${esc(n.public_id)} · ${n.member_count} 位成员 · ${count} 条申请 · ${reportCount} 条待处理举报</p><form method="post" action="/network/${n.id}"><input type="hidden" name="csrf" value="${token}"><label>网络名称<input name="name" value="${esc(n.name)}" required maxlength="60" ${system ? "" : "readonly"}></label><label><input type="checkbox" name="join_enabled" ${n.join_enabled ? "checked" : ""}>允许新成员加入</label><input type="hidden" name="status" value="${n.status}"><label>网络状态<select disabled><option>${n.status === "active" ? "正常" : "停用"}</option></select></label><button>保存网络设置</button></form><div class="network-tools">${form("view_code", "查看当前加入码")}${form("rotate", "生成随机新加入码")}${form(n.join_enabled ? "join_off" : "join_on", n.join_enabled ? "停用加入" : "恢复加入")}</div><form method="post" action="/network/${n.id}/code" class="code-form"><input type="hidden" name="csrf" value="${token}"><label>新的加入码<input name="code" type="password" required minlength="16" maxlength="200" autocomplete="new-password" placeholder="请使用较长且难以猜测的编码"></label><p class="muted">更换后旧码立即失效，现有成员不受影响。加入码不会在页面回显。</p><button class="danger">立即更换加入码</button></form></section>`;
      const rows = members
        .map((m) => {
          const code = `<input type="hidden" name="member_code" value="${esc(m.member_code)}">`;
          return `<tr><td><strong>${esc(m.nickname)}</strong><br><small>${esc(m.member_code)}</small></td><td><form method="post" action="/member/${m.id}"><input type="hidden" name="csrf" value="${token}"><input type="hidden" name="network_id" value="${n.id}"><select name="role" aria-label="成员角色" ${system ? "" : "disabled"}><option value="member" ${m.role === "member" ? "selected" : ""}>普通成员</option><option value="network_admin" ${m.role === "network_admin" ? "selected" : ""}>网络管理员</option></select><select name="status" aria-label="成员状态"><option value="active">正常</option><option value="removed">已移除</option></select><span class="privacy">目录：${m.discoverable ? "公开" : "隐藏"} · 申请：${m.accepts_requests ? "接收" : "暂停"}<small>由成员本人设置</small></span><button>保存</button></form><details><summary>更多管理操作</summary>${m.role === "member" ? form("remove", "移除成员", code) : system ? form("revoke", "撤销管理员", code) + form("web_access", "生成／重置网页账号", code) : ""}</details></td></tr>`;
        })
        .join("");
      const query = (p: number) =>
        "/?" +
        new URLSearchParams({ network: String(n.id), q, page_no: String(p) }) +
        "#members";
      detail += `<section class="card" id="members"><h2>成员管理 <small>匹配 ${total} 位</small></h2><form method="get" action="/"><input type="hidden" name="network" value="${n.id}"><label>搜索昵称或成员编号<input name="q" value="${esc(q)}" maxlength="80" placeholder="输入昵称或成员编号"></label><button>搜索</button></form><div class="table-wrap"><table><thead><tr><th>成员</th><th>角色与状态</th></tr></thead><tbody>${rows || '<tr><td colspan="2" class="muted">暂无成员</td></tr>'}</tbody></table></div><nav class="pagination" aria-label="成员分页"><a href="${esc(query(Math.max(1, pageNo - 1)))}">上一页</a><span>第 ${pageNo} / ${Math.max(1, Math.ceil(total / 20))} 页 · 每页 20 人</span><a href="${esc(query(Math.min(Math.max(1, Math.ceil(total / 20)), pageNo + 1)))}">下一页</a></nav></section>`;
      const labels: Record<string, string> = {
        pending_target: "等待同意",
        processing: "交换中",
        completed: "已完成",
        rejected: "已拒绝",
        withdrawn: "已撤销",
        expired: "已过期",
        invalid: "已终止",
      };
      const requests = s.all(
        "SELECT public_id,status,created_at FROM contact_requests WHERE network_id=? ORDER BY id DESC LIMIT 20",
        n.id,
      );
      detail += `<section class="card" id="requests"><h2>联系申请</h2><p class="muted">最近 20 条 · 仅显示状态，不展示联系方式及申请理由。</p><div class="table-wrap"><table><thead><tr><th>编号</th><th>状态</th><th>创建时间（UTC）</th></tr></thead><tbody>${requests.map((r) => `<tr><td>${esc(r.public_id)}</td><td>${labels[r.status] || esc(r.status)}</td><td>${esc(r.created_at.slice(0, 16))}</td></tr>`).join("") || '<tr><td colspan="3">暂无申请</td></tr>'}</tbody></table></div></section>`;
      detail += `<section class="card" id="reports"><h2>举报处理 <small>${reportCount} 条待处理</small></h2><p class="muted">最近 30 条举报，仅供授权管理员处理。</p>${reports.map((r) => `<article class="report"><strong>${esc(r.public_id)}</strong><span class="badge">${r.status === "open" ? "待处理" : "已处理"}</span><p>${esc(r.reason)}</p>${r.status === "open" ? form("resolve_report", "标记已处理", `<input type="hidden" name="report_id" value="${esc(r.public_id)}"><label>处理说明<input name="resolution" required maxlength="300"></label>`) : `<p>处理结果：${esc(r.resolution)}</p>`}</article>`).join("") || "<p>暂无举报</p>"}</section>`;
      const logs = s.all(
        "SELECT action,target_ref,created_at FROM audit_log WHERE network_id=? ORDER BY id DESC LIMIT 30",
        n.id,
      );
      detail += `<section class="card" id="audit"><h2>操作记录</h2><p class="muted">本网络最近 30 次操作 · 时间为 UTC</p><div class="table-wrap"><table><thead><tr><th>时间</th><th>操作</th><th>对象</th></tr></thead><tbody>${logs.map((r) => `<tr><td>${esc(r.created_at.slice(0, 16))}</td><td>${esc(r.action)}</td><td>${esc(r.target_ref)}</td></tr>`).join("") || '<tr><td colspan="3">暂无记录</td></tr>'}</tbody></table></div></section>`;
      const recoveries = s.all(
          "SELECT ar.*,m.nickname,m.member_code FROM account_recoveries ar JOIN memberships m ON m.id=ar.membership_id WHERE m.network_id=? ORDER BY ar.id DESC LIMIT 50",
          n.id,
        ),
        deliveries = s.all(
          "SELECT j.id,r.public_id FROM v2_jobs j JOIN contact_requests r ON r.id=j.request_id WHERE r.network_id=? AND j.status='uncertain'",
          n.id,
        );
      detail += `<section class="card" id="recoveries"><h2>账号恢复审核</h2><p class="muted">批准前请通过可信渠道核实新旧账号属于同一成员。</p>${recoveries.map((r) => `<article class="report"><strong>${esc(r.public_id)}</strong><span class="badge">${esc(r.status)}</span><p>${esc(r.nickname)}</p>${r.status === "pending" && r.expires_at > now() ? form("approve_recovery", "批准恢复", `<input type="hidden" name="recovery_id" value="${esc(r.public_id)}">`) + form("reject_recovery", "拒绝恢复", `<input type="hidden" name="recovery_id" value="${esc(r.public_id)}">`) : ""}</article>`).join("") || "<p>暂无恢复申请</p>"}${deliveries.length ? "<h2>待核查名片交付</h2>" + deliveries.map((j) => `<h3>${esc(j.public_id)}</h3>` + form("delivery_close", "结束交换，不重发", `<input type="hidden" name="outbox_id" value="${j.id}">`)).join("") : ""}</section>`;
    }
    if (system)
      detail += `<section class="card" id="system"><h2>系统管理</h2><p class="muted">创建、改名、管理员任免和网络停用均与机器人同步。</p>${form("create", "创建网络", '<label>网络名称<input name="name" required maxlength="60"></label><label>初始加入码（留空随机生成）<input name="code" type="password" autocomplete="new-password"></label>', "")}${n ? form(n.status === "active" ? "disable" : "enable", n.status === "active" ? "停用当前网络" : "恢复当前网络") : ""}</section>`;
    const nav = [
      ["settings", "网络设置"],
      ["members", "成员管理"],
      ["requests", "联系申请"],
      ["reports", "举报处理"],
      ["audit", "操作记录"],
      ["recoveries", "恢复审核"],
      ...(system ? [["system", "系统管理"]] : []),
    ];
    return `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>好友助手 · 网络管理</title><link rel="stylesheet" href="/admin.css"><script src="/admin.js" defer></script><style>${baseStyle}</style></head><body><header><a class="brand" href="/" aria-label="好友助手首页"><span class="brand-mark" aria-hidden="true">友</span><h1>好友助手</h1></a><span class="header-caption">网络管理工作台</span><span class="session"><i aria-hidden="true"></i>管理员会话</span></header><main><aside><h2>我的网络</h2>${networks.map((x) => `<a class="network ${x.id === n?.id ? "selected" : ""}" href="/?network=${x.id}"><strong>${esc(x.name)}</strong><span>${x.member_count} 位成员</span></a>`).join("") || '<p class="muted">暂无网络</p>'}<hr><h2>管理工具</h2><nav class="sections" aria-label="后台导航">${nav.map(([id, title], i) => `<a href="#${id}"><span aria-hidden="true">0${i + 1}</span>${title}</a>`).join("")}</nav><p class="privacy-note">${system ? "系统管理员 · 全部网络" : "网络管理员 · 授权网络"}<br>成员联系方式始终隐藏。<br>隐私选项由成员本人决定。</p></aside><div class="workspace">${summary}<div role="status">${message ? `<div class="notice">${esc(message)}</div>` : ""}</div>${detail}</div></main><footer>好友助手 · 让联系建立在双方同意之上</footer></body></html>`;
  }
  app.get("/", (c) =>
    c.html(
      page(
        c.get("actor"),
        Number(c.req.query("network")) || null,
        "",
        (c.req.query("q") || "").slice(0, 80),
        Number(c.req.query("page_no")) || 1,
      ),
    ),
  );
  app.post("*", async (c) => {
    const u = c.get("actor");
    let network: number | null = null;
    try {
      if (
        !c.req
          .header("content-type")
          ?.startsWith("application/x-www-form-urlencoded")
      )
        throw new UserError("不支持的表单格式");
      if (Number(c.req.header("content-length") || 0) > 10000)
        throw new UserError("表单过大");
      const raw = await c.req.text();
      if (raw.length > 10000) throw new UserError("表单过大");
      const data = Object.fromEntries(new URLSearchParams(raw));
      if (!equal(data.csrf || "", csrf(u)))
        throw new UserError("页面验证已失效，请刷新后重试");
      network = Number(data.network_id) || null;
      const message = s.tx(() => {
        const path = c.req.path;
        let match;
        if (path === "/action")
          return admin.action(u, network, data.action || "", data);
        if ((match = path.match(/^\/network\/(\d+)\/code$/))) {
          network = Number(match[1]);
          return admin.action(u, network, "rotate", { code: data.code });
        }
        if ((match = path.match(/^\/network\/(\d+)$/))) {
          network = Number(match[1]);
          admin.require(u, network);
          const n = s.one("SELECT * FROM networks WHERE id=?", network);
          if (!n) throw new UserError("网络不存在");
          if (data.name !== n.name)
            admin.action(u, network, "rename", { name: data.name });
          if (data.status !== n.status)
            admin.action(
              u,
              network,
              data.status === "active" ? "enable" : "disable",
              { confirmed: "yes" },
            );
          admin.action(
            u,
            network,
            data.join_enabled === "on" ? "join_on" : "join_off",
            {},
          );
          return "网络设置已保存。";
        }
        if ((match = path.match(/^\/member\/(\d+)$/))) {
          const m = s.one(
            "SELECT * FROM memberships WHERE id=? AND network_id=?",
            Number(match[1]),
            network,
          );
          if (!m) throw new UserError("成员不存在");
          admin.require(u, network);
          if (data.role && data.role !== m.role)
            admin.action(
              u,
              network,
              data.role === "network_admin" ? "appoint" : "revoke",
              { member_code: m.member_code },
            );
          if (data.status === "removed")
            admin.action(u, network, "remove", {
              member_code: m.member_code,
              confirmed: "yes",
            });
          return "成员设置已保存。";
        }
        if ((match = path.match(/^\/report\/(\d+)$/))) {
          const r = s.one(
            "SELECT public_id FROM reports WHERE id=? AND network_id=?",
            Number(match[1]),
            network,
          );
          if (!r) throw new UserError("举报不存在");
          return admin.action(u, network, "resolve_report", {
            report_id: r.public_id,
            resolution: data.resolution,
          });
        }
        throw new UserError("未知操作");
      });
      changed();
      return c.html(page(u, network, message));
    } catch (e) {
      if (e instanceof UserError)
        return c.html(page(u, network, e.message), 400);
      console.error("Management operation failed");
      return c.text("操作未完成，请刷新查看状态", 500);
    }
  });
  return app;
}
