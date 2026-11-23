FROM node:22-slim

WORKDIR /app
COPY . /app
ENV NODE_ENV=production TZ=Asia/Shanghai
VOLUME /app/data

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD node -e "fetch('http://127.0.0.1:'+(process.env.FUXI_KERNEL_PORT||8787)+'/healthz').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"

CMD ["sh", "-c", "node docker-entrypoint.js && exec node server.js"]
