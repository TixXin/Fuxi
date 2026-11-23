# 上游来源

本目录是 [whale_girl](https://github.com/Banan1cee/whale_girl) 的 Node 内核，AGPL-3.0（见本目录 `LICENSE`）。

- 来源 commit：见 `.upstream-commit`
- 引入范围：`src/`、`server.js`、`package.json`、`config/`、`assets/`（不含 `assets/stickers/` 表情包库，容器默认关闭表情包功能）
- 未引入：Windows 桥接 `bridge/`、`vendor/wechatauto/`、启动器、安装器、网站、测试

## 浮汐的改动

上游文件**未做修改**。新增文件：

| 文件 | 作用 |
|---|---|
| `docker-entrypoint.js` | 容器启动时把 `FUXI_KERNEL_TOKEN`、端口、主人 wxid 写入 `data/config.json`（并默认关闭 `log.traceRaw`），随后 `exec node server.js` |
| `Dockerfile` | node:22-slim 镜像，`data/` 为卷 |
| `UPSTREAM.md`、`.upstream-commit` | 本说明 |

## 同步上游

```bash
git clone --depth 1 https://github.com/Banan1cee/whale_girl /tmp/wg
rsync -a --delete /tmp/wg/src kernel/ && rsync -a /tmp/wg/server.js /tmp/wg/package.json /tmp/wg/config kernel/
rsync -a --exclude stickers /tmp/wg/assets kernel/
git -C /tmp/wg rev-parse HEAD > kernel/.upstream-commit
```

同步后检查 `src/adapters/gateway.js` 的 webhook 字段与 `src/http/routes/` 的桥接接口是否有变，
对应调整 `bridge/fuxi_bridge/kernel.py`。
