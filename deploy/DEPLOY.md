# Linux 部署

## 交互式全自动部署（推荐）

在全新克隆的项目目录中运行：

```bash
chmod +x scripts/install-interactive.sh
./scripts/install-interactive.sh
```

脚本会询问机器人名称、Chatmail 中继服务器、系统管理员地址、后台端口和持久化目录，随后自动生成密钥、下载并校验 RPC Server、创建机器人账号、初始化数据库并启动两个容器。已有 `.env` 或业务数据时，使用 `scripts/deploy-server.sh` 更新，不要运行全新安装脚本。

## 手动准备配置后部署

从 GitHub 克隆项目后，先准备环境和 Delta Chat RPC Server：

```bash
git clone https://github.com/cxze299/delta-contact-network-bot.git
cd delta-contact-network-bot
cp .env.example .env
chmod 600 .env
```

编辑 `.env`，至少填写 `CONTACTBOT_SECRET`、`CONTACTBOT_ADMIN_WEB_TOKEN`、系统管理员地址，以及三个宿主机挂载路径。把已验证兼容的 RPC Server 2.59.0 放入 `CONTACTBOT_RPC_HOST_PATH` 指向的目录，然后运行：

```bash
chmod +x scripts/deploy-server.sh
./scripts/deploy-server.sh
```

脚本会检查环境与 Compose 配置、创建所需目录、备份运行中的 SQLite 数据库、给旧镜像添加回退标签、从源码构建镜像、更新两个容器，并检查容器状态、后台鉴权和启动日志。默认复用 Docker 构建缓存；需要完全重建时使用：

```bash
CONTACTBOT_NO_CACHE=1 ./scripts/deploy-server.sh
```

若 Docker 安装在非标准路径，可设置 `CONTACTBOT_DOCKER_BIN`。项目不在脚本上级目录时，可设置 `CONTACTBOT_PROJECT_DIR`。以后更新使用：

```bash
git pull --ff-only
./scripts/deploy-server.sh
```

## 前提

- Debian 12 / Ubuntu 24.04 或同等 Linux
- Python 3.11 或 3.12
- `sqlite3`
- `deltachat-rpc-server` 2.59.0；当前项目固定的 `deltachat2==2.58.0` 已与该版本完成实际部署验证
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
CONTACTBOT_ADMIN_WEB_BIND=127.0.0.1
```

Compose 只监听 NAS 回环地址。管理员在自己的电脑建立 SSH 隧道后访问 `http://127.0.0.1:8787`：

```bash
ssh -L 8787:127.0.0.1:8787 NAS用户@NAS地址
```

浏览器身份验证用户名为 `admin`，密码为管理令牌。不要把端口改成公网监听，也不要复用机器人 Secret 或网络加入码作为管理令牌。

如果需要在 NAS 局域网中直接访问，将 `.env` 中的 `CONTACTBOT_ADMIN_WEB_BIND` 改为 `0.0.0.0`，然后重新运行部署脚本。访问地址为 `http://NAS局域网IP:8787`。后台仍要求 HTTP Basic 登录；如需从公网访问，建议在 DSM 反向代理中配置 HTTPS 和访问控制，不要直接转发明文 HTTP 端口。
