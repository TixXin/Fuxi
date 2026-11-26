# 上游来源

本目录是 [whale_girl](https://github.com/Banan1cee/whale_girl) 的 Node 内核，AGPL-3.0（见本目录 `LICENSE`）。

- 来源 commit：见 `.upstream-commit`
- 引入范围：`src/`、`server.js`、`package.json`、`config/`、`assets/`（不含 `assets/stickers/` 表情包库，容器默认关闭表情包功能）
- 未引入：Windows 桥接 `bridge/`、`vendor/wechatauto/`、启动器、安装器、网站、测试

## 浮汐的改动

上游文件**未做修改**。新增文件：

| 文件 | 作用 |
|---|---|
| `docker-entrypoint.js` | 容器启动时把 `FUXI_KERNEL_TOKEN`、端口写入 `data/config.json`（并默认关闭 `log.traceRaw`），随后 `exec node -r ./fuxi/preload.js server.js` |
| `fuxi/preload.js` | 预加载：包一层 `createRouter`，追加浮汐自己的接口；安装链接阅读增强（不改上游文件） |
| `fuxi/asr.js` | `POST /api/fuxi/asr`：WAV → 文字，用内核的 voice 配置（调用方式同上游「测试语音」，返回完整文本） |
| `fuxi/complete.js` | `POST /api/fuxi/complete {system, text, maxTokens}`：用内核的聊天模型配置做一次非流式补全（关思考），给桥接整理群反馈用 |
| `fuxi/links.js`、`fuxi/linkWorker.js` | 链接阅读增强：替换 `src/adapters/links.js` 导出的 `describeLink` / `formatForPrompt`。普通网页用 Readability 抽正文（线程里跑），GitHub issue / PR / release 走 API，短视频交回上游；输出控制在上游 `linkBlock` 的 1200 字截断内 |
| `fuxi/intent.js` | 收紧不经模型的固定话术：取消提醒不再被「别的 / 不用 / 这个」误触发，群里没叫她时不回「不是主人 / 没找到提醒」；提醒、退场、固定问答只看本人打的字（去掉桥接拼进来的截图识别和被引用内容）。包的是 `core/reminder` 的 `parseReminderCancellation`、`pipeline/stages/canned` 的两个导出和 `Bot.prototype.tryReminder`，须在 `pipeline/chat` 加载前安装 |
| `fuxi/package.json`、`fuxi/test/` | 扩展依赖（`@mozilla/readability`、`linkedom`，镜像里 `npm ci`）与单元测试（`node --test kernel/fuxi/test/`） |
| `Dockerfile` | node:22-slim 镜像，装 `fuxi/` 依赖，`data/` 为卷 |
| `UPSTREAM.md`、`.upstream-commit` | 本说明 |

## 同步上游

用 `scripts/sync-upstream.sh --check` 先看上游新了什么、下面这些对接文件有没有变；没问题再去掉 `--check` 同步（会跑测试，不自动提交）。
脚本做的就是下面这几步：

```bash
git clone --depth 1 https://github.com/Banan1cee/whale_girl /tmp/wg
rsync -a --delete /tmp/wg/src kernel/ && rsync -a /tmp/wg/server.js /tmp/wg/package.json /tmp/wg/config kernel/
rsync -a --exclude stickers /tmp/wg/assets kernel/
git -C /tmp/wg rev-parse HEAD > kernel/.upstream-commit
```

同步后检查 `src/adapters/gateway.js` 的 webhook 字段与 `src/http/routes/` 的桥接接口是否有变，
对应调整 `bridge/fuxi_bridge/kernel.py`。
还要检查 `src/adapters/links.js` 的导出（`fetchUrl`、`kindOf`、`parsePage`、`describeLink`、`formatForPrompt`）
和 `src/persona/prompt.js` 里 `linkBlock` 的截断长度是否有变，对应调整 `fuxi/links.js`。
还要检查 `src/pipeline/chat.js` 是否仍以 `const { cannedReply, dismissReply } = require('./stages/canned')`、`reminderCore.parseReminderCancellation`、`this.tryReminder({ msg, clean, isGroup, atMe, … })` 的方式调用，对应调整 `fuxi/intent.js`。
