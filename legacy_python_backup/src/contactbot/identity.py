"""Transactional identity reconciliation using evidence supplied by Delta Core."""
from __future__ import annotations

import base64
import hashlib
import re


def public_key_fingerprint(encoded: str) -> str:
    """Fingerprint the primary OpenPGP public-key packet, not mutable signatures."""
    raw = base64.b64decode(encoded, validate=True)
    if not raw or not raw[0] & 0x80:
        raise ValueError("Invalid public key")
    header, pos = raw[0], 1
    if header & 0x40:
        tag = header & 63
        first = raw[pos]; pos += 1
        if first < 192:
            size = first
        elif first < 224:
            size = ((first - 192) << 8) + raw[pos] + 192; pos += 1
        elif first == 255:
            size = int.from_bytes(raw[pos:pos + 4], "big"); pos += 4
        else:
            raise ValueError("Partial key packet")
    else:
        tag, kind = (header >> 2) & 15, header & 3
        if kind == 3:
            raise ValueError("Indeterminate key packet")
        length = (1, 2, 4)[kind]
        size = int.from_bytes(raw[pos:pos + length], "big"); pos += length
    body = raw[pos:pos + size]
    if tag != 6 or len(body) != size or not body:
        raise ValueError("Missing primary public key")
    if body[0] == 4 and size <= 65535:
        return hashlib.sha1(b"\x99" + size.to_bytes(2, "big") + body).hexdigest()
    if body[0] in (5, 6):
        prefix = b"\x9a" if body[0] == 5 else b"\x9b"
        return hashlib.sha256(prefix + size.to_bytes(4, "big") + body).hexdigest()
    raise ValueError("Unsupported key version")


def _exists(conn, table):
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


def _invalidate(conn, mids, now):
    for mid in mids:
        for row in conn.execute("SELECT public_id FROM contact_requests WHERE status IN ('pending_target','processing') AND (requester_membership_id=? OR target_membership_id=?)", (mid, mid)).fetchall():
            conn.execute("UPDATE contact_requests SET status='invalid',updated_at=? WHERE public_id=?", (now, row[0]))
            for prefix in ("exchange", "request-notify", "vcard", "reissue", "vcard-request-notify"):
                conn.execute("DELETE FROM outbox WHERE status='pending' AND dedupe_key LIKE ?", (f"{prefix}:{row[0]}:%",))


def _merge_membership(conn, service, keeper, left, right, address, now):
    stable, duplicate = sorted((left, right), key=lambda m: m["id"])
    _invalidate(conn, (stable["id"], duplicate["id"]), now)
    # Recovery approvals referencing either identity must be reviewed again.
    conn.execute("UPDATE account_recoveries SET status='expired',resolved_at=? WHERE membership_id IN (?,?) AND status='pending'", (now, stable["id"], duplicate["id"]))
    for table, columns in (("contact_requests", ("requester_membership_id", "target_membership_id")), ("reports", ("reporter_membership_id", "target_membership_id")), ("link_shares", ("sender_membership_id", "target_membership_id")), ("account_recoveries", ("membership_id",))):
        for column in columns:
            conn.execute(f"UPDATE {table} SET {column}=? WHERE {column}=?", (stable["id"], duplicate["id"]))
    blocks = conn.execute("SELECT * FROM blocks WHERE blocker_membership_id=? OR blocked_membership_id=?", (duplicate["id"], duplicate["id"])).fetchall()
    conn.execute("DELETE FROM blocks WHERE blocker_membership_id=? OR blocked_membership_id=?", (duplicate["id"], duplicate["id"]))
    for block in blocks:
        a = stable["id"] if block["blocker_membership_id"] == duplicate["id"] else block["blocker_membership_id"]
        b = stable["id"] if block["blocked_membership_id"] == duplicate["id"] else block["blocked_membership_id"]
        if a != b:
            conn.execute("INSERT OR IGNORE INTO blocks VALUES(?,?,?,?)", (block["network_id"], a, b, block["created_at"]))
    auto = _exists(conn, "xdc_auto_share") and conn.execute("SELECT count(*) FROM xdc_auto_share WHERE membership_id IN (?,?)", (stable["id"], duplicate["id"])).fetchone()[0] == 2
    if _exists(conn, "xdc_auto_share"):
        conn.execute("DELETE FROM xdc_auto_share WHERE membership_id IN (?,?)", (stable["id"], duplicate["id"]))
    latest = max((left, right), key=lambda m: (m["updated_at"], m["id"]))
    status = "removed" if "removed" in (left["status"], right["status"]) else "left" if "left" in (left["status"], right["status"]) else "active"
    share = address if status == "active" and any((m["share_contact"] or "").casefold() == address.casefold() for m in (left, right)) else None
    conn.execute("DELETE FROM memberships WHERE id=?", (duplicate["id"],))
    conn.execute("""UPDATE memberships SET user_id=?,nickname=?,bio=?,discoverable=?,accepts_requests=?,share_contact=?,share_version=share_version+1,role=?,status=?,updated_at=? WHERE id=?""", (keeper["id"], latest["nickname"], latest["bio"], min(left["discoverable"], right["discoverable"]), min(left["accepts_requests"], right["accepts_requests"]), share, "network_admin" if status == "active" and "network_admin" in (left["role"], right["role"]) else "member", status, now, stable["id"]))
    if auto and share:
        conn.execute("INSERT INTO xdc_auto_share VALUES(?)", (stable["id"],))
    service._audit(conn, stable["network_id"], keeper["id"], "merge_duplicate_identity", stable["member_code"], "merged duplicate " + duplicate["member_code"], now)


