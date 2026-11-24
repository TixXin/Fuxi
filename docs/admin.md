# 网页管理台：架构与开发约定

> 2026-11-24 定稿，开发前的准备。界面约定见 [design/admin.md](design/admin.md)，静态预览见
> [design/admin-preview.html](design/admin-preview.html)。ROADMAP 里对应 P3「网页管理台」。

## 目标与边界

- 给主人一个网页：看状态、看对话流、改设置和白名单、处理发版公告和群反馈、编辑人设、手动发消息。
- 只有主人一个用户，不做账号体系和权限分级。
- 能做的事不超过文件传输助手遥控指令 + 现有对外 API 的范围；不新增「读任意聊天记录」「导出数据」这类能力。
- 对话流只显示桥接内存里最近的消息（重启清空），不新落盘聊天内容。

## 组成

```
浏览器 ──HTTPS──> EdgeOne ──HTTP──> OpenResty（管理台域名）
                                     │ Basic 认证（和网页版微信同一组用户名密码）
                                     │ 补请求头 X-Fuxi-Admin: <FUXI_ADMIN_TOKEN>
                                     ▼
                         宿主 127.0.0.1:8791 ──> fuxi-bridge 里的管理服务（admin.py）
                                                   ├─ /            前端静态文件（镜像里 /app/admin）
                                                   ├─ /api/*       管理接口（读写桥接状态）
                                                   ├─ /api/events  服务端推送（SSE）
                                                   └─ /api/kernel/* 白名单内的内核接口，转发到 127.0.0.1:8787
```

- **独立端口**：管理服务和对外发送 API（8790）分开。8790 继续「只给机器调用、不反代到公网」（见 deploy.md「安全边界」）；8791 是唯一允许经反代出去的口子，接口按人用的场景设计。
- **端口映射**：bridge 和内核同一网络命名空间，所以 8791 也加在 compose 里 kernel 服务的 `ports`：`127.0.0.1:${FUXI_ADMIN_PORT:-8791}:8791`。只绑宿主回环。
- **静态文件和接口同源**：前端由管理服务自己发，不需要 CORS。

## 认证与安全

1. **Basic 认证在 OpenResty 做**：用网页版微信同一组用户名密码生成 htpasswd（服务器上，不进仓库）。
2. **管理服务只认带口令的请求**：
   - OpenResty 在转发时补 `X-Fuxi-Admin: <FUXI_ADMIN_TOKEN>`，并先清掉客户端自带的同名头。
   - 管理服务用恒定时间比较；没设 `FUXI_ADMIN_TOKEN` 时管理服务不启动。
   - 这样即使有人在宿主上直连 8791，或反代配置漏了 Basic 认证，也进不来。
3. **写操作防跨站**：
   - 所有非 GET 请求必须是 `Content-Type: application/json`，并带 `X-Requested-With: fuxi-admin`；浏览器跨站表单发不出这两个。
   - `Origin` 存在时必须和 `Host` 一致。
4. **缓存**：
   - 接口一律 `Cache-Control: private, no-store`，防 EdgeOne 缓存。
   - `/assets/*`（文件名带哈希）可以长缓存，`index.html` 不缓存。
5. **危险操作**（发送、发布公告、移出白名单、重启）：前端先确认，接口也要求请求体带 `"confirm": true`；发送另有 `"dry_run"`。
6. **内核接口只转发白名单里的路径和方法**，例如 `GET /api/persona`、`POST /api/persona`、`GET /api/status`。
   - `GET /api/config` 转发前删掉密钥字段（`apiKey`、`token` 之类）。
   - `POST /api/config`、`/api/config/clear-secrets`、`/api/config/reset` 不开放。
7. **日志**：管理服务的访问日志不记请求体和消息正文，和 `FUXI_LOG_TEXT` 的约定一致。

新增的部署配置只有 `.env` 里两项：
- `FUXI_ADMIN_TOKEN`：`openssl rand -hex 16`，同时写进 OpenResty 配置。
- 可选的 `FUXI_ADMIN_PORT`。

这两项是部署参数，不是行为设置；行为设置照旧进 `settings.json`。

## 管理接口（第一版草案）

