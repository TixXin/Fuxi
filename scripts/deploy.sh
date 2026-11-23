#!/usr/bin/env bash
# 在本机构建镜像并部署到服务器（服务器拉 Docker Hub 慢，所以本机构建再传）。
#
#   FUXI_HOST=<ssh 别名> FUXI_DIR=<服务器上的部署目录> scripts/deploy.sh [bridge|kernel|watchdog|all]
#
# 服务器地址与目录只从环境变量读，不写进仓库。需要本机 Docker 在运行。
set -euo pipefail

: "${FUXI_HOST:?请设置 FUXI_HOST（ssh 别名）}"
: "${FUXI_DIR:?请设置 FUXI_DIR（服务器上的部署目录）}"
what="${1:-all}"
root="$(cd "$(dirname "$0")/.." && pwd)"

case "$what" in
  bridge) services=(bridge) ;;
  kernel) services=(kernel) ;;
  watchdog) services=(watchdog) ;;
  all) services=(kernel bridge watchdog) ;;
  *) echo "用法：$0 [bridge|kernel|watchdog|all]" >&2; exit 2 ;;
esac

images=()
for s in "${services[@]}"; do
  echo "==> 构建 fuxi-$s:dev（linux/amd64）"
  docker build -q --platform linux/amd64 -t "fuxi-$s:dev" "$root/$s" >/dev/null
  images+=("fuxi-$s:dev")
done

echo "==> 传输镜像到 $FUXI_HOST"
docker save "${images[@]}" | gzip -1 | ssh "$FUXI_HOST" docker load

echo "==> 更新 compose.yaml"
ssh "$FUXI_HOST" "mkdir -p '$FUXI_DIR' && chmod 700 '$FUXI_DIR'"
scp -q "$root/deploy/compose.yaml" "$FUXI_HOST:$FUXI_DIR/compose.yaml"
ssh "$FUXI_HOST" "test -f '$FUXI_DIR/.env'" || {
  echo "!! 服务器上还没有 $FUXI_DIR/.env：按 deploy/.env.example 创建（chmod 600）后再运行本脚本" >&2
  exit 1
}

echo "==> 启动"
if [ "$what" = bridge ] || [ "$what" = watchdog ]; then
  ssh "$FUXI_HOST" "cd '$FUXI_DIR' && docker compose up -d --no-deps $what"
elif [ "$what" = kernel ]; then
  ssh "$FUXI_HOST" "cd '$FUXI_DIR' && docker compose up -d kernel && docker compose up -d --no-deps --force-recreate bridge"
else
  # 桥接共用内核的网络命名空间：内核重建后桥接必须跟着重建，否则还挂在旧的命名空间上
  ssh "$FUXI_HOST" "cd '$FUXI_DIR' && docker compose up -d kernel && docker compose up -d --no-deps --force-recreate bridge && docker compose up -d --no-deps watchdog"
fi

echo "==> 等待桥接就绪（首次扫描密钥约 30 秒）"
ssh "$FUXI_HOST" "for i in \$(seq 1 40); do docker logs --since 2m fuxi-bridge 2>&1 | grep -q '开始监听' && break; sleep 3; done
  docker ps --filter name=fuxi --format '{{.Names}}\t{{.Status}}'
  docker logs --since 2m fuxi-bridge 2>&1 | grep -E '解出密钥|白名单已|开始监听|ERROR|CRITICAL' | tail -5 | sed -E 's/wxid_[a-z0-9]+/wxid_<id>/g'"