def _merge_user(conn, service, keeper, old, message, now):
    if keeper["id"] == old["id"]:
        return
    for member in conn.execute("SELECT * FROM memberships WHERE user_id=?", (old["id"],)).fetchall():
        other = conn.execute("SELECT * FROM memberships WHERE user_id=? AND network_id=?", (keeper["id"], member["network_id"])).fetchone()
        if other:
            _merge_membership(conn, service, keeper, member, other, message.contact_address, now)
        else:
            _invalidate(conn, (member["id"],), now)
            share = member["share_contact"] if (member["share_contact"] or "").casefold() == message.contact_address.casefold() else None
            conn.execute("UPDATE memberships SET user_id=?,share_contact=?,share_version=share_version+1,updated_at=? WHERE id=?", (keeper["id"], share, now, member["id"]))
            service._audit(conn, member["network_id"], keeper["id"], "merge_duplicate_identity", member["member_code"], "same verified identity", now)
    # Preserve restrictions across all historical address aliases.
    conn.execute("INSERT OR IGNORE INTO bans SELECT network_id,?,reason,created_by,created_at FROM bans WHERE actor_key=?", (message.actor_key, old["actor_key"]))
    conn.execute("UPDATE rate_events SET actor_key=? WHERE actor_key=?", (message.actor_key, old["actor_key"]))
    if _exists(conn, "xdc_instances"):
        conn.execute("UPDATE xdc_instances SET actor_key=? WHERE actor_key=? AND account_id=?", (message.actor_key, old["actor_key"], message.account_id))
    for table in ("xdc_admin_sessions", "xdc_admin_challenges"):
        if _exists(conn, table):
            conn.execute(f"DELETE FROM {table} WHERE user_id=?", (old["id"],))
    if _exists(conn, "xdc_admin_grants"):
        conn.execute("INSERT OR IGNORE INTO xdc_admin_grants SELECT ?,version,granted_at FROM xdc_admin_grants WHERE user_id=?", (keeper["id"], old["id"]))
        conn.execute("DELETE FROM xdc_admin_grants WHERE user_id=?", (old["id"],))
    if _exists(conn, "web_admin_logins"):
        conn.execute("DELETE FROM web_admin_logins WHERE user_id=?", (old["id"],))
    conn.execute("DELETE FROM pending_actions WHERE user_id IN (?,?)", (keeper["id"], old["id"]))
    conn.execute("DELETE FROM outbox WHERE recipient_user_id=? AND status='pending'", (old["id"],))
    conn.execute("INSERT OR IGNORE INTO identity_bindings SELECT account_id,contact_id,chat_id,fingerprint,?,updated_at FROM identity_bindings WHERE user_id=?", (keeper["id"], old["id"]))
    conn.execute("DELETE FROM identity_bindings WHERE user_id=?", (old["id"],))
    conn.execute("UPDATE users SET is_system_admin=max(is_system_admin,?),updated_at=? WHERE id=?", (old["is_system_admin"], now, keeper["id"]))
    conn.execute("UPDATE users SET merged_into=? WHERE merged_into=?", (keeper["id"], old["id"]))
    conn.execute("UPDATE users SET merged_into=?,contact_id=-id,chat_id=-id,current_network_id=NULL,is_system_admin=0,updated_at=? WHERE id=?", (keeper["id"], now, old["id"]))


