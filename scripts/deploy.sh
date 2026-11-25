#!/usr/bin/env bash
# 在本机构建镜像并部署到服务器（服务器拉 Docker Hub 慢，所以本机构建再传）。
#
#   scripts/deploy.sh [bridge|kernel|watchdog|all]     构建并部署（默认 all）
#   scripts/deploy.sh rollback <服务> [版本]            退回上一版（或指定版本），不重新构建
#   scripts/deploy.sh versions                         服务器上的部署记录和可回滚的版本
#   scripts/deploy.sh backups [本机目录]                立即备份一次，并把服务器上的备份拉回本机（默认 ~/fuxi-backups）
#
# 环境变量：
#   FUXI_HOST       ssh 别名（必填，不写进仓库）
#   FUXI_DIR        服务器上的部署目录（必填，不写进仓库）
#   FUXI_VERBOSE=1  输出完整构建日志（默认单行动态进度，失败时自动展开末尾日志）
#   NO_COLOR=1      禁用颜色输出
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
VERBOSE="${FUXI_VERBOSE:-0}"

# ---------- 终端样式与日志工具 ----------
IS_TTY=0
if [ -t 1 ] && [ "${TERM:-dumb}" != "dumb" ]; then
  IS_TTY=1
fi
USE_COLOR=0
if [ "$IS_TTY" -eq 1 ] && [ -z "${NO_COLOR:-}" ]; then
  USE_COLOR=1
fi

if [ "$USE_COLOR" -eq 1 ]; then
  C_RESET=$'\033[0m'
  C_BOLD=$'\033[1m'
  C_DIM=$'\033[2m'
  C_CYAN=$'\033[36m'
  C_GREEN=$'\033[32m'
  C_YELLOW=$'\033[33m'
  C_RED=$'\033[31m'
else
  C_RESET="" C_BOLD="" C_DIM="" C_CYAN="" C_GREEN="" C_YELLOW="" C_RED=""
fi

TMP_FILES=()
cleanup() {
  local f
  if [ "${#TMP_FILES[@]}" -gt 0 ]; then
    for f in "${TMP_FILES[@]}"; do
      [ -n "$f" ] && rm -f "$f" 2>/dev/null || true
    done
  fi
}
trap cleanup EXIT
trap 'printf "\n"; exit 130' INT TERM

mktemp_tracked() {
  local f
  f=$(mktemp)
  TMP_FILES+=("$f")
  echo "$f"
}

fmt_dur() {
  local s="${1:-0}"
  if [ "$s" -ge 60 ]; then
    printf "%dm %02ds" "$((s / 60))" "$((s % 60))"
  else
    printf "%ds" "$s"
  fi
}

fmt_bytes() {
  python3 -c '
import sys
b = float(sys.argv[1])
for u in ("B", "KB", "MB", "GB"):
    if b < 1024 or u == "GB":
        print(f"{b:.1f} {u}" if u != "B" else f"{int(b)} B")
        break
    b /= 1024
' "${1:-0}"
}

hr() {
  printf "%s━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━%s\n" "$C_DIM" "$C_RESET"
}

step() {
  local cur="$1" total="$2" title="$3"
  printf "\n%s[%s/%s]%s %s%s%s\n" "$C_CYAN$C_BOLD" "$cur" "$total" "$C_RESET" "$C_BOLD" "$title" "$C_RESET"
}

log_ok()   { printf "  %s✓%s %s\n" "$C_GREEN" "$C_RESET" "$*"; }
log_info() { printf "  %s·%s %s\n" "$C_DIM" "$C_RESET" "$*"; }
log_warn() { printf "  %s!%s %s%s%s\n" "$C_YELLOW" "$C_RESET" "$C_YELLOW" "$*" "$C_RESET" >&2; }
log_err()  { printf "  %s✗%s %s%s%s\n" "$C_RED" "$C_RESET" "$C_RED" "$*" "$C_RESET" >&2; }

# 单行刷新输出（TTY 用 \r\033[K 原地覆盖；非 TTY 静默跳过，避免刷屏）
status_line() {
  if [ "$IS_TTY" -eq 1 ]; then
    printf "\r\033[K  %s" "$1"
  fi
}

clear_line() {
  if [ "$IS_TTY" -eq 1 ]; then
    printf "\r\033[K"
  fi
}

