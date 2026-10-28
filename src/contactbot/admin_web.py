from __future__ import annotations

import base64
import hashlib
import hmac
import html
import os
from contextvars import ContextVar
from datetime import UTC, datetime
from importlib.resources import files
from urllib.parse import parse_qs, urlencode

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response

from .admin_actions import execute, password_matches
from .config import Settings
from .database import Database
from .service import BotService, UserError


def create_admin_app(settings: Settings, token: str | None = None) -> FastAPI:
    admin_token = token or os.environ.get("CONTACTBOT_ADMIN_WEB_TOKEN", "")
    if len(admin_token) < 32:
        raise RuntimeError("CONTACTBOT_ADMIN_WEB_TOKEN 至少需要 32 个字符")
    database = Database(settings.database)
    database.initialize()
    database.connection.execute('CREATE TABLE IF NOT EXISTS web_admin_logins (user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,password_hash TEXT NOT NULL)')
    database.connection.commit()
    current_actor = ContextVar('web_admin_actor', default=None)
    service = BotService(database, settings.secret)
    csrf = hmac.new(admin_token.encode(), b"contactbot-admin-csrf", hashlib.sha256).hexdigest()
    app = FastAPI(title="联系人网络本地管理后台", docs_url=None, redoc_url=None)

    def identify(request: Request):
        value = request.headers.get("authorization", "")
        if not value.startswith("Basic "):
            return None
        try:
            decoded = base64.b64decode(value[6:], validate=True).decode("utf-8")
            username, password = decoded.split(":", 1)
        except (ValueError, UnicodeDecodeError):
            return None
        with database.transaction() as conn:
            if hmac.compare_digest(username, 'admin') and hmac.compare_digest(password, admin_token):
                configured = os.environ.get('CONTACTBOT_ADMIN_WEB_USER_ID')
                return conn.execute('SELECT * FROM users WHERE is_system_admin=1 AND merged_into IS NULL AND (? IS NULL OR id=(SELECT coalesce(merged_into,id) FROM users WHERE id=?)) ORDER BY id LIMIT 1', (configured, configured)).fetchone()
            if username.startswith('user-') and username[5:].isdigit():
                row = conn.execute('SELECT u.*,w.password_hash FROM web_admin_logins w JOIN users u ON u.id=w.user_id WHERE u.id=?', (int(username[5:]),)).fetchone()
                if row and password_matches(password, row['password_hash']):
                    allowed = row['is_system_admin'] or conn.execute("SELECT 1 FROM memberships WHERE user_id=? AND role='network_admin' AND status='active'", (row['id'],)).fetchone()
                    return row if allowed else None
        return None

    def authorized(request: Request) -> bool:
        return current_actor.get() is not None

    def challenge() -> PlainTextResponse:
        return PlainTextResponse(
            "需要管理员身份验证",
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="ContactBot Local Admin"'},
        )

    async def form_data(request: Request) -> dict[str, str]:
        content_type = request.headers.get("content-type", "")
        if not content_type.startswith("application/x-www-form-urlencoded"):
            raise UserError("不支持的表单格式")
        values = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
        supplied_csrf = values.get("csrf", [""])[0]
        if not hmac.compare_digest(supplied_csrf, csrf):
            raise UserError("页面验证已失效，请刷新后重试")
        return {key: items[0] for key, items in values.items()}

    def now() -> str:
        return datetime.now(UTC).isoformat(timespec="seconds")

    def admin_actor(conn):
        row = current_actor.get()
        return row['id'] if row else None

    def audit(conn, network_id, action, target, details=""):
        actor_id = admin_actor(conn)
        if actor_id is not None:
            conn.execute(
                """INSERT INTO audit_log(
                       network_id,actor_user_id,action,target_ref,details,created_at
                   ) VALUES(?,?,?,?,?,?)""",
                (network_id, actor_id, f"web:{action}", target, details[:300], now()),
            )

    def redirect(network_id: int | None = None, message: str = ""):
        query = {}
        if network_id is not None:
            query["network"] = str(network_id)
        if message:
            query["message"] = message
        suffix = f"?{urlencode(query)}" if query else ""
        panel = 'members' if message.startswith('成员') else 'reports' if message.startswith('举报') else 'settings'
        return RedirectResponse(f"/{suffix}#{panel}", status_code=303)

    def page(network_id: int | None, message: str, q: str = "", page_no: int = 1) -> str:
        actor = current_actor.get()
        system = bool(actor and actor['is_system_admin'])
        page_no = max(1, page_no)
        with database.transaction() as conn:
            networks = conn.execute(
                """SELECT n.*,
                          (SELECT count(*) FROM memberships m
                           WHERE m.network_id=n.id AND m.status='active') member_count
                   FROM networks n WHERE ?=1 OR EXISTS(SELECT 1 FROM memberships m WHERE m.network_id=n.id AND m.user_id=? AND m.role='network_admin' AND m.status='active') ORDER BY n.id""", (int(system), actor['id'] if actor else -1)
            ).fetchall()
            selected = next((row for row in networks if row["id"] == network_id), None)
            if selected is None and networks:
                selected = networks[0]
            members = []
            banned_members = []
            request_count = 0
            report_count = 0
            reports = []
            requests = []
            deliveries = []
            logs = []
            total_members = 0
            if selected:
                pattern = '%' + q[:80].replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
                total_members = conn.execute(
                    "SELECT count(*) FROM memberships WHERE network_id=? AND status='active' AND (nickname LIKE ? ESCAPE '\\' OR member_code LIKE ? ESCAPE '\\')",
                    (selected['id'], pattern, pattern),
                ).fetchone()[0]
                page_no = min(page_no, max(1, (total_members + 19) // 20))
                members = conn.execute(
                    """SELECT m.*,u.is_system_admin,EXISTS(SELECT 1 FROM bans b WHERE b.network_id=m.network_id AND b.actor_key=u.actor_key) banned FROM memberships m
                       JOIN users u ON u.id=m.user_id
                       WHERE m.network_id=? AND m.status='active' AND (m.nickname LIKE ? ESCAPE '\\' OR m.member_code LIKE ? ESCAPE '\\')
                       ORDER BY m.status,m.nickname,m.id LIMIT 20 OFFSET ?""",
                    (selected["id"], pattern, pattern, (page_no - 1) * 20),
                ).fetchall()
                banned_members = conn.execute("""SELECT m.member_code,m.nickname FROM bans b JOIN users u ON u.actor_key=b.actor_key
                    JOIN memberships m ON m.user_id=u.id AND m.network_id=b.network_id WHERE b.network_id=? ORDER BY b.created_at DESC LIMIT 150""", (selected['id'],)).fetchall()
                request_count = conn.execute(
                    "SELECT count(*) FROM contact_requests WHERE network_id=?",
                    (selected["id"],),
                ).fetchone()[0]
                report_count = conn.execute(
                    "SELECT count(*) FROM reports WHERE network_id=? AND status='open'",
                    (selected["id"],),
                ).fetchone()[0]
                reports = conn.execute(
                    "SELECT id,public_id,reason,status,resolution,created_at FROM reports WHERE network_id=? ORDER BY status,created_at DESC LIMIT 30",
                    (selected['id'],),
                ).fetchall()
                requests = conn.execute(
                    "SELECT public_id,status,created_at,expires_at FROM contact_requests WHERE network_id=? ORDER BY id DESC LIMIT 20",
                    (selected['id'],),
                ).fetchall()
                deliveries = conn.execute("""SELECT o.id,coalesce(r.public_id,'D' || o.id) public_id FROM outbox o LEFT JOIN contact_requests r ON o.dedupe_key LIKE 'exchange:' || r.public_id || ':%' OR o.dedupe_key LIKE 'reissue:' || r.public_id || ':%'
                    WHERE o.delivery_network_id=? AND o.status='sending' AND o.delivery_closed_at IS NULL ORDER BY o.id LIMIT 30""", (selected['id'],)).fetchall()
                logs = conn.execute(
                    "SELECT action,target_ref,created_at FROM audit_log WHERE network_id=? ORDER BY id DESC LIMIT 30",
                    (selected['id'],),
                ).fetchall()
                recoveries = conn.execute("SELECT ar.public_id,ar.status,ar.expires_at,m.member_code,m.nickname FROM account_recoveries ar JOIN memberships m ON m.id=ar.membership_id WHERE m.network_id=? ORDER BY ar.id DESC LIMIT 50", (selected['id'],)).fetchall()

        def option(value, label, current):
            chosen = " selected" if value == current else ""
            return f'<option value="{html.escape(value)}"{chosen}>{html.escape(label)}</option>'

        network_links = "".join(
            f'<a class="network {"selected" if selected and row["id"] == selected["id"] else ""}" href="/?network={row["id"]}">'
            f'<strong>{html.escape(row["name"])}</strong><span>{row["member_count"]} 位成员</span></a>'
            for row in networks
        ) or '<p class="muted">暂无网络</p>'

        def action_form(action, label, fields='', net_id=None):
            nid = net_id if net_id is not None else (selected['id'] if selected else '')
            confirmation = '<label class="confirm"><input type="checkbox" name="confirmed" value="yes" required>已核实并确认操作</label>' if action in {'ban','remove','disable','approve_recovery','reject_recovery','delivery_close'} else ''
            return f'<form method="post" action="/action"><input type="hidden" name="csrf" value="{csrf}"><input type="hidden" name="network_id" value="{nid}"><input type="hidden" name="action" value="{action}">{fields}{confirmation}<button>{label}</button></form>'

        def member_actions(member):
            code = f'<input type="hidden" name="member_code" value="{html.escape(member["member_code"])}">'
            if member['is_system_admin']:
                return ''
            if member['banned']:
                return '<span class="badge">已封禁</span>' + action_form('unban', '解除封禁', code)
            if member['role'] == 'network_admin':
                return (action_form('revoke','撤销管理员',code) + action_form('web_access','生成／重置网页账号',code)) if system else ''
            if member['status'] != 'active':
                return '<small>成员已离开，需自行重新加入</small>'
            return action_form('ban', '封禁成员', code + '<label>封禁原因<input name="reason" required maxlength="300" placeholder="填写原因"></label>')

        detail = '<section class="card"><p>请先通过机器人创建网络。</p></section>'
        if selected:
            ban_rows = ''.join('<p>' + html.escape(m['nickname']) + ' · ' + html.escape(m['member_code']) + '</p>' + action_form('unban', '解除封禁', '<input type="hidden" name="member_code" value="' + html.escape(m['member_code']) + '">') for m in banned_members)
            bans_panel = '<details><summary>封禁名单</summary>' + ban_rows + '</details>' if ban_rows else ''
            rows = "".join(
                f"""<tr><td><strong>{html.escape(member['nickname'])}</strong><br>
                <small>{html.escape(member['member_code'])}</small></td><td>
                <form method="post" action="/member/{member['id']}">
                <input type="hidden" name="csrf" value="{csrf}">
                <input type="hidden" name="network_id" value="{selected['id']}">
                {f'<input type="hidden" name="role" value="{member["role"]}">' if not system else ''}
                <select name="role" aria-label="成员角色" {'disabled' if member['is_system_admin'] or not system else ''}>
                  {option('member', '普通成员', member['role'])}
                  {option('network_admin', '网络管理员', member['role'])}
                </select>
                <select name="status" aria-label="成员状态" {'disabled' if member['is_system_admin'] else ''}>
                  {option('active', '正常', member['status'])}
                  {option('removed', '已移除', member['status'])}
                  {'<option value="left" selected disabled>已退出（选择状态后保存）</option>' if member['status'] == 'left' else ''}
                </select>
                <span class="privacy">目录：{'公开' if member['discoverable'] else '隐藏'} · 申请：{'接收' if member['accepts_requests'] else '暂停'}<small>由成员本人设置</small></span>
                <span>{'系统管理员（只读）' if member['is_system_admin'] else ''}</span>
                <button {'disabled' if member['is_system_admin'] else ''}>保存</button>
                </form><details class="member-actions"><summary>更多管理操作</summary>{member_actions(member)}</details></td></tr>"""
                for member in members
            ) or '<tr><td colspan="2" class="muted">暂无成员</td></tr>'
            detail = f"""
            <section class="card" id="settings">
              <h2>{html.escape(selected['name'])}</h2>
              <p class="muted">{html.escape(selected['public_id'])} · {selected['member_count']} 位成员 ·
              {request_count} 条申请 · {report_count} 条待处理举报</p>
              <form method="post" action="/network/{selected['id']}">
                <input type="hidden" name="csrf" value="{csrf}">
                <label>网络名称<input name="name" value="{html.escape(selected['name'])}" required maxlength="60" {'readonly' if not system else ''}></label>
                <label><input type="checkbox" name="join_enabled" {'checked' if selected['join_enabled'] else ''}>允许新成员加入</label>
                {f'<input type="hidden" name="status" value="{selected["status"]}">' if not system else ''}
                <input type="hidden" name="status" value="{selected['status']}">
                <label>网络状态<select disabled>
                  {option('active', '正常', selected['status'])}
                  {option('disabled', '停用', selected['status'])}
                </select></label>
                <button>保存网络设置</button>
              </form>
              <div class="network-tools">{action_form('view_code','查看当前加入码')}{action_form('rotate','生成随机新加入码')}{action_form('join_off' if selected['join_enabled'] else 'join_on','停用加入' if selected['join_enabled'] else '恢复加入')}</div>
              <form method="post" action="/network/{selected['id']}/code" class="code-form">
                <input type="hidden" name="csrf" value="{csrf}">
                <label>新的加入码<input name="code" type="password" required maxlength="200" autocomplete="new-password" placeholder="请使用较长且难以猜测的编码"></label>
                <p class="muted">更换后旧码立即失效，现有成员不受影响。加入码不会在页面回显。</p>
                <button class="danger">立即更换加入码</button>
              </form>
            </section>
            <section class="card" id="members"><h2>成员管理 <small>匹配 {total_members} 位</small></h2>
            <form method="get" action="/"><input type="hidden" name="network" value="{selected['id']}">
            <label>搜索昵称或成员编号<input name="q" value="{html.escape(q)}" maxlength="80" placeholder="输入昵称或成员编号"></label><button>搜索</button></form>
            <div class="table-wrap"><table><thead><tr><th>成员</th><th>角色与状态</th></tr></thead><tbody>{rows}</tbody></table></div>{bans_panel}
            <nav class="pagination" aria-label="成员分页">
            <a href="/?{urlencode({'network':selected['id'],'q':q,'page_no':max(1,page_no-1)})}#members">上一页</a>
            <span>第 {page_no} / {max(1,(total_members+19)//20)} 页 · 每页 20 人</span>
            <a href="/?{urlencode({'network':selected['id'],'q':q,'page_no':min(max(1,(total_members+19)//20),page_no+1)})}#members">下一页</a></nav></section>"""

            status_labels = {'pending_target':'等待同意','processing':'交换中','completed':'已完成','rejected':'已拒绝','withdrawn':'已撤销','expired':'已过期','invalid':'已终止'}
            request_rows = ''.join(f"<tr><td>{html.escape(r['public_id'])}</td><td>{status_labels.get(r['status'], html.escape(r['status']))}</td><td>{html.escape(r['created_at'][:16])}</td></tr>" for r in requests)
            detail += f'<section class="card" id="requests"><h2>联系申请</h2><p class="muted">最近 20 条 · 仅显示状态，不展示联系方式及申请理由。</p><div class="table-wrap"><table><thead><tr><th>编号</th><th>状态</th><th>创建时间（UTC）</th></tr></thead><tbody>{request_rows or "<tr><td colspan=3>暂无申请</td></tr>"}</tbody></table></div></section>'
            if deliveries:
                delivery_forms = ''.join('<h3>' + html.escape(d['public_id']) + '</h3>' + action_form('delivery_close', '结束交换，不重发', '<input type="hidden" name="outbox_id" value="' + str(d['id']) + '">') for d in deliveries)
                detail += '<section class="card"><h2>待核查名片交付</h2><p>系统会自动核对 Core 已发送记录，不会重复发送。请双方检查聊天中的名片后再结束交换；确实未收到时可重新发起。</p>' + delivery_forms + '</section>'
            report_rows = ''
            for r in reports:
                resolve = f'<form method="post" action="/report/{r["id"]}"><input type="hidden" name="csrf" value="{csrf}"><input type="hidden" name="network_id" value="{selected["id"]}"><label>处理说明<input name="resolution" required maxlength="300"></label><button>标记已处理</button></form>' if r['status'] == 'open' else f'<p>处理结果：{html.escape(r["resolution"])}</p>'
                report_rows += f'<article class="report"><strong>{html.escape(r["public_id"])}</strong><span class="badge">{"待处理" if r["status"] == "open" else "已处理"}</span><p>{html.escape(r["reason"])}</p>{resolve}</article>'
            detail += f'<section class="card" id="reports"><h2>举报处理 <small>{report_count} 条待处理</small></h2><p class="muted">最近 30 条举报，仅供授权管理员处理。</p>{report_rows or "<p>暂无举报</p>"}</section>'
            log_rows = ''.join(f'<tr><td>{html.escape(r["created_at"][:16])}</td><td>{html.escape(r["action"])}</td><td>{html.escape(r["target_ref"])}</td></tr>' for r in logs)
            detail += f'<section class="card" id="audit"><h2>操作记录</h2><p class="muted">本网络最近 30 次操作 · 时间为 UTC</p><div class="table-wrap"><table><thead><tr><th>时间</th><th>操作</th><th>对象</th></tr></thead><tbody>{log_rows or "<tr><td colspan=3>暂无记录</td></tr>"}</tbody></table></div></section>'

            recovery_rows = ''
            for recovery in recoveries:
                public_id = html.escape(recovery['public_id'])
                fields = f'<input type="hidden" name="recovery_id" value="{public_id}">'
                pending = recovery['status'] == 'pending' and recovery['expires_at'] > now()
                state = '已过期' if recovery['status'] == 'pending' and not pending else {'pending':'待审核','approved':'已批准','rejected':'已拒绝','expired':'已过期'}.get(recovery['status'], recovery['status'])
                buttons = action_form('approve_recovery','批准恢复',fields) + action_form('reject_recovery','拒绝恢复',fields) if pending else ''
                recovery_rows += f'<article class="report"><strong>{public_id}</strong><span class="badge">{html.escape(state)}</span><p>{html.escape(recovery["nickname"])} · {html.escape(recovery["member_code"])}<br>到期时间（UTC）：{html.escape(recovery["expires_at"])}</p>{buttons}</article>'
            detail += f'<section class="card" id="recoveries"><h2>账号恢复审核</h2><p class="muted">批准前请通过可信渠道核实新旧账号属于同一成员。批准后资料迁移及通知由机器人处理。</p>{recovery_rows or "<p>暂无恢复申请</p>"}</section>'
        if system:
            detail += '<section class="card" id="system"><h2>系统管理</h2><p class="muted">左侧可查看和切换全部网络。创建、改名、管理员任免和网络停用均与机器人同步。</p>'
            detail += action_form('create', '创建网络', '<label>网络名称<input name="name" required maxlength="60"></label><label>初始加入码（留空随机生成）<input name="code" type="password" autocomplete="new-password"></label>', net_id='')
            if selected:
                detail += action_form('disable' if selected['status']=='active' else 'enable', '停用当前网络' if selected['status']=='active' else '恢复当前网络')
            detail += '</section>'

        notice = f'<div class="notice">{html.escape(message)}</div>' if message else ""
        extra_nav = '<a href="#recoveries"><span aria-hidden="true">06</span>恢复审核</a>' + ('<a href="#system"><span aria-hidden="true">07</span>系统管理</a>' if system else '')
        summary = ''
        if selected:
            summary = f'''<div class="workspace-heading"><div><p class="eyebrow">联系网络 / 管理工作台</p><h2>{html.escape(selected['name'])}</h2><p class="muted">管理成员与联系申请，让每一次连接都有明确的同意。</p></div><span class="network-state">{'正常运行' if selected['status'] == 'active' else '已停用'}</span></div><div class="metrics"><article><span>活跃成员</span><strong>{selected['member_count']}</strong><small>当前网络</small></article><article><span>联系申请</span><strong>{request_count}</strong><small>累计申请</small></article><article><span>待处理举报</span><strong>{report_count}</strong><small>需要关注</small></article></div>'''
        return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
        <meta name="viewport" content="width=device-width,initial-scale=1">
        <title>好友助手 · 网络管理</title><link rel="stylesheet" href="/admin.css"><script src="/admin.js" defer></script><style>
        *{{box-sizing:border-box}}body{{margin:0;background:#f4f6f8;color:#18212b;font:15px system-ui,sans-serif}}
        header{{padding:20px 28px;background:#172235;color:white}}main{{display:grid;grid-template-columns:240px 1fr;gap:20px;max-width:1200px;margin:24px auto;padding:0 20px}}
        aside,.card{{background:white;border:1px solid #dce2e8;border-radius:12px;padding:18px}}.network{{display:flex;flex-direction:column;padding:10px;border-radius:8px;color:#18212b;text-decoration:none}}.network:hover{{background:#eef4ff}}.network span,.muted,small{{color:#687482}}h1,h2{{margin-top:0}}form{{display:flex;gap:12px;align-items:end;flex-wrap:wrap;margin:14px 0}}label{{display:flex;gap:7px;flex-direction:column}}input[type=text],select{{min-height:38px;padding:8px;border:1px solid #b9c4cf;border-radius:7px}}input[name=name]{{min-width:280px}}input[name=code]{{min-width:360px}}button{{min-height:38px;padding:8px 14px;border:0;border-radius:7px;background:#1769e0;color:white;cursor:pointer}}button.danger{{background:#b42318}}button:disabled{{opacity:.45}}table{{width:100%;border-collapse:collapse}}td{{padding:12px 8px;border-top:1px solid #e6eaee;vertical-align:top}}.notice{{max-width:1160px;margin:18px auto 0;padding:12px 18px;background:#e7f5ec;border-radius:8px;color:#17653a}}.code-form{{margin-top:24px;padding-top:20px;border-top:1px solid #e6eaee}}@media(max-width:760px){{main{{grid-template-columns:1fr}}input[name=name],input[name=code]{{min-width:100%;width:100%}}}}
        </style></head><body><header><a class="brand" href="/" aria-label="好友助手首页"><span class="brand-mark" aria-hidden="true">友</span><h1>好友助手</h1></a><span class="header-caption">网络管理工作台</span><span class="session"><i aria-hidden="true"></i>管理员会话</span></header>
        <main><aside><h2>我的网络</h2>{network_links}<hr><h2>管理工具</h2><nav class="sections" aria-label="后台导航"><a href="#settings"><span aria-hidden="true">01</span>网络设置</a><a href="#members"><span aria-hidden="true">02</span>成员管理</a><a href="#requests"><span aria-hidden="true">03</span>联系申请</a><a href="#reports"><span aria-hidden="true">04</span>举报处理</a><a href="#audit"><span aria-hidden="true">05</span>操作记录</a>{extra_nav}</nav><p class="privacy-note">{'系统管理员 · 全部网络' if system else '网络管理员 · 授权网络'}<br>成员联系方式始终隐藏。<br>隐私选项由成员本人决定。</p></aside><div class="workspace">{summary}<div role="status">{notice}</div>{detail}</div></main><footer>好友助手 · 让联系建立在双方同意之上</footer></body></html>"""

    @app.middleware("http")
    async def protect_response(request: Request, call_next):
        current_actor.set(identify(request))
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Content-Security-Policy'] = "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'"
        return response

    @app.get('/admin.css')
    def admin_css():
        return Response(files('contactbot').joinpath('admin.css').read_text(encoding='utf-8'), media_type='text/css')

    @app.get('/favicon.ico')
    def favicon():
        return Response(status_code=204)

    @app.get('/admin.js')
    def admin_js():
        return Response(files('contactbot').joinpath('admin.js').read_text(encoding='utf-8'), media_type='text/javascript')

    @app.get("/health", response_class=PlainTextResponse)
    def health():
        return "ok"

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request, network: int | None = None, message: str = "", q: str = "", page_no: int = 1):
        if not authorized(request):
            return challenge()
        return HTMLResponse(page(network, message, q[:80], page_no))

    @app.post("/network/{network_id}")
    async def update_network(request: Request, network_id: int):
        if not authorized(request):
            return challenge()
        try:
            data = await form_data(request)
            name = data.get("name", "").strip()
            status = data.get("status", "")
            if not 1 <= len(name) <= 60 or status not in {"active", "disabled"}:
                raise UserError("网络设置无效")
            timestamp = now()
            with database.transaction() as conn:
                actor = current_actor.get()
                service._assert_admin_for(conn, actor, network_id)
                network = conn.execute(
                    "SELECT * FROM networks WHERE id=?", (network_id,)
                ).fetchone()
                if not network:
                    raise UserError("网络不存在")
                if not actor['is_system_admin'] and (name != network['name'] or status != network['status']):
                    raise UserError('仅系统管理员可修改名称或网络状态')
                join_enabled = int(data.get("join_enabled") == "on")
                if name != network['name']:
                    execute(service, conn, actor, network_id, 'rename', {'name':name}, timestamp)
                if status != network['status']:
                    execute(service, conn, actor, network_id, 'disable' if status=='disabled' else 'enable', data, timestamp)
                if join_enabled != network['join_enabled']:
                    execute(service, conn, actor, network_id, 'join_on' if join_enabled else 'join_off', {}, timestamp)
            return redirect(network_id, "网络设置已保存")
        except (UserError, UnicodeDecodeError) as exc:
            return HTMLResponse(page(network_id, str(exc)), status_code=400)

    @app.post("/network/{network_id}/code")
    async def update_join_code(request: Request, network_id: int):
        if not authorized(request):
            return challenge()
        try:
            data = await form_data(request)
            code = data.get("code", "")
            if not code:
                raise UserError("加入码不能为空")
            timestamp = now()
            with database.transaction() as conn:
                service._assert_admin_for(conn, current_actor.get(), network_id)
                network = conn.execute(
                    "SELECT * FROM networks WHERE id=?", (network_id,)
                ).fetchone()
                if not network:
                    raise UserError("网络不存在")
                duplicate = service._find_network_by_code(conn, code)
                if duplicate and duplicate["id"] != network_id:
                    raise UserError("该加入码已被其他网络使用")
                conn.execute(
                    """UPDATE networks SET join_code_hash=?,join_code_ciphertext=?,
                       join_code_hint=?,updated_at=? WHERE id=?""",
                    (
                        service._join_hash(code),
                        service._encrypt_join_code(code),
                        code[-4:],
                        timestamp,
                        network_id,
                    ),
                )
                audit(conn, network_id, "rotate_join_code", network["public_id"])
            return redirect(network_id, "加入码已更换，旧加入码已失效")
        except (UserError, UnicodeDecodeError) as exc:
            return HTMLResponse(page(network_id, str(exc)), status_code=400)

    @app.post("/member/{membership_id}")
    async def update_member(request: Request, membership_id: int):
        if not authorized(request):
            return challenge()
        network_id = None
        try:
            data = await form_data(request)
            network_id = int(data.get("network_id", "0"))
            role = data.get("role", "")
            status = data.get("status", "")
            if role not in {"member", "network_admin"} or status not in {
                "active",
                "removed",
            }:
                raise UserError("成员设置无效")
            timestamp = now()
            with database.transaction() as conn:
                actor = current_actor.get()
                service._assert_admin_for(conn, actor, network_id)
                member = conn.execute(
                    """SELECT m.*,u.is_system_admin FROM memberships m
                       JOIN users u ON u.id=m.user_id WHERE m.id=? AND m.network_id=?""",
                    (membership_id, network_id),
                ).fetchone()
                if not member:
                    raise UserError("成员不存在")
                if member["is_system_admin"]:
                    raise UserError("不能在网页后台修改系统管理员")
                if not actor['is_system_admin'] and role != member['role']:
                    raise UserError('仅系统管理员可以任命或撤销管理员')
                if status == "removed":
                    execute(service, conn, actor, network_id, 'remove', {'member_code': member['member_code'], 'confirmed':'yes'}, timestamp)
                else:
                    if member['status'] != 'active':
                        raise UserError('移除或退出的成员应自行重新加入，不能直接恢复资料')
                    conn.execute(
                        """UPDATE memberships SET role=?,status='active',
                           updated_at=? WHERE id=?""",
                        (
                            role,
                            timestamp,
                            membership_id,
                        ),
                    )
                audit(conn, network_id, "update_member", member["member_code"], status)
            return redirect(network_id, "成员设置已保存")
        except (UserError, UnicodeDecodeError, ValueError) as exc:
            return HTMLResponse(page(network_id, str(exc)), status_code=400)

    @app.post('/report/{report_id}')
    async def resolve_report(request: Request, report_id: int):
        if not authorized(request):
            return challenge()
        network_id = None
        try:
            data = await form_data(request)
            network_id = int(data.get('network_id', '0'))
            resolution = data.get('resolution', '').strip()
            if not 1 <= len(resolution) <= 300:
                raise UserError('请填写 1–300 字处理说明')
            with database.transaction() as conn:
                service._assert_admin_for(conn, current_actor.get(), network_id)
                report = conn.execute('SELECT public_id,status FROM reports WHERE id=? AND network_id=?', (report_id, network_id)).fetchone()
                if not report or report['status'] != 'open':
                    raise UserError('举报不存在或已处理，请刷新页面')
                execute(service, conn, current_actor.get(), network_id, 'resolve_report', {'report_id':report['public_id'], 'resolution':resolution}, now())
            return redirect(network_id, '举报已处理，结果已保存')
        except (UserError, ValueError, UnicodeDecodeError) as exc:
            return HTMLResponse(page(network_id, str(exc)), status_code=400)

    @app.post('/action')
    async def management_action(request: Request):
        if not authorized(request):
            return challenge()
        network_id = None
        try:
            data = await form_data(request)
            network_id = int(data.get('network_id') or 0) or None
            action = data.get('action', '')
            with database.transaction() as conn:
                result = execute(service, conn, current_actor.get(), network_id, action, data, now())
            # Sensitive results are rendered only in this POST response, never in a URL.
            panel = 'recoveries' if 'recovery' in action else 'members' if action in {'ban','remove','unban','appoint','revoke','web_access'} else 'settings'
            response = HTMLResponse(page(network_id, result).replace('<body>', f'<body data-panel="{panel}">'))
            response.headers['X-Admin-Panel'] = panel
            return response
        except (UserError, ValueError, UnicodeDecodeError) as exc:
            return HTMLResponse(page(network_id, str(exc)), status_code=400)

    return app


def run_admin_web(settings: Settings, host: str, port: int) -> None:
    if host not in {"127.0.0.1", "::1", "localhost", "0.0.0.0"}:
        raise RuntimeError("管理后台只能绑定本机或容器接口")
    uvicorn.run(create_admin_app(settings), host=host, port=port, log_level="info")
