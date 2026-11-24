FROM alpine:3.20
RUN apk add --no-cache docker-cli docker-cli-compose curl tzdata
ENV TZ=Asia/Shanghai
COPY watchdog.sh /watchdog.sh
RUN chmod +x /watchdog.sh
# 部署脚本传入的版本（git 提交号），/状态 和 /healthz 里显示；放最后，不影响前面的构建缓存
ARG FUXI_BUILD=dev
ENV FUXI_BUILD=$FUXI_BUILD
CMD ["/watchdog.sh"]
