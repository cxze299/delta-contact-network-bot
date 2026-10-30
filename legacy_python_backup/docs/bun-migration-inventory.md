# Bun 与实时通信迁移：现有功能清单

此清单根据当前源码生成；仅供迁移核对，不代表 TypeScript 迁移已完成。

## 业务与接入函数

### service.py

- `UserError`：
- `IncomingMessage`：
- `BotService`：`__init__`、`handle`、`pending_outbox`、`mark_outbox_sent`、`_mark_outbox_sent`、`_claim_delivery`、`send_native_vcard`、`send_approved_vcard`、`mark_outbox_failed`、`uncertain_deliveries`、`reconcile_delivery`、`close_uncertain_delivery`、`cleanup_retention`、`seed_network`、`_ensure_user`、`_dispatch`、`_start_account_recovery`、`_submit_account_recovery`、`_start_join`、`_begin_guidance`、`_guided_reply`、`_set_guided_contact`、`_guide_privacy_choice`、`_guide_next`、`_guide_profile`、`_confirm`、`_confirm_join`、`_my_networks`、`_switch_network`、`_my_profile`、`_edit_profile`、`_set_privacy`、`_search`、`_confirm_vcard_request`、`_accept_vcard_request`、`_share_link_with_target`、`_exchange_now`、`_start_request`、`_confirm_request`、`_list_requests`、`_start_accept`、`_confirm_accept`、`_reject`、`_withdraw`、`_block`、`_block_list`、`_report`、`_start_leave`、`_confirm_leave`、`_start_delete`、`_confirm_delete`、`_admin_help`、`_list_account_recoveries`、`_resolve_account_recovery`、`_admin_command`、`_confirm_rotate_code`、`_create_network_with_code`、`_confirm_remove_member`、`_confirm_ban_member`、`_admin_membership_action`、`_confirm_disable_network`、`_admin_network`、`_admin_target`、`_assert_admin_for`、`_require_system_admin`、`_network_by_public`、`_audit`、`_invalidate_membership`、`_current_membership`、`_is_blocked`、`_pending`、`_reply`、`_join_hash`、`_legacy_join_hash`、`_find_network_by_code`、`_encrypt_join_code`、`_decrypt_join_code`、`_encrypt_share_link`、`_decrypt_share_link`、`_validate_share_link`、`_new_join_code`、`_public_id`、`_member_code`、`_validate_nickname`、`_valid_contact`、`_rate_allowed`、`_expire`
- `_now`
- `_iso`
- `_parse`

### webxdc_bridge.py

- `package`
- `package_p2p`
- `WebxdcBridge`：`__init__`、`offer`、`handle`、`sync`、`deliver_outbox`、`clear_previous_summaries`、`sync_all`、`consume_updates`

### delta_adapter.py

- `DeltaAdapter`：`__init__`、`run_forever`、`_identity_message`、`_refresh_identity_bindings`、`_recover_recent_contacts`、`_on_webxdc_update`、`_poll_outbox`、`_on_message`、`_on_securejoin`、`_drain_outbox`、`_sync_webxdc`、`_drain_outbox_locked`、`_reconcile_deliveries`、`_send_approved_vcard`、`_send_outbox_item`
- `approved_vcard`

### xdc_admin.py

- `make_verifier`
- `parse_verifier`
- `XdcAdmin`：`__init__`、`has_access`、`require`、`challenge`、`open`、`prepare_attempt`、`unlock`、`select`、`action`、`snapshot`

### admin_web.py

- `create_admin_app`
- `run_admin_web`

### database.py

- `Database`：`__init__`、`initialize`、`transaction`、`close`

### delivery.py

- `DeliveryNotStarted`：
- `filename`

### identity.py

- `public_key_fingerprint`
- `_exists`
- `_invalidate`
- `_merge_membership`
- `_merge_user`
- `reconcile`
- `remember`

## 数据表

以下同时包含 schema.sql 和运行时迁移创建的表；迁移时必须保留现有列、索引和关联。

- `schema.sql`：`users`、`networks`、`memberships`、`bans`、`contact_requests`、`blocks`、`reports`、`audit_log`、`incoming_messages`、`pending_actions`、`outbox`、`rate_events`、`link_shares`、`account_recoveries`
- `webxdc_bridge.py`：`xdc_instances`、`xdc_requests`、`xdc_request_results`、`xdc_join_feedback`、`xdc_cursors`、`xdc_receipts`、`xdc_directory`、`xdc_auto_share`、`xdc_auto_exchanges`
- `xdc_admin.py`：`xdc_admin_sessions`、`xdc_admin_challenges`、`xdc_admin_context`、`xdc_admin_grants`
- `admin_web.py`：`web_admin_logins`
- `database.py`：`identity_bindings`

## 已核对的约束

- 现有 schema 已启用 WAL；现有 xdc_cursors 使用 account_id 和 message_id 作为联合主键，xdc_requests 以实例令牌和请求 ID 去重，迁移不得重置或替换这些记录。
- 当前数据库初始化还执行版本迁移和数据修正，不能只复制 schema.sql。
- 官方 @deltachat/jsonrpc-client 2.59.0 生成代码包含 sendWebxdcRealtimeData、sendWebxdcRealtimeAdvertisement、getWebxdcStatusUpdates；这仅确认 RPC 声明存在，不证明实时信道在当前 NAS 与手机上已打通。
- 必须验证实时数据的来源认证、断线和历史实例兼容；不能信任前端自报账号，也不能把实时发送成功当作持久化或业务完成。
- 双轨重放应复用同一实例和请求 ID、同一事务中的业务结果；状态增量需要版本和恢复快照，不能依赖无序浅合并。
- 三秒未确认时不可自动重发；保留迟到回执对账、跨网络隔离、旧版小程序及名片不重复发送的现有行为。
- SQLite WAL 和 busy_timeout 不能保证绝不出现锁冲突；涉及网络调用或异步等待的事务需要格外设计。
- Bun 类型检查不能证明业务等价，完整迁移需回归测试、隔离环境迁移及真实设备验证，之后才能考虑切换生产。
