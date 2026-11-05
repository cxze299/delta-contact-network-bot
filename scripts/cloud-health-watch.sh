#!/bin/sh
set -eu
# Restart only a running container that Docker has marked unhealthy.
# Manual stops stay stopped. No user messages or business actions are replayed.
state=$(docker inspect --format '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}' friend-assistant-bun 2>/dev/null) || exit 0
if [ "$state" = "running unhealthy" ]; then
  docker restart --time 60 friend-assistant-bun >/dev/null
fi
