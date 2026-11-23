"""直连 DeepSeek（OpenAI 兼容接口），只用于阶段 3 的最小闭环；接入内核后由内核调模型。"""

from __future__ import annotations

import json
import os
import urllib.request


def chat(text: str, system: str | None = None, timeout: float = 60) -> str:
    key = os.environ["DEEPSEEK_API_KEY"]
    base = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
    model = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": text})
    req = urllib.request.Request(
        base + "/chat/completions",
        data=json.dumps({"model": model, "messages": msgs, "max_tokens": 800}).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.load(r)
    return data["choices"][0]["message"]["content"].strip()