usage() { sed -n '4,7p' "$0" | sed 's/^# *//' >&2; exit 2; }

# 连服务器：传完大镜像后 ssh 偶尔被断（Connection closed，退出码 255），连接失败就等几秒重试；
# 命令本身失败（其他退出码）不重试。这里跑的步骤都能重复执行。
remote() {
  local n=1 rc
  while :; do
    "$@" && return 0 || rc=$?
    [ "$rc" -eq 255 ] && [ "$n" -lt 4 ] || return "$rc"
    clear_line
    log_warn "连接服务器断了，${n}0 秒后重试（${n}/3）"
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

# 单行进度构建镜像：解析 BuildKit plain 日志，只原地更新一行；失败时展开末尾错误日志
build_service() {
  local svc="$1" tag="$2" img="fuxi-$1:$2"
  local t0 logf rc=0
  t0=$(date +%s)
  logf=$(mktemp_tracked)

  if [ "$VERBOSE" = "1" ]; then
    log_info "构建 ${img}（linux/amd64，详细日志模式）"
    docker build --progress=plain --platform linux/amd64 \
      --build-arg FUXI_BUILD="$tag" --label "fuxi.version=$tag" \
      -t "$img" "$root/$svc"
    local dt=$(($(date +%s) - t0))
    local sz
    sz=$(fmt_bytes "$(docker image inspect -f '{{.Size}}' "$img" 2>/dev/null || echo 0)")
    log_ok "${img} 构建完成（大小 ${sz}，耗时 $(fmt_dur "$dt")）"
    return 0
  fi

  local build_py
  read -r -d '' build_py <<'PY' || true
import re, sys, time

img = sys.argv[1]
is_tty = sys.argv[2] == "1"
use_color = sys.argv[3] == "1"
c_cyan = "\033[36m" if use_color else ""
c_dim = "\033[2m" if use_color else ""
c_reset = "\033[0m" if use_color else ""

frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
t0 = time.monotonic()
step_re = re.compile(r"^#\d+\s+(?:\[([^\]]+)\]\s+)?(.+)$")
cur_stage = "准备构建上下文…"
last_draw = 0.0
tick = 0

def draw(force=False):
    global last_draw, tick
    if not is_tty:
        return
    now = time.monotonic()
    if not force and now - last_draw < 0.12:
        return
    last_draw = now
    sp = frames[tick % len(frames)]
    tick += 1
    el = int(now - t0)
    desc = cur_stage if len(cur_stage) <= 56 else cur_stage[:53] + "…"
    sys.stdout.write(f"\r\033[K  {c_cyan}{sp}{c_reset} {img}  {desc}  {c_dim}({el}s){c_reset}")
    sys.stdout.flush()

draw(force=True)
for raw in sys.stdin:
    line = raw.strip()
    if not line:
        continue
    if line.endswith("CACHED"):
        draw()
        continue
    m = step_re.match(line)
    if m:
        stage, body = m.group(1), m.group(2)
        if body.startswith(("DONE ", "naming to ", "writing image ")):
            if body.startswith("naming to "):
                cur_stage = "写入镜像索引…"
            draw()
            continue
        if stage:
            cur_stage = f"[{stage}] {body}"
        elif not body.startswith(("transferring ", "load ", "sha256:")):
            cur_stage = body
        draw()

if is_tty:
    sys.stdout.write("\r\033[K")
    sys.stdout.flush()
PY

  set +e
  docker build --progress=plain --platform linux/amd64 \
    --build-arg FUXI_BUILD="$tag" --label "fuxi.version=$tag" \
    -t "$img" "$root/$svc" 2>&1 | tee "$logf" | python3 -c "$build_py" "$img" "$IS_TTY" "$USE_COLOR"
  local ps=("${PIPESTATUS[@]}")
  rc=${ps[0]}
  set -e

  local dt=$(($(date +%s) - t0))
  if [ "$rc" -ne 0 ]; then
    clear_line
    log_err "${img} 构建失败（退出码 ${rc}，耗时 $(fmt_dur "$dt")），最后 40 行日志："
    tail -n 40 "$logf" | sed 's/^/    /' >&2
    return "$rc"
  fi

  local sz cached_cnt
  sz=$(fmt_bytes "$(docker image inspect -f '{{.Size}}' "$img" 2>/dev/null || echo 0)")
  cached_cnt=$(grep -c ' CACHED$' "$logf" 2>/dev/null || true)
  if [ "${cached_cnt:-0}" -gt 0 ]; then
    log_ok "${img}  大小 ${sz}  缓存复用 ${cached_cnt} 步  耗时 $(fmt_dur "$dt")"
  else
    log_ok "${img}  大小 ${sz}  耗时 $(fmt_dur "$dt")"
  fi
}

# 打包 + gzip 压缩 + SSH 传输 + 远端 docker load，单行进度条实时显示百分比、已传大小、压缩比、速率与剩余时间
transfer_images() {
  local send=("$@")
  local total_bytes=0 img sz load_log t0 rc=0
  for img in "${send[@]}"; do
    sz=$(docker image inspect -f '{{.Size}}' "$img" 2>/dev/null || echo 0)
    total_bytes=$((total_bytes + sz))
  done
  load_log=$(mktemp_tracked)
  t0=$(date +%s)

  local transfer_py
  read -r -d '' transfer_py <<'PY' || true
import sys, time, zlib

total = max(1, int(sys.argv[1]))
is_tty = sys.argv[2] == "1"
use_color = sys.argv[3] == "1"

c_cyan = "\033[36m" if use_color else ""
c_dim = "\033[2m" if use_color else ""
c_reset = "\033[0m" if use_color else ""

def fmt_mb(n):
    return f"{n / (1024 * 1024):.1f} MB"

def fmt_eta(sec):
    s = max(0, int(sec))
    return f"{s // 60}m{s % 60:02d}s" if s >= 60 else f"{s}s"

comp = zlib.compressobj(level=1, method=zlib.DEFLATED, wbits=16 + zlib.MAX_WBITS)
stdin = sys.stdin.buffer
stdout = sys.stdout.buffer
raw_in = 0
wire_out = 0
t0 = time.monotonic()
last_draw = 0.0
last_milestone = 0
bar_w = 24

def render(done=False):
    global last_draw, last_milestone
    now = time.monotonic()
    dt = max(0.05, now - t0)
    pct = 100.0 if done else min(99.9, raw_in * 100.0 / total)
    wire_rate = wire_out / dt
    raw_rate = raw_in / dt
    eta = "" if done or raw_rate <= 0 else f"  剩余 {fmt_eta(max(0.0, total - raw_in) / raw_rate)}"

    if is_tty:
        if not done and now - last_draw < 0.15:
            return
        last_draw = now
        filled = int(bar_w * pct / 100.0)
        bar = "█" * filled + "░" * (bar_w - filled)
        tail = "  等待服务器解包加载…" if done else eta
        line = (
            f"\r\033[K  {c_cyan}[{bar}]{c_reset} {pct:5.1f}%  "
            f"原始 {fmt_mb(raw_in)}/{fmt_mb(total)}  "
            f"实传 {fmt_mb(wire_out)} ({fmt_mb(wire_rate)}/s){c_dim}{tail}{c_reset}"
        )
        sys.stderr.write(line)
        sys.stderr.flush()
    else:
        ms = int(pct // 25) * 25
        if (ms >= last_milestone + 25 and ms < 100) or done:
            last_milestone = ms
            label = "100%（等待服务器加载）" if done else f"{ms}%"
            sys.stderr.write(
                f"  · 传输进度 {label}  原始 {fmt_mb(raw_in)}/{fmt_mb(total)}  "
                f"实传 {fmt_mb(wire_out)} ({fmt_mb(wire_rate)}/s)\n"
            )
            sys.stderr.flush()

render()
try:
    while True:
        chunk = stdin.read(262144)
        if not chunk:
            break
        raw_in += len(chunk)
        out = comp.compress(chunk)
        if out:
            stdout.write(out)
            stdout.flush()
            wire_out += len(out)
        render()
    tail = comp.flush()
    if tail:
        stdout.write(tail)
        stdout.flush()
        wire_out += len(tail)
    render(done=True)
finally:
    try:
        stdout.close()
    except Exception:
        pass
PY

  set +e
  docker save "${send[@]}" | python3 -c "$transfer_py" "$total_bytes" "$IS_TTY" "$USE_COLOR" | ssh "$FUXI_HOST" docker load >"$load_log" 2>&1
  local ps=("${PIPESTATUS[@]}")
  rc=${ps[0]}
  local py_rc=${ps[1]:-0} ssh_rc=${ps[2]:-0}
  set -e

  clear_line
  local dt=$(($(date +%s) - t0))
  if [ "$rc" -ne 0 ] || [ "$py_rc" -ne 0 ] || [ "$ssh_rc" -ne 0 ]; then
    log_err "传输或加载镜像失败（save=${rc}, compress=${py_rc}, ssh/load=${ssh_rc}，耗时 $(fmt_dur "$dt")）"
    [ -s "$load_log" ] && sed 's/^/    /' "$load_log" >&2
    return 1
  fi

  local loaded
  loaded=$(grep -c 'Loaded image:' "$load_log" 2>/dev/null || true)
  log_ok "已传输并加载 ${loaded:-${#send[@]}} 个镜像到 ${FUXI_HOST}（原始 $(fmt_bytes "$total_bytes")，耗时 $(fmt_dur "$dt")）"
  if [ -s "$load_log" ]; then
    grep 'Loaded image:' "$load_log" | sed 's/^/    /' || true
  fi
}

# 重建容器：桥接共用内核的网络命名空间，内核重建后桥接必须跟着重建，否则还挂在旧的命名空间上
restart() {
  local what="$1" cmd t0 logf rc=0
  t0=$(date +%s)
  logf=$(mktemp_tracked)
  case "$what" in
    bridge|watchdog)
      cmd="cd '$FUXI_DIR' && docker compose up -d --no-deps --force-recreate $what"
      ;;
    kernel)
      cmd="cd '$FUXI_DIR' && docker compose up -d --force-recreate kernel && docker compose up -d --no-deps --force-recreate bridge"
      ;;
    all)
      cmd="cd '$FUXI_DIR' && docker compose up -d --force-recreate kernel && docker compose up -d --no-deps --force-recreate bridge && docker compose up -d --no-deps --force-recreate watchdog"
      ;;
  esac

  status_line "${C_CYAN}⠋${C_RESET} 正在重建并启动容器（${what}）…"
  set +e
  remote ssh "$FUXI_HOST" "$cmd" >"$logf" 2>&1
  rc=$?
  set -e
  clear_line
  local dt=$(($(date +%s) - t0))

  if [ "$rc" -ne 0 ]; then
    log_err "容器重建失败（退出码 ${rc}，耗时 $(fmt_dur "$dt")）："
    sed 's/^/    /' "$logf" >&2
    return "$rc"
  fi
  log_ok "容器已重建启动（目标: ${what}，耗时 $(fmt_dur "$dt")）"
  # 只打印简洁的容器状态变更行（去重且屏蔽多余进度帧）
  grep -E 'Container .* (Started|Recreated|Running)' "$logf" 2>/dev/null | sed 's/^[[:space:]]*/    /' || true
}

