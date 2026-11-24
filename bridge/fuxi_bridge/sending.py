"""发送：单线程串行的发送队列、发送前等人停手、发送后到库里核对（核对失败熔断），以及限速和打字节奏。

Bridge 通过 SendingMixin 拿到这些方法；SendJob 是各处往发送队列里放的任务。
"""

from __future__ import annotations

import hashlib
import logging
import random
import threading
import time
from dataclasses import dataclass, field

from .sender import SendError

log = logging.getLogger("fuxi")


def _h(text: str) -> str:
    return hashlib.sha1(text.strip().encode()).hexdigest()


def _squash(text: str) -> str:
    """核对用：去掉所有空白（微信存库时可能改动换行/空格），其余逐字比较。"""
    return "".join(text.split())


@dataclass
class SendJob:
    chat: str               # username
    text: str | None = None
    source: str = "bot"     # bot（自动回复，受限速）| reminder | api
    image: str | None = None
    sticker: str | None = None  # 表情包文件（按文件粘贴，GIF 保持动图）
    at: str = ""                # 群里真 @ 的对象（成员在群里的显示名）；用不上真 @ 时退回文字「@名字」
    verify: bool = True
    done: threading.Event = field(default_factory=threading.Event)
    ok: bool = False
    verified: bool | None = None
    error: str = ""
    # 耗时（time.monotonic）：入队、开始处理、回车发出、核对完成；进入会话用时（秒）
    t_queued: float = 0.0
    t_start: float = 0.0
    t_sent: float = 0.0
    t_done: float = 0.0
    open_s: float = 0.0


