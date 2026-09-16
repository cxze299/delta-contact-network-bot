from __future__ import annotations


def create_network(app):
    sequence = app.conn.execute("SELECT count(*) FROM networks").fetchone()[0] + 1
    code = f"管理员自定义!码-{sequence}"
    app.send("admin", "创建网络 测试网络", admin=True)
    output = app.send("admin", code, admin=True)
    body = output[-1][1]
    network_id = body.split("（", 1)[1].split("）", 1)[0]
    assert code in body
    return network_id, code


def join(app, actor, join_code, nickname):
    pending = app.send(actor, f"加入 {join_code} {nickname}")
    return app.confirm_from(actor, pending)


def test_complete_consent_flow(app):
    _, code = create_network(app)
    join(app, "alice", code, "小艾")
    join(app, "bob", code, "小波")
    app.send("alice", "公开资料")
    app.send("bob", "公开资料")
    app.send("alice", "接收申请")
    app.send("bob", "接收申请")
    app.send("alice", "设置联系方式 alice@example.org")
    app.send("bob", "设置联系方式 bob@example.org")

    target_code = app.conn.execute(
        "SELECT member_code FROM memberships m JOIN users u ON u.id=m.user_id WHERE u.actor_key='bob'"
    ).fetchone()[0]
    pending = app.send("alice", f"申请 {target_code} 我们在同一个项目组")
    sent = app.confirm_from("alice", pending)
    request_id = next(body for who, body in sent if who == "alice").split()[1]
    acceptance = app.send("bob", f"同意申请 {request_id}")
    exchanged = app.confirm_from("bob", acceptance)

    assert any(who == "alice" and "bob@example.org" in body for who, body in exchanged)
    assert any(who == "bob" and "alice@example.org" in body for who, body in exchanged)
    assert app.conn.execute("SELECT status FROM contact_requests").fetchone()[0] == "completed"


def test_contact_change_invalidates_confirmation(app):
    _, code = create_network(app)
    join(app, "alice", code, "甲")
    join(app, "bob", code, "乙")
    app.send("bob", "接收申请")
    app.send("alice", "设置联系方式 old@example.org")
    target = app.conn.execute(
        "SELECT member_code FROM memberships m JOIN users u ON u.id=m.user_id WHERE u.actor_key='bob'"
    ).fetchone()[0]
    pending = app.send("alice", f"申请 {target} 你好")
    app.send("alice", "设置联系方式 new@example.org")
    output = app.confirm_from("alice", pending)
    assert "联系方式已修改" in output[-1][1]
    assert app.conn.execute("SELECT count(*) FROM contact_requests").fetchone()[0] == 0


def test_network_admin_cannot_manage_other_network(app):
    n1, code1 = create_network(app)
    n2, code2 = create_network(app)
    join(app, "manager", code1, "管理员")
    join(app, "member", code2, "成员")
    manager_code = app.conn.execute(
        "SELECT member_code FROM memberships m JOIN users u ON u.id=m.user_id WHERE u.actor_key='manager'"
    ).fetchone()[0]
    app.send("admin", f"任命管理员 {n1} {manager_code}", admin=True)
    output = app.send("manager", f"停用网络 {n2}")
    assert "仅系统管理员" in output[-1][1]


def test_duplicate_incoming_message_is_idempotent(app):
    app.send("admin", "创建网络 唯一网络", admin=True)
    before = app.conn.execute("SELECT count(*) FROM networks").fetchone()[0]
    app.next_message_id -= 1
    output = app.send("admin", "创建网络 重复消息", admin=True)
    assert output == []
    assert app.conn.execute("SELECT count(*) FROM networks").fetchone()[0] == before


def test_block_prevents_request(app):
    _, code = create_network(app)
    join(app, "alice", code, "甲")
    join(app, "bob", code, "乙")
    app.send("bob", "接收申请")
    app.send("alice", "设置联系方式 alice@example.org")
    alice_code = app.conn.execute(
        "SELECT member_code FROM memberships m JOIN users u ON u.id=m.user_id WHERE u.actor_key='alice'"
    ).fetchone()[0]
    bob_code = app.conn.execute(
        "SELECT member_code FROM memberships m JOIN users u ON u.id=m.user_id WHERE u.actor_key='bob'"
    ).fetchone()[0]
    app.send("bob", f"屏蔽 {alice_code}")
    output = app.send("alice", f"申请 {bob_code} 你好")
    assert "当前无法" in output[-1][1]


