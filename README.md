# 好友助手

一个适用于Delta Chat中的联系人网络机器人

机器人支持多个相互隔离的联系网络。
成员可以同时加入多个网络，网络管理员负责成员和加入码，系统管理员负责创建网络及分配管理员权限。

## 功能

- 私聊机器人完成加入、建档、查找和发送联系人名片
- 管理员自定义网络加入码，允许中文、空格和特殊字符
- 按昵称查找成员，并通过数字编号选择结果
- 一个账号加入多个联系网络
- 联系人名片只发送给选中的成员
- 退出、屏蔽、举报、封禁和数据删除
- 换号后提交账号恢复申请，经管理员核实后迁移资料和历史关系
- 普通成员、网络管理员、系统管理员三级权限
- 可编辑的网页管理后台
- SQLite 持久化、重复消息去重、管理审计和部署前自动备份

## 工作方式

```text
成员添加机器人
      ↓
输入管理员提供的加入码
      ↓
输入昵称并加入联系网络
      ↓
发送“查找好友”并输入昵称
      ↓
回复搜索结果前的数字编号
      ↓
机器人把申请人的联系人名片发送给对方
      ↓
对方点击名片完成添加
```

所有成员功能和管理员功能均通过与机器人私聊完成。聊天中的任意普通消息会打开主菜单，每一步都可以发送 `q` 退出。

## Docker 部署

### 1. 准备服务器

服务器需要：

- Linux x86_64 主机或 NAS
- Docker Engine
- Docker Compose v2
- Git
- `curl`
- 与 Python 依赖 `deltachat2==2.58.0` 兼容的 `deltachat-rpc-server`

建议为机器人准备一个专用 Delta Chat 账号，不要使用个人主账号。

### 2. 下载项目

```bash
git clone https://github.com/cxze299/delta-contact-network-bot.git
cd delta-contact-network-bot
cp .env.example .env
chmod 600 .env
```

### 3. 准备 Delta Chat RPC Server

从 Delta Chat 官方发布渠道下载与项目版本兼容的 `deltachat-rpc-server`，放入项目的 `runtime/bin` 目录：

```bash
mkdir -p runtime/bin runtime/account data
cp /下载位置/deltachat-rpc-server runtime/bin/deltachat-rpc-server
chmod +x runtime/bin/deltachat-rpc-server
```

确认它可以运行：

```bash
./runtime/bin/deltachat-rpc-server --version
```

不要使用来源不明的 RPC Server 可执行文件。

### 4. 修改配置

编辑 `.env`。首次部署至少需要修改以下内容：

```env
# 用于身份摘要和加入码加密，至少 32 个字符
CONTACTBOT_SECRET=请替换为独立的长随机字符串

# SQLite 数据库在容器中的位置
CONTACTBOT_DB=/app/data/contactbot.sqlite3

# 机器人显示名称
CONTACTBOT_DISPLAY_NAME=好友助手

# Delta Chat Core 账号目录和 RPC Server
CONTACTBOT_ACCOUNTS_DIR=/app/account
CONTACTBOT_RPC_SERVER=/app/rpc/deltachat-rpc-server

# 系统管理员的 Delta Chat 地址，多个地址用英文逗号分隔
CONTACTBOT_SYSTEM_ADMINS=admin@example.org

# 网页后台密码，至少 32 个字符，不能与 CONTACTBOT_SECRET 相同
CONTACTBOT_ADMIN_WEB_TOKEN=请替换为另一段独立的长随机字符串

# 后台端口
CONTACTBOT_ADMIN_WEB_PORT=8787

# 仅服务器本机访问填 127.0.0.1；局域网直接访问填 0.0.0.0
CONTACTBOT_ADMIN_WEB_BIND=127.0.0.1

# Docker 宿主机上的持久化目录
CONTACTBOT_DATA_HOST_PATH=./data
CONTACTBOT_ACCOUNT_HOST_PATH=./runtime/account
CONTACTBOT_RPC_HOST_PATH=./runtime/bin
```

可以在服务器上生成随机字符串：

```bash
openssl rand -hex 32
```

`CONTACTBOT_SECRET`、`CONTACTBOT_ADMIN_WEB_TOKEN`、机器人账号配置内容和 `.env` 都不能提交到 GitHub。

### 5. 配置机器人账号

先构建镜像并初始化数据库：

```bash
docker compose build
docker compose run --rm contact-bot init-db
```

从可信的 Delta Chat 账号配置二维码中取得以 `dcaccount:` 或 `dclogin:` 开头的配置内容，然后执行：

```bash
read -rsp "机器人账号配置内容：" CONTACTBOT_ACCOUNT_QR
echo
docker compose run --rm contact-bot configure "$CONTACTBOT_ACCOUNT_QR"
unset CONTACTBOT_ACCOUNT_QR
```

配置成功后，账号文件会保存在 `CONTACTBOT_ACCOUNT_HOST_PATH` 指定的目录中。该目录需要纳入加密备份。

### 6. 启动服务

```bash
chmod +x scripts/deploy-server.sh
./scripts/deploy-server.sh
```

部署脚本会：

1. 检查 `.env`、Docker、Compose 和 RPC Server；
2. 创建数据目录；
3. 备份正在使用的 SQLite 数据库；
4. 构建新镜像并保留旧镜像回退标签；
5. 启动机器人和网页后台；
6. 检查容器状态、后台鉴权和错误日志。

查看运行状态：

```bash
docker compose ps
docker compose logs --tail=100 contact-bot
docker compose logs --tail=100 admin-web
```

