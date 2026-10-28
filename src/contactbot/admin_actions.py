"""Web adapters for the existing bot administration rules.

All calls run in the caller's transaction. Recipient notifications stay in the
shared outbox; the acting administrator's command reply is returned to the web.
"""
import hashlib
import hmac
import secrets

from .service import UserError


def password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    return salt + ':' + hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 200_000).hex()


def password_matches(password, stored):
    return hmac.compare_digest(password_hash(password, stored.split(':')[0]), stored)


def execute(service, conn, actor, network_id, action, data, timestamp, *, verified_system=False):
    if action == 'ban':
        raise UserError('封禁功能已停用')
    key = 'web:' + secrets.token_hex(16)
    user = conn.execute('SELECT * FROM users WHERE id=?', (actor['id'],)).fetchone()
    if verified_system:
        user = {**dict(user), 'is_system_admin': 1}
    original_network = user['current_network_id']
    pending_actions = conn.execute('SELECT * FROM pending_actions WHERE user_id=?', (user['id'],)).fetchall()
    network = None
    if network_id:
        service._assert_admin_for(conn, user, network_id)
        network = conn.execute('SELECT * FROM networks WHERE id=?', (network_id,)).fetchone()
        if not network:
            raise UserError('网络不存在')
    conn.execute('UPDATE users SET current_network_id=? WHERE id=?', (network_id, user['id']))
    try:
        if action == 'delivery_close':
            if data.get('confirmed') != 'yes' or not str(data.get('outbox_id', '')).isdigit():
                raise UserError('请先确认双方已检查聊天中的名片')
            return service.close_uncertain_delivery(conn, user, network_id, int(data['outbox_id']), timestamp)
        if action == 'create':
            name = data.get('name', '').strip()
            if not 1 <= len(name) <= 60:
                raise UserError('网络名称需要 1–60 字')
            service._create_network_with_code(conn, user, name, data.get('code') or service._new_join_code(), timestamp, key)
        elif action in {'remove', 'ban'}:
            if data.get('confirmed') != 'yes':
                raise UserError('请勾选确认操作')
            target = service._admin_target(conn, user, network, data.get('member_code', ''))
            reason = data.get('reason', '').strip()
            if action == 'ban' and not 1 <= len(reason) <= 300:
                raise UserError('封禁必须填写 1–300 字原因')
            service._admin_membership_action(conn, user, {'network_id':network_id, 'membership_id':target['id'], 'reason':reason}, timestamp, key, ban=action=='ban')
        elif action == 'disable':
            if data.get('confirmed') != 'yes':
                raise UserError('请勾选确认停用网络')
            service._confirm_disable_network(conn, user, {'network_id':network_id}, timestamp, key)
        elif action == 'rotate':
            code = data.get('code', '') or service._new_join_code()
            duplicate = service._find_network_by_code(conn, code)
            if duplicate and duplicate['id'] != network_id:
                raise UserError('该加入码已被其他网络使用')
            service._confirm_rotate_code(conn, user, {'network_id':network_id, 'code':code}, timestamp, key)
        elif action in {'approve_recovery', 'reject_recovery'}:
            if data.get('confirmed') != 'yes':
                raise UserError('请先核实身份并勾选确认')
            recovery = conn.execute('SELECT ar.public_id FROM account_recoveries ar JOIN memberships m ON m.id=ar.membership_id WHERE ar.public_id=? AND m.network_id=?', (data.get('recovery_id',''),network_id)).fetchone()
            if not recovery:
                raise UserError('恢复申请不属于当前网络')
            service._resolve_account_recovery(conn, user, recovery['public_id'], action=='approve_recovery', timestamp, key)
        elif action == 'web_access':
            service._require_system_admin(user)
            target = conn.execute("SELECT m.user_id FROM memberships m WHERE network_id=? AND member_code=? AND status='active' AND role='network_admin'", (network_id, data.get('member_code',''))).fetchone()
            if not target:
                raise UserError('请先任命该成员为网络管理员')
            password = secrets.token_urlsafe(32)
            conn.execute('INSERT OR REPLACE INTO web_admin_logins(user_id,password_hash) VALUES(?,?)', (target['user_id'], password_hash(password)))
            service._audit(conn, network_id, user['id'], 'web:issue_login', data['member_code'], '', timestamp)
            return f"网页账号：user-{target['user_id']}\n密码（仅显示本次）：{password}\n权限随该成员的管理员角色自动生效或失效。"
        else:
            commands = {'view_code':('查看加入码',''), 'join_off':('停用加入',''), 'join_on':('恢复加入',''), 'enable':('恢复网络',network['public_id'] if network else ''), 'unban':('解除封禁',data.get('member_code','')), 'appoint':('任命管理员',f"{network['public_id']} {data.get('member_code','')}"), 'revoke':('撤销管理员',f"{network['public_id']} {data.get('member_code','')}"), 'rename':('修改网络名称',f"{network['public_id']} {data.get('name','').strip()}"), 'resolve_report':('处理举报',f"{data.get('report_id','')} {data.get('resolution','').strip()}")}
            if action not in commands:
                raise UserError('未知管理操作')
            command, args = commands[action]
            service._admin_command(conn, user, command, args, True, timestamp, key)
            if action == 'view_code':
                service._audit(conn, network_id, user['id'], 'web:view_join_code', network['public_id'], '', timestamp)
        replies = conn.execute('SELECT body FROM outbox WHERE recipient_user_id=? AND dedupe_key=?', (user['id'],f'{key}:{user["id"]}')).fetchall()
        conn.execute('DELETE FROM outbox WHERE recipient_user_id=? AND dedupe_key=?', (user['id'],f'{key}:{user["id"]}'))
        return '\n'.join(r['body'] for r in replies) or '操作已完成'
    finally:
        # Web network selection never changes the administrator's chat context.
        conn.execute('UPDATE users SET current_network_id=? WHERE id=?', (original_network,user['id']))
        if action == 'create':
            for pending in pending_actions:
                conn.execute('INSERT OR IGNORE INTO pending_actions(token,user_id,action,payload,expires_at,created_at) VALUES(?,?,?,?,?,?)', tuple(pending))
