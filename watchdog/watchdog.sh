#!/bin/sh
# 浮汐看门狗：跑在宿主网络上，每分钟做两件事
#   1. 微信容器或内核容器被重建 → bridge 还挂在旧的 pid/网络命名空间上 → 重建 bridge
#   2. 读 bridge /healthz → 推送到 Uptime Kuma Push 监控（Kuma 负责 Bark 等通知）
# 另外每天 BACKUP_AT（默认 04:30）把内核数据和桥接状态打包到 /backups（compose 挂到部署目录的 backups/），
# 留最近 BACKUP_KEEP（默认 14）份；备份失败会在 Kuma 里报出来。
# 所需环境变量见 deploy/.env.example；KUMA_PUSH_URL 为空时只写日志。
set -u

INTERVAL="${WATCHDOG_INTERVAL:-60}"
WECHAT="${WECHAT_CONTAINER:-wechat-selkies}"
API="http://127.0.0.1:${FUXI_API_PORT:-8790}"
COMPOSE="docker compose --project-directory /fuxi"
last_status=""
BACKUP_AT="${BACKUP_AT:-04:30}"
BACKUP_KEEP="${BACKUP_KEEP:-14}"
backup_day=""
backup_err=""
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

# 备份：两个卷只读挂在 /src 下；先写 .tmp 再改名，半截文件不会被当成备份
backup_now() {
  [ -d /backups ] && [ -d /src ] || { backup_err="备份目录没挂载"; return 1; }
  stamp=$(date +%Y%m%d-%H%M)
  for v in kernel-data bridge-state; do
    [ -d "/src/$v" ] || continue
    out="/backups/$v-$stamp.tgz"
    if ! tar czf "$out.tmp" -C "/src/$v" . 2>/tmp/backup.err; then
      rm -f "$out.tmp"
      backup_err="备份 $v 失败：$(head -c 200 /tmp/backup.err)"
      log "$backup_err"
      return 1
    fi
    chmod 600 "$out.tmp" && mv "$out.tmp" "$out"
    # 只留最近 BACKUP_KEEP 份
    ls -1t /backups/"$v"-*.tgz 2>/dev/null | tail -n +"$((BACKUP_KEEP + 1))" | xargs -r rm -f
  done
  backup_err=""
  log "备份完成：$(ls -1t /backups/*.tgz | head -2 | xargs -n1 basename | tr '\n' ' ')"
}

maybe_backup() {
  [ -n "$BACKUP_AT" ] || return 0
  today=$(date +%F)
  # 前面补个 1 再按数字比：0430 → 10430，避免前导 0 被当成八进制
  [ "$backup_day" != "$today" ] && [ "1$(date +%H%M)" -ge "1$(echo "$BACKUP_AT" | tr -d :)" ] || return 0
  backup_day=$today
  backup_now
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
  if [ -n "$backup_err" ]; then
    if [ "$status" = up ]; then msg="$backup_err"; else msg="$msg；$backup_err"; fi
    status=down
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

log "看门狗启动：间隔 ${INTERVAL}s，Kuma 推送$([ -n "${KUMA_PUSH_URL:-}" ] && echo 已配置 || echo 未配置)，每天 ${BACKUP_AT:-（不备份）} 备份"
[ "${1:-}" = backup ] && { backup_now; exit $?; }   # docker exec fuxi-watchdog /watchdog.sh backup：立即备份一次
# 启动时如果今天还没有备份（比如 04:30 时容器没在跑），过了时间点就补一次
ls /backups/kernel-data-"$(date +%Y%m%d)"-*.tgz >/dev/null 2>&1 && backup_day=$(date +%F)
while true; do
  maybe_backup
  if check_namespaces; then check_health; fi
  sleep "$INTERVAL"
done