统一约定：
- 请求、响应都是 JSON。
- 成功 `{ok: true, ...}`，失败 `{ok: false, error: "给人看的中文原因"}` 加合适的 HTTP 状态码。
- 时间用 Unix 秒（浮点）。

| 方法 路径 | 用途 | 数据来源 |
|---|---|---|
| `GET /api/overview` | 总状态、三个服务、今天的数字、待办事项（待发公告、转人工） | `/healthz` 的同一套检查、`/状态` 的各行、`announcer.pending()`、`handoffs` |
| `GET /api/events` | SSE：`message`（新收到/新回复，带耗时）、`status`（状态变化）、`settings`（设置被改） | 新增的事件中心（见下） |
| `GET /api/messages?before=&limit=` | 对话流的历史（内存里最近 500 条） | 事件中心的环形缓冲 |
| `GET /api/settings`、`POST /api/settings` | 同对外 API 的 `/settings` | `Bridge.get_setting` / `set_setting` |
| `GET /api/allow`、`POST /api/allow` | 白名单增删 | 同对外 API |
| `GET /api/chats?q=` | 搜会话（最近说过话的在前） | 同对外 API，加最近活跃时间 |
| `POST /api/pause`、`POST /api/resume` | 暂停/恢复回复；解除熔断 | `set_paused`、`/resume` |
| `GET /api/announce`、`POST /api/announce/{code}/publish`、`…/cancel` | 待发公告 | `Announcer` |
| `GET /api/feedback`、`POST /api/feedback/...` | 群反馈收件箱、摘要 | `feedback.py`（F1 负责人对接） |
| `GET /api/knowledge`、`POST /api/knowledge/sync`、`GET /api/knowledge/search?q=` | 资料库 | `Knowledge` |
| `GET /api/fuyu` | 浮屿仓库同步状态 | `Fuyu` |
| `POST /api/send` | 手动发送：`dry_run` 只打开核对；真发要 `confirm` | `SendJob(source="admin")` |
| `GET /api/schedule`、`POST /api/schedule`（要 `confirm`）、`POST /api/schedule/{id}/cancel` | 定时发送 | `schedule.Scheduler` |
| `GET /api/templates`、`POST /api/templates`、`…/delete`、`…/render` | 消息模板 | `schedule.Templates` |
| `GET /api/stats?days=14` | 回复节奏：每个白名单会话的收到/回复次数、长度、用时、连发、没被 @ 就开口（只有汇总数字），缓存 10 分钟 | 聊天记录（只读） |
| `GET /api/persona`、`POST /api/persona`、`POST /api/persona/preset` | 人设读写、套用预设（要 `confirm`）；只转发人设字段，不碰内核的密钥和模型配置 | 内核 `/api/persona*` |

对外 API 和管理接口的处理逻辑放在同一组函数里，两个服务都调用，不复制粘贴。
做法是把 `api.py` 里的处理函数抽成 `bridge/fuxi_bridge/handlers.py`，两边只负责认证和路由。

### 事件中心

- 新增 `bridge/fuxi_bridge/events.py`：线程安全的发布/订阅，外加最近 500 条消息的环形缓冲，只在内存里。
- `app.py` 在以下几处各发一个事件，每处一行：
  - 收到消息
  - 决定不回（带原因：不在白名单、静默、限速……）
  - 回复完成（带 `timing.Trace` 的分段）
  - 暂停、熔断、设置变化
- SSE 连接每 15 秒发一个注释行保活。OpenResty 对 `/api/events` 关缓冲（`proxy_buffering off`），读超时调长。

## 前端

- **位置**：`bridge/admin/`。放在 bridge 目录下，原因有两个：
  - `deploy.sh` 用 `bridge/` 作构建上下文，不用改脚本。
  - 「工作区有没提交的改动」的检查自动覆盖前端。
- **技术栈**：
  - React 19、Vite 7、TypeScript 5.8、lucide-react，版本跟浮屿对齐。
  - 包管理用 pnpm，有 `pnpm-lock.yaml`。
  - 不引入组件库、状态库、路由库：页面切换用 `location.hash`，数据用一个小的 `useApi` 钩子加 SSE 订阅。
