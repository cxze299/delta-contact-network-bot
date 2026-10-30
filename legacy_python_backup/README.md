# 好友助手

好友助手是 Delta Chat 私聊中的 Webxdc 联系人小程序。成员可以加入多个独立网络、查看同网络主动公开资料的好友，并按双方授权交换原生联系人名片。管理网络可在小程序或网页后台完成。

## 全新 Linux 服务器一键部署

适用 **x86_64 或 ARM64 Linux**，约 150 人使用一台服务器即可。请先安装 Docker Engine、Docker Compose v2、Git、curl（或 wget），并确保当前账号可以使用 Docker；脚本也支持已配置免交互 sudo 的 Docker。服务器需要能访问 GitHub、Docker 镜像源和所选 Chatmail 中继。

当前可用版本在 `fix/webxdc-reliability-20261028` 分支。不要直接克隆默认分支来安装此版本：

```bash
git clone --branch fix/webxdc-reliability-20261028 --single-branch https://github.com/cxze299/delta-contact-network-bot.git
cd delta-contact-network-bot
./install.sh
```

按提示填写机器人名称、支持账号创建的 Chatmail 中继域名、管理员 Delta Chat 地址、后台监听方式与端口，以及数据目录。脚本会：

1. 生成独立的业务密钥、网页后台密码和小程序管理员密钥；
2. 根据服务器架构从 Delta Chat Core 官方发布下载 RPC Server 2.59.0，并核对内置 SHA-256；
3. 构建本仓库源码镜像，初始化 SQLite，并通过 `dcaccount:` 配置新的机器人账号；
4. 启动机器人和后台，检查容器和后台鉴权状态。

不同中继可能不支持自动创建账号或需要邀请；配置失败时，脚本会报错，不能把该中继视为可用。脚本不安装 Docker，不自动开放防火墙端口。

安装结果保存在项目目录下的 `admin-web-access.txt`（权限 `0600`），其中有网页后台用户名 `admin`、密码、小程序管理员密钥，以及 Core 能生成时的机器人添加链接。`.env` 包含业务密钥与管理员验证器，权限为 `0600`。请妥善保管这两个文件；拿到小程序管理员密钥的人可以取得系统管理权限。

**全新安装只适用于空目录。** 如果已有 `.env`、凭据文件、机器人账号、RPC 文件或业务数据库，安装脚本会拒绝覆盖。途中失败时不要删除或覆盖已有账号、数据库和 `.env`：凭据已在账号注册前保存。若账号尚未创建，修复下载、中继或构建问题后仍须完成账号配置；更新脚本不会代为创建账号。新服务器如需继承旧机器的成员和机器人身份，应恢复完整备份，不能直接创建一个新机器人账号后期待自动继承。

## 首次使用

1. 在 Delta Chat 中打开安装结果中的机器人添加链接，完成加密连接，向机器人发送任意消息。
2. 打开机器人发来的好友助手小程序，在“我的 → 管理网络”输入安装时生成的**小程序管理员密钥**。
3. 在系统管理中创建网络，保存系统生成的加入码，通过可信渠道发给成员。加入码是加入凭证，不要公开传播。
4. 成员添加机器人、发送任意消息并打开收到的小程序，在本地表单填写加入码和昵称，确认使用当前账号后一次提交加入。已加入的成员可在“我的 → 我的网络”加入更多网络或切换网络；打开及返回加入表单不等待网络。

小程序管理密钥与网页后台密码是两组不同的凭据。服务器能够处理发给机器人的数据；名片交付后不能从接收方设备撤回。

## 网页后台

安装时默认仅监听 `127.0.0.1:8787`，可通过 SSH 隧道访问：

```bash
ssh -L 8787:127.0.0.1:8787 用户名@服务器地址
```

然后在本机打开 `http://127.0.0.1:8787/`，使用 `admin-web-access.txt` 中的网页账号和密码。需要通过公网访问时，应配置 HTTPS 反向代理和访问控制；直接监听 `0.0.0.0` 时还需自行配置防火墙。不要把明文 HTTP 管理后台直接暴露到公网。

小程序通过 Delta Chat 通信，不依赖网页后台端口。

## 更新已有部署

```bash
cd delta-contact-network-bot
git pull --ff-only
./scripts/deploy-server.sh
```

更新脚本不会创建新账号；会在已有机器人运行时用 SQLite 在线备份 API 生成部署前数据库副本，保留旧镜像标签，然后构建并启动新容器。**这个部署前副本是受限权限的明文文件，不是加密备份**；应将其纳入加密备份和保留期管理。镜像标签本身不会自动回滚数据库迁移，恢复前需核对版本兼容性。

查看运行状态：