def test_delete_removes_referenced_business_data(app):
    _, code = create_network(app)
    join(app, "alice", code, "甲")
    join(app, "bob", code, "乙")
    bob_code = app.conn.execute(
        "SELECT member_code FROM memberships m JOIN users u ON u.id=m.user_id WHERE u.actor_key='bob'"
    ).fetchone()[0]
    app.send("alice", f"举报 {bob_code} 测试原因")
    pending = app.send("alice", "删除我的数据")
    output = app.confirm_from("alice", pending)
    assert "已删除" in output[-1][1]
    assert app.conn.execute("SELECT count(*) FROM reports").fetchone()[0] == 0
    assert (
        app.conn.execute(
            "SELECT count(*) FROM memberships m JOIN users u ON u.id=m.user_id WHERE u.actor_key='alice'"
        ).fetchone()[0]
        == 0
    )


def test_sensitive_commands_require_encrypted_private_chat(app):
    _, code = create_network(app)
    join(app, "alice", code, "甲")
    output = app.send("alice", "设置联系方式 alice@example.org", encrypted=False)
    assert "加密私聊" in output[-1][1]
    assert (
        app.conn.execute(
            "SELECT share_contact FROM memberships m JOIN users u ON u.id=m.user_id WHERE u.actor_key='alice'"
        ).fetchone()[0]
        is None
    )


def test_join_confirmation_invalid_after_code_rotation(app):
    _, code = create_network(app)
    pending_join = app.send("alice", f"加入 {code} 甲")
    app.send("admin", "更换加入码", admin=True)
    app.send("admin", "全新 自定义 加入码 ✅", admin=True)
    app.send("admin", "确认更换", admin=True)
    output = app.confirm_from("alice", pending_join)
    assert "加入码已更换" in output[-1][1]


def test_search_isolated_between_networks(app):
    n1, code1 = create_network(app)
    _, code2 = create_network(app)
    join(app, "alice", code1, "同名成员")
    join(app, "bob", code2, "同名成员")
    app.send("bob", "公开资料")
    app.send("alice", f"切换网络 {n1}")
    output = app.send("alice", "查找 同名成员")
    assert "本人" in output[-1][1]
    assert output[-1][1].count("同名成员") == 1


def test_banned_account_cannot_rejoin(app):
    _, code = create_network(app)
    join(app, "member", code, "成员")
    member_code = app.conn.execute(
        "SELECT member_code FROM memberships m JOIN users u ON u.id=m.user_id WHERE u.actor_key='member'"
    ).fetchone()[0]
    pending = app.send("admin", f"封禁成员 {member_code} 违规", admin=True)
    app.confirm_from("admin", pending)
    output = app.send("member", f"加入 {code} 成员")
    assert "不能加入" in output[-1][1]


def test_join_code_only_visible_in_encrypted_admin_chat(app):
    _, code = create_network(app)
    output = app.send("admin", "查看加入码", admin=True)
    assert code in output[-1][1]
    output = app.send("admin", "查看加入码", admin=True, encrypted=False)
    assert "加密私聊" in output[-1][1]


def test_departed_network_admin_does_not_regain_role_on_rejoin(app):
    network_id, code = create_network(app)
    join(app, "manager1", code, "管理一")
    join(app, "manager2", code, "管理二")
    rows = app.conn.execute(
        """SELECT u.actor_key,m.member_code FROM memberships m
           JOIN users u ON u.id=m.user_id WHERE u.actor_key IN ('manager1','manager2')"""
    ).fetchall()
    codes = {row["actor_key"]: row["member_code"] for row in rows}
    app.send("admin", f"任命管理员 {network_id} {codes['manager1']}", admin=True)
    app.send("admin", f"任命管理员 {network_id} {codes['manager2']}", admin=True)
    pending = app.send("manager1", "退出网络")
    assert "确认-请输入：确认 " in pending[-1][1]
    assert "退出-请输入q。" in pending[-1][1]
    app.confirm_from("manager1", pending)
    join(app, "manager1", code, "管理一")
    role = app.conn.execute(
        """SELECT m.role FROM memberships m JOIN users u ON u.id=m.user_id
           WHERE u.actor_key='manager1'"""
    ).fetchone()[0]
    assert role == "member"