- **目录约定**：

  ```
  bridge/admin/
    index.html
    package.json  pnpm-lock.yaml  tsconfig.json  vite.config.ts
    src/
      main.tsx  App.tsx
      api.ts                 # fetch 封装（带 X-Requested-With）、SSE 订阅、类型
      styles/tokens.css      # 从浮屿抄的变量（见 design/admin.md）
      styles/base.css
      components/            # Switch、Segmented、Row、Group、ConfirmBar、Chip……一个组件一个 .tsx + 同名 .css
      pages/                 # Overview、Stream、Chats、Persona、Settings、Announce、Feedback、Knowledge、Send
  ```

- **文件大小**：单文件不超过 300 行，超了就拆。
- **样式**：
  - 组件的样式写在同名 `.css`，类名用 `组件名__部分` 形式（照浮屿 `settings-row__main`）。
  - 颜色只用变量，不写死色值。
- **镜像构建**：`bridge/Dockerfile` 加一个 `node:22-slim` 构建阶段：
  1. `corepack enable && pnpm install --frozen-lockfile && pnpm build`
  2. 把 `dist/` 拷到最终镜像的 `/app/admin`
  - 最终镜像里没有 Node。
  - `bridge/.dockerignore` 排除 `admin/node_modules`、`admin/dist`。
- **本地开发**：
  1. `ssh -L 8791:127.0.0.1:8791 <服务器>` 把线上管理服务接到本机。
  2. `pnpm dev`，Vite 把 `/api` 代理到 `127.0.0.1:8791`，并补上 `X-Fuxi-Admin`（从本机环境变量 `FUXI_ADMIN_TOKEN` 读，不写进文件）。
  - 也可以 `pnpm dev:mock`，用 `src/mock/` 里的假数据，不连服务器。

## 测试

- **后端**：`admin.py`、`handlers.py`、`events.py` 的单元测试照旧放 `bridge/tests/`，`scripts/test.sh` 一起跑。必须覆盖：
  - 没带口令 → 401；口令错 → 401
  - 写操作缺 `X-Requested-With` 或 Content-Type 不对 → 403
  - `Origin` 不符 → 403
  - 内核转发白名单外的路径 → 404；`GET /api/config` 不含密钥
  - 发送没 `confirm` → 400
- **前端**：`pnpm typecheck`（`tsc --noEmit`）和 `pnpm build` 必须通过。
  - 先不上组件测试框架；关键的纯函数（时间格式化、设置项到控件的映射）用 `node --test` 写几个。
- **改完界面**：用 headless 浏览器截概览、对话流、设置三页，深色、浅色和 390px 宽各一张，自己看过再交。

## 部署

- 第一次上线要在服务器上做三件事。这些都是服务器配置，动手前先和用户确认：
  1. `.env` 加 `FUXI_ADMIN_TOKEN`
  2. 用网页版微信的用户名密码生成 htpasswd
  3. OpenResty 新增管理台站点：照网页版微信那份配置的写法，加 Basic 认证、补口令头、SSE 关缓冲、`no-store`
- 管理台域名走现有的泛解析，不用另加解析记录。
- 域名、服务器路径都不写进仓库。
- 之后更新前端或接口，照常 `scripts/deploy.sh bridge`。

## 分期

| 期 | 内容 | 验收 |
|---|---|---|
| A 骨架 | 管理服务、认证、事件中心、`handlers.py` 抽取；前端骨架、侧栏、深浅色、概览页 | 线上能登录看到真实状态，401/403 测试全过 |
| B 只读 | 对话流（SSE）、设置页（自动生成，可改）、资料库页 | 群里来一条消息，对话流 2 秒内出现，带耗时 |
| C 管理 | 会话/白名单、发版公告、群反馈 | 网页上发布一条公告，效果和 `/发布` 一样 |
| D 编辑 | 人设、手动发送（空跑 + 确认） | 改人设后下一条回复生效 |

## 和其他会话协作

- 这个仓库常有别的会话同时在改。会改到别人也在动的文件时，开工前先打招呼：
  - 共享文件：`app.py`（事件中心埋点）、`api.py`（抽 handlers）、`compose.yaml`（端口）、`Dockerfile`
  - 管理台自己的新文件：`admin.py`、`handlers.py`、`events.py`、`bridge/admin/`
- 提交只 `git add` 自己的文件。