class SendingMixin:
    # ---------- 节奏 ----------
    def in_quiet_hours(self, now: float | None = None) -> bool:
        if not self.quiet or "-" not in self.quiet:
            return False
        try:
            a, b = (int(x[:2]) * 60 + int(x[3:5]) for x in self.quiet.split("-", 1))
        except ValueError:
            return False
        t = time.localtime(now or time.time())
        m = t.tm_hour * 60 + t.tm_min
        return a <= m < b if a <= b else (m >= a or m < b)

    def rate_ok(self, chat: str) -> bool:
        cut = time.time() - 60
        recent = [c for t, c in self.sent_times if t >= cut]
        return len(recent) < self.rate_global and recent.count(chat) < self.rate_chat

    @staticmethod
    def typing_delay(text: str) -> float:
        """模拟打字：约每秒 6 字，0.8~6 秒，加随机抖动。"""
        return min(6.0, 0.8 + len(text) / 6) + random.uniform(0, 0.8)

    @classmethod
    def first_delay(cls, text: str, spent: float) -> float:
        """第一个气泡：像人边想边打，模型已经花掉的时间算进打字时间里，至少留 0.3~0.8 秒。"""
        return max(random.uniform(0.3, 0.8), cls.typing_delay(text) - spent)

    # ---------- 发送（单线程串行） ----------
    def send(self, job: SendJob, timeout: float = 90) -> SendJob:
        job.t_queued = time.monotonic()
        self.sendq.put(job)
        job.done.wait(timeout)
        if not job.done.is_set():
            job.error = "发送排队超时"
        return job

    def _send_worker(self) -> None:
        while True:
            job = self.sendq.get()
            # 遥控回报（只发文件传输助手）在熔断时也放行，否则主人看不到 /状态
            if self.halted and not (job.source == "control" and job.chat == "filehelper"):
                job.error = f"发送已熔断：{self.halted}"
                job.done.set()
                continue
            try:
                self._attempt(job)
                for retry in range(self.RETRIES):
                    if not (job.error and "放弃发送" in job.error) or self.halted:
                        break
                    log.warning("发送到 %s 被放弃，%.1f 秒后重试（%d/%d）：%s", self.name_of(job.chat),
                                self.RETRY_DELAY, retry + 1, self.RETRIES, job.error)
                    time.sleep(self.RETRY_DELAY)
                    job.error = ""
                    self._attempt(job)
            finally:
                job.t_done = time.monotonic()
                job.done.set()
                time.sleep(0.6)  # 气泡间隔

    RETRIES = 1          # 「放弃发送」（还没输入任何内容）后重试次数
    RETRY_DELAY = 1.5

    def _wait_for_human(self, job: SendJob) -> bool:
        """有人正在网页版微信里操作时先等他停手，免得抢键盘鼠标、把他打了一半的字一起发出去。"""
        quiet, max_wait = self.cfg.get("human_quiet_s"), self.cfg.get("human_max_wait_s")
        if not quiet:
            return True
        t0 = time.monotonic()
        while True:
            idle = self.sender.human_idle()
            if idle is None or idle >= quiet:
                if time.monotonic() - t0 > 0.5:
                    log.info("等人停手 %.0f 秒后发往 %s", time.monotonic() - t0, self.name_of(job.chat))
                return True
            if time.monotonic() - t0 >= max_wait:
                job.error = f"有人在用微信，等了 {max_wait} 秒还没停手，这条没发"
                self.last_abort = (time.time(), job.error)
                log.warning("发往 %s 没发：%s", self.name_of(job.chat), job.error)
                return False
            if time.monotonic() - t0 < 0.5:
                log.info("有人在用微信（%.0f 秒前操作过），等停手 %d 秒再发往 %s", idle, quiet, self.name_of(job.chat))
            time.sleep(max(0.5, min(3.0, quiet - idle)))

    def _attempt(self, job: SendJob) -> None:
        """发送一次。放弃（未发出）只记 job.error；粘贴之后出错或核对失败才熔断。"""
        if not self._wait_for_human(job):
            self.last_send_ok = False
            return
        try:
            t0 = int(time.time()) - 2
            job.t_start = time.monotonic()
            self.sender.last_open = 0.0
            name = self.name_of(job.chat)
            if job.text is not None and job.at:
                self.sent_hashes.append(_h(job.text))
                self.sender.send_text_at(name, job.at, job.text)
            elif job.text is not None:
                self.sent_hashes.append(_h(job.text))
                self.sender.send_text(name, job.text)
            if job.image:
                self.sender.send_image(name, job.image)
            if job.sticker:
                self.sender.send_sticker(name, job.sticker)
            if job.image or job.sticker:
                self.media_sent[job.chat] = time.time()
            job.ok = True
            job.t_sent = time.monotonic()
            job.open_s = self.sender.last_open
            if job.verify:
                job.verified = self._verify(job, t0)
                if not job.verified:
                    self.sender.forget_chat()
                    self.halted = f"{time.strftime('%H:%M:%S')} 发往「{name}」后核对失败，可能发错会话"
                    log.critical("已熔断，停止一切发送：%s。确认无误后 POST /resume 恢复", self.halted)
            self.last_send_ok = job.verified is not False
        except (SendError, OSError) as e:
            job.error = str(e)
            # 回车之后出错时界面状态未知，保守起见熔断；回车之前的错误都带「放弃发送」
            if "放弃发送" not in str(e):
                self.halted = f"{time.strftime('%H:%M:%S')} 发往「{job.chat}」时出错：{e}"
                log.critical("已熔断：%s", self.halted)
            else:
                self.last_abort = (time.time(), str(e))
            self.last_send_ok = False
            log.error("发送到 %s 失败：%s", job.chat, e)

    def _verify(self, job: SendJob, t0: int, wait: float = 8) -> bool:
        want = _squash(job.text or "")
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            try:
                for mtype, text in self.reader.self_sent_since(job.chat, t0):
                    if job.image and mtype == "image":
                        return True
                    if job.sticker and mtype in ("emoji", "image"):
                        return True
                    if job.text is not None and _squash(text) == want:
                        return True
                    if job.at and _squash(text).startswith("@") and _squash(text).endswith(want):
                        return True   # 真 @ 存库是「@群昵称\u2005正文」
            except Exception:
                log.debug("核对读取失败", exc_info=True)
            time.sleep(0.5)
        return False
