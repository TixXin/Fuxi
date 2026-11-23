#!/usr/bin/env bash
# 提交前检查：拦截疑似密钥、IP、真实 wxid 进入仓库。
# 用法：scripts/check-secrets.sh [文件...]；不带参数时检查暂存区。
set -euo pipefail

if [ $# -gt 0 ]; then
  files=("$@")
else
  files=()
  while IFS= read -r f; do files+=("$f"); done < <(git diff --cached --name-only --diff-filter=ACM)
fi
[ ${#files[@]} -eq 0 ] && exit 0

fail=0
for f in "${files[@]}"; do
  [ -f "$f" ] || continue
  case "$f" in LICENSE|*.png|*.jpg|*.ico) continue ;; esac
  case "$(basename "$f")" in
    .env|*.db|*.db-wal|keys.json) echo "禁止提交：$f"; fail=1; continue ;;
  esac
  # 64 位以上连续十六进制（SQLCipher 密钥形态）；掩码常量按两段 32 位写，不会命中
  if grep -nE '\b[0-9a-fA-F]{64,}\b' "$f" >/dev/null; then
    echo "疑似密钥（64+ 位十六进制）：$f"; grep -nE '\b[0-9a-fA-F]{64,}\b' "$f" | head -3; fail=1
  fi
  # IPv4（放行回环、0.0.0.0、私有示例段写法 x.x.x.x）
  if grep -nE '\b([0-9]{1,3}\.){3}[0-9]{1,3}\b' "$f" | grep -vE '\b(127\.0\.0\.1|0\.0\.0\.0)\b' >/dev/null; then
    echo "疑似 IP 地址：$f"; grep -nE '\b([0-9]{1,3}\.){3}[0-9]{1,3}\b' "$f" | grep -vE '\b(127\.0\.0\.1|0\.0\.0\.0)\b' | head -3; fail=1
  fi
  # 真实 wxid（示例请写 wxid_<id>）
  if grep -oE 'wxid_[a-z0-9]{8,}' "$f" | grep -vqE '^wxid_x+$'; then
    echo "疑似真实 wxid：$f"; fail=1
  fi
  # API key 形态
  if grep -nE 'sk-[A-Za-z0-9]{20,}' "$f" >/dev/null; then
    echo "疑似 API key：$f"; fail=1
  fi
done

if [ $fail -ne 0 ]; then
  echo "提交被拦截：请移除上述内容（机密只放服务器 .env / ~/Documents）。" >&2
  exit 1
fi
