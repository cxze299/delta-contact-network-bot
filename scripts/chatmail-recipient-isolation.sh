#!/bin/sh
set -eu
[ "$(docker inspect --format '{{.State.Running}}' chatmail 2>/dev/null)" = true ] || exit 0
changed=0
for setting in lmtp-filtermail_destination_recipient_limit=1 lmtp-filtermail_destination_concurrency_limit=1; do
 key=${setting%=*}
 value=${setting#*=}
 current=$(docker exec chatmail postconf -h "$key")
 if [ "$current" != "$value" ]; then
  docker exec chatmail postconf -e "$setting"
  changed=1
 fi
done
if [ "$changed" = 1 ]; then
 docker exec chatmail postfix check
 docker exec chatmail postfix reload
fi
