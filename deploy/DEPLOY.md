# Linux 部署

## 前提

- Debian 12 / Ubuntu 24.04 或同等 Linux
- Python 3.11 或 3.12
- `sqlite3`
- `deltachat-rpc-server` 2.58，与项目固定的 `deltachat2==2.58.0` 保持同一版本线
- 独立的低权限系统用户 `contactbot`

RPC Server 应从 Delta Chat 官方发布渠道取得，并校验发布方提供的校验值。确认 `deltachat-rpc-server --version` 后再启动服务。

## 安装

```bash
sudo install -d -o contactbot -g contactbot /opt/contactbot /var/lib/contactbot /etc/contactbot
sudo -u contactbot python3 -m venv /opt/contactbot/.venv
sudo -u contactbot /opt/contactbot/.venv/bin/pip install /path/to/delta-contact-bot
sudo install -o root -g contactbot -m 0640 .env /etc/contactbot/contactbot.env
sudo -u contactbot env $(grep -v '^#' /etc/contactbot/contactbot.env | xargs) \
  /opt/contactbot/.venv/bin/contactbot init-db
```

不要在共享 shell 历史中直接输入账号配置 QR。首次配置时使用仅管理员可读的临时脚本或禁用当前 shell 历史，配置完成后立即删除临时内容。

安装 `deploy/contactbot.service` 到 `/etc/systemd/system/`，按实际路径调整后：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now contactbot
sudo systemctl status contactbot
```

## 定期清理

每天执行一次：

```bash
/opt/contactbot/.venv/bin/contactbot cleanup
```

它会清空结束超过 30 天的申请正文和联系方式快照，并删除超过 90 天的已处理举报、管理审计和消息去重记录。

## 备份与恢复

使用 `scripts/backup.sh` 对运行中的 WAL 数据库创建一致性备份。备份目录应位于加密磁盘或先由备份系统加密再离机保存，最长保留 30 天。

恢复前停止服务，验证目标绝对路径，保留原数据库副本，再替换数据库。恢复后必须重放单独保存的删除清单，然后运行 `contactbot cleanup`，最后启动服务并用两个测试账号验证。

当前 MVP 不包含自动化删除清单的独立存储，生产启用备份恢复前必须完成这一项运维集成。

## 本地管理后台

配置一个与 `CONTACTBOT_SECRET` 不同的随机令牌：

```text
CONTACTBOT_ADMIN_WEB_TOKEN=至少32字符的独立随机值
CONTACTBOT_ADMIN_WEB_PORT=8787
```

Compose 只监听 NAS 回环地址。管理员在自己的电脑建立 SSH 隧道后访问 `http://127.0.0.1:8787`：

```bash
ssh -L 8787:127.0.0.1:8787 NAS用户@NAS地址
```

浏览器身份验证用户名为 `admin`，密码为管理令牌。不要把端口改成公网监听，也不要复用机器人 Secret 或网络加入码作为管理令牌。
