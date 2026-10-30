#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
project_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
env_file="$project_dir/.env"
credentials_file="$project_dir/admin-web-access.txt"
rpc_version=2.59.0

fail() {
    printf '安装失败：%s\n' "$*" >&2
    exit 1
}

say() {
    printf '%s\n' "$*"
}

prompt() {
    label=$1
    default_value=${2:-}
    if [ -n "$default_value" ]; then
        printf '%s [%s]：' "$label" "$default_value" >&2
    else
        printf '%s：' "$label" >&2
    fi
    IFS= read -r answer
    if [ -z "$answer" ]; then
        answer=$default_value
    fi
    printf '%s' "$answer"
}

generate_secret() {
    if command -v openssl >/dev/null 2>&1; then
        openssl rand -hex 32
    elif [ -r /dev/urandom ] && command -v od >/dev/null 2>&1; then
        od -An -N32 -tx1 /dev/urandom | tr -d ' \n'
    else
        fail "无法生成安全随机密钥；请安装 openssl"
    fi
}

find_docker() {
    if command -v docker >/dev/null 2>&1; then
        command -v docker
    elif [ -x /usr/local/bin/docker ]; then
        printf '%s\n' /usr/local/bin/docker
    else
        fail "没有找到 Docker；请先安装 Docker Engine 和 Compose v2"
    fi
}

download_file() {
    url=$1
    destination=$2
    if command -v curl >/dev/null 2>&1; then
        curl -fL --retry 3 --connect-timeout 15 -o "$destination" "$url"
    elif command -v wget >/dev/null 2>&1; then
        wget -O "$destination" "$url"
    else
        fail "缺少 curl 或 wget，无法下载 Delta Chat RPC Server"
    fi
}

case "$(uname -s)" in
    Linux) ;;
    *) fail "该脚本只支持 Linux 服务器或 NAS" ;;
esac

case "$(uname -m)" in
    x86_64|amd64)
        rpc_arch=x86_64
        rpc_sha256=b73ce0f8732f7589cd34e59db4b2ed6a0f6ab6857e691b73b06710e150af4ee0
        ;;
    aarch64|arm64)
        rpc_arch=aarch64
        rpc_sha256=9ea514d0e9ef9c1b76ca9e490b05e07047cff48b53188e282d4ee482f2078ba0
        ;;
    *) fail "暂不支持此 CPU 架构：$(uname -m)" ;;
esac

[ -f "$project_dir/compose.yaml" ] || fail "缺少 compose.yaml，请在项目目录中运行本脚本"
[ -f "$project_dir/Dockerfile" ] || fail "缺少 Dockerfile，请在项目目录中运行本脚本"
[ -f "$project_dir/scripts/deploy-server.sh" ] || fail "缺少 deploy-server.sh"

if [ -e "$env_file" ]; then
    fail "已存在 .env。为防止覆盖现有部署，请使用 ./scripts/deploy-server.sh 更新；全新安装请换一个空目录"
fi
[ ! -e "$credentials_file" ] || fail "已存在 admin-web-access.txt，请换一个空目录以免覆盖现有凭据"

docker_bin=$(find_docker)
docker_with_sudo=0
if "$docker_bin" info >/dev/null 2>&1; then
    :
elif command -v sudo >/dev/null 2>&1 \
    && sudo -n "$docker_bin" info >/dev/null 2>&1; then
    docker_with_sudo=1
else
    fail "当前账号不能访问 Docker；请加入 docker 组或配置 Docker 的免交互 sudo"
fi

docker_run() {
    if [ "$docker_with_sudo" -eq 1 ]; then
        sudo -n "$docker_bin" "$@"
    else
        "$docker_bin" "$@"
    fi
}

docker_run compose version >/dev/null 2>&1 || fail "需要 Docker Compose v2"

say ""
say "好友助手｜全自动 Docker 部署"
say "脚本会创建机器人账号、数据库、后台密码并启动服务。"
say ""

