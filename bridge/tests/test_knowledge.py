"""产品助手：挑文件、切段、检索阈值、增量同步、群里被 @ 时附资料或转人工。合成数据。"""

import collections
import json
import os
import tempfile
import unittest
from unittest import mock

from fuxi_bridge import knowledge as kn
from fuxi_bridge import settings

FEATURE = """# 截图

浮屿内置的区域截图：冻结屏幕、选区或选中窗口、标注，然后复制、保存、贴到屏幕上。

## 贴图

按 F3 把截图贴到屏幕上，贴图可以拖动、缩放、旋转，双击关闭。
"""

RELEASE = """## 关于这个版本

**下载**：[Windows](https://x/a.exe)。安装包没有代码签名，Windows SmartScreen 提示"未知发布者"时，点「更多信息 → 仍要运行」。

## 更新内容

### 剪贴板

- **剪贴板管理视图**：可以多选、一次删掉多条。历史条数上限从 200 提到 500。
- **收藏网页链接**：复制网址后岛上弹出「已复制链接」卡片。

## What's New

- **Clipboard manager**: multi-select.
"""


class PickChunkTest(unittest.TestCase):
    def test_pick(self):
        paths = ["README.md", "docs/privacy.md", "docs/features/screenshot.md", "docs/features/sub/x.md", "src/a.ts",
                 "docs/releases/v0.3.8.md", "docs/releases/v0.3.9-beta.1.md", "docs/releases/v0.3.9.md",
                 "docs/releases/v0.3.10-beta.1.md", "docs/releases/README.md"]
        got = kn.pick(paths)
        self.assertEqual(got[:3], ["README.md", "docs/privacy.md", "docs/features/screenshot.md"])
        # 正式版全要；测试版只要比最新正式版新的
        self.assertEqual(got[3:], ["docs/releases/v0.3.10-beta.1.md", "docs/releases/v0.3.9.md", "docs/releases/v0.3.8.md"])

    def test_feature_sections(self):
        cs = kn.chunk_file("docs/features/screenshot.md", FEATURE)
        self.assertEqual([c.title for c in cs], ["截图", "截图 › 贴图"])

    def test_release_items_and_about(self):
        cs = kn.chunk_file("docs/releases/v0.3.9.md", RELEASE)
        self.assertEqual([c.title for c in cs], ["v0.3.9 · 剪贴板 · 剪贴板管理视图", "v0.3.9 · 剪贴板 · 收藏网页链接"])
        self.assertNotIn("Clipboard", "".join(c.text for c in cs))       # 英文段不要
        about = kn.chunk_file("docs/releases/v0.3.9.md", RELEASE, about=True)
        self.assertEqual(about[-1].title, "v0.3.9 下载、安装和升级")
        self.assertIn("未知发布者", about[-1].text)
        self.assertEqual(kn.latest_stable(["docs/releases/v0.3.9.md", "docs/releases/v0.3.10-beta.1.md",
                                           "docs/releases/v0.3.8.md"]), "docs/releases/v0.3.9.md")


class SearchTest(unittest.TestCase):
    def setUp(self):
        k = object.__new__(kn.Knowledge)
        chunks = kn.chunk_file("docs/features/screenshot.md", FEATURE) + kn.chunk_file("docs/releases/v0.3.9.md", RELEASE, True)
        filler = [kn.Chunk("x", f"其他功能{i}", f"待办事项第{i}条，日历提醒会议，音乐歌词同步，翻译窗口快捷键") for i in range(30)]
        k.index = kn.Index(chunks + filler)
        self.k = k

    def test_relevant_and_irrelevant(self):
        self.assertEqual(self.k.search("截图怎么贴到屏幕上")[0].title, "截图 › 贴图")
        self.assertIn("未知发布者", self.k.search("Windows 提示未知发布者怎么办")[0].text)
        self.assertEqual(self.k.search("今天晚上吃什么"), [])
        self.assertEqual(self.k.search("你好"), [])

    def test_is_question(self):
        self.assertTrue(kn.is_question("这个能关掉吗"))
        self.assertTrue(kn.is_question("怎么设置"))
        self.assertFalse(kn.is_question("哈哈哈哈"))


class SyncTest(unittest.TestCase):
    def test_incremental(self):
        root = tempfile.mkdtemp()
        k = kn.Knowledge(root, "o/r")
        listing = {"": [{"path": "README.md", "type": "file", "sha": "r1"}, {"path": "docs", "type": "dir", "sha": "d"}],
                   "docs": [{"path": "docs/privacy.md", "type": "file", "sha": "p1"}],
                   "docs/features": [{"path": "docs/features/screenshot.md", "type": "file", "sha": "a"}],
                   "docs/releases": []}
        calls = []

        def get(url, raw=False, timeout=30):
            calls.append(url)
            if raw:
                return FEATURE.encode()
            d = url.split("/contents/", 1)[1]
            return json.dumps(listing[d]).encode()
        k._get = get
        self.assertIn("3 个文件（新下载 3 个）", k.sync())
        self.assertEqual(len(calls), 4 + 3)
        self.assertIn("api.github.com/repos/o/r/git/blobs/a", calls[-1])
        calls.clear()
        k.sync()
        self.assertEqual(len(calls), 4)                     # sha 没变不再下载
        listing["docs/features"][0]["sha"] = "b"
        calls.clear()
        k.sync()
        self.assertEqual(len(calls), 5)                     # 只下变了的那个
        self.assertIn("docs__features__screenshot.md@b", os.listdir(os.path.join(root, "files")))
        self.assertNotIn("docs__features__screenshot.md@a", os.listdir(os.path.join(root, "files")))
        k2 = kn.Knowledge(root, "o/r")                      # 重启后从磁盘重建
        self.assertEqual(len(k2.index.chunks), len(k.index.chunks))
        for d in listing:
            listing[d] = []
        with self.assertRaises(RuntimeError):
            k.sync()
        self.assertIn("没找到文档", k.meta["error"])


