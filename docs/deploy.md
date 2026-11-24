# 部署与运维

本文用占位符代替真实环境，**服务器地址、目录、口令都不写进仓库**：

| 占位符 | 含义 |
|---|---|
| `<host>` | 服务器的 ssh 别名 |
| `<wechat-dir>` | wechat-selkies 的 compose 目录 |
| `<fuxi-dir>` | 浮汐的部署目录（放 `compose.yaml` 和 `.env`） |

架构见 [README](../README.md)，对外接口见 [api.md](api.md)。

## 一、组成

| 容器 | 镜像 | 作用 | 与其他容器的关系 |
|---|---|---|---|
| `wechat-selkies` | `nickrunning/wechat-selkies` | 虚拟桌面 + 官方 Linux 微信 + 网页远程桌面 | 独立 compose，浮汐只给它加了一个挂载 |
| `fuxi-kernel` | `fuxi-kernel`（本仓库 `kernel/`） | 对话、记忆、人设、调模型 | 数据在卷 `fuxi_kernel-data` |
| `fuxi-bridge` | `fuxi-bridge`（本仓库 `bridge/`） | 读库收消息、操作窗口发消息、对外 API | 共用**内核的网络**、**微信的 pid 命名空间**，只读挂载微信数据，共享 X socket |
| `fuxi-watchdog` | `fuxi-watchdog`（本仓库 `watchdog/`） | 每分钟查健康并推送告警；微信或内核容器重建后自动重建 bridge | 宿主网络 + docker.sock |

```
wechat-selkies ──pid 命名空间──┐
               ──config/（只读）─┼──> fuxi-bridge ──回环──> fuxi-kernel ──> DeepSeek
               ──X socket 目录───┘          └── 宿主 127.0.0.1:8790（对外发送 API）
```

## 二、首次部署

### 0. 前提

- 服务器上 wechat-selkies 已在运行，并用**小号**扫码登录（主号有封号风险）。
- 本机装有 Docker（用来构建 `linux/amd64` 镜像），能 `ssh <host>`。
- 一个 DeepSeek API key。

### 1. 给 wechat-selkies 加共享 X socket

bridge 要连上微信所在的虚拟屏幕。在 `<wechat-dir>/docker-compose.yml` 的 `volumes` 里加一行：

```yaml
    volumes:
      - ./config:/config
      - ./x11:/tmp/.X11-unix      # 新增
```

```bash
ssh <host> 'cd <wechat-dir> && cp -p docker-compose.yml docker-compose.yml.bak && mkdir -p x11 && chmod 1777 x11 && docker compose up -d'
```

重建后微信会显示「需在手机上完成登录」，**在小号手机上点确认**。
确认 `<wechat-dir>/x11/X1` 出现即可。

### 2. 准备 `.env`

```bash
ssh <host> 'mkdir -p <fuxi-dir> && chmod 700 <fuxi-dir>'
scp deploy/.env.example <host>:<fuxi-dir>/.env
ssh <host> 'chmod 600 <fuxi-dir>/.env'
```

在服务器上编辑 `<fuxi-dir>/.env`：

| 键 | 填什么 |
|---|---|
| `WECHAT_CONTAINER` | 微信容器名，默认 `wechat-selkies` |
| `WX_CONFIG_DIR` | `<wechat-dir>/config` 的绝对路径 |
| `X11_SOCKET_DIR` | `<wechat-dir>/x11` 的绝对路径 |
| `FUXI_KERNEL_TOKEN` | `openssl rand -hex 16` 生成 |
| `FUXI_API_TOKEN` | `openssl rand -hex 24` 生成；留空则不开对外 API |
| `FUXI_API_PORT` | 对外 API 在宿主回环上的端口，默认 8790，先确认没被占用 |
| `DEEPSEEK_API_KEY` | 模型 key |
| `KUMA_PUSH_URL` | 告警用的 Uptime Kuma Push 地址，见「告警」；留空只写日志 |