display_name=$(prompt "机器人名称" "好友助手")
[ -n "$display_name" ] || fail "机器人名称不能为空"
case "$display_name" in *'$'*|*'#'*|*'\'*) fail "机器人名称不能包含美元符号、井号或反斜杠" ;; esac

relay_server=$(prompt "Chatmail 中继服务器域名" "nine.testrun.org")
relay_server=${relay_server#dcaccount:}
case "$relay_server" in
    ""|.*|*.|*[!A-Za-z0-9.-]*)
        fail "中继服务器应填写域名，例如 nine.testrun.org"
        ;;
esac

system_admins=$(prompt "系统管理员的 Delta Chat 地址（多个用英文逗号分隔）")
system_admins=$(printf '%s' "$system_admins" | tr -d '[:space:]')
case "$system_admins" in
    *@*) ;;
    *) fail "请至少填写一个有效的 Delta Chat 地址" ;;
esac

say "后台监听方式："
say "  1. 仅服务器本机访问（127.0.0.1，推荐配合反向代理）"
say "  2. 局域网或服务器网卡直接访问（0.0.0.0）"
admin_access=$(prompt "请选择" "1")
case "$admin_access" in
    1) admin_bind=127.0.0.1 ;;
    2) admin_bind=0.0.0.0 ;;
    *) fail "后台监听方式只能选择 1 或 2" ;;
esac

admin_port=$(prompt "后台端口" "8787")
case "$admin_port" in
    ""|*[!0-9]*) fail "后台端口必须是数字" ;;
esac
if [ "$admin_port" -lt 1 ] || [ "$admin_port" -gt 65535 ]; then
    fail "后台端口范围应为 1 至 65535"
fi

