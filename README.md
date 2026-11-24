# 浮汐 / Fuxi

把一个微信 AI Bot 整套跑在 Linux 服务器的 Docker 里，不需要常开的 Windows。
是「浮屿 / Fuyu」的配套小项目。

## 架构

```
┌──────────────── wechat-selkies 容器 ────────────────┐
│ Xvfb :1  ←─ 官方 Linux 微信 4.x ─→ 本地库（SQLCipher4）│
└──────┬──────────────────┬──────────────────┬────────┘
   X socket(共享卷)    pid 命名空间        config/（只读挂载）
       │                  │                  │
┌──────▼──────────────────▼──────────────────▼────────┐
│ fuxi-bridge（Python，本仓库 bridge/）                │
│  keyscan   读 /proc/<微信>/mem 提每个库的密钥（只读）│
│  store     库+WAL 快照 → 解密到 tmpfs，按变化增量重建 │
│  messages  会话/消息表轮询，sort_seq 水位             │
│  sender    xdotool + xclip 往微信窗口发文本/图片      │
└──────┬──────────────────────────────────────────────┘
       │ POST /webhook/wechat、拉提醒、心跳（同一网络命名空间，回环）
┌──────▼──────────────────────────────────────────────┐
│ kernel：whale_girl 的 Node 内核（对话/记忆/人设/调模型）│
└─────────────────────────────────────────────────────┘
        ▲ 对外发送 API（宿主 127.0.0.1:FUXI_API_PORT，Bearer token）
        └── 其他项目，如浮屿发版分享图 → 微信群
```

桥接主循环（`bridge/fuxi_bridge/app.py`；发送在 `sending.py`、回复后半段在 `replying.py`、
产品助手在 `assist.py`、浮屿用户群的后台工作在 `groupwork.py`）：

1. session/消息库有变化 → 找出时间戳变了的会话 → 按 sort_seq 水位取新消息；
2. 过滤：只收**白名单**会话（`allow.txt` 热加载，见 `docs/api.md`；设置「回复范围」可放宽到全部私聊/群）、只收文本和可读的 appmsg
   （引用、链接、文件、小程序、聊天记录、拍一拍我），跳过自己发的、公众号、转账红包、
   超过 120 秒的旧消息；同一会话 1.5 秒内连发合并；
3. 转成内核 webhook 格式（群聊带 `at_me`，从 source XML 的 `atuserlist` 和「@名字」判断；
   引用消息附上被引用内容，引用 bot 的消息时带 `reply_to_bot`）；
4. 内核回复的气泡逐条发送，发送前问内核 `send-permission`，发送后到库里核对确实落在目标会话；
5. 另有线程：每 5 秒拉内核到期提醒、每 20 秒心跳、启动时同步联系人；
6. 文件传输助手里本人发的 `/状态` `/暂停` `/允许 xx` 等是遥控指令（见 `docs/deploy.md`）。

- **收消息**：不看界面，直接解密微信本地数据库。密钥以 `x'<hex>'` 字符串形式存在
  进程内存里，被一个固定 32 字节掩码异或混淆；按 32 种相位锚点单遍扫内存即可取出，
  再用每个库页 1 的 HMAC 验证。新消息先落在 `-wal`，桥接只合并当前世代、已提交的帧。
- **发消息**：容器里是专用虚拟桌面，没人抢焦点；Ctrl+F 搜会话名，剪贴板粘贴中文。
  搜索结果的分区顺序随关键词变化，桥接等下拉结果稳定后只点击**标题整行被标绿（与关键词完全相同）
  且唯一**的本地结果，否则放弃；点开后 OCR 核对聊天标题；发送后到数据库核对消息确实落在目标会话，
  核对失败立即熔断全部发送。搜索到进入会话约 1.3~1.9 秒（numpy 扫描截图、OCR 只跑识别模型）。
- **数据安全**：只读内存、只读挂载微信数据；解密结果和密钥缓存只在 tmpfs，不落盘、
  不出服务器。

## 目录

