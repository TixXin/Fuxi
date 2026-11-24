FROM node:22-slim

WORKDIR /app
COPY . /app
# 浮汐扩展的依赖（上游内核零依赖；链接阅读增强要 Readability + linkedom）
RUN cd fuxi && npm ci --omit=dev --no-audit --no-fund && npm cache clean --force
ENV NODE_ENV=production TZ=Asia/Shanghai
VOLUME /app/data

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD node -e "fetch('http://127.0.0.1:'+(process.env.FUXI_KERNEL_PORT||8787)+'/healthz').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"

# 部署脚本传入的版本（git 提交号），/状态 和 /healthz 里显示；放最后，不影响前面的构建缓存
ARG FUXI_BUILD=dev
ENV FUXI_BUILD=$FUXI_BUILD
CMD ["sh", "-c", "node docker-entrypoint.js && exec node -r ./fuxi/preload.js server.js"]