def assist_bridge():
    from fuxi_bridge.app import Bridge
    b = object.__new__(Bridge)
    b.cfg = settings.Settings(os.path.join(tempfile.mkdtemp(), "s.json"), {})
    b.cfg.set("assistant_groups", "浮屿用户群")
    b.names_cache = {"g@chatroom": "浮屿用户群", "h@chatroom": "别的群"}
    k = object.__new__(kn.Knowledge)
    k.index = kn.Index(kn.chunk_file("docs/features/screenshot.md", FEATURE)
                       + [kn.Chunk("x", f"其他{i}", f"待办日历音乐歌词翻译第{i}条") for i in range(30)])
    k.floor = 3.0          # 合成语料小、idf 低，分数比真实文档低
    b.knowledge = k
    b.fuyu = mock.Mock(latest=mock.Mock(return_value=None), find=mock.Mock(return_value=[]),
                       version_brief=mock.Mock(return_value="还没有同步到浮屿的版本信息"))
    b.handoffs = collections.deque(maxlen=50)
    b.send = mock.Mock(return_value=mock.Mock(ok=True))
    return b


def payload(text, at=True, name="小明"):
    return {"text": text, "at_me": at, "reply_to_bot": False, "self_names": ["浮汐"], "senderName": name}


class AssistTest(unittest.TestCase):
    def test_attaches_knowledge(self):
        b = assist_bridge()
        p = payload("@浮汐 截图怎么贴到屏幕上")
        b._assist("g@chatroom", p)
        self.assertIn("【截图 › 贴图】", p["text"])
        self.assertTrue(p["text"].startswith("@浮汐 截图怎么贴到屏幕上\n\n（以下是浮屿的说明资料"))

    def test_only_when_addressed_in_assistant_group(self):
        b = assist_bridge()
        for chat, p in (("g@chatroom", payload("截图怎么贴到屏幕上", at=False)), ("h@chatroom", payload("@浮汐 截图怎么贴"))):
            before = p["text"]
            b._assist(chat, p)
            self.assertEqual(p["text"], before)

    def test_handoff_once(self):
        b = assist_bridge()
        with mock.patch("threading.Thread") as th:
            th.return_value.start = lambda: th.call_args.kwargs["target"]()
            p = payload("@浮汐 能导出到 Notion 吗")
            b._assist("g@chatroom", p)
            self.assertIn("已经转给主人", p["text"])
            msg = b.send.call_args.args[0]
            self.assertEqual(msg.chat, "filehelper")
            self.assertIn("小明 在「浮屿用户群」问", msg.text)
            b._assist("g@chatroom", payload("@浮汐 那能导出到飞书吗"))   # 3 分钟内同群不重复私聊
            self.assertEqual(b.send.call_count, 1)
        p = payload("@浮汐 哈哈哈")
        b._assist("g@chatroom", p)
        self.assertEqual(p["text"], "@浮汐 哈哈哈")                       # 不是提问：什么都不加

    def test_version_question_uses_fuyu(self):
        b = assist_bridge()
        b.fuyu.latest.return_value = {"tag_name": "v0.3.13"}
        b.fuyu.version_brief.return_value = "浮屿最新正式版 v0.3.13（2026-11-24 发布）"
        p = payload("@浮汐 现在最新版本是多少？")
        b._assist("g@chatroom", p)
        self.assertIn("浮屿最新正式版 v0.3.13", p["text"])
        b.send.assert_not_called()                                    # 有版本信息就不转人工

    def test_issue_number_and_bug_match(self):
        b = assist_bridge()
        b.fuyu.status.side_effect = lambda n: f"#{n} 已修复"
        p = payload("@浮汐 #1234 修了吗")
        b._assist("g@chatroom", p)
        self.assertIn("#1234 已修复", p["text"])
        b.fuyu.find.assert_not_called()
        b.fuyu.find.return_value = [(0.59, {"number": 7}), (0.3, {"number": 8})]
        p = payload("@浮汐 全屏时提醒看不到")
        b._assist("g@chatroom", p)
        self.assertIn("#7 已修复", p["text"])
        self.assertNotIn("#8", p["text"])                            # 低于阈值的不带
        b.fuyu.find.reset_mock()
        b._assist("g@chatroom", payload("@浮汐 怎么设置开机自启"))     # 用法问题不查 issue
        b.fuyu.find.assert_not_called()


if __name__ == "__main__":
    unittest.main()