| 路径 | 内容 |
|---|---|
| `bridge/` | Linux 桥接（Python 3.12，依赖 cryptography、zstandard、RapidOCR；镜像另装 xdotool/xclip） |
| `bridge/admin/` | 网页管理台前端（React + Vite），打进桥接镜像 |
| `watchdog/` | 健康检查与告警推送（Uptime Kuma Push），微信/内核容器重建后自动重建 bridge，每日备份 |
| `kernel/` | whale_girl Node 内核（上游文件未改，见 `kernel/UPSTREAM.md`）；`kernel/fuxi/` 是我们的扩展（链接阅读、语音、补全） |
| `docs/deploy.md` | 部署与运维（首次部署、白名单与回复范围、熔断、升级微信、备份与恢复、排障） |
| `docs/api.md` | 对外发送 API 说明 |
| `docs/admin.md` | 网页管理台架构与接口 |
| `docs/feedback-loop.md` | 浮屿群反馈闭环设计 |
| `docs/ROADMAP.md` | 待办计划 |
| `deploy/` | `compose.yaml` 与 `.env.example`；真实 `.env` 只在服务器 |
| `scripts/deploy.sh` | 本机构建镜像并部署到服务器；`rollback` 回滚、`backups` 拉备份 |
| `scripts/sync-upstream.sh` | 同步 whale_girl 上游，`--check` 只看有没有新提交、对接文件变没变 |
| `scripts/test.sh` | 在桥接镜像里跑单元测试 |
| `scripts/check-secrets.sh` | 提交前拦截密钥/IP/wxid（`git config core.hooksPath .githooks` 启用） |

## 部署

服务器拉 Docker Hub 慢，镜像在本机构建再传：

```bash
docker build --platform linux/amd64 -t fuxi-bridge:dev bridge
docker build --platform linux/amd64 -t fuxi-kernel:dev kernel
docker save fuxi-bridge:dev fuxi-kernel:dev | gzip -1 | ssh <服务器> docker load
```

服务器上：

1. wechat-selkies 的 compose 在 volumes 里加一行 `- ./x11:/tmp/.X11-unix`（目录 `chmod 1777`），
   `docker compose up -d` 重建后微信需在手机上确认登录一次；
2. `deploy/compose.yaml` 与 `.env`（按 `.env.example` 填，`chmod 600`）放同一目录，`docker compose up -d`；
3. 内核设置（人设、群聊策略、白名单等）在卷 `kernel-data` 的 `config.json`，改完自动生效。

调试命令（在桥接容器里）：

```bash
python -m fuxi_bridge.cli keys   --db-root <db_storage>
python -m fuxi_bridge.cli tables --db-root <db_storage> --db message/message_0.db --work /work/t
python -m fuxi_bridge.cli send   --chat 文件传输助手 --text 你好
```

## 风险与边界

- UI 自动化与读取本地库可能违反微信服务条款，**只用小号**。
- 本仓库不包含任何密钥、密码、服务器地址或端口。

## 许可证与上游（AGPL-3.0）

本项目整体以 **GNU AGPL-3.0** 发布（见 `LICENSE`），因为它包含并衍生自
[whale_girl](https://github.com/Banan1cee/whale_girl)（AGPL-3.0）：

- `kernel/`：whale_girl 的 Node 内核，来源 commit 记录在 `kernel/UPSTREAM.md`，
  我们的改动逐条列在同一文件；
- `bridge/`：独立重写的 Linux 桥接，但其中的内存混淆掩码常量、WAL 合并思路、
  消息表字段与解码方式来自 whale_girl `vendor/wechatauto/db.py`，按衍生作品对待。

合规要点：

1. 自建自用不需要公开任何东西。
2. 对外分发（给别人镜像/代码）时须同时提供完整对应源码，并保留版权与许可声明。
3. AGPL §13：如果修改后的版本让**其他人**通过网络与之交互（例如把 bot 拉进别人的群），
   须向这些用户提供获取对应源码的途径——本仓库公开即可满足，bot 可在被问到时给出仓库地址。