# 单连接实时轮询启动阶段，单行原地刷新，就绪后展示容器状态与关键启动日志
wait_ready() {
  local what="${1:-all}" max_sec=120 t0 ready=0 last_stage=""
  t0=$(date +%s)

  if [ "$what" = "watchdog" ]; then
    sleep 2
    log_ok "watchdog 容器已启动"
    printf "\n  %s容器状态：%s\n" "$C_BOLD" "$C_RESET"
    remote ssh "$FUXI_HOST" "docker ps --filter name=fuxi --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}'" | sed 's/^/    /'
    return 0
  fi

  if [ "$IS_TTY" -eq 0 ]; then
    log_info "等待桥接就绪（首次扫描微信内存密钥约 30-45 秒）…"
  fi

  # 单条 SSH 连接每 2 秒回传当前启动阶段，避免反复握手
  while IFS='|' read -r code stage detail; do
    local dt=$(($(date +%s) - t0))
    if [ "$code" = "READY" ]; then
      ready=1
      clear_line
      log_ok "桥接已就绪并开始监听（耗时 $(fmt_dur "$dt")）"
      break
    elif [ "$code" = "TIMEOUT" ]; then
      clear_line
      log_warn "等待超过 ${max_sec}s 仍未看到「开始监听」（当前阶段: ${stage}），请检查下方日志"
      break
    else
      if [ "$IS_TTY" -eq 1 ]; then
        local pct=$((dt * 100 / max_sec))
        [ "$pct" -gt 99 ] && pct=99
        local spin_idx=$(( (dt / 2) % 4 ))
        local spin
        case "$spin_idx" in
          0) spin="⠋" ;; 1) spin="⠙" ;; 2) spin="⠸" ;; *) spin="⠴" ;;
        esac
        status_line "${C_CYAN}${spin}${C_RESET} 等待就绪  ${stage}${detail:+（${detail}）}  ${C_DIM}(${dt}s / ${max_sec}s)${C_RESET}"
      elif [ "$stage" != "$last_stage" ]; then
        last_stage="$stage"
        log_info "启动阶段：${stage}${detail:+（${detail}）}（${dt}s）"
      fi
    fi
  done < <(remote ssh "$FUXI_HOST" "bash -s" <<'REMOTE_WAIT'
t0=$(date +%s)
max=120
while :; do
  logs=$(docker logs --since 2m fuxi-bridge 2>&1 || true)
  if echo "$logs" | grep -q '开始监听'; then
    echo "READY|已就绪|"
    exit 0
  fi
  now=$(date +%s)
  el=$((now - t0))
  if [ "$el" -ge "$max" ]; then
    echo "TIMEOUT|等待超时|"
    exit 0
  fi
  if echo "$logs" | grep -q '重启补回'; then
    echo "WAIT|正在检查重启补回|"
  elif echo "$logs" | grep -q '解出密钥'; then
    k=$(echo "$logs" | grep '解出密钥' | tail -1 | sed -E 's/.*解出密钥 //')
    echo "WAIT|密钥已解出，正在启动服务|$k"
  elif echo "$logs" | grep -q '白名单已同步'; then
    echo "WAIT|正在扫描微信内存密钥|约 30-45 秒"
  elif echo "$logs" | grep -q '等待内核就绪'; then
    echo "WAIT|等待内核就绪|"
  else
    echo "WAIT|桥接启动中|"
  fi
  sleep 2
done
REMOTE_WAIT
)

  printf "\n  %s容器状态：%s\n" "$C_BOLD" "$C_RESET"
  remote ssh "$FUXI_HOST" "docker ps --filter name=fuxi --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}'" | sed 's/^/    /'

  printf "\n  %s启动关键日志：%s\n" "$C_BOLD" "$C_RESET"
  remote ssh "$FUXI_HOST" "docker logs --since 2m fuxi-bridge 2>&1 | grep -E '解出密钥|白名单已|重启补回|开始监听|ERROR|CRITICAL' | tail -8 | sed -E 's/wxid_[a-z0-9]+/wxid_<id>/g'" | sed 's/^/    /' || true

  [ "$ready" -eq 1 ] || return 0
}

