FROM alpine:3.20
RUN apk add --no-cache docker-cli docker-cli-compose curl tzdata
ENV TZ=Asia/Shanghai
COPY watchdog.sh /watchdog.sh
RUN chmod +x /watchdog.sh
CMD ["/watchdog.sh"]
