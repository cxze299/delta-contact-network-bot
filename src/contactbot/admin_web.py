from __future__ import annotations

import base64
import hashlib
import hmac
import html
import os
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlencode

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

from .config import Settings
from .database import Database
from .service import BotService, UserError


def create_admin_app(settings: Settings, token: str | None = None) -> FastAPI:
    admin_token = token or os.environ.get("CONTACTBOT_ADMIN_WEB_TOKEN", "")
    if len(admin_token) < 32:
        raise RuntimeError("CONTACTBOT_ADMIN_WEB_TOKEN 至少需要 32 个字符")
    database = Database(settings.database)
    database.initialize()
    service = BotService(database, settings.secret)
    csrf = hmac.new(admin_token.encode(), b"contactbot-admin-csrf", hashlib.sha256).hexdigest()
    app = FastAPI(title="联系人网络本地管理后台", docs_url=None, redoc_url=None)

    def authorized(request: Request) -> bool:
        value = request.headers.get("authorization", "")
        if not value.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(value[6:], validate=True).decode("utf-8")
            username, password = decoded.split(":", 1)
        except (ValueError, UnicodeDecodeError):
            return False
        return hmac.compare_digest(username, "admin") and hmac.compare_digest(
            password, admin_token
        )

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
        row = conn.execute(
            "SELECT id FROM users WHERE is_system_admin=1 ORDER BY id LIMIT 1"
        ).fetchone()
        return row["id"] if row else None

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
        return RedirectResponse(f"/{suffix}", status_code=303)

    def page(network_id: int | None, message: str) -> str:
        with database.transaction() as conn:
            networks = conn.execute(
                """SELECT n.*,
                          (SELECT count(*) FROM memberships m
                           WHERE m.network_id=n.id AND m.status='active') member_count
                   FROM networks n ORDER BY n.id"""
            ).fetchall()
            selected = next((row for row in networks if row["id"] == network_id), None)
            if selected is None and networks:
                selected = networks[0]
            members = []
            request_count = 0
            report_count = 0
            if selected:
                members = conn.execute(
                    """SELECT m.*,u.is_system_admin FROM memberships m
                       JOIN users u ON u.id=m.user_id
                       WHERE m.network_id=? ORDER BY m.status,m.nickname LIMIT 300""",
                    (selected["id"],),
                ).fetchall()
                request_count = conn.execute(
                    "SELECT count(*) FROM contact_requests WHERE network_id=?",
                    (selected["id"],),
                ).fetchone()[0]
                report_count = conn.execute(
                    "SELECT count(*) FROM reports WHERE network_id=? AND status='open'",
                    (selected["id"],),
                ).fetchone()[0]

        def option(value, label, current):
            chosen = " selected" if value == current else ""
            return f'<option value="{html.escape(value)}"{chosen}>{html.escape(label)}</option>'

        network_links = "".join(
            f'<a class="network" href="/?network={row["id"]}">'
            f'<strong>{html.escape(row["name"])}</strong><span>{row["member_count"]} 位成员</span></a>'
            for row in networks
        ) or '<p class="muted">暂无网络</p>'

        detail = '<section class="card"><p>请先通过机器人创建网络。</p></section>'
        if selected:
            rows = "".join(
                f"""<tr><td><strong>{html.escape(member['nickname'])}</strong><br>
                <small>{html.escape(member['member_code'])}</small></td><td>
                <form method="post" action="/member/{member['id']}">
                <input type="hidden" name="csrf" value="{csrf}">
                <input type="hidden" name="network_id" value="{selected['id']}">
                <select name="role" {'disabled' if member['is_system_admin'] else ''}>
                  {option('member', '普通成员', member['role'])}
                  {option('network_admin', '网络管理员', member['role'])}
                </select>
                <select name="status" {'disabled' if member['is_system_admin'] else ''}>
                  {option('active', '正常', member['status'])}
                  {option('removed', '已移除', member['status'])}
                </select>
                <label><input type="checkbox" name="discoverable" {'checked' if member['discoverable'] else ''}>目录可见</label>
                <label><input type="checkbox" name="accepts_requests" {'checked' if member['accepts_requests'] else ''}>接收申请</label>
                <button {'disabled' if member['is_system_admin'] else ''}>保存</button>
                </form></td></tr>"""
                for member in members
            ) or '<tr><td colspan="2" class="muted">暂无成员</td></tr>'
            detail = f"""
            <section class="card">
              <h2>{html.escape(selected['name'])}</h2>
              <p class="muted">{html.escape(selected['public_id'])} · {selected['member_count']} 位成员 ·
              {request_count} 条申请 · {report_count} 条待处理举报</p>
              <form method="post" action="/network/{selected['id']}">
                <input type="hidden" name="csrf" value="{csrf}">
                <label>网络名称<input name="name" value="{html.escape(selected['name'])}" required maxlength="60"></label>
                <label><input type="checkbox" name="join_enabled" {'checked' if selected['join_enabled'] else ''}>允许新成员加入</label>
                <label>网络状态<select name="status">
                  {option('active', '正常', selected['status'])}
                  {option('disabled', '停用', selected['status'])}
                </select></label>
                <button>保存网络设置</button>
              </form>
              <form method="post" action="/network/{selected['id']}/code" class="code-form">
                <input type="hidden" name="csrf" value="{csrf}">
                <label>设置新的加入码<input name="code" type="text" required autocomplete="off" placeholder="允许中文、空格、符号和表情"></label>
                <button class="danger">立即更换加入码</button>
              </form>
            </section>
            <section class="card"><h2>成员</h2><table><tbody>{rows}</tbody></table></section>"""

        notice = f'<div class="notice">{html.escape(message)}</div>' if message else ""
        return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
        <meta name="viewport" content="width=device-width,initial-scale=1">
        <title>联系人网络后台</title><style>
        *{{box-sizing:border-box}}body{{margin:0;background:#f4f6f8;color:#18212b;font:15px system-ui,sans-serif}}
        header{{padding:20px 28px;background:#172235;color:white}}main{{display:grid;grid-template-columns:240px 1fr;gap:20px;max-width:1200px;margin:24px auto;padding:0 20px}}
        aside,.card{{background:white;border:1px solid #dce2e8;border-radius:12px;padding:18px}}.network{{display:flex;flex-direction:column;padding:10px;border-radius:8px;color:#18212b;text-decoration:none}}.network:hover{{background:#eef4ff}}.network span,.muted,small{{color:#687482}}h1,h2{{margin-top:0}}form{{display:flex;gap:12px;align-items:end;flex-wrap:wrap;margin:14px 0}}label{{display:flex;gap:7px;flex-direction:column}}input[type=text],select{{min-height:38px;padding:8px;border:1px solid #b9c4cf;border-radius:7px}}input[name=name]{{min-width:280px}}input[name=code]{{min-width:360px}}button{{min-height:38px;padding:8px 14px;border:0;border-radius:7px;background:#1769e0;color:white;cursor:pointer}}button.danger{{background:#b42318}}button:disabled{{opacity:.45}}table{{width:100%;border-collapse:collapse}}td{{padding:12px 8px;border-top:1px solid #e6eaee;vertical-align:top}}.notice{{max-width:1160px;margin:18px auto 0;padding:12px 18px;background:#e7f5ec;border-radius:8px;color:#17653a}}.code-form{{margin-top:24px;padding-top:20px;border-top:1px solid #e6eaee}}@media(max-width:760px){{main{{grid-template-columns:1fr}}input[name=name],input[name=code]{{min-width:100%;width:100%}}}}
        </style></head><body><header><h1>联系人网络本地管理后台</h1></header>{notice}
        <main><aside><h2>网络</h2>{network_links}</aside><div>{detail}</div></main></body></html>"""

    @app.get("/health", response_class=PlainTextResponse)
    def health():
        return "ok"

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request, network: int | None = None, message: str = ""):
        if not authorized(request):
            return challenge()
        return HTMLResponse(page(network, message))

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
                network = conn.execute(
                    "SELECT * FROM networks WHERE id=?", (network_id,)
                ).fetchone()
                if not network:
                    raise UserError("网络不存在")
                join_enabled = int(data.get("join_enabled") == "on")
                conn.execute(
                    """UPDATE networks SET name=?,join_enabled=?,status=?,updated_at=?
                       WHERE id=?""",
                    (name, join_enabled, status, timestamp, network_id),
                )
                if status == "disabled" and network["status"] != "disabled":
                    conn.execute(
                        """UPDATE contact_requests SET status='invalid',updated_at=?
                           WHERE network_id=? AND status IN ('pending_target','processing')""",
                        (timestamp, network_id),
                    )
                audit(conn, network_id, "update_network", network["public_id"], status)
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
                member = conn.execute(
                    """SELECT m.*,u.is_system_admin FROM memberships m
                       JOIN users u ON u.id=m.user_id WHERE m.id=? AND m.network_id=?""",
                    (membership_id, network_id),
                ).fetchone()
                if not member:
                    raise UserError("成员不存在")
                if member["is_system_admin"]:
                    raise UserError("不能在网页后台修改系统管理员")
                if status == "removed":
                    service._invalidate_membership(conn, membership_id, "removed", timestamp)
                else:
                    conn.execute(
                        """UPDATE memberships SET role=?,status='active',discoverable=?,
                           accepts_requests=?,updated_at=? WHERE id=?""",
                        (
                            role,
                            int(data.get("discoverable") == "on"),
                            int(data.get("accepts_requests") == "on"),
                            timestamp,
                            membership_id,
                        ),
                    )
                audit(conn, network_id, "update_member", member["member_code"], status)
            return redirect(network_id, "成员设置已保存")
        except (UserError, UnicodeDecodeError, ValueError) as exc:
            return HTMLResponse(page(network_id, str(exc)), status_code=400)

    return app


def run_admin_web(settings: Settings, host: str, port: int) -> None:
    if host not in {"127.0.0.1", "::1", "localhost", "0.0.0.0"}:
        raise RuntimeError("管理后台只能绑定本机或容器接口")
    uvicorn.run(create_admin_app(settings), host=host, port=port, log_level="info")
