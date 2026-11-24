# 对外发送 API

桥接内置一个小 HTTP 服务，供其他项目往微信会话/群里发文字或图片（例如浮屿发版时把分享图发到用户群）。

- 监听：宿主 `127.0.0.1:${FUXI_API_PORT}`（默认 8790），**不对公网暴露**。
  从别的机器调用请走 SSH（`ssh <服务器> curl ...`）或 SSH 隧道。
- 鉴权：`Authorization: Bearer ${FUXI_API_TOKEN}`（服务器 `.env` 里，留空则 API 不启动）。
- 发送是串行的，一次请求会等到真正发完并在数据库里核对后才返回（通常 3~10 秒）；不想等就用异步（见下）。

## 接口

### `GET /chats?q=关键词`

按昵称/备注/群名模糊搜，用于查 `chat_id`。

```json
{"ok": true, "items": [{"chat_id": "123@chatroom", "name": "浮屿用户交流群", "remark": "", "isGroup": true}]}
```

### `POST /send`

```json
{
  "to": "浮屿用户交流群",          // 或 "chat_id": "123@chatroom"（推荐，名字可能重名）
  "text": "浮屿 v1.2.0 发布了！",   // 可选
  "image_base64": "<png/jpg 的 base64>",  // 可选，最大约 15MB
  "image_name": "share.png"
}
```

同时给了图和文字时先发图、再发文字。返回：

```json
{"ok": true, "chat_id": "123@chatroom", "verified": true, "error": ""}
```

- `409`：`to` 匹配到 0 个或多个会话，返回 `candidates`，请改用 `chat_id`。
- `verified=false`：界面上发出去了，但 8 秒内没在该会话的消息表里看到，可能发错会话；桥接会随即熔断。
- `502` + `发送已熔断`：之前出过核对失败，需先人工检查再 `POST /resume`。

### `GET /healthz`

`{"ok": true, "healthy": true, "problems": [], "wechat": true, "halted": "", "paused": false,
"open_chat": {"n": 12, "p50": 1.5, "max": 1.9, "last": 0.1}}`：`wechat=false` 表示最近一次发送失败；
`paused` 是文件传输助手 `/暂停` 的状态；`open_chat` 是最近几次「搜索并进入会话」的耗时（秒）；
`halted` 非空表示已**熔断**——某次发送后核对失败（疑似发错会话）或发送中途出错，
此后所有发送（bot 回复、提醒、API）一律拒绝。

### `GET /fuyu`

浮屿仓库同步缓存（见 `bridge/fuxi_bridge/fuyu.py`）。

- 不带参数：`{"brief": "...", "version": "浮屿最新正式版 v0.3.13（…）", "latest": {release}, "latest_beta": {release}, "events": [...]}`
- `?issue=1234`：`{"number": 1234, "status": "#1234「…」已修复：PR #1235 合并于 2026-11-23，包含在 v0.3.13（…）"}`
- `?q=设置页闪退`：相关 issue 前 5 个 `{"items": [{score, number, title, state, url, status}]}`，`score` 0~1

### `POST /resume`

人工确认微信界面无误后解除熔断。重启桥接容器也会解除。

### 白名单 `GET /allow`、`PUT /allow`、`POST /allow`

bot 只回复白名单里的会话。名单存在桥接状态卷的 `allow.txt`（每行一个 chat_id，`#` 后是注释），
**改文件或调接口都立即生效，不用重启**，并自动同步到内核（`wechat.whitelistOnly` + `care.whitelist`）。

- 空名单 = 谁都不回；`"*"` = 全部放行（慎用）。
- 名单以外还回哪些、哪些会话永远不回，是设置「回复范围」`reply_scope` 和「不回的会话」`blocked_chats`
  （`POST /settings`、管理台设置页、遥控 `/设置`），见 deploy.md「白名单」。
- 元素可以是 chat_id 或**唯一**的显示名（备注/昵称/群名），有歧义返回 409。
- `PUT {"items": [...]}` 整体替换；`POST {"add": [...], "remove": [...]}` 增删。

```bash
curl -s -H "Authorization: Bearer $T" -H "Content-Type: application/json" \
  -X POST -d '{"add":["某个群名"]}' 127.0.0.1:8790/allow
```

注意：白名单只约束 bot 的**自动回复**和内核提醒；`/send` 是调用方明确指定目标的主动发送，不受它限制。

#### 用模板发

