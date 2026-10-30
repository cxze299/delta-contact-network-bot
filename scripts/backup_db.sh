#!/bin/sh
set -eu
umask 077
cd /app
exec bun scripts/backup-db.ts