storage_root=$(prompt "持久化数据根目录" "$project_dir")
case "$storage_root" in
    /*) ;;
    *) fail "持久化数据根目录必须使用绝对路径" ;;
esac
case "$storage_root" in *'$'*|*'#'*|*':'*|*'\'*) fail "数据目录不能包含美元符号、井号、冒号或反斜杠" ;; esac

data_dir="$storage_root/data"
account_dir="$storage_root/runtime/account"
rpc_dir="$storage_root/runtime/bin"

if [ -e "$data_dir/bun-contactbot.sqlite3" ] || [ -n "$(find "$account_dir" -mindepth 1 -print -quit 2>/dev/null || true)" ]; then
    fail "目标目录中已有数据库或账号数据。请换一个空目录，避免覆盖现有部署"
fi
[ ! -e "$rpc_dir/deltachat-rpc-server" ] || fail "目标目录中已有 RPC Server；请换一个空目录，避免覆盖已有文件"

say ""
say "即将部署："
say "  机器人名称：$display_name"
say "  中继服务器：$relay_server"
say "  系统管理员：$system_admins"
say "  后台监听：$admin_bind:$admin_port"
say "  数据目录：$storage_root"
confirm=$(prompt "输入 yes 开始部署" "yes")
[ "$confirm" = yes ] || fail "已取消"

contactbot_secret=$(generate_secret)
admin_web_token=$(generate_secret)
backup_password=$(generate_secret)
xdc_admin_key=$(generate_secret)
umask 077
mkdir -p "$data_dir" "$account_dir" "$rpc_dir"

temp_env="$env_file.tmp.$$"
temp_rpc="$rpc_dir/deltachat-rpc-server.tmp.$$"
cleanup_temp() {
    rm -f "$temp_env" "$temp_rpc"
}
trap cleanup_temp EXIT HUP INT TERM

cat > "$temp_env" <<EOF
CONTACTBOT_SECRET=$contactbot_secret
CONTACTBOT_BUN_DB=/app/data/bun-contactbot.sqlite3
CONTACTBOT_DISPLAY_NAME=$display_name
CONTACTBOT_ACCOUNTS_DIR=/app/account
CONTACTBOT_SYSTEM_ADMINS=$system_admins
CONTACTBOT_SYSTEM_ADMIN_CONTACT_IDS=
CONTACTBOT_ACCOUNT_ID=
CONTACTBOT_RPC_SERVER=/app/rpc/deltachat-rpc-server
CONTACTBOT_REQUIRE_VERIFIED=true
CONTACTBOT_ADMIN_WEB_TOKEN=$admin_web_token
CONTACTBOT_BACKUP_PASSWORD=$backup_password
CONTACTBOT_ADMIN_WEB_PORT=$admin_port
CONTACTBOT_ADMIN_WEB_BIND=$admin_bind
CONTACTBOT_INITIAL_NETWORK_CODE=
CONTACTBOT_DATA_HOST_PATH=$data_dir
CONTACTBOT_ACCOUNT_HOST_PATH=$account_dir
CONTACTBOT_RPC_HOST_PATH=$rpc_dir
CONTACTBOT_LOG_LEVEL=INFO
EOF
chmod 0600 "$temp_env"
mv "$temp_env" "$env_file"

# Keep the generated credentials even if a later download or registration
# fails. The installer never replaces this file on an existing deployment.
cat > "$credentials_file" <<EOF
好友助手网页管理后台
用户名：admin
密码：$admin_web_token
小程序管理员密钥：$xdc_admin_key
备份密码：$backup_password
端口：$admin_port
监听地址：$admin_bind
EOF
chmod 0600 "$credentials_file"

rpc_url="https://github.com/chatmail/core/releases/download/v$rpc_version/deltachat-rpc-server-$rpc_arch-linux"
say "下载 Delta Chat RPC Server $rpc_version ($rpc_arch)…"
download_file "$rpc_url" "$temp_rpc"

if command -v sha256sum >/dev/null 2>&1; then
    actual_sha256=$(sha256sum "$temp_rpc" | awk '{print $1}')
elif command -v shasum >/dev/null 2>&1; then
    actual_sha256=$(shasum -a 256 "$temp_rpc" | awk '{print $1}')
else
    fail "缺少 sha256sum 或 shasum，无法校验 RPC Server"
fi
[ "$actual_sha256" = "$rpc_sha256" ] || fail "RPC Server SHA-256 校验失败"
chmod 0755 "$temp_rpc"
mv "$temp_rpc" "$rpc_dir/deltachat-rpc-server"

cd "$project_dir"
say "构建 Docker 镜像…"
docker_run compose build

say "设置小程序管理员密钥…"
xdc_admin_verifier=$(printf '%s' "$xdc_admin_key" | docker_run compose run --rm -T --entrypoint bun contact-bot src/cli.ts verifier)
case "$xdc_admin_verifier" in 250000:????????????????????????????????:????????????????????????????????????????????????????????????????) ;; *) fail "小程序管理员密钥配置失败；请保留现有数据并检查安装状态" ;; esac
printf 'CONTACTBOT_XDC_ADMIN_VERIFIER=%s\n' "$xdc_admin_verifier" >> "$env_file"

say "初始化数据库…"
docker_run compose run --rm contact-bot init-db

say "通过 $relay_server 自动创建机器人账号…"
account_result=$(docker_run compose run --rm contact-bot configure "dcaccount:$relay_server")
say "机器人添加链接已保存至凭据文件"

printf '\n%s\n' "$account_result" >> "$credentials_file"

say "启动并检查服务…"
CONTACTBOT_DOCKER_BIN="$docker_bin" "$project_dir/scripts/deploy-server.sh"

trap - EXIT HUP INT TERM
say ""
say "部署完成。"
say "后台用户名：admin"
say "后台密码已保存到：$credentials_file"
if [ "$admin_bind" = 0.0.0.0 ]; then
    say "后台地址：http://服务器IP:$admin_port"
else
    say "后台仅监听本机：http://127.0.0.1:$admin_port"
fi
say "向机器人发任意消息，打开收到的小程序，并在“我的 → 管理网络”输入密钥。"