```bash
docker compose ps
docker compose logs --tail=100 contact-bot
docker compose logs --tail=100 admin-web
curl -fsS http://127.0.0.1:8787/health
```

若安装时修改过后台端口，请相应修改上面的地址。默认 Compose 因兼容既有 NAS 数据以容器内 root 运行，仍使用 `no-new-privileges`；应限制主机和 Docker 管理权限。

## 备份与恢复

需要一起保存：`.env`、`admin-web-access.txt`、数据库目录 `CONTACTBOT_DATA_HOST_PATH`，以及 Core 账号目录 `CONTACTBOT_ACCOUNT_HOST_PATH`。**业务密钥和 Core 账号不能丢失或随意更换**，否则无法正确识别账号或恢复数据。

下面的示例仅适用于安装时使用项目目录作为数据根目录的情况。先停止两个容器以取得一致的数据库和 Core 账号快照，按提示输入备份加密密码：

```bash
mkdir -p backups
chmod 700 backups
docker compose stop
umask 077
set -o pipefail
tar -czf - .env admin-web-access.txt data runtime/account | openssl enc -aes-256-cbc -salt -pbkdf2 -out "backups/contactbot-$(date -u +%Y%m%dT%H%M%SZ).tar.gz.enc"
docker compose up -d
```

如果备份命令失败，也要执行 `docker compose up -d` 恢复服务。数据目录自定义时，请按 `.env` 中的实际路径调整归档内容。备份密码应单独保管，定期复制密文到另一台机器，并按实际策略删除超过 30 天的旧备份。

恢复时先在隔离目录解密和检查备份，确认版本与目录，再停止目标服务、保存当前数据并由管理员恢复 `.env`、数据库和 Core 账号目录；不要让同一个机器人账号在两台服务器同时运行。恢复旧备份可能重新引入备份之后已删除的资料；当前项目**没有自动重放删除记录的完整机制**，必须先核对并重新执行删除，不能声称恢复后已自动满足删除要求。

```bash
mkdir -p /secure/path/contactbot-restore
chmod 700 /secure/path/contactbot-restore
openssl enc -d -aes-256-cbc -pbkdf2 -in /secure/path/备份.tar.gz.enc | tar -xz -C /secure/path/contactbot-restore
```

恢复目录也含明文密钥和个人数据，处理完毕后应安全清理。

## 常见问题

- **安装中断或账号创建失败**：检查中继是否支持注册、服务器网络、Docker 日志及已生成的 `.env` 和账号目录。不要重新生成密钥覆盖原有数据。确认下载的 RPC 文件存在且 SHA-256 与 `scripts/install-interactive.sh` 中对应架构的校验值一致；缺失时按该脚本中的官方地址下载并校验。修复后执行 `docker compose build`。如果 `.env` 尚无 `CONTACTBOT_XDC_ADMIN_VERIFIER`，可使用凭据文件中的小程序密钥在本机通过以下命令补上：

  ```bash
  key=$(sed -n 's/^小程序管理员密钥：//p' admin-web-access.txt)
  test -n "$key" || { echo '缺少小程序管理员密钥'; exit 1; }
  verifier=$(printf '%s' "$key" | docker compose run --rm -T --entrypoint /venv/bin/python contact-bot -c 'import sys; from contactbot.xdc_admin import make_verifier; print(make_verifier(sys.stdin.read()))')
  printf 'CONTACTBOT_XDC_ADMIN_VERIFIER=%s\n' "$verifier" >> .env
  unset key verifier
  ```

  仅当账号尚未配置时，执行 `docker compose run --rm contact-bot configure dcaccount:中继域名`；已配置账号会拒绝再次配置。保留输出的机器人添加链接，再运行 `./scripts/deploy-server.sh`。若不确定账号状态，先备份并检查，勿清空账号目录。
- **后台打不开**：检查 `docker compose ps`、`/health`、监听地址、端口和防火墙。默认只允许服务器本机访问。
- **管理密钥无效**：使用 `admin-web-access.txt` 中的“小程序管理员密钥”，不是网页密码。修改验证器会使旧的系统管理授权失效。
- **小程序还是旧版本**：向机器人发送任意消息，打开新收到的小程序；旧包不会自动更新。
- **状态暂未确认**：等待后续状态同步。系统不会自动重发操作，同一项尚未确认的操作会被拦住以避免重复。

## 更多资料

- [部署说明](deploy/DEPLOY.md)
- [小程序操作与多网络状态](docs/webxdc-operation-state.md)
- [成员与管理员指南](docs/USER_GUIDE.md)

## 许可证

当前仓库未声明开源许可证。未经项目所有者明确授权，不代表允许复制、修改或再发布。
