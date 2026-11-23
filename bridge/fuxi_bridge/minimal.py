"""阶段 3 最小闭环：盯住指定会话 → 新消息 → DeepSeek → 回复到同一会话。

  python -m fuxi_bridge.minimal

环境变量：
  FUXI_DB_ROOT     微信 db_storage 路径（只读挂载）
  FUXI_STATE_DIR   水位等状态（持久卷）
  FUXI_WORK_DIR    解密库与密钥缓存（tmpfs）
  FUXI_WATCH       逗号分隔的会话 username，默认 filehelper
  FUXI_REPLY_SELF  1 = 自己发的消息也回复（文件传输助手自测必须开）
  DEEPSEEK_API_KEY
"""

from __future__ import annotations

import collections
import hashlib
import logging
import os
import time

from . import llm, messages, store
from .sender import Sender

log = logging.getLogger("fuxi.minimal")

SYSTEM = "你是一个微信里的聊天助手，回复简短自然，用中文。"


def _h(text: str) -> str:
    return hashlib.sha1(text.strip().encode()).hexdigest()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    db_root = os.environ.get("FUXI_DB_ROOT") or store.find_db_root(os.environ.get("FUXI_WX_CONFIG", "/wxconfig"))
    state = os.environ.get("FUXI_STATE_DIR", "/state")
    work = os.environ.get("FUXI_WORK_DIR", "/work")
    watch = [c.strip() for c in os.environ.get("FUXI_WATCH", "filehelper").split(",") if c.strip()]
    reply_self = os.environ.get("FUXI_REPLY_SELF") == "1"
    interval = float(os.environ.get("FUXI_POLL_INTERVAL", "1.0"))

    keyring = store.KeyRing(db_root, os.path.join(work, "keys.json"))
    cache = store.DbCache(db_root, os.path.join(work, "db"), keyring)
    reader = messages.MessageReader(cache, os.path.join(state, "watermarks.json"),
                                    messages.self_wxid_from_root(db_root))
    sender = Sender(display=os.environ.get("DISPLAY", ":1"))
    sent = collections.deque(maxlen=200)  # 自己发出的回复指纹，防止回复自己的回复

    for chat in watch:
        reader.poll(chat)  # 初始化水位，不回放历史
    log.info("开始监听 %s（reply_self=%s）", ",".join(watch), reply_self)

    while True:
        for chat in watch:
            try:
                new = reader.poll(chat)
            except Exception:
                log.exception("读取 %s 失败", chat)
                continue
            for m in new:
                if m.type != "text" or not m.text.strip():
                    continue
                if _h(m.text) in sent:
                    continue  # 这是 bot 自己刚发的
                if m.is_self and not reply_self:
                    continue
                log.info("收到 %s: %s", chat, m.text[:60])
                try:
                    reply = llm.chat(m.text, SYSTEM)
                except Exception:
                    log.exception("调用模型失败")
                    continue
                sent.append(_h(reply))
                try:
                    sender.send_text(reader.display_name(chat), reply)
                except Exception:
                    log.exception("发送失败")
        time.sleep(interval)


if __name__ == "__main__":
    main()
