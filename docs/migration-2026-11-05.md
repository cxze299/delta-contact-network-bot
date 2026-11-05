# 好友助手迁移（2026-11-05）

目标部署目录：`/opt/friend-assistant`；Compose 位于 `app/compose.yaml`。

## 保留内容

迁移 NAS 实际运行镜像及部署源码，不使用可能落后的 Git 版本替换线上功能。停止源机器人和共享数据测试容器后复制整个 data、runtime/account、runtime/bin，保留业务数据库、删除账本、Core 身份密钥、聊天数据库、附件及配置密钥。账号不重新注册。源环境设置 restart=no，避免双活。

## 入口

新公开入口：`https://talk.cmsv.space/join`。只代理 `/join`、`/join.js`、`/api/public-invite`，保留 Chatmail 原有首页和服务。配置位于主机 `/srv/chatmail-relay/data/nginx/nginx.conf`，修改前备份至 `/opt/friend-assistant/nginx-before-migration.conf`。Chatmail 更新可能生成新配置，更新后必须复查路由。

旧 NAS 8888 HTTPS 入口保留，通过 `NAS 上的 friend-assistant-forward.service` 加密转发到新服务器 localhost:8787。NAS 不运行机器人，只保留旧链接入口；NAS 或隧道停止会影响旧链接，不影响机器人和新邀请域名。

后台仅监听新服务器 localhost:8787。管理员可通过 SSH 本地转发访问，不直接开放后台公网端口。

## 运维与回退

- 服务：在 app 目录运行 `docker compose ps`，健康接口为 `http://127.0.0.1:8787/api/health`。
- 隧道：在 NAS 执行 `sudo systemctl status friend-assistant-forward`。SSH key 受新服务器侧 permitopen=127.0.0.1:8787 限制；不提供 shell 权限。
- 新服务器开始处理业务后，不能直接启动 NAS 旧数据。回退必须先停止新机器人和隧道，反向同步最新 data 与 runtime/account（含删除账本），核验后再启动 NAS 单实例。
- 所有备份必须包含配置密钥和 Core 身份；只备份业务数据库无法恢复同一机器人身份。私钥、.env、数据库不得提交 Git。

## 切换验收

停止源端后，316 个迁移文件的相对路径与内容联合 SHA-256 两端一致：`dd768d8c57dabecaf0a2087e900a77b3d3b554bea316d63545f6cee906a3c946`。业务数据库完整性检查均为 ok；4 个网络、15 条成员记录、36 个小程序实例一致。镜像文件层摘要一致（Docker 存储格式不同可导致整体镜像 ID 不同）。新服务 healthy、零重启，新旧邀请页和 Chatmail 原首页返回 HTTP 200。临时只读迁移授权已移除。
