# 管理台前端（bridge/admin，React + Vite）：只在构建阶段用 Node，最终镜像里只有打包好的静态文件
FROM node:22-slim AS admin
WORKDIR /admin
RUN corepack enable
COPY admin/package.json admin/pnpm-lock.yaml admin/pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile
COPY admin/ ./
RUN pnpm build

FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends xdotool xclip x11-apps libxss1 tzdata \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt \
 # rapidocr 会拉入带 GUI 依赖的 opencv-python，换成 headless 版
 && pip uninstall -y opencv-python opencv-python-headless \
 && pip install --no-cache-dir --force-reinstall --no-deps opencv-python-headless

COPY fuxi_bridge /app/fuxi_bridge
COPY --from=admin /admin/dist /app/admin
WORKDIR /app
ENV PYTHONUNBUFFERED=1 DISPLAY=:1 TZ=Asia/Shanghai
# 静默时段（/设置 静默时段）按这个时区解释

# 部署脚本传入的版本（git 提交号），/状态 和 /healthz 里显示；放最后，不影响前面的构建缓存
ARG FUXI_BUILD=dev
ENV FUXI_BUILD=$FUXI_BUILD
CMD ["python", "-m", "fuxi_bridge.app"]
