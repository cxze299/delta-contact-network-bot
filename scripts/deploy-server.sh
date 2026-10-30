#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
docker_bin=${CONTACTBOT_DOCKER_BIN:-docker}
"$docker_bin" compose config --quiet
if "$docker_bin" inspect friend-assistant-bun >/dev/null 2>&1; then
  "$docker_bin" compose exec -T contact-bot sh scripts/backup_db.sh
fi
"$docker_bin" compose build
"$docker_bin" compose up -d --no-build
attempt=0
while [ "$attempt" -lt 30 ]; do
  if "$docker_bin" compose exec -T contact-bot bun -e "process.exit((await fetch('http://127.0.0.1:8787/api/health')).ok?0:1)"; then
    printf '好友助手已启动\n'
    exit 0
  fi
  attempt=$((attempt + 1))
  sleep 2
done
printf '服务尚未通过健康检查，请检查容器状态\n' >&2
exit 1
