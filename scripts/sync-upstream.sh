#!/usr/bin/env bash
# 同步上游 whale_girl 内核到 kernel/（见 kernel/UPSTREAM.md）。
#
#   scripts/sync-upstream.sh --check [ref]   只看：上游比 .upstream-commit 新了哪些提交、浮汐依赖的接口文件有没有变
#   scripts/sync-upstream.sh [ref]           同步（默认上游默认分支最新），然后跑测试；不自动提交
#
# 「依赖的接口文件」是桥接和 kernel/fuxi 扩展直接对接的上游文件，变了要对照调整
# bridge/fuxi_bridge/kernel.py、kernel/fuxi/*。
set -euo pipefail

UPSTREAM=https://github.com/Banan1cee/whale_girl
root="$(cd "$(dirname "$0")/.." && pwd)"
CONTRACT=(
  src/adapters/gateway.js
  src/adapters/links.js
  src/persona/prompt.js
  src/http/routes/bridge.js
  src/http/routes/wechat.js
  src/http/routes/persona.js
  src/http/routes/config.js
  src/http/routes/reminders.js
  src/http/routes/care.js
  src/http/routes/stickers.js
  src/http/routes/vision.js
  src/http/router.js
  server.js
)

check=0
if [ "${1:-}" = "--check" ]; then check=1; shift; fi
ref="${1:-}"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
echo "==> 拉取上游 $UPSTREAM ${ref:-（默认分支）}"
git clone -q --filter=blob:none "$UPSTREAM" "$tmp/wg"
[ -n "$ref" ] && git -C "$tmp/wg" checkout -q "$ref"
new=$(git -C "$tmp/wg" rev-parse HEAD)
old=$(cat "$root/kernel/.upstream-commit")

if [ "$new" = "$old" ]; then
  echo "上游没有新提交（$old）"
  exit 0
fi
if ! git -C "$tmp/wg" cat-file -e "$old" 2>/dev/null; then
  echo "!! 上游找不到当前记录的提交 $old（被强推过？），只能整体比对" >&2
  range=""
else
  range="$old..$new"
  echo "==> 上游新提交（$(git -C "$tmp/wg" rev-list --count "$range") 个）"
  git -C "$tmp/wg" log --oneline --no-decorate "$range" | head -30
fi

echo "==> 浮汐依赖的接口文件"
changed=0
for f in "${CONTRACT[@]}"; do
  if [ ! -e "$tmp/wg/$f" ]; then
    echo "  删除或改名：$f"; changed=1
  elif ! cmp -s "$tmp/wg/$f" "$root/kernel/$f" 2>/dev/null; then
    echo "  有变化：$f（$(diff "$root/kernel/$f" "$tmp/wg/$f" 2>/dev/null | grep -c '^[<>]') 行）"; changed=1
  fi
done
[ $changed -eq 0 ] && echo "  都没变"

if [ $check -eq 1 ]; then
  echo "（--check 只看不改。要同步就去掉 --check 再跑一次）"
  exit 0
fi

echo "==> 同步到 kernel/"
rsync -a --delete "$tmp/wg/src" "$root/kernel/"
rsync -a "$tmp/wg/server.js" "$tmp/wg/package.json" "$tmp/wg/config" "$root/kernel/"
rsync -a --exclude stickers "$tmp/wg/assets" "$root/kernel/"
echo "$new" > "$root/kernel/.upstream-commit"
git -C "$root" status --short -- kernel | head -40

echo "==> 跑测试"
if "$root/scripts/test.sh" > "$tmp/test.log" 2>&1; then
  echo "测试通过"
else
  tail -40 "$tmp/test.log"; echo "!! 测试没过" >&2; exit 1
fi
echo "检查上面「有变化」的接口文件，对照调整 bridge/fuxi_bridge/kernel.py 和 kernel/fuxi/*，再构建内核镜像实际跑一遍后提交。"
