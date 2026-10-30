#!/bin/sh
set -eu

# The project root defaults to the parent of this script. Override it when the
# script is stored elsewhere on the server.
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
project_dir=${CONTACTBOT_PROJECT_DIR:-$(CDPATH= cd -- "$script_dir/.." && pwd)}
env_file="$project_dir/.env"
bot_container=${CONTACTBOT_BOT_CONTAINER:-delta-contact-network-bot}
admin_container=${CONTACTBOT_ADMIN_CONTAINER:-delta-contact-network-admin}
timestamp=$(date -u +%Y%m%dT%H%M%SZ)

fail() {
    echo "部署失败：$*" >&2
    exit 1
}

env_value() {
    key=$1
    sed -n "s/^${key}=//p" "$env_file" | tail -n 1
}

absolute_path() {
    value=$1
    case "$value" in
        /*) printf '%s\n' "$value" ;;
        *) printf '%s/%s\n' "$project_dir" "$value" ;;
    esac
}

command -v sed >/dev/null 2>&1 || fail "缺少 sed"
command -v grep >/dev/null 2>&1 || fail "缺少 grep"

docker_bin=${CONTACTBOT_DOCKER_BIN:-}
if [ -z "$docker_bin" ]; then
    if command -v docker >/dev/null 2>&1; then
        docker_bin=$(command -v docker)
    elif [ -x /usr/local/bin/docker ]; then
        docker_bin=/usr/local/bin/docker
    else
        fail "没有找到 Docker"
    fi
fi
[ -x "$docker_bin" ] || fail "Docker 不可执行：$docker_bin"

docker_with_sudo=0
if "$docker_bin" info >/dev/null 2>&1; then
    :
elif command -v sudo >/dev/null 2>&1 \
    && sudo -n "$docker_bin" info >/dev/null 2>&1; then
    docker_with_sudo=1
else
    fail "当前账号不能访问 Docker；请配置 docker 组或免交互 sudo"
fi

docker_run() {
    if [ "$docker_with_sudo" -eq 1 ]; then
        sudo -n "$docker_bin" "$@"
    else
        "$docker_bin" "$@"
    fi
}

docker_run compose version >/dev/null 2>&1 || fail "需要 Docker Compose v2"
[ -f "$project_dir/compose.yaml" ] || fail "缺少 compose.yaml：$project_dir"
[ -f "$project_dir/Dockerfile" ] || fail "缺少 Dockerfile：$project_dir"
[ -f "$env_file" ] || fail "缺少 .env；请先复制 .env.example 并填写真实配置"

if grep -Eq '^CONTACTBOT_SECRET=(|replace-with-a-long-random-secret)$' "$env_file"; then
    fail "请先在 .env 中设置 CONTACTBOT_SECRET"
fi
if grep -Eq '^CONTACTBOT_ADMIN_WEB_TOKEN=(|replace-with-an-independent-random-token-at-least-32-characters)$' "$env_file"; then
    fail "请先在 .env 中设置 CONTACTBOT_ADMIN_WEB_TOKEN"
fi
chmod 0600 "$env_file"

data_host=$(env_value CONTACTBOT_DATA_HOST_PATH)
account_host=$(env_value CONTACTBOT_ACCOUNT_HOST_PATH)
rpc_host=$(env_value CONTACTBOT_RPC_HOST_PATH)
data_host=${data_host:-./data}
account_host=${account_host:-./runtime/account}
rpc_host=${rpc_host:-./runtime/bin}
data_host=$(absolute_path "$data_host")
account_host=$(absolute_path "$account_host")
rpc_host=$(absolute_path "$rpc_host")

mkdir -p "$data_host" "$account_host"
[ -d "$rpc_host" ] || fail "RPC 目录不存在：$rpc_host"
[ -n "$(find "$rpc_host" -maxdepth 1 -type f -print -quit)" ] \
    || fail "RPC 目录为空：$rpc_host"

cd "$project_dir"
docker_run compose config --quiet

if docker_run inspect "$bot_container" >/dev/null 2>&1; then
    old_status=$(docker_run inspect --format '{{.State.Status}}' "$bot_container")
    if [ "$old_status" = "running" ]; then
        backup_path=$(docker_run exec "$bot_container" /venv/bin/python -c '
import os
import pathlib
import sqlite3
import time

source = pathlib.Path(os.environ.get("CONTACTBOT_DB", "/app/data/contactbot.sqlite3"))
if not source.is_absolute():
    source = pathlib.Path("/app") / source
if not source.exists():
    print("数据库尚未创建，跳过备份")
else:
    target = source.with_name("%s-before-deploy-%s.sqlite3" % (
        source.stem, time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    ))
    source_db = sqlite3.connect(source)
    target_db = sqlite3.connect(target)
    source_db.backup(target_db)
    target_db.close()
    source_db.close()
    os.chmod(target, 0o600)
    print(target)
')
        echo "数据库备份：$backup_path"
    else
        echo "原机器人容器未运行，跳过在线数据库备份。"
    fi

    current_image=$(docker_run inspect --format '{{.Config.Image}}' "$bot_container")
    if [ -n "$current_image" ]; then
        docker_run image tag "$current_image" "delta-contact-network-bot:pre-deploy-$timestamp"
        echo "旧镜像标签：delta-contact-network-bot:pre-deploy-$timestamp"
    fi
fi

if [ "${CONTACTBOT_NO_CACHE:-0}" = "1" ]; then
    docker_run compose build --no-cache
else
    docker_run compose build
fi
docker_run compose up -d --force-recreate --remove-orphans --no-build

attempt=0
bot_status=""
admin_status=""
while [ "$attempt" -lt 15 ]; do
    bot_status=$(docker_run inspect --format '{{.State.Status}}' "$bot_container" 2>/dev/null || true)
    admin_status=$(docker_run inspect --format '{{.State.Status}}' "$admin_container" 2>/dev/null || true)
    if [ "$bot_status" = "running" ] && [ "$admin_status" = "running" ]; then
        break
    fi
    attempt=$((attempt + 1))
    sleep 2
done

[ "$bot_status" = "running" ] || fail "机器人容器未正常运行"
[ "$admin_status" = "running" ] || fail "管理后台容器未正常运行"

installed_version=$(docker_run exec "$bot_container" /venv/bin/python -c \
    'import contactbot; print(contactbot.__version__)')
admin_port=$(env_value CONTACTBOT_ADMIN_WEB_PORT)
admin_port=${admin_port:-8787}

if command -v curl >/dev/null 2>&1; then
    attempt=0
    admin_status_code=""
    while [ "$attempt" -lt 15 ]; do
        admin_status_code=$(curl -sS -o /dev/null -w '%{http_code}' \
            "http://127.0.0.1:$admin_port/" || true)
        [ "$admin_status_code" = "401" ] && break
        attempt=$((attempt + 1))
        sleep 2
    done
    [ "$admin_status_code" = "401" ] \
        || fail "管理后台鉴权检查失败，HTTP 状态：${admin_status_code:-无法连接}"
fi

error_count=$(
    {
        docker_run logs --since 5m "$bot_container" 2>&1
        docker_run logs --since 5m "$admin_container" 2>&1
    } | grep -Eic 'Traceback|ERROR|CRITICAL|Exception' || true
)
[ "$error_count" -eq 0 ] || fail "启动日志中发现 $error_count 条错误"

echo "部署成功：版本 $installed_version"
echo "机器人容器：$bot_container ($bot_status)"
echo "管理后台：$admin_container ($admin_status，127.0.0.1:$admin_port)"