`.env` 只放部署相关的配置。**行为设置**（看图、语音转文字、主动搭话、主人、bot 名字、静默时段、限速、
回复自己、合并等待、表情包）在桥接状态卷的 `settings.json` 里，改了立即生效、不用重建容器：

- 文件传输助手发 `/设置` 查看全部，`/设置 看图` 看说明和可选值，`/设置 看图 只看私聊` 修改；
  `/设置 主人 名字` 可以直接写备注/昵称。
- 对外 API：`GET /settings` 列出（含类型、默认值、说明，管理台照此生成表单），`POST /settings {"vision": "private"}` 修改。
- 表情包开关属于内核配置，`/设置` 只是转发给内核，桥接不另存。
- 白名单在 `allow.txt`，用 `/允许` `/禁止` 改；第一次启动只放行文件传输助手。
- 旧版放在 `.env` 的 `FUXI_CARE`、`FUXI_VISION`、`FUXI_OWNER_WXIDS` 等只在第一次生成 `settings.json` 时搬过去一次，之后不再起作用。

内核默认不保留聊天原文（`log.traceRaw=false`）；排障需要时在 `.env` 加 `FUXI_KERNEL_TRACE_RAW=1`
并 `up -d kernel`，用完删掉。

口令可以直接在服务器上生成写入，不经过本机：

```bash
ssh <host> 'cd <fuxi-dir> && sed -i "s/^FUXI_KERNEL_TOKEN=.*/FUXI_KERNEL_TOKEN=$(openssl rand -hex 16)/; s/^FUXI_API_TOKEN=.*/FUXI_API_TOKEN=$(openssl rand -hex 24)/" .env'
```

### 3. 构建并部署

```bash
FUXI_HOST=<host> FUXI_DIR=<fuxi-dir> scripts/deploy.sh
```

脚本做的事：本机构建三个镜像 → `docker save | ssh docker load` 传过去 → 上传 `compose.yaml` →
`docker compose up -d` → 等桥接扫完密钥，打印状态。正常输出最后几行类似：

```
fuxi-bridge   Up 40 seconds
fuxi-watchdog Up 41 seconds
fuxi-kernel   Up 41 seconds (healthy)
... 解出密钥 19 / 19 个库
... 白名单已同步到内核（1 个会话）
... 开始监听全部会话
```

### 4. 自测

先临时打开「文件传输助手里自己发的也回复」：在文件传输助手发 `/设置 回复自己 开`。

用手机往文件传输助手发一句话，几秒内应收到 bot 回复。也可以从服务器上直接触发：

```bash
ssh <host> 'docker exec fuxi-bridge python -m fuxi_bridge.cli send --chat 文件传输助手 --text 你好'
```

测完发 `/设置 回复自己 关`。

### 5. 内核设置（可选）

人设、群聊策略（是否必须 @）、主人等在卷 `fuxi_kernel-data` 的 `config.json`，**改完自动生效**：

```bash
ssh <host> 'docker cp fuxi-kernel:/app/data/config.json /tmp/k.json'   # 取出，编辑 /tmp/k.json
ssh <host> 'docker cp /tmp/k.json fuxi-kernel:/app/data/config.json && rm /tmp/k.json'   # 放回，自动生效
```

`gateway.wechat.inboundToken`、`server.host/port`、`wechat.whitelistOnly`、`care.whitelist` 由浮汐管理，
手改会被覆盖。模型 key 只走环境变量，不写进这个文件。

## 三、日常运维

以下命令都在 `ssh <host>` 后、`cd <fuxi-dir>` 里执行。`$T` 表示：

```bash
T=$(grep ^FUXI_API_TOKEN= .env | cut -d= -f2)
```

### 状态与日志