def test_guided_flow_needs_no_member_commands(app):
    app.send("admin", "创建网络 引导网络", admin=True)
    code = "管理员 自定义✨加入码"
    app.send("admin", code, admin=True)

    for actor, nickname in (("alice", "小艾"), ("bob", "小波")):
        welcome = app.send(actor, "")
        assert "一步步" in welcome[-1][1]
        app.send(actor, code)
        app.send(actor, nickname)

    app.send("alice", "查找好友")
    app.send("alice", "小波")
    sent = app.send("alice", "1")
    assert any(who == "alice" and "名片已发送" in body for who, body in sent)
    assert any(who == "bob" and "联系人名片" in body for who, body in sent)
    row = app.conn.execute(
        "SELECT request_kind,status FROM contact_requests"
    ).fetchone()
    assert tuple(row) == ("vcard", "completed")


def test_vcard_request_respects_target_pause(app):
    _, code = create_network(app)
    join(app, "alice", code, "小艾")
    join(app, "bob", code, "小波")
    app.send("bob", "暂停申请")
    app.send("alice", "")
    app.send("alice", "查找好友")
    app.send("alice", "小波")
    output = app.send("alice", "1")
    assert "暂停接收" in output[-1][1]


def test_unrelated_text_outside_guidance_opens_menu(app):
    _, code = create_network(app)
    join(app, "alice", code, "小艾")
    output = app.send("alice", "这是一句无关的话")
    assert "查找好友" in output[-1][1]
    assert "输入编号" not in output[-1][1]


def test_profile_submenu_edits_and_returns(app):
    _, code = create_network(app)
    join(app, "alice", code, "旧昵称")
    app.send("alice", "任意消息")
    opened = app.send("alice", "查看资料")
    assert any("资料操作" in body and "修改昵称" in body for _, body in opened)

    app.send("alice", "修改昵称")
    changed = app.send("alice", "新昵称")
    assert any("昵称：新昵称" in body for _, body in changed)
    assert any("资料操作" in body for _, body in changed)

    returned = app.send("alice", "返回菜单")
    assert "查找好友" in returned[-1][1]


def test_first_greeting_only_starts_join_guidance(app):
    output = app.send("new-member", "你好")
    assert len(output) == 1
    assert "好友助手" in output[0][1]
    assert "网络加入码" in output[0][1]
    assert "无效" not in output[0][1]
    pending = app.conn.execute(
        """SELECT action FROM pending_actions p JOIN users u ON u.id=p.user_id
           WHERE u.actor_key='new-member'"""
    ).fetchone()
    assert pending["action"] == "guide_join_code"


def test_member_can_find_self_even_when_profile_is_hidden(app):
    _, code = create_network(app)
    join(app, "alice", code, "只对自己可见")

    command_result = app.send("alice", "查找 只对自己可见")
    assert "本人" in command_result[-1][1]

    app.send("alice", "")
    app.send("alice", "查找好友")
    guided_result = app.send("alice", "只对自己可见")
    assert "本人" in guided_result[-1][1]
    selected = app.send("alice", "1")
    assert any("成员编号" in body for _, body in selected)


def test_q_exits_each_guided_step(app):
    _, code = create_network(app)
    app.send("alice", "")
    exited = app.send("alice", "q")
    assert "已退出当前步骤" in exited[-1][1]
    assert app.conn.execute(
        """SELECT COUNT(*) FROM pending_actions p JOIN users u ON u.id=p.user_id
           WHERE u.actor_key='alice'"""
    ).fetchone()[0] == 0

    app.send("alice", "")
    app.send("alice", code)
    exited = app.send("alice", "q")
    assert "已退出当前步骤" in exited[-1][1]


def test_greeting_reopens_member_menu_without_error(app):
    _, code = create_network(app)
    join(app, "alice", code, "小艾")
    app.send("alice", "")
    output = app.send("alice", "您好")
    assert "查找好友" in output[-1][1]
    assert "无法完成" not in output[-1][1]


def test_invalid_old_search_selection_returns_to_menu(app):
    _, code = create_network(app)
    join(app, "alice", code, "小艾")
    join(app, "bob", code, "小波")
    app.send("alice", "任意消息")
    app.send("alice", "查找好友")
    app.send("alice", "小波")

    output = app.send("alice", "旧用户随便发送的内容")
    assert "查找好友" in output[-1][1]
    assert "查看资料" in output[-1][1]
    assert "无法完成" not in output[-1][1]


