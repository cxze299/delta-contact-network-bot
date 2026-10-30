# 架构与状态

Webxdc（保留 HTML/CSS/原 DOM 渲染）→ Iroh 实时通道及 Delta Chat 状态更新 → 官方 RPC SDK → 单一串行任务执行器 → TypeScript 业务及 Hono 后台 → SQLite WAL。

`src/delta_adapter.ts` 启动 Core stdio 子进程、读取事件、检查加密私聊身份、打包专属 Webxdc 和交付名片。`src/webxdc_bridge.ts` 解密并以 Zod 校验命令，在同一 SQLite 事务中保存业务结果、回执和消费游标。管理权限与挑战证明在 `src/xdc_admin.ts`；共用业务在 `src/service.ts`，网页路由在 `src/admin_web.ts`。

原有业务表完整保留，补充 `v2_users`、`v2_instances`、`v2_results`、`v2_jobs`、`v2_notifications`、`v2_tombstones`、`v2_runtime`、`v2_aliases`。幂等键按实例令牌和请求号区分，游标按 Core 账号和消息号区分，不能只使用消息号。

请求：准备 → 双通道携带同一编号 → 服务端校验 → 事务完成／明确失败 → 认证回执。3 秒未确认时释放界面，保留该操作以便后续对账；不自动生成新请求重试。

交换：pending_target（需要逐次确认）→ processing → completed；或 rejected / withdrawn / expired / invalid。双方已开启自动交换时直接进入 processing。退出、移除、屏蔽、停用网络及过期会使未完成交换失效。

交付任务：pending → sending（先持久化意图）→ sent。发送调用之后发生异常则 uncertain；通过唯一附件名在 Core 中核对，找到则 sent，无法确认时保留，不重发。未调用发送前失败可标记 failed；管理员可将待核查任务结束为 closed。所有 queued 任务在交付前重新校验网络、身份、双方授权版本及屏蔽状态。

下行快照使用内容哈希版本、递增序号与明确的基线。增量以顶层字段替换，列表整体替换避免遗漏删除；缺少基线时仅请求只读完整快照。两条通道中重复或乱序的版本不会重复应用。