```bash
docker ps --filter name=fuxi
docker logs --tail 50 fuxi-bridge        # 收发、白名单、熔断
docker logs --tail 50 fuxi-kernel        # 对话、模型用量
curl -s -H "Authorization: Bearer $T" 127.0.0.1:8790/healthz
```

`healthz` 的 `healthy` 为 false 时，`problems` 列出原因：已熔断、微信待登录/窗口不在、
有库解不出密钥、轮询停滞超过 2 分钟、内核心跳超过 90 秒、10 分钟内有放弃发送。
`halted` 非空表示**已熔断**，见下文。

### 告警

watchdog 每分钟读一次 `healthz`，推送到 Uptime Kuma 的 Push 监控，再由 Kuma 发 Bark 等通知：

1. Kuma 里新建监控，类型选 **Push**，心跳间隔 60 秒，挂上通知；
2. 复制它给的地址，**去掉** `?status=up&msg=OK&ping=`，写进 `.env` 的 `KUMA_PUSH_URL`；
3. `docker compose up -d watchdog`。

连续 3 次不健康才报 down（避开重启、扫描密钥的空窗），熔断立即报。watchdog 没有推送（宿主宕机、
watchdog 自己挂了）时，Kuma 按超时报警。

### 白名单（改完即时生效，不用重启）

bot 只回复白名单里的会话。空名单 = 谁都不回，`*` = 全部放行。

```bash
# 看
curl -s -H "Authorization: Bearer $T" 127.0.0.1:8790/allow
# 加 / 删（可用唯一的显示名或 chat_id；查 chat_id：GET /chats?q=关键词）
curl -s -H "Authorization: Bearer $T" -H "Content-Type: application/json" \
  -X POST -d '{"add":["某个群名"],"remove":[]}' 127.0.0.1:8790/allow
```

也可以直接改文件（镜像里没有编辑器，用 `docker cp` 进出）：

```bash
docker cp fuxi-bridge:/state/allow.txt /tmp/allow.txt   # 编辑后放回，1~2 秒内生效
docker cp /tmp/allow.txt fuxi-bridge:/state/allow.txt && rm /tmp/allow.txt
```

`.env` 里的 `FUXI_ALLOW` 只在第一次生成文件时使用，之后改它**不会**生效。

### 发送安全

每次发送：搜索会话名 → 等下拉结果**连续稳定**后，只点**整行标题完全匹配且唯一**的本地结果 →
用 OCR 读聊天标题，与目标一致才粘贴 → 发送后到数据库核对。前三步任何一步不符就**放弃这一条**
（日志 `放弃发送`），不影响后续；第四步不符就熔断。

若上一条就是发往同一会话、且 OCR 标题仍一致，则跳过搜索直接粘贴。日志 `进入会话耗时 x 秒`
记录每次耗时（目标 2 秒内，实测切换会话约 1.3~1.9 秒），`/healthz` 的 `open_chat` 有统计。
每次自动回复另有一行 `回复耗时 … 首条 Xs / 全部 Ys：发现 合并 内核 打字 进会话 粘贴 核对`，
`/healthz` 的 `latency` 是近 50 次的各段中位数。
只测打开、不发送：`docker exec fuxi-bridge python -m fuxi_bridge.cli open --chat 文件传输助手 -n 3 --fresh`。

### 熔断与恢复

发送后桥接会到数据库核对消息确实落在目标会话。核对失败（疑似发错会话）或发送中途出错时，
桥接**停止一切发送**（bot 回复、提醒、API 都拒绝），日志里有 `CRITICAL 已熔断`。

1. 打开网页版微信看看实际发到了哪里，必要时手动撤回（2 分钟内）；
2. 找到原因后解除：

```bash
curl -s -H "Authorization: Bearer $T" -X POST 127.0.0.1:8790/resume
```

也可以在手机上给**文件传输助手**发 `/恢复`（见下节）。重启 bridge 也会解除熔断，但请先确认原因。

### 网页管理台

在浏览器里看状态、暂停/恢复（后续还有对话流、设置、白名单……），架构见 [admin.md](admin.md)。服务器上需要：