def test_member_can_join_second_network_from_guide(app):
    first_id, first_code = create_network(app)
    second_id, second_code = create_network(app)
    join(app, "alice", first_code, "小艾")

    menu = app.send("alice", "任意消息")
    assert "发送“加入新网络”加入新网络" in menu[-1][1]
    app.send("alice", "加入新网络")
    app.send("alice", second_code)
    completed = app.send("alice", "小艾二号")

    memberships = app.conn.execute(
        """SELECT n.public_id,m.nickname FROM memberships m
           JOIN networks n ON n.id=m.network_id JOIN users u ON u.id=m.user_id
           WHERE u.actor_key='alice' AND m.status='active' ORDER BY n.public_id"""
    ).fetchall()
    assert {row["public_id"] for row in memberships} == {first_id, second_id}
    assert app.conn.execute(
        """SELECT n.public_id FROM users u JOIN networks n ON n.id=u.current_network_id
           WHERE u.actor_key='alice'"""
    ).fetchone()[0] == second_id
    assert any("已加入" in body for _, body in completed)
    assert any("查找好友" in body for _, body in completed)


def test_help_is_only_shown_for_help_command(app):
    _, code = create_network(app)
    join(app, "admin", code, "管理员", )

    menu = app.send("admin", "普通消息", admin=True)
    assert "发送“加入新网络”加入新网络" in menu[-1][1]
    assert "常用指令" not in menu[-1][1]

    help_output = app.send("admin", "帮助", admin=True)
    assert "【联系网络】" in help_output[-1][1]
    assert "【屏蔽、举报与数据】" in help_output[-1][1]
    assert "切换网络" in help_output[-1][1]


def test_admin_approved_account_recovery_moves_identity_and_resends_history(app):
    _, code = create_network(app)
    join(app, "old-alice", code, "小艾")
    join(app, "bob", code, "小波")

    app.send("old-alice", "任意消息")
    app.send("old-alice", "查找好友")
    app.send("old-alice", "小波")
    app.send("old-alice", "1")
    old_membership = app.conn.execute(
        """SELECT m.id,m.member_code,m.nickname FROM memberships m
           JOIN users u ON u.id=m.user_id WHERE u.actor_key='old-alice'"""
    ).fetchone()
    assert app.conn.execute(
        "SELECT status FROM contact_requests WHERE requester_membership_id=?",
        (old_membership["id"],),
    ).fetchone()[0] == "completed"

    app.send("new-alice", "恢复账号")
    submitted = app.send("new-alice", old_membership["member_code"])
    assert any(who == "new-alice" and "已提交" in body for who, body in submitted)
    recovery_id = app.conn.execute(
        "SELECT public_id FROM account_recoveries WHERE status='pending'"
    ).fetchone()[0]

    denied = app.send("bob", f"批准恢复 {recovery_id}")
    assert "没有管理该网络的权限" in denied[-1][1]
    approved = app.send("admin", f"批准恢复 {recovery_id}", admin=True)
    assert any(who == "new-alice" and "账号恢复成功" in body for who, body in approved)
    assert any(
        row["actor_key"] == "new-alice"
        and row["vcard_contact_id"] == sum(b"bob")
        and row["requires_encryption"] == 1
        for row in app.last_outbox
    )

    restored = app.conn.execute(
        """SELECT m.member_code,m.nickname,u.actor_key FROM memberships m
           JOIN users u ON u.id=m.user_id WHERE m.id=?""",
        (old_membership["id"],),
    ).fetchone()
    assert tuple(restored) == (old_membership["member_code"], "小艾", "new-alice")
    assert app.conn.execute(
        "SELECT status FROM account_recoveries WHERE public_id=?", (recovery_id,)
    ).fetchone()[0] == "approved"
    assert app.conn.execute(
        "SELECT status FROM contact_requests WHERE requester_membership_id=?",
        (old_membership["id"],),
    ).fetchone()[0] == "completed"


