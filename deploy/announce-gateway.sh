#!/bin/sh
# 只给 CI 用的 SSH 入口：这把钥匙登进来只能做一件事 —— 把 stdin 里的公告 JSON 交给 POST /announce。
#
# 装法（docs/api.md「从 CI 调用」）：服务器上 authorized_keys 给 CI 专用公钥加强制命令
#   command="<部署目录>/announce-gateway.sh",restrict ssh-ed25519 AAAA... fuyu-ci
# restrict 关掉端口转发、终端、代理转发等；客户端要求执行的命令（SSH_ORIGINAL_COMMAND）一律忽略，
# 所以浮屿现有的 share-push（ssh 主机 'sh -c …'，JSON 走 stdin）不用改就能用。
#
# 钥匙泄露的最坏情况：往主人的文件传输助手里塞公告预览。公告要主人回 /发布 才会进群（见 announce.py），
# 发不了别的消息，也读不到任何东西。
set -eu
cd "$(dirname "$0")"
umask 077
max=$((20 * 1024 * 1024))
body=$(mktemp)
cfg=$(mktemp)
trap 'rm -f "$body" "$cfg"' EXIT
head -c $((max + 1)) > "$body"
if [ "$(wc -c < "$body")" -gt "$max" ]; then
  echo '{"ok": false, "error": "body 超过 20MB"}'
  exit 1
fi
printf 'header = "Authorization: Bearer %s"\n' "$(sed -n 's/^FUXI_API_TOKEN=//p' .env)" > "$cfg"
port=$(sed -n 's/^FUXI_API_PORT=//p' .env)
logger -t fuxi-announce-gateway "CI 公告请求 $(wc -c < "$body") 字节" 2>/dev/null || true
curl -sS --max-time 300 -K "$cfg" -X POST "http://127.0.0.1:${port:-8790}/announce" \
  -H "Content-Type: application/json" --data-binary @"$body"