1. `.env` 设 `FUXI_ADMIN_TOKEN`（`openssl rand -hex 16`），重新部署 kernel（8791 端口挂在内核容器上）和 bridge。
2. 反代（OpenResty）新建一个站点：`auth_basic` + htpasswd（和网页版微信同一组用户名密码），`location /` 反代到
   `127.0.0.1:8791`，并 `proxy_set_header X-Fuxi-Admin <口令>`、`proxy_set_header Authorization ""`；
   `location = /api/events` 另加 `proxy_buffering off` 和较长的 `proxy_read_timeout`；统一
   `add_header Cache-Control "private, no-store" always`（前面有 CDN 时防缓存）。
3. 自测：不带认证 401；带认证能打开页面；写操作不带 `X-Requested-With: fuxi-admin` 是 403。

域名、htpasswd 位置等服务器信息不写进仓库。

### 文件传输助手遥控

用 bot 这个微信号在**文件传输助手**里发指令，桥接执行后回报到文件传输助手。只认本人发的、
以下列指令开头的消息（别人写不进你的文件传输助手），指令不交给内核，也不受白名单、静默时段影响。

| 指令 | 作用 |
|---|---|
| `/状态` | 熔断/暂停/微信窗口/问题清单/白名单数/进入会话耗时 |
| `/暂停`、`/恢复` | 暂停自动回复与提醒（落盘，重启后仍暂停；API 主动发送不受影响）；`/恢复` 同时解除熔断 |
| `/名单` | 查看白名单 |
| `/允许 名字`、`/禁止 名字` | 增删白名单，名字须唯一匹配（备注/昵称/群名），也可写 chat_id |
| `/测试 名字` | 只打开该会话并核对标题、**不发送**，回报耗时 |
| `/帮助` | 指令列表 |

熔断时遥控回报仍会发往文件传输助手（同样经过 OCR 核对与发送后核对）。

### 升级微信（提示版本过期时）

wechat-selkies 照原来的方式更新（本机拉镜像 → 传到服务器 → `docker compose up -d`）。之后：

1. 在手机上确认登录；
2. bridge 共用的是旧微信容器的 pid 命名空间，必须跟着重建。**watchdog 会在 1 分钟内自动重建**
   （日志 `检测到微信或内核容器已重建`）；没开 watchdog 时手动执行：

```bash
cd <fuxi-dir> && docker compose up -d --force-recreate bridge
```

bridge 启动时会重新从内存扫描密钥（约 30 秒）。微信大版本更新后如果日志显示 `解出密钥 0 / N`，
说明内存里的密钥格式变了，需要重新分析（见「排障」）。

### 更新浮汐

```bash
git pull   # 本机
FUXI_HOST=<host> FUXI_DIR=<fuxi-dir> scripts/deploy.sh bridge   # 只改了 bridge/
FUXI_HOST=<host> FUXI_DIR=<fuxi-dir> scripts/deploy.sh          # 改了 kernel/ 或不确定
```

### 版本与回滚

每次部署的镜像都以 git 提交号为标签（如 `fuxi-bridge:e17da99`；工作区里 bridge/ kernel/ watchdog/ deploy/
有没提交的改动时记成 `e17da99-dirty11242037`，最好先提交再部署）。compose 一直用 `:dev`，部署和回滚就是把 `:dev`
指向某个版本再重建容器。服务器上 `<fuxi-dir>/deploys.log` 记每次部署，每个服务保留最近 5 个版本的镜像。

```bash
FUXI_HOST=<host> FUXI_DIR=<fuxi-dir> scripts/deploy.sh versions              # 部署记录、服务器上有哪些版本
FUXI_HOST=<host> FUXI_DIR=<fuxi-dir> scripts/deploy.sh rollback bridge       # 退回上一个版本
FUXI_HOST=<host> FUXI_DIR=<fuxi-dir> scripts/deploy.sh rollback kernel e17da99   # 换到指定版本
```