def test_account_recovery_does_not_resend_blocked_history(app):
    _, code = create_network(app)
    join(app, "old-alice", code, "小艾")
    join(app, "bob", code, "小波")
    app.send("old-alice", "任意消息")
    app.send("old-alice", "查找好友")
    app.send("old-alice", "小波")
    app.send("old-alice", "1")
    rows = app.conn.execute(
        """SELECT u.actor_key,m.id,m.member_code FROM memberships m
           JOIN users u ON u.id=m.user_id"""
    ).fetchall()
    members = {row["actor_key"]: row for row in rows}
    app.send("bob", f"屏蔽 {members['old-alice']['member_code']}")

    app.send("new-alice", "恢复账号")
    app.send("new-alice", members["old-alice"]["member_code"])
    recovery_id = app.conn.execute(
        "SELECT public_id FROM account_recoveries WHERE status='pending'"
    ).fetchone()[0]
    approved = app.send("admin", f"批准恢复 {recovery_id}", admin=True)
    assert any(who == "new-alice" and "重新发送 0 位" in body for who, body in approved)
    assert not any(
        row["actor_key"] == "new-alice" and row["vcard_contact_id"] is not None
        for row in app.last_outbox
    )


def test_account_recovery_can_be_rejected_and_expires(app):
    _, code = create_network(app)
    join(app, "old-alice", code, "小艾")
    member_code = app.conn.execute(
        """SELECT m.member_code FROM memberships m JOIN users u ON u.id=m.user_id
           WHERE u.actor_key='old-alice'"""
    ).fetchone()[0]
    app.send("new-alice", "恢复账号")
    app.send("new-alice", member_code)
    recovery_id = app.conn.execute(
        "SELECT public_id FROM account_recoveries WHERE status='pending'"
    ).fetchone()[0]
    rejected = app.send("admin", f"拒绝恢复 {recovery_id}", admin=True)
    assert any(who == "new-alice" and "未通过" in body for who, body in rejected)
    assert app.conn.execute(
        "SELECT status FROM account_recoveries WHERE public_id=?", (recovery_id,)
    ).fetchone()[0] == "rejected"

    app.send("another-account", "恢复账号")
    app.send("another-account", member_code)
    expiring_id = app.conn.execute(
        "SELECT public_id FROM account_recoveries WHERE status='pending'"
    ).fetchone()[0]
    app.conn.execute(
        "UPDATE account_recoveries SET expires_at='2000-01-01T00:00:00+00:00' WHERE public_id=?",
        (expiring_id,),
    )
    app.conn.commit()
    listed = app.send("admin", "恢复申请列表", admin=True)
    assert "没有待审核" in listed[-1][1]
    assert app.conn.execute(
        "SELECT status FROM account_recoveries WHERE public_id=?", (expiring_id,)
    ).fetchone()[0] == "expired"


def test_stale_current_network_is_repaired_automatically(app):
    first_id, first_code = create_network(app)
    join(app, "admin", first_code, "管理员")
    second_id, _ = create_network(app)
    assert first_id != second_id

    current_before = app.conn.execute(
        "SELECT n.public_id FROM users u JOIN networks n ON n.id=u.current_network_id "
        "WHERE u.actor_key='admin'"
    ).fetchone()[0]
    assert current_before == second_id

    result = app.send("admin", "查找 管理员", admin=True)
    assert "当前网络成员资格已失效" not in result[-1][1]
    assert "本人" in result[-1][1]
    current_after = app.conn.execute(
        "SELECT n.public_id FROM users u JOIN networks n ON n.id=u.current_network_id "
        "WHERE u.actor_key='admin'"
    ).fetchone()[0]
    assert current_after == first_id


def test_switch_network_without_id_selects_only_active_membership(app):
    first_id, first_code = create_network(app)
    join(app, "alice", first_code, "小艾")
    app.conn.execute(
        "UPDATE users SET current_network_id=NULL WHERE actor_key='alice'"
    )
    app.conn.commit()

    listed = app.send("alice", "我的网络")
    assert f"* {first_id}" in listed[-1][1]
    app.conn.execute(
        "UPDATE users SET current_network_id=NULL WHERE actor_key='alice'"
    )
    app.conn.commit()

    switched = app.send("alice", "切换网络")
    assert "当前网络已切换" in switched[-1][1]
    current = app.conn.execute(
        "SELECT n.public_id FROM users u JOIN networks n ON n.id=u.current_network_id "
        "WHERE u.actor_key='alice'"
    ).fetchone()[0]
    assert current == first_id