def reconcile(conn, service, message, now):
    """Run before user upsert. Never accept identity evidence from Webxdc input."""
    fp = message.identity_fingerprint
    if not (message.is_private and message.is_encrypted and message.identity_verified and re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", fp)):
        return
    actor = conn.execute("SELECT * FROM users WHERE actor_key=? AND merged_into IS NULL", (message.actor_key,)).fetchone()
    candidates = {r["id"]: r for r in conn.execute("SELECT u.* FROM identity_bindings b JOIN users u ON u.id=b.user_id WHERE b.account_id=? AND b.fingerprint=? AND u.merged_into IS NULL", (message.account_id, fp))}
    # Authorized legacy bootstrap: same Core contact AND private chat, currently
    # verified, and no contradictory stored fingerprint. Never chat number alone.
    for old in conn.execute("SELECT * FROM users WHERE contact_id=? AND chat_id=? AND core_account_id IN (0,?) AND merged_into IS NULL", (message.contact_id, message.chat_id, message.account_id)):
        known = conn.execute("SELECT fingerprint FROM identity_bindings WHERE account_id=? AND user_id=?", (message.account_id, old["id"])).fetchall()
        if not known or any(r[0] == fp for r in known):
            candidates[old["id"]] = old
    if actor:
        if actor["core_account_id"] not in (0, message.account_id):
            return
        candidates[actor["id"]] = actor
    if not candidates:
        return
    keeper = actor or min(candidates.values(), key=lambda u: u["id"])
    if actor and len(candidates) == 1:
        return
    if keeper["actor_key"] != message.actor_key:
        # Retain the stable user ID and all attached memberships when only the
        # address-derived key changed. All aliases retain their bans.
        stale = conn.execute("SELECT id,merged_into FROM users WHERE actor_key=?", (message.actor_key,)).fetchone()
        if stale:
            if stale["merged_into"] != keeper["id"]:
                return
            conn.execute("UPDATE users SET actor_key=? WHERE id=?", ("merged:" + str(stale["id"]) + ":" + message.actor_key, stale["id"]))
        conn.execute("UPDATE users SET actor_key=? WHERE id=?", (message.actor_key, keeper["id"]))
        conn.execute("INSERT OR IGNORE INTO bans SELECT network_id,?,reason,created_by,created_at FROM bans WHERE actor_key=?", (message.actor_key, keeper["actor_key"]))
        conn.execute("UPDATE rate_events SET actor_key=? WHERE actor_key=?", (message.actor_key, keeper["actor_key"]))
        if _exists(conn, "xdc_instances"):
            conn.execute("UPDATE xdc_instances SET actor_key=? WHERE actor_key=? AND account_id=?", (message.actor_key, keeper["actor_key"], message.account_id))
        for m in conn.execute("SELECT * FROM memberships WHERE user_id=?", (keeper["id"],)).fetchall():
            _invalidate(conn, (m["id"],), now)
            if (m["share_contact"] or "").casefold() != message.contact_address.casefold():
                conn.execute("UPDATE memberships SET share_contact=NULL,share_version=share_version+1 WHERE id=?", (m["id"],))
            service._audit(conn, m["network_id"], keeper["id"], "merge_duplicate_identity", m["member_code"], "rebound verified identity", now)
        conn.execute("DELETE FROM pending_actions WHERE user_id=?", (keeper["id"],))
    for old in candidates.values():
        _merge_user(conn, service, keeper, old, message, now)
    # A removed/banned membership must never be reactivated by identity merging.
    for m in conn.execute("SELECT id,network_id FROM memberships WHERE user_id=? AND status='active'", (keeper["id"],)).fetchall():
        if conn.execute("SELECT 1 FROM bans WHERE network_id=? AND actor_key=?", (m["network_id"], message.actor_key)).fetchone():
            service._invalidate_membership(conn, m["id"], "removed", now)
    current = conn.execute("SELECT current_network_id FROM users WHERE id=?", (keeper["id"],)).fetchone()[0]
    if not conn.execute("SELECT 1 FROM memberships WHERE user_id=? AND network_id=? AND status='active'", (keeper["id"], current)).fetchone():
        replacement = conn.execute("SELECT network_id FROM memberships WHERE user_id=? AND status='active' ORDER BY id LIMIT 1", (keeper["id"],)).fetchone()
        conn.execute("UPDATE users SET current_network_id=? WHERE id=?", (replacement[0] if replacement else None, keeper["id"]))


def remember(conn, message, user_id, now):
    if message.identity_verified and re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", message.identity_fingerprint) and message.is_encrypted and message.is_private:
        conn.execute("INSERT INTO identity_bindings(account_id,contact_id,chat_id,fingerprint,user_id,updated_at) VALUES(?,?,?,?,?,?) ON CONFLICT(account_id,contact_id,chat_id,fingerprint,user_id) DO UPDATE SET updated_at=excluded.updated_at", (message.account_id, message.contact_id, message.chat_id, message.identity_fingerprint, user_id, now))
