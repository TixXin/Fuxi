# 对外发送 API

桥接内置一个小 HTTP 服务，供其他项目往微信会话/群里发文字或图片（例如浮屿发版时把分享图发到用户群）。

- 监听：宿主 `127.0.0.1:${FUXI_API_PORT}`（默认 8790），**不对公网暴露**。
  从别的机器调用请走 SSH（`ssh <服务器> curl ...`）或 SSH 隧道。
- 鉴权：`Authorization: Bearer ${FUXI_API_TOKEN}`（服务器 `.env` 里，留空则 API 不启动）。
- 发送是串行的，一次请求会等到真正发完并在数据库里核对后才返回（通常 3~10 秒）。

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

`{"ok": true, "wechat": true, "halted": ""}`：`wechat=false` 表示最近一次发送失败；
`halted` 非空表示已**熔断**——某次发送后核对失败（疑似发错会话）或发送中途出错，
此后所有发送（bot 回复、提醒、API）一律拒绝。

### `POST /resume`

人工确认微信界面无误后解除熔断。重启桥接容器也会解除。

### 白名单 `GET /allow`、`PUT /allow`、`POST /allow`

bot 只回复白名单里的会话。名单存在桥接状态卷的 `allow.txt`（每行一个 chat_id，`#` 后是注释），
**改文件或调接口都立即生效，不用重启**，并自动同步到内核（`wechat.whitelistOnly` + `care.whitelist`）。

- 空名单 = 谁都不回；`"*"` = 全部放行（慎用）。
- 元素可以是 chat_id 或**唯一**的显示名（备注/昵称/群名），有歧义返回 409。
- `PUT {"items": [...]}` 整体替换；`POST {"add": [...], "remove": [...]}` 增删。

```bash
curl -s -H "Authorization: Bearer $T" -H "Content-Type: application/json" \
  -X POST -d '{"add":["某个群名"]}' 127.0.0.1:8790/allow
```

注意：白名单只约束 bot 的**自动回复**和内核提醒；`/send` 是调用方明确指定目标的主动发送，不受它限制。

## 示例（在本机通过 SSH 调用）

```bash
python3 -c 'import base64,json;print(json.dumps({"chat_id":"123@chatroom","text":"新版本发布","image_base64":base64.b64encode(open("share.png","rb").read()).decode()}))' \
  | ssh <服务器> 'T=$(grep ^FUXI_API_TOKEN= /path/to/fuxi/.env | cut -d= -f2); curl -s -H "Authorization: Bearer $T" -H "Content-Type: application/json" --data-binary @- 127.0.0.1:8790/send'
```