# 服务器上：把 :dev 指向版本 $2，记日志；第一次用版本部署时，把原来没有版本号的 :dev 存成 initial 方便回滚
point_dev() {
  local svc="$1" tag="$2" action="$3" pruned
  pruned=$(remote ssh "$FUXI_HOST" "set -e
    img=fuxi-$svc
    cur=\$(docker image inspect -f '{{.Id}}' \$img:dev 2>/dev/null || true)
    if [ -n \"\$cur\" ] && ! grep -q \" $svc \" '$LOG' 2>/dev/null; then
      docker tag \$img:dev \$img:initial
      echo \"\$(date '+%F %T') $svc initial 原有版本\" >> '$LOG'
    fi
    docker tag \$img:$tag \$img:dev
    echo \"\$(date '+%F %T') $svc $tag $action\" >> '$LOG'
    keep=\$(grep \" $svc \" '$LOG' | awk '{print \$4}' | tac | awk '!s[\$0]++' | head -n $KEEP)
    n=0
    for t in \$(docker images \$img --format '{{.Tag}}' | grep -vx dev); do
      if ! echo \"\$keep\" | grep -qx \"\$t\"; then
        docker rmi -f \$img:\$t >/dev/null 2>&1 && n=\$((n + 1)) || true
      fi
    done
    echo \$n")
  if [ "${pruned:-0}" -gt 0 ]; then
    log_ok "fuxi-${svc}:dev → ${tag}（已清理 ${pruned} 个旧镜像，保留最近 ${KEEP} 版）"
  else
    log_ok "fuxi-${svc}:dev → ${tag}"
  fi
}

deploy() {
  local what="$1" svcs tag images=() send=() total_t0
  total_t0=$(date +%s)
  svcs=$(services_of "$what")
  tag=$(git -C "$root" rev-parse --short=7 HEAD)
  local dirty=0
  if [ -n "$(git -C "$root" status --porcelain -- bridge kernel watchdog deploy)" ]; then
    tag="$tag-dirty$(date +%m%d%H%M)"
    dirty=1
  fi

  hr
  printf "  %s浮汐部署%s   目标: %s%s:%s%s\n" "$C_BOLD$C_CYAN" "$C_RESET" "$C_BOLD" "$FUXI_HOST" "$FUXI_DIR" "$C_RESET"
  printf "  服务: %s   |   版本: %s%s%s\n" "$svcs" "$C_BOLD" "$tag" "$C_RESET"
  hr
  if [ "$dirty" -eq 1 ]; then
    log_warn "工作区有未提交改动（bridge/ kernel/ watchdog/ deploy/），版本记为 ${tag}"
  fi

  step 1 5 "本机构建镜像（linux/amd64）"
  for s in $svcs; do
    build_service "$s" "$tag"
    images+=("fuxi-$s:$tag")
  done

  step 2 5 "检查并传输镜像到 ${FUXI_HOST}"
  for i in "${images[@]}"; do
    if remote ssh "$FUXI_HOST" "docker image inspect $i >/dev/null 2>&1"; then
      log_info "${i} 服务器已有，跳过传输"
    else
      local isz
      isz=$(fmt_bytes "$(docker image inspect -f '{{.Size}}' "$i" 2>/dev/null || echo 0)")
      log_info "${i} 待传输（原始大小 ${isz}）"
      send+=("$i")
    fi
  done
  if [ "${#send[@]}" -gt 0 ]; then
    transfer_images "${send[@]}"
  else
    log_ok "所有镜像已在服务器就绪，无需传输"
  fi

  step 3 5 "同步 compose.yaml 与版本指针"
  remote ssh "$FUXI_HOST" "mkdir -p '$FUXI_DIR' && chmod 700 '$FUXI_DIR'"
  remote scp -q "$root/deploy/compose.yaml" "$FUXI_HOST:$FUXI_DIR/compose.yaml"
  log_ok "compose.yaml 已同步"
  remote ssh "$FUXI_HOST" "test -f '$FUXI_DIR/.env'" || {
    log_err "服务器上还没有 ${FUXI_DIR}/.env：按 deploy/.env.example 创建（chmod 600）后再运行本脚本"
    exit 1
  }
  log_ok ".env 校验通过"
  for s in $svcs; do point_dev "$s" "$tag" deploy; done

  step 4 5 "重建并启动容器"
  restart "$what"

  step 5 5 "等待服务就绪与健康检查"
  wait_ready "$what"

  local total_dt=$(($(date +%s) - total_t0))
  printf "\n"
  hr
  printf "  %s✓ 部署完成%s   版本: %s%s%s   总耗时: %s%s%s\n" \
    "$C_GREEN$C_BOLD" "$C_RESET" "$C_BOLD" "$tag" "$C_RESET" "$C_BOLD" "$(fmt_dur "$total_dt")" "$C_RESET"
  hr
}

rollback() {
  local svc="${1:-}" want="${2:-}" total_t0
  total_t0=$(date +%s)
  case "$svc" in bridge|kernel|watchdog) ;; *) usage ;; esac
  if [ -z "$want" ]; then
    want=$(ssh "$FUXI_HOST" "grep ' $svc ' '$LOG' 2>/dev/null | awk '{print \$4}' | tac | awk '!s[\$0]++' | sed -n '2,\$p' |
      while read t; do docker image inspect fuxi-$svc:\$t >/dev/null 2>&1 && { echo \$t; break; }; done")
    [ -n "$want" ] || { log_err "没有可回滚的 ${svc} 版本（运行 scripts/deploy.sh versions 查看）"; exit 1; }
  fi
  ssh "$FUXI_HOST" "docker image inspect fuxi-$svc:$want >/dev/null 2>&1" || {
    log_err "服务器上没有 fuxi-${svc}:${want}"; exit 1; }

  hr
  printf "  %s浮汐回滚%s   服务: %s   →   版本: %s%s%s\n" "$C_BOLD$C_YELLOW" "$C_RESET" "$svc" "$C_BOLD" "$want" "$C_RESET"
  hr

  step 1 3 "切换版本指针"
  point_dev "$svc" "$want" rollback
  step 2 3 "重建容器"
  restart "$svc"
  step 3 3 "等待服务就绪"
  wait_ready "$svc"

  local total_dt=$(($(date +%s) - total_t0))
  printf "\n"
  hr
  printf "  %s✓ 回滚完成%s   %s → %s   总耗时: %s\n" "$C_GREEN$C_BOLD" "$C_RESET" "$svc" "$want" "$(fmt_dur "$total_dt")"
  hr
}

versions() {
  printf "%s== 最近的部署记录（时间 服务 版本 动作）%s\n" "$C_BOLD" "$C_RESET"
  ssh "$FUXI_HOST" "tail -n 15 '$LOG' 2>/dev/null || echo '（还没有记录）'" | sed 's/^/  /'
  printf "\n%s== 服务器上的版本镜像（* = 当前正在运行）%s\n" "$C_BOLD" "$C_RESET"
  ssh "$FUXI_HOST" "for s in kernel bridge watchdog; do
    dev=\$(docker image inspect -f '{{.Id}}' fuxi-\$s:dev 2>/dev/null | cut -c8-19)
    tags=\$(docker images fuxi-\$s --format '{{.Tag}}={{.ID}}' | grep -v '^dev=' |
      sed \"s/=\$dev\\\$/ *(当前)/; s/=.*//\" | tr '\n' ' ')
    printf '  %-10s %s\n' \"fuxi-\$s\" \"\$tags\"
  done"
}

# 备份里有记忆、人设和 config.json（含模型密钥），只拉到仓库外的目录，权限 700
backups() {
  local dest="${1:-$HOME/fuxi-backups}" total_t0
  total_t0=$(date +%s)
  case "$dest" in "$root"|"$root"/*) log_err "备份不能放在仓库里"; exit 1 ;; esac

  hr
  printf "  %s浮汐备份%s   目标目录: %s%s%s\n" "$C_BOLD$C_CYAN" "$C_RESET" "$C_BOLD" "$dest" "$C_RESET"
  hr

  step 1 2 "服务器上立即执行一次备份"
  local out
  out=$(remote ssh "$FUXI_HOST" "docker exec fuxi-watchdog /watchdog.sh backup | tail -1")
  log_ok "${out}"

  step 2 2 "增量拉回本机目录 ${dest}"
  mkdir -p "$dest" && chmod 700 "$dest"
  local have files pulled=0 skipped=0 f total_cnt=0 idx=0
  have=$(ls "$dest" 2>/dev/null || true)
  files=$(ssh "$FUXI_HOST" "ls -1 '$FUXI_DIR/backups' 2>/dev/null | grep '\\.tgz\$'" || true)
  if [ -n "$files" ]; then
    total_cnt=$(echo "$files" | wc -l | tr -d ' ')
    for f in $files; do
      idx=$((idx + 1))
      if echo "$have" | grep -qx "$f"; then
        skipped=$((skipped + 1))
        continue
      fi
      status_line "${C_CYAN}⠋${C_RESET} [${idx}/${total_cnt}] 拉取 ${f}…"
      remote scp -q "$FUXI_HOST:$FUXI_DIR/backups/$f" "$dest/$f" && chmod 600 "$dest/$f"
      clear_line
      local fsz
      fsz=$(fmt_bytes "$(stat -f%z "$dest/$f" 2>/dev/null || stat -c%s "$dest/$f" 2>/dev/null || echo 0)")
      log_ok "已拉取 ${f}（${fsz}）"
      pulled=$((pulled + 1))
    done
  fi
  log_info "本次拉取 ${pulled} 个，跳过已有 ${skipped} 个（耗时 $(fmt_dur "$(($(date +%s) - total_t0))")）"
  printf "\n  %s本机最近备份：%s\n" "$C_BOLD" "$C_RESET"
  ls -lh -t "$dest" 2>/dev/null | awk 'NR>1 && NR<=5 {printf "    %-28s %6s  %s %s %s\n", $9, $5, $6, $7, $8}'
}

case "${1:-all}" in
  rollback) shift; rollback "$@" ;;
  backups) shift; backups "$@" ;;
  versions) versions ;;
  *) deploy "${1:-all}" ;;
esac