把 `text` 换成 `"template": "模板名", "vars": {"who": "阿青"}`。模板在管理台「发送」页维护（`GET /templates` 看有哪些、
各要什么变量）；`{date}` `{time}` `{weekday}` 不给也会自动填。缺变量返回 `400`，不会发半截消息。

#### 异步发送

加 `"async": true`：立即返回 `202 {"ok": true, "chat_id", "job": {"id", "status": "queued"}}`，
之后 `GET /jobs/<id>` 查 `{status: queued|running|done|failed, ok, verified, error}`。
任务只记在内存里（最近 200 个），桥接重启后查不到就当没发成。适合调用方不想阻塞 3~10 秒、或一次排很多条。

### 定时发送 `POST /schedule`、`GET /schedule`、`POST /schedule/<id>/cancel`

```json
{"to": "浮屿用户交流群", "at": "2026-11-25 20:00", "text": "今晚 8 点发新版～"}
```

- 时间：`at` 给 Unix 秒或服务器本地时间字符串（`2026-11-25 20:00`、`2026-11-25T20:00:30`），或者 `in_minutes`。
  最远 90 天。
- 内容：`text`、`template`+`vars`（到点那一刻再套，`{time}` 是到点的时间），可加 `image_base64`。
- 存在 `/state/schedule.json`，重启不丢。到点时桥接没在运行、晚了 10 分钟以上的记为「错过」**不补发**，
  并私聊主人；发送失败也私聊主人。发到一半桥接重启的记为失败，不重发（避免发两遍）。
- 暂停自动回复不影响定时消息；熔断时不发（和其他发送一样）。
- 遥控：`/定时` 列出等发的，`/定时 取消 编号`。管理台「发送」页也能定时和取消。

### `POST /announce`、`GET /announce?key=`

要人确认的群发（浮屿发版公告）：先发到文件传输助手预览，主人在那里回 `/发布 编号` 才真的发到群。

```json
{"key": "fuyu-v1.2.3", "text": "浮屿 v1.2.3 已更新…", "image_base64": "<png>", "to": "可省，默认用 /设置 发版群"}
```

→ `{"ok": true, "created": true, "item": {"key", "code", "to", "status", "status_zh"}}`。`key` 是幂等键：
同一个 key 再调只返回原记录（`created: false`），不会重复预览或重复发群。`status`：`pending` 等确认、
`sent` 已发出、`cancelled`、`expired`（24 小时没确认）、`failed`（发了一半不会自动重试，避免刷屏）。
遥控：`/待发` 列出、`/发布 [编号]`、`/取消 [编号]`。

## 示例（在本机通过 SSH 调用）

```bash
python3 -c 'import base64,json;print(json.dumps({"chat_id":"123@chatroom","text":"新版本发布","image_base64":base64.b64encode(open("share.png","rb").read()).decode()}))' \
  | ssh <服务器> 'T=$(grep ^FUXI_API_TOKEN= /path/to/fuxi/.env | cut -d= -f2); curl -s -H "Authorization: Bearer $T" -H "Content-Type: application/json" --data-binary @- 127.0.0.1:8790/send'
```

## 从 CI 调用（可选，默认没装）

浮屿现在在 Mac 上发版，经 SSH 调 `/announce`（`FUYU_FUXI=<ssh 主机>:<部署目录>`）。以后要放到 GitHub Actions 里跑，
不要把 API 暴露到公网，也不要给 CI 一把能登服务器的普通钥匙，用**只能发公告的 SSH 钥匙**：

1. 生成 CI 专用钥匙：`ssh-keygen -t ed25519 -N '' -C fuyu-ci -f fuyu-ci`，私钥放进浮屿仓库的 Actions secret。
2. 把 `deploy/announce-gateway.sh` 复制到服务器部署目录（`chmod 700`，属主同 `.env`）。
3. 服务器上部署用户的 `~/.ssh/authorized_keys` 加一行（`restrict` 关掉端口转发、终端等）：
   ```
   command="<部署目录>/announce-gateway.sh",restrict ssh-ed25519 AAAA... fuyu-ci
   ```
4. CI 里照常用 `share-push`：它执行的命令会被强制命令替换掉，JSON 照样从 stdin 进，网关只转给 `POST /announce`。

这把钥匙泄露的最坏结果：往主人的文件传输助手塞公告预览（要主人回 `/发布` 才进群），发不了别的、也读不到东西。
不用了就删掉 `authorized_keys` 那一行。网关每次被调用会写一条系统日志（`journalctl -t fuxi-announce-gateway`）。