当前版本也能在遥控 `/状态`（「版本：…」）和 `GET /healthz` 的 `version` 里看到。改了数据格式（状态卷里的 JSON）
的版本回滚前先看一眼提交说明；目前各模块读到不认识的字段都会忽略，回滚是安全的。

更新 kernel 时 bridge 也会被重建（它共用内核的网络命名空间）。bridge 重建后约 30 秒扫描密钥，
期间不收消息；启动后会**补处理停机期间**的消息，但只补 `FUXI_STALE_SEC`（默认 120 秒）以内的，
更早的不回，避免上线时乱回旧消息。

同步 whale_girl 上游内核见 [kernel/UPSTREAM.md](../kernel/UPSTREAM.md)。

### 备份

需要备份的只有内核数据（记忆、人设演化、提醒、设置）和桥接的白名单/水位：

```bash
docker run --rm -v fuxi_kernel-data:/d -v "$PWD":/b alpine tar czf /b/kernel-data-$(date +%F).tgz -C /d .
docker run --rm -v fuxi_bridge-state:/d -v "$PWD":/b alpine tar czf /b/bridge-state-$(date +%F).tgz -C /d .
```

解密后的库和密钥只在 bridge 的 tmpfs 里，重启即消失，不需要也不应该备份。

### 停用

```bash
docker compose stop bridge     # 只停收发（内核和数据保留）
docker compose down            # 全停；卷保留，加 -v 会删掉记忆等数据
```

## 四、排障

| 现象 | 原因 / 处理 |
|---|---|
| 日志 `内存扫描 … 候选 0 个`、`解出密钥 0 / N` | 微信未登录（停在「需在手机上完成登录」）→ 手机确认后 `up -d --force-recreate bridge`。已登录仍为 0 → 微信版本变了，需重新分析 |
| bridge 反复重启、报错或 `找不到微信进程` | 微信容器重建过而 bridge 没跟着重建 → 等 watchdog 处理，或手动 `up -d --force-recreate bridge` |
| `找不到微信主窗口` | 微信没启动或已退出登录；打开网页版微信检查。窗口只是缩到托盘时桥接会自己点托盘图标恢复 |
| `搜索「xx」没有唯一的完全匹配结果，放弃发送` | 会话显示名与搜索结果对不上（改了备注、含表情、或有同名）。给联系人设一个唯一的纯文字备注 |
| `打开的会话标题是「…」，与目标「…」不符，放弃发送` | 点错了行，或 OCR 读错了生僻字/表情。偶发可忽略；同一会话反复出现时给它设纯文字备注 |
| 熔断 `后核对失败` | 见「熔断与恢复」。常见于有人同时在网页版微信里操作，抢了焦点 |
| 收到消息但不回复 | 依次看：是否在白名单（`GET /allow`）→ bridge 日志有无 `→ 内核` → kernel 日志里是否因群聊未 @ 等策略不回 |
| `发送许可查询失败` | 内核没起来或口令不一致；检查 `fuxi-kernel` 状态和 `.env` 的 `FUXI_KERNEL_TOKEN` |
| 网页版微信里看到窗口自己在动 | 正常：bot 发消息就是在这块虚拟屏幕上搜索、点开会话、粘贴、发送。**bot 发送时不要同时操作** |

## 五、安全边界

- 只用小号；UI 自动化与读本地库可能违反微信服务条款。
- bridge 只读微信进程内存、只读挂载微信数据；解密结果和密钥只在 tmpfs。
- 对外 API 只监听宿主 `127.0.0.1`，必须带 token；不要反代到公网。
- 网页管理台（8791）是唯一经反代出去的口子：OpenResty 做 Basic 认证并补 `X-Fuxi-Admin` 口令头，管理服务再校验口令和防跨站头，见 [admin.md](admin.md)。
