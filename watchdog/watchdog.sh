#!/bin/sh
# 浮汐看门狗：跑在宿主网络上，每分钟做两件事
#   1. 微信容器或内核容器被重建 → bridge 还挂在旧的 pid/网络命名空间上 → 重建 bridge
#   2. 读 bridge /healthz → 推送到 Uptime Kuma Push 监控（Kuma 负责 Bark 等通知）
# 所需环境变量见 deploy/.env.example；KUMA_PUSH_URL 为空时只写日志。
set -u

INTERVAL="${WATCHDOG_INTERVAL:-60}"
WECHAT="${WECHAT_CONTAINER:-wechat-selkies}"
API="http://127.0.0.1:${FUXI_API_PORT:-8790}"
COMPOSE="docker compose --project-directory /fuxi"
last_status=""
down_count=0
DOWN_AFTER="${WATCHDOG_DOWN_AFTER:-3}"   # 连续几次异常才报警（部署重启约 30 秒，避免误报）

log() { echo "$(date '+%F %T') $*"; }

push() {  # push <up|down> <msg>
  [ -n "${KUMA_PUSH_URL:-}" ] || return 0
  curl -s -o /dev/null -m 10 -G "$KUMA_PUSH_URL" \
    --data-urlencode "status=$1" --data-urlencode "msg=$2" --data-urlencode "ping=" \
    || log "推送 Kuma 失败"
}

id_of() { docker inspect -f '{{.Id}}' "$1" 2>/dev/null; }
running() { [ "$(docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null)" = "true" ]; }

check_namespaces() {
  wechat_id=$(id_of "$WECHAT"); kernel_id=$(id_of fuxi-kernel)
  [ -n "$wechat_id" ] && [ -n "$kernel_id" ] || return 0
  running "$WECHAT" && running fuxi-kernel || return 0
  pid_mode=$(docker inspect -f '{{.HostConfig.PidMode}}' fuxi-bridge 2>/dev/null)
  net_mode=$(docker inspect -f '{{.HostConfig.NetworkMode}}' fuxi-bridge 2>/dev/null)
  if [ "$pid_mode" != "container:$wechat_id" ] || [ "$net_mode" != "container:$kernel_id" ]; then
    log "检测到微信或内核容器已重建，重建 bridge"
    push down "微信或内核容器已重建，正在重建桥接"
    $COMPOSE up -d --no-deps --force-recreate bridge 2>&1 | tail -2
    return 1
  fi
}

check_health() {
  body=$(curl -s -m 10 -H "Authorization: Bearer ${FUXI_API_TOKEN:-}" "$API/healthz")
  if [ -z "$body" ]; then
    status=down; msg="桥接无响应（可能在启动中或已退出）"
  elif echo "$body" | grep -q '"healthy": true'; then
    status=up; msg="OK"
  else
    status=down
    # 取 problems 数组内容，压成一行
    msg=$(echo "$body" | sed -n 's/.*"problems": \[\(.*\)\], "wechat".*/\1/p' | sed 's/", "/；/g; s/"//g')
    [ -n "$msg" ] || msg="桥接不健康"
  fi
  if [ "$status" = down ]; then down_count=$((down_count + 1)); else down_count=0; fi
  # 熔断立即报警；其他问题连续 DOWN_AFTER 次才报
  if [ "$status" = down ] && [ "$down_count" -lt "$DOWN_AFTER" ] && ! echo "$msg" | grep -q "已熔断"; then
    log "异常观察中（$down_count/$DOWN_AFTER）：$msg"
    push up "观察中：$msg"
    return
  fi
  if [ "$status" != "$last_status" ]; then log "状态 → $status：$msg"; fi
  last_status=$status
  push "$status" "$msg"
}

log "看门狗启动：间隔 ${INTERVAL}s，Kuma 推送$([ -n "${KUMA_PUSH_URL:-}" ] && echo 已配置 || echo 未配置)"
while true; do
  if check_namespaces; then check_health; fi
  sleep "$INTERVAL"
done
