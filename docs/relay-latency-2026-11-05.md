# talk.cmsv.space 中继延迟排查与调整

## 已验证事实

2026-11-05：CPU 和内存负载低；原邮件队列约 226 封，主要为外部投递延迟。系统 DNS 对 nine.testrun.org 多次返回不一致且可疑的地址，公共加密 DNS 端点多处连接失败。对三个外部中继 IPv4 的 TCP/25 探测均超时，但 nyan.email 的 HTTPS 可连接。主机 OUTPUT 默认 ACCEPT。现有证据不能区分云平台限制、路由故障、目标故障和上游网络干扰，不能宣称已彻底修复。

filtermail 已支持 HTTPS 优先、SMTP 回退，不关闭 TLS 或 DNSSEC，不硬编码未经核验的外部 IP。未读取邮件正文，诊断不输出收发件人地址。未清空或全量强制重发邮件。

## 生效配置

- lmtp-filtermail_destination_recipient_limit = 1（原继承默认 50）
- lmtp-filtermail_destination_concurrency_limit = 1（原 10000）

Postfix 收件人限制为 1 时，目的并发按收件人计算；不同收件人可分别调度，同一收件人串行，减少慢目标拖住同批其他收件人的风险。多个收件人的消息会增加独立投递次数，需观察队列吞吐及连接开销。没有声称已测得端到端加速。

脚本：/srv/chatmail-relay/latency-tuning/apply.sh；主机 systemd 定时器 chatmail-latency.timer 每 60 秒检查配置漂移，仅变化时 postfix check/reload，以兼容容器重建。备份同目录 main.cf.before、master.cf.before。仓库脚本 scripts/chatmail-recipient-isolation.sh 与部署脚本一致。

回退：先停用 chatmail-latency.timer，再执行 docker exec chatmail postconf -X lmtp-filtermail_destination_recipient_limit，以及 docker exec chatmail postconf -e lmtp-filtermail_destination_concurrency_limit=10000，检查并 reload Postfix。完整旧配置仅作灾难恢复参考，勿覆盖之后其他合法配置变化。

## 验证与未解决项

配置检查成功；Postfix、Dovecot、filtermail-transport active；好友助手健康检查成功。本机三次 TLS 建连到欢迎响应：SMTP/465 为 48–77ms，IMAP/993 为 5–6ms。这不是外网客户端或实际邮件投递时延。调整后仍约 216 封 deferred，跨中继根因未解决。

下一步需要云平台确认出站 TCP/25 是否限制、网络侧提供可验证可达的加密 DNS/外部 MX 通路，再对不同中继真实账号做端到端测试。不要用缩短重试间隔、关闭 TLS 或降低落盘可靠性替代根因修复。
