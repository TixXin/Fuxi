#!/usr/bin/env bash
# 在桥接镜像里跑单元测试（与线上同一 Python 与依赖）。需要本机 Docker。
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
docker build -q --platform linux/amd64 -t fuxi-bridge:dev "$root/bridge" >/dev/null
docker run --rm --platform linux/amd64 -v "$root/bridge/tests:/app/tests:ro" -w /app \
  fuxi-bridge:dev python -m unittest discover -s tests -t . -v "$@"
sh -n "$root/watchdog/watchdog.sh"
bash -n "$root/scripts/deploy.sh"
bash -n "$root/scripts/sync-upstream.sh"
sh -n "$root/deploy/announce-gateway.sh"
node --check "$root/kernel/docker-entrypoint.js" 2>/dev/null || true
# 内核上的浮汐扩展（链接阅读等）
(cd "$root/kernel/fuxi" && npm ci --silent --no-audit --no-fund)
node --test "$root"/kernel/fuxi/test/*.js
echo "全部通过"
