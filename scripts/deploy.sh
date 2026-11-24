#!/usr/bin/env bash
# 在本机构建镜像并部署到服务器（服务器拉 Docker Hub 慢，所以本机构建再传）。
#
#   scripts/deploy.sh [bridge|kernel|watchdog|all]     构建并部署（默认 all）
#   scripts/deploy.sh rollback <服务> [版本]            退回上一版（或指定版本），不重新构建
#   scripts/deploy.sh versions                         服务器上的部署记录和可回滚的版本
#   scripts/deploy.sh backups [本机目录]                立即备份一次，并把服务器上的备份拉回本机（默认 ~/fuxi-backups）
#
# 需要环境变量 FUXI_HOST（ssh 别名）、FUXI_DIR（服务器上的部署目录），不写进仓库。需要本机 Docker 在运行。
#
# 版本：镜像标签用 git 提交号（如 fuxi-bridge:941eb30）；工作区有没提交的改动时加 -dirty<月日时分>。
# compose 一直用 :dev，部署 / 回滚就是把 :dev 指向某个版本再重建容器。
# 服务器上的 $FUXI_DIR/deploys.log 记每次部署（时间 服务 版本 动作），每个服务保留最近 KEEP 个版本镜像。
set -euo pipefail

: "${FUXI_HOST:?请设置 FUXI_HOST（ssh 别名）}"
: "${FUXI_DIR:?请设置 FUXI_DIR（服务器上的部署目录）}"
root="$(cd "$(dirname "$0")/.." && pwd)"
KEEP=5
LOG="$FUXI_DIR/deploys.log"

usage() { sed -n '4,7p' "$0" | sed 's/^# *//' >&2; exit 2; }

# 连服务器：传完大镜像后 ssh 偶尔被断（Connection closed，退出码 255），连接失败就等几秒重试；
# 命令本身失败（其他退出码）不重试。这里跑的步骤都能重复执行。
remote() {
  local n=1 rc
  while :; do
    "$@" && return 0 || rc=$?
    [ "$rc" -eq 255 ] && [ "$n" -lt 4 ] || return "$rc"
    echo "!! 连接服务器断了，${n}0 秒后重试（$n/3）" >&2
    sleep "${n}0"
    n=$((n + 1))
  done
}

services_of() {
  case "$1" in
    bridge|kernel|watchdog) echo "$1" ;;
    all) echo "kernel bridge watchdog" ;;
    *) usage ;;
  esac
}

# 重建容器：桥接共用内核的网络命名空间，内核重建后桥接必须跟着重建，否则还挂在旧的命名空间上
restart() {
  local what="$1"
  case "$what" in
    bridge|watchdog) remote ssh "$FUXI_HOST" "cd '$FUXI_DIR' && docker compose up -d --no-deps --force-recreate $what" ;;
    kernel) remote ssh "$FUXI_HOST" "cd '$FUXI_DIR' && docker compose up -d --force-recreate kernel && docker compose up -d --no-deps --force-recreate bridge" ;;
    all) remote ssh "$FUXI_HOST" "cd '$FUXI_DIR' && docker compose up -d --force-recreate kernel && docker compose up -d --no-deps --force-recreate bridge && docker compose up -d --no-deps --force-recreate watchdog" ;;
  esac
}

wait_ready() {
  echo "==> 等待桥接就绪（首次扫描密钥约 30 秒）"
  remote ssh "$FUXI_HOST" "for i in \$(seq 1 40); do docker logs --since 2m fuxi-bridge 2>&1 | grep -q '开始监听' && break; sleep 3; done
    docker ps --filter name=fuxi --format '{{.Names}}\t{{.Status}}\t{{.Image}}'
    docker logs --since 2m fuxi-bridge 2>&1 | grep -E '解出密钥|白名单已|开始监听|ERROR|CRITICAL' | tail -5 | sed -E 's/wxid_[a-z0-9]+/wxid_<id>/g'"
}

