#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
mkdir -p dist
bun scripts/build-ui.ts
bun scripts/pack.ts "${1:-dist/好友助手.xdc}"
