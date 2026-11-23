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
       │ POST /webhook/wechat（同一网络命名空间，回环）
┌──────▼──────────────────────────────────────────────┐
│ kernel：whale_girl 的 Node 内核（对话/记忆/人设/调模型）│
└─────────────────────────────────────────────────────┘
```

- **收消息**：不看界面，直接解密微信本地数据库。密钥以 `x'<hex>'` 字符串形式存在
  进程内存里，被一个固定 32 字节掩码异或混淆；按 32 种相位锚点单遍扫内存即可取出，
  再用每个库页 1 的 HMAC 验证。新消息先落在 `-wal`，桥接只合并当前世代、已提交的帧。
- **发消息**：容器里是专用虚拟桌面，没人抢焦点；Ctrl+F 搜会话名打开，剪贴板粘贴中文。
- **数据安全**：只读内存、只读挂载微信数据；解密结果和密钥缓存只在 tmpfs，不落盘、
  不出服务器。

## 目录

| 路径 | 内容 |
|---|---|
| `bridge/` | Linux 桥接（Python 3.12，依赖 cryptography、zstandard；镜像另装 xdotool/xclip） |
| `kernel/` | whale_girl Node 内核（阶段 4 引入） |
| `deploy/` | `compose.yaml` 与 `.env.example`；真实 `.env` 只在服务器 |
| `scripts/check-secrets.sh` | 提交前拦截密钥/IP/wxid（`git config core.hooksPath .githooks` 启用） |

## 部署

服务器拉 Docker Hub 慢，镜像在本机构建再传：

```bash
docker build --platform linux/amd64 -t fuxi-bridge:dev bridge
docker save fuxi-bridge:dev | gzip -1 | ssh <服务器> docker load
```

服务器上：wechat-selkies 的 compose 需额外挂一个共享目录到 `/tmp/.X11-unix`；
`deploy/compose.yaml` 与 `.env`（chmod 600）放同一目录后 `docker compose up -d`。

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