# 服务器上：把 :dev 指向版本 $2，记日志；第一次用版本部署时，把原来没有版本号的 :dev 存成 initial 方便回滚
point_dev() {
  local svc="$1" tag="$2" action="$3"
  remote ssh "$FUXI_HOST" "set -e
    img=fuxi-$svc
    cur=\$(docker image inspect -f '{{.Id}}' \$img:dev 2>/dev/null || true)
    if [ -n \"\$cur\" ] && ! grep -q \" $svc \" '$LOG' 2>/dev/null; then
      docker tag \$img:dev \$img:initial
      echo \"\$(date '+%F %T') $svc initial 原有版本\" >> '$LOG'
    fi
    docker tag \$img:$tag \$img:dev
    echo \"\$(date '+%F %T') $svc $tag $action\" >> '$LOG'
    # 只留最近 $KEEP 个版本（按部署记录从新到旧；正在用的 :dev 指向的那个不会被删掉）
    keep=\$(grep \" $svc \" '$LOG' | awk '{print \$4}' | tac | awk '!s[\$0]++' | head -n $KEEP)
    for t in \$(docker images \$img --format '{{.Tag}}' | grep -vx dev); do
      echo \"\$keep\" | grep -qx \"\$t\" || docker rmi -f \$img:\$t >/dev/null 2>&1 || true
    done"
}

deploy() {
  local what="$1" svcs tag images=()
  svcs=$(services_of "$what")
  tag=$(git -C "$root" rev-parse --short=7 HEAD)
  if [ -n "$(git -C "$root" status --porcelain -- bridge kernel watchdog deploy)" ]; then
    tag="$tag-dirty$(date +%m%d%H%M)"
    echo "!! 工作区有没提交的改动（bridge/ kernel/ watchdog/ deploy/），版本记为 $tag" >&2
  fi
  for s in $svcs; do
    echo "==> 构建 fuxi-$s:${tag}（linux/amd64）"
    docker build -q --platform linux/amd64 --build-arg FUXI_BUILD="$tag" --label "fuxi.version=$tag" \
      -t "fuxi-$s:$tag" "$root/$s" >/dev/null
    images+=("fuxi-$s:$tag")
  done

  local send=()
  for i in "${images[@]}"; do
    remote ssh "$FUXI_HOST" "docker image inspect $i >/dev/null 2>&1" && echo "==> 服务器上已有 $i，不再传输" || send+=("$i")
  done
  if [ "${#send[@]}" -gt 0 ]; then
    echo "==> 传输镜像到 $FUXI_HOST"
    docker save "${send[@]}" | gzip -1 | ssh "$FUXI_HOST" docker load
  fi

  echo "==> 更新 compose.yaml"
  remote ssh "$FUXI_HOST" "mkdir -p '$FUXI_DIR' && chmod 700 '$FUXI_DIR'"
  remote scp -q "$root/deploy/compose.yaml" "$FUXI_HOST:$FUXI_DIR/compose.yaml"
  remote ssh "$FUXI_HOST" "test -f '$FUXI_DIR/.env'" || {
    echo "!! 服务器上还没有 $FUXI_DIR/.env：按 deploy/.env.example 创建（chmod 600）后再运行本脚本" >&2
    exit 1
  }
  for s in $svcs; do point_dev "$s" "$tag" deploy; done

  echo "==> 启动（版本 ${tag}）"
  restart "$what"
  wait_ready
}

rollback() {
  local svc="${1:-}" want="${2:-}"
  case "$svc" in bridge|kernel|watchdog) ;; *) usage ;; esac
  if [ -z "$want" ]; then
    # 当前版本之前、镜像还在的最近一个
    want=$(ssh "$FUXI_HOST" "grep ' $svc ' '$LOG' 2>/dev/null | awk '{print \$4}' | tac | awk '!s[\$0]++' | sed -n '2,\$p' |
      while read t; do docker image inspect fuxi-$svc:\$t >/dev/null 2>&1 && { echo \$t; break; }; done")
    [ -n "$want" ] || { echo "!! 没有可回滚的 $svc 版本（scripts/deploy.sh versions 看看）" >&2; exit 1; }
  fi
  ssh "$FUXI_HOST" "docker image inspect fuxi-$svc:$want >/dev/null 2>&1" || {
    echo "!! 服务器上没有 fuxi-$svc:$want" >&2; exit 1; }
  echo "==> $svc 回滚到 $want"
  point_dev "$svc" "$want" rollback
  restart "$svc"
  wait_ready
}

versions() {
  ssh "$FUXI_HOST" "echo '== 最近的部署（时间 服务 版本 动作）'; tail -n 15 '$LOG' 2>/dev/null || echo '（还没有记录）'
    echo; echo '== 服务器上的版本镜像（dev = 正在用的）'
    for s in kernel bridge watchdog; do
      dev=\$(docker image inspect -f '{{.Id}}' fuxi-\$s:dev 2>/dev/null | cut -c8-19)
      echo \"fuxi-\$s: \$(docker images fuxi-\$s --format '{{.Tag}}={{.ID}}' | grep -v '^dev=' |
        sed \"s/=\$dev\\\$/ (当前)/; s/=.*//\" | tr '\n' ' ')\"
    done"
}

# 备份里有记忆、人设和 config.json（含模型密钥），只拉到仓库外的目录，权限 700
backups() {
  local dest="${1:-$HOME/fuxi-backups}"
  case "$dest" in "$root"|"$root"/*) echo "!! 备份不能放在仓库里" >&2; exit 1 ;; esac
  echo "==> 服务器上立即备份一次"
  remote ssh "$FUXI_HOST" "docker exec fuxi-watchdog /watchdog.sh backup | tail -1"
  mkdir -p "$dest" && chmod 700 "$dest"
  echo "==> 拉回 $dest（只拉本机还没有的）"
  local have
  have=$(ls "$dest" 2>/dev/null || true)
  for f in $(ssh "$FUXI_HOST" "ls -1 '$FUXI_DIR/backups' 2>/dev/null | grep '\\.tgz\$'"); do
    echo "$have" | grep -qx "$f" && continue
    remote scp -q "$FUXI_HOST:$FUXI_DIR/backups/$f" "$dest/$f" && chmod 600 "$dest/$f" && echo "  $f"
  done
  ls -1t "$dest" | head -4
}

case "${1:-all}" in
  rollback) shift; rollback "$@" ;;
  backups) shift; backups "$@" ;;
  versions) versions ;;
  *) deploy "${1:-all}" ;;
esac
