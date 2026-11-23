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
| `FUXI_ALLOW` | 初始白名单，建议先只写 `filehelper` |
| `DEEPSEEK_API_KEY` | 模型 key |

口令可以直接在服务器上生成写入，不经过本机：

```bash
ssh <host> 'cd <fuxi-dir> && sed -i "s/^FUXI_KERNEL_TOKEN=.*/FUXI_KERNEL_TOKEN=$(openssl rand -hex 16)/; s/^FUXI_API_TOKEN=.*/FUXI_API_TOKEN=$(openssl rand -hex 24)/" .env'
```

### 3. 构建并部署

```bash
FUXI_HOST=<host> FUXI_DIR=<fuxi-dir> scripts/deploy.sh
```

脚本做的事：本机构建两个镜像 → `docker save | ssh docker load` 传过去 → 上传 `compose.yaml` →
`docker compose up -d` → 等桥接扫完密钥，打印状态。正常输出最后几行类似：

```
fuxi-bridge   Up 40 seconds
fuxi-kernel   Up 41 seconds (healthy)
... 解出密钥 19 / 19 个库
... 白名单已同步到内核（1 个会话）
... 开始监听全部会话
```

### 4. 自测

先临时打开「文件传输助手里自己发的也回复」：`.env` 里 `FUXI_REPLY_SELF=1`，然后
`ssh <host> 'cd <fuxi-dir> && docker compose up -d bridge'`。

用手机往文件传输助手发一句话，几秒内应收到 bot 回复。也可以从服务器上直接触发：

```bash
ssh <host> 'docker exec fuxi-bridge python -m fuxi_bridge.cli send --chat 文件传输助手 --text 你好'
```

测完把 `FUXI_REPLY_SELF` 改回 `0` 并再 `up -d bridge`。

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

`healthz` 里 `halted` 非空表示**已熔断**，见下文。

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

### 熔断与恢复

发送后桥接会到数据库核对消息确实落在目标会话。核对失败（疑似发错会话）或发送中途出错时，
桥接**停止一切发送**（bot 回复、提醒、API 都拒绝），日志里有 `CRITICAL 已熔断`。

1. 打开网页版微信看看实际发到了哪里，必要时手动撤回（2 分钟内）；
2. 找到原因后解除：

```bash
curl -s -H "Authorization: Bearer $T" -X POST 127.0.0.1:8790/resume
```

重启 bridge 也会解除熔断，但请先确认原因。

### 升级微信（提示版本过期时）

wechat-selkies 照原来的方式更新（本机拉镜像 → 传到服务器 → `docker compose up -d`）。之后：

1. 在手机上确认登录；
2. **重建 bridge**：它共用的是旧微信容器的 pid 命名空间，微信容器重建后必须跟着重建：

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

更新 kernel 时 bridge 也会被重建（它共用内核的网络命名空间）。每次 bridge 重建会有约 30 秒
（扫描密钥）不收消息，**这期间到达的消息不会被回复**——启动时只记录各会话当前进度、不回放，
避免上线时乱回旧消息。

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
| bridge 反复重启、报错或 `找不到微信进程` | 微信容器重建过而 bridge 没跟着重建 → `up -d --force-recreate bridge` |
| `找不到微信窗口` | 微信没启动或已退出登录；打开网页版微信检查 |
| `搜索「xx」没有唯一的完全匹配结果，放弃发送` | 会话显示名与搜索结果对不上（改了备注、含表情、或有同名）。给联系人设一个唯一的纯文字备注 |
| 熔断 `后核对失败` | 见「熔断与恢复」。常见于有人同时在网页版微信里操作，抢了焦点 |
| 收到消息但不回复 | 依次看：是否在白名单（`GET /allow`）→ bridge 日志有无 `→ 内核` → kernel 日志里是否因群聊未 @ 等策略不回 |
| `发送许可查询失败` | 内核没起来或口令不一致；检查 `fuxi-kernel` 状态和 `.env` 的 `FUXI_KERNEL_TOKEN` |
| 网页版微信里看到窗口自己在动 | 正常：bot 发消息就是在这块虚拟屏幕上搜索、点开会话、粘贴、发送。**bot 发送时不要同时操作** |

## 五、安全边界

- 只用小号；UI 自动化与读本地库可能违反微信服务条款。
- bridge 只读微信进程内存、只读挂载微信数据；解密结果和密钥只在 tmpfs。
- 对外 API 只监听宿主 `127.0.0.1`，必须带 token；不要反代到公网。