两个容器都应显示为运行状态，机器人日志中不应出现未配置账号或 RPC Server 启动失败。

## 首次使用

1. `CONTACTBOT_SYSTEM_ADMINS` 中的系统管理员先私聊机器人。
2. 发送 `管理` 查看管理员指令。
3. 发送 `创建网络 网络名称`。
4. 按机器人提示设置自定义加入码。
5. 通过可信渠道把加入码发给成员。
6. 成员添加机器人，发送任意消息，然后按引导输入加入码和昵称。

系统管理员只有在私聊机器人后才会写入数据库，不会自动把第一位使用者设为管理员。

## 成员使用

主菜单：

```text
🔎 查找好友
👤 查看资料
🌐 加入新网络
📖 帮助
🚪 q 退出
```

常用流程：

- 查找好友：发送 `查找好友`，输入昵称，再回复结果前的数字编号。
- 修改资料：发送 `查看资料`，再选择编辑昵称或编辑介绍。
- 加入另一个网络：发送 `加入新网络`，再输入新的加入码。
- 切换当前网络：发送 `我的网络`，根据提示选择网络。
- 换号恢复：新账号发送 `恢复账号`，输入原成员编号，等待管理员核实。
- 查看完整帮助：发送 `帮助`。

联系人名片发送给对方后，无法从对方设备撤回。

## 管理员使用

管理员私聊机器人发送 `管理` 可以查看当前可用指令。

网络管理员可以管理自己负责的网络：

```text
网络概况
成员列表
查看加入码
更换加入码
停用加入
恢复加入
移除成员 <成员编号>
封禁成员 <成员编号> <原因>
解除封禁 <成员编号>
举报列表
处理举报 <举报编号> <处理结果>
管理记录
恢复申请列表
批准恢复 <恢复编号>
拒绝恢复 <恢复编号>
```

系统管理员还可以：

```text
创建网络 <网络名称>
网络列表
修改网络名称 <网络编号> <新名称>
任命管理员 <网络编号> <成员编号>
撤销管理员 <网络编号> <成员编号>
停用网络 <网络编号>
恢复网络 <网络编号>
```

管理员权限绑定 Delta Chat 账号身份，不根据昵称判断。

## 网页管理后台

后台随 Docker Compose 一起启动，登录用户名固定为 `admin`，密码是 `.env` 中的 `CONTACTBOT_ADMIN_WEB_TOKEN`。

如果配置为：

```env
CONTACTBOT_ADMIN_WEB_BIND=0.0.0.0
CONTACTBOT_ADMIN_WEB_PORT=8787
```

局域网访问地址为：

```text
http://服务器IP:8787
```

后台可以编辑网络名称和状态、加入码、成员角色、成员状态、目录可见性及申请设置。页面不会显示成员联系方式、Delta Chat 地址、账号摘要或加入码原文。

需要从公网访问时，应通过 NAS 或反向代理配置 HTTPS 和访问控制。

## 更新

```bash
cd delta-contact-network-bot
git pull --ff-only
./scripts/deploy-server.sh
```

需要忽略 Docker 构建缓存时：

```bash
CONTACTBOT_NO_CACHE=1 ./scripts/deploy-server.sh
```

部署脚本会在更新前创建一致的 SQLite 备份，并给旧镜像添加 `pre-deploy-*` 标签。

## 数据与备份

需要备份：

- `CONTACTBOT_DATA_HOST_PATH`：SQLite 数据库和部署前备份；
- `CONTACTBOT_ACCOUNT_HOST_PATH`：Delta Chat Core 机器人账号；
- `.env`：加密密钥和部署配置。

这些内容应存放在受限目录或加密备份中。备份最长保留 30 天。恢复旧备份后，需要重新执行数据清理并检查已经删除的用户记录。

手动执行数据保留期限清理：

```bash
docker compose exec contact-bot /venv/bin/contactbot cleanup
```

## 常见问题

### 机器人容器反复重启

先查看日志：

```bash
docker compose logs --tail=200 contact-bot
```

常见原因包括机器人账号尚未配置、RPC Server 路径错误、RPC Server 没有执行权限，或 `CONTACTBOT_SECRET` 少于 32 个字符。

### 后台无法访问

检查后台容器和端口：

```bash
docker compose ps
curl -I http://127.0.0.1:8787/
```

未携带登录信息时返回 `401 Unauthorized` 表示后台运行正常。局域网访问还需要将 `CONTACTBOT_ADMIN_WEB_BIND` 设置为 `0.0.0.0`，并允许服务器防火墙放行对应端口。

### 系统管理员没有管理权限

确认 `.env` 中的 `CONTACTBOT_SYSTEM_ADMINS` 是管理员实际使用的 Delta Chat 地址，然后让该账号重新私聊机器人。修改 `.env` 后需要重新运行部署脚本。

## 安全与隐私

- 网络之间严格隔离，服务端会校验成员身份、网络归属和权限。
- 网络加入码以带密钥摘要和加密密文保存，不写入普通日志。
- 联系人名片发送完成后会删除临时文件。
- 网页后台使用独立密码并校验修改请求。
- 群聊不处理业务指令，敏感管理信息只在加密私聊中返回。
- 删除个人数据会清理联系人网络业务资料；Delta Chat Core 历史消息和客户端缓存由部署维护流程处理。

## 更多文档

- [成员与管理员指南](docs/USER_GUIDE.md)
- [验收清单](docs/ACCEPTANCE.md)
- [服务器部署说明](deploy/DEPLOY.md)

## 许可证

当前仓库未声明开源许可证。未经项目所有者明确授权，不代表允许复制、修改或再发布。
