"""产品助手：挑文件、切段、检索阈值、增量同步、群里被 @ 时附资料或转人工。合成数据。"""

import collections
import json
import os
import tempfile
import time
import unittest
from unittest import mock

from fuxi_bridge import events
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

    def test_synonym_and_per_candidate_coverage(self):
        k = object.__new__(kn.Knowledge)
        mac_doc = kn.Chunk(
            "docs/features/macos-install.md",
            "在 Mac 上安装浮屿 › 第一次打开",
            "系统会提示无法验证这个应用，点完成，去系统设置里点仍要打开，或者执行 xattr -dr com.apple.quarantine /Applications/Fuyu.app",
        )
        # 故意放一篇只匹配了「Mac」的低覆盖文档，检验不会一票否决后面的真正命中
        noisy = kn.Chunk("docs/features/other.md", "Mac 权限", "在 Mac 上需要开几个权限")
        filler = [kn.Chunk("x", f"其他功能{i}", f"待办事项第{i}条，日历提醒会议，音乐歌词同步") for i in range(30)]
        k.index = kn.Index([mac_doc, noisy] + filler)
        hits = k.search("Mac 提示已损坏打不开怎么办")
        self.assertTrue(hits)
        self.assertEqual(hits[0].title, mac_doc.title)

    def test_empty_heading_and_faq_pick(self):
        cs = kn.chunk_file("docs/features/a.md", "# 总标题\n\n## ![img](x.png)\n\n正文内容需要超过二十个字符才能通过切段函数的最小长度门槛")
        self.assertEqual(cs[0].title, "总标题")
        self.assertIn("docs/faq.md", kn.pick(["README.md", "docs/faq.md"]))

    def test_local_faq_crud_and_search(self):
        root = tempfile.mkdtemp()
        k = kn.Knowledge(root, "o/r")
        self.assertEqual(k.faq_list(), [])
        items = k.faq_save("支持 Linux 吗", "目前只支持 Windows 10/11 和 macOS 15+（Apple Silicon），暂不支持 Linux。")
        self.assertEqual(len(items), 1)
        fid = items[0]["id"]
        # 重启后仍在索引里，且能被搜到
        k2 = kn.Knowledge(root, "o/r")
        k2.floor = 1.0
        self.assertEqual(k2.search("支持 Linux 吗")[0].source, "local/faq")
        # 修改与删除
        items = k2.faq_save("支持 Linux 系统吗", "暂不支持 Linux，支持 Win 和 Mac。", fid=fid)
        self.assertEqual(items[0]["q"], "支持 Linux 系统吗")
        self.assertEqual(k2.faq_delete(fid), [])
        with self.assertRaises(ValueError):
            k2.faq_delete(fid)

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
    b.kernel = mock.Mock()
    b.kernel.complete.side_effect = lambda system, text, n: "1" if "贴" in text.split("资料：")[0] else "0"
    b.handoffs = collections.deque(maxlen=50)
    b.events = events.Events()
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

    def test_non_assistant_group_ignored(self):
        b = assist_bridge()
        p = payload("@浮汐 截图怎么贴")
        before = p["text"]
        b._assist("h@chatroom", p)
        self.assertEqual(p["text"], before)

    def test_proactive_qa_wakes_and_attaches_when_answerable(self):
        b = assist_bridge()
        p = payload("截图怎么贴到屏幕上", at=False)
        with mock.patch("fuxi_bridge.assist.random.random", return_value=0.0):
            b._assist("g@chatroom", p)
        self.assertTrue(p["at_me"])
        self.assertIn("【截图 › 贴图】", p["text"])
        self.assertIn("没直接 @ 你", p["text"])
        b.send.assert_not_called()                                    # 主动答疑不转人工

    def test_proactive_qa_stays_silent_when_unanswerable(self):
        b = assist_bridge()
        p = payload("浮屿能导出到 Notion 吗", at=False)
        before = p["text"]
        with mock.patch("fuxi_bridge.assist.random.random", return_value=0.0):
            b._assist("g@chatroom", p)
        self.assertFalse(p["at_me"])
        self.assertEqual(p["text"], before)
        b.send.assert_not_called()

    def test_proactive_skips_when_talking_to_others(self):
        b = assist_bridge()
        for extra in ({"at_other": True}, {"reply_to_user_id": "wxid_b"}, {"mentioned_names": ["小红"]}):
            p = {**payload("截图怎么贴到屏幕上", at=False), **extra}
            before = p["text"]
            with mock.patch("fuxi_bridge.assist.random.random", return_value=0.0):
                b._assist("g@chatroom", p)
            self.assertFalse(p["at_me"])
            self.assertEqual(p["text"], before)

    def test_proactive_fuyu_chat_respects_probability_and_cooldown(self):
        b = assist_bridge()
        b.cfg.set("fuyu_chat_pct", 20)
        b.cfg.set("fuyu_chat_cooldown", 240)
        # 非浮屿闲聊：完全不动，交给内核走 3%
        p0 = payload("今天中午吃什么好呢", at=False)
        with mock.patch("fuxi_bridge.assist.random.random", return_value=0.0):
            b._assist("g@chatroom", p0)
        self.assertFalse(p0["at_me"])

        # 概率未命中（0.5 >= 0.20）
        p1 = payload("浮屿这次更新真好用", at=False)
        with mock.patch("fuxi_bridge.assist.random.random", return_value=0.5), \
             mock.patch("fuxi_bridge.assist.time.monotonic", return_value=1000.0):
            b._assist("g@chatroom", p1)
        self.assertFalse(p1["at_me"])

        # 概率命中（0.1 < 0.20）→ 唤醒并记录冷却
        p2 = payload("浮屿这次更新真好用", at=False)
        with mock.patch("fuxi_bridge.assist.random.random", return_value=0.1), \
             mock.patch("fuxi_bridge.assist.time.monotonic", return_value=1000.0):
            b._assist("g@chatroom", p2)
        self.assertTrue(p2["at_me"])
        self.assertIn("在聊浮屿", p2["text"])

        # 冷却期内（+60s < 240s）不再主动插话
        p3 = payload("浮屿的灵动岛很丝滑", at=False)
        with mock.patch("fuxi_bridge.assist.random.random", return_value=0.0), \
             mock.patch("fuxi_bridge.assist.time.monotonic", return_value=1060.0):
            b._assist("g@chatroom", p3)
        self.assertFalse(p3["at_me"])

        # 冷却过后（+300s >= 240s）再次允许
        p4 = payload("浮屿的灵动岛很丝滑", at=False)
        with mock.patch("fuxi_bridge.assist.random.random", return_value=0.0), \
             mock.patch("fuxi_bridge.assist.time.monotonic", return_value=1300.0):
            b._assist("g@chatroom", p4)
        self.assertTrue(p4["at_me"])

    def test_proactive_generic_version_word_without_fuyu_ignored(self):
        b = assist_bridge()
        b.fuyu.latest.return_value = {"tag_name": "v0.3.13"}
        b.fuyu.version_brief.return_value = "浮屿最新正式版 v0.3.13"
        # 未 @ 且没提浮屿，泛泛说「微信更新了吗」不应误判为浮屿版本提问
        p = payload("微信最新版本是多少？", at=False)
        with mock.patch("fuxi_bridge.assist.random.random", return_value=0.0):
            b._assist("g@chatroom", p)
        self.assertFalse(p["at_me"])
        # 提了浮屿就触发
        p2 = payload("浮屿最新版本是多少？", at=False)
        with mock.patch("fuxi_bridge.assist.random.random", return_value=0.0):
            b._assist("g@chatroom", p2)
        self.assertTrue(p2["at_me"])
        self.assertIn("v0.3.13", p2["text"])

    def test_proactive_respects_dismiss_silence(self):
        b = assist_bridge()
        b.masters = ["wxid_m"]
        with mock.patch("fuxi_bridge.assist.time.monotonic", return_value=1000.0):
            b._assist("g@chatroom", {**payload("@浮汐 闭嘴", at=True), "sender_id": "wxid_m"})
        # 静默期内（+60s < 600s）未 @ 的浮屿提问/闲聊都不会主动触发
        p = payload("截图怎么贴到屏幕上", at=False)
        with mock.patch("fuxi_bridge.assist.random.random", return_value=0.0), \
             mock.patch("fuxi_bridge.assist.time.monotonic", return_value=1060.0):
            b._assist("g@chatroom", p)
        self.assertFalse(p["at_me"])
        # 主人显式 @ 后解除静默
        with mock.patch("fuxi_bridge.assist.time.monotonic", return_value=1070.0):
            b._assist("g@chatroom", {**payload("@浮汐 回来吧", at=True), "sender_id": "wxid_m"})
        p2 = payload("截图怎么贴到屏幕上", at=False)
        with mock.patch("fuxi_bridge.assist.random.random", return_value=0.0), \
             mock.patch("fuxi_bridge.assist.time.monotonic", return_value=1080.0):
            b._assist("g@chatroom", p2)
        self.assertTrue(p2["at_me"])

    def test_proactive_qa_yields_when_new_message_arrives(self):
        b = assist_bridge()
        b._chat_msg_seq = {"g@chatroom": 5}
        b.paused = False
        b.cfg.set("fuyu_qa_delay", 2)
        now = [0.0]

        def fake_sleep(dt):
            now[0] += dt
            if now[0] >= 0.5:
                b._chat_msg_seq["g@chatroom"] = 6   # 等待期间群里来了新消息 → 让贤

        p = {**payload("截图怎么贴到屏幕上", at=False), "_chat_seq": 5}
        with mock.patch("fuxi_bridge.assist.random.random", return_value=0.0), \
             mock.patch("fuxi_bridge.assist.time.monotonic", lambda: now[0]), \
             mock.patch("fuxi_bridge.assist.time.sleep", fake_sleep):
            b._assist("g@chatroom", p)
        self.assertFalse(p["at_me"])

        # 等待期间没人接话 → 正常主动答疑
        b._chat_msg_seq["g@chatroom"] = 6
        now[0] = 0.0
        p2 = {**payload("截图怎么贴到屏幕上", at=False), "_chat_seq": 6}
        with mock.patch("fuxi_bridge.assist.random.random", return_value=0.0), \
             mock.patch("fuxi_bridge.assist.time.monotonic", lambda: now[0]), \
             mock.patch("fuxi_bridge.assist.time.sleep", lambda dt: now.__setitem__(0, now[0] + dt)):
            b._assist("g@chatroom", p2)
        self.assertTrue(p2["at_me"])

    def test_fit_append_keeps_under_kernel_limit_and_preserves_tail(self):
        from fuxi_bridge.assist import PAYLOAD_TEXT_LIMIT, _fit_append
        out = _fit_append("原问题", "\n\n（资料）\n", "长" * 3000, "\n\n（尾注不要丢）")
        self.assertLessEqual(len(out), PAYLOAD_TEXT_LIMIT)
        self.assertTrue(out.startswith("原问题\n\n（资料）\n"))
        self.assertTrue(out.endswith("…\n\n（尾注不要丢）"))

    def test_proactive_feedback_fallback_and_cooldown(self):
        b = assist_bridge()
        b.masters = ["wxid_owner"]
        # 群友报新 bug（没写「浮屿」、资料库也查不到）→ 兜底接住
        p = {**payload("目前还有一个小问题，放到任务栏鼠标移上去色块会压暗不和谐", at=False), "sender_id": "wxid_u1"}
        with mock.patch("fuxi_bridge.assist.random.random", return_value=0.0), \
             mock.patch("fuxi_bridge.assist.time.monotonic", return_value=1000.0):
            b._assist("g@chatroom", p)
        self.assertTrue(p["at_me"])
        self.assertIn("反馈浮屿的问题或建议", p["text"])
        # 120 秒内连发第二条不重复兜底
        p2 = {**payload("而且移动到任务栏还是有阴影，这一块底下", at=False), "sender_id": "wxid_u1"}
        with mock.patch("fuxi_bridge.assist.random.random", return_value=0.0), \
             mock.patch("fuxi_bridge.assist.time.monotonic", return_value=1030.0):
            b._assist("g@chatroom", p2)
        self.assertFalse(p2["at_me"])
        # 主人自己说 bug 不触发兜底
        p3 = {**payload("这里有个小问题阴影不对，我晚上修", at=False), "sender_id": "wxid_owner"}
        with mock.patch("fuxi_bridge.assist.random.random", return_value=0.0), \
             mock.patch("fuxi_bridge.assist.time.monotonic", return_value=1200.0):
            b._assist("g@chatroom", p3)
        self.assertFalse(p3["at_me"])
        # 群友只 @ 主人报 bug → 允许兜底；@ 普通群友 → 不插嘴
        p4 = {**payload("@tixxin 建议加个导出按钮", at=False), "sender_id": "wxid_u2", "at_other": True, "at_owner_only": True}
        with mock.patch("fuxi_bridge.assist.random.random", return_value=0.0), \
             mock.patch("fuxi_bridge.assist.time.monotonic", return_value=1300.0):
            b._assist("g@chatroom", p4)
        self.assertTrue(p4["at_me"])
        p5 = {**payload("@小明 建议你重启试试", at=False), "sender_id": "wxid_u2", "at_other": True, "at_owner_only": False}
        with mock.patch("fuxi_bridge.assist.random.random", return_value=0.0), \
             mock.patch("fuxi_bridge.assist.time.monotonic", return_value=1500.0):
            b._assist("g@chatroom", p5)
        self.assertFalse(p5["at_me"])

    def test_private_chat_attaches_fuyu_context(self):
        b = assist_bridge()
        b.names_cache["wxid_owner"] = "煜哥"
        b.fuyu.latest.return_value = {"tag_name": "v0.3.14-beta.1"}
        b.fuyu.version_brief.return_value = "浮屿最新测试版 v0.3.14-beta.1"
        p = {**payload("浮屿现在最新版本是多少", at=False), "chat_type": "private"}
        b._assist("wxid_owner", p)
        self.assertIn("v0.3.14-beta.1", p["text"])
        b.send.assert_not_called()                                    # 私聊不转人工

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

    def test_bug_report_mentioning_update_still_hands_off(self):
        b = assist_bridge()
        b.fuyu.latest.return_value = {"tag_name": "v0.3.13"}
        b.fuyu.find.return_value = [(0.41, {"number": 1030})]
        with mock.patch("threading.Thread"):
            p = payload("@浮汐 更新后打不开了怎么办")
            b._assist("g@chatroom", p)
        self.assertIn("已经转给主人", p["text"])
        self.assertEqual(len(b.handoffs), 1)

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

    def test_judge_drops_unrelated_and_hands_off(self):
        b = assist_bridge()
        b.kernel.complete.side_effect = None
        b.kernel.complete.return_value = "0"
        b._handoff = mock.Mock()                 # 不能 patch threading.Thread：_judge 的线程池也用它
        p = payload("@浮汐 截图能自动上传到图床吗")
        b._assist("g@chatroom", p)
        b._handoff.assert_called_once()
        self.assertNotIn("【截图", p["text"])
        self.assertIn("已经转给主人", p["text"])

    def test_judge_error_keeps_hits(self):
        b = assist_bridge()
        b.kernel.complete.side_effect = RuntimeError("内核挂了")
        p = payload("@浮汐 截图怎么贴到屏幕上")
        b._assist("g@chatroom", p)
        self.assertIn("【截图 › 贴图】", p["text"])

    def test_is_question(self):
        for q in ("翻译用的什么引擎", "剪贴板历史能保存多久", "更新后打不开了", "能导出吗"):
            self.assertTrue(kn.is_question(q), q)
        self.assertFalse(kn.is_question("哈哈哈好用"))

    def test_clean_question_words(self):
        self.assertNotIn("么用", kn.tokens(kn.clean("截图怎么用")))


class BootCatchupTest(unittest.TestCase):
    def _bridge(self, tmp):
        from fuxi_bridge.allow import AllowList
        from fuxi_bridge.messages import Message

        b = assist_bridge()
        b.self_wxid = "wxid_bot"
        b.extra_names = ["浮汐"]
        b.masters = ["wxid_owner"]
        b.paused = False
        b.quiet = ""
        b.boot_catchup_min = 60
        b.catchup_path = os.path.join(tmp, "boot_catchup.json")
        b.pending_boot = ["g@chatroom", "wxid_p1"]
        b._chat_msg_seq = {}
        b.names_cache.update({"wxid_p1": "小王", "wxid_owner": "煜哥", "wxid_u1": "Lucifer", "wxid_u2": "路人"})
        b.allowlist = AllowList(os.path.join(tmp, "allow.txt"), "g@chatroom, wxid_p1, wxid_p2")
        b.reader = mock.Mock()
        b.reader.watermarks = {}
        b.reader.self_names.return_value = ["浮汐"]
        b._revoked = lambda ms: set()
        b.is_feedback_group = lambda c: False
        b.pool = mock.Mock()
        self.Message = Message
        return b

    def _msg(self, chat, seq, text, sender="wxid_u1", is_self=False, mtype="text", source="", age=1200):
        return self.Message(
            chat=chat, local_id=seq, sort_seq=seq, type=mtype,
            sender=sender, is_self=is_self, create_time=int(time.time() - age),
            text=text, source=source, shard="message_0.db",
        )

    def test_group_feedback_and_private_catchup_and_dedup(self):
        tmp = tempfile.mkdtemp()
        b = self._bridge(tmp)
        now = int(time.time())
        b.reader.sessions.return_value = [
            {"username": "g@chatroom", "last_timestamp": now - 600},
            {"username": "wxid_p1", "last_timestamp": now - 300},
            {"username": "wxid_p2", "last_timestamp": now - 300},
            {"username": "filehelper", "last_timestamp": now - 60},
        ]
        # 群里：Lucifer 报 bug + @主人（无人回复），路人发了句闲聊「哈哈哈」
        g_hist = [
            self._msg("g@chatroom", 10, "早上的公告", sender="wxid_bot", is_self=True, age=3000),
            self._msg("g@chatroom", 11, "哈哈哈", sender="wxid_u2", age=1500),
            self._msg("g@chatroom", 12, "目前还有一个小问题，放到任务栏旁边色块会压暗", sender="wxid_u1", age=1200),
            self._msg("g@chatroom", 13, "@tixxin", sender="wxid_u1",
                      source="<atuserlist>wxid_owner</atuserlist>", age=1190),
            self._msg("g@chatroom", 14, "而且移动到任务栏还是有阴影", sender="wxid_u1", age=1100),
        ]
        # 私聊 p1：对方发消息后 bot 还没回；私聊 p2：bot 已经回过了
        p1_hist = [self._msg("wxid_p1", 20, "在吗帮我看看", sender="wxid_p1", age=300)]
        p2_hist = [
            self._msg("wxid_p2", 30, "早", sender="wxid_p2", age=400),
            self._msg("wxid_p2", 31, "早呀", sender="wxid_bot", is_self=True, age=390),
        ]
        b.reader.history.side_effect = lambda c, s: {"g@chatroom": g_hist, "wxid_p1": p1_hist, "wxid_p2": p2_hist}[c]

        with mock.patch.dict(os.environ, {"FUXI_BUILD": "v1"}):
            n = b._boot_catchup()
        self.assertEqual(n, 2)
        dispatched = [(call.args[1], [m.sort_seq for m in call.args[2]], call.args[5])
                      for call in b.pool.submit.call_args_list]
        self.assertEqual(dispatched, [
            ("g@chatroom", [12, 13, 14], True),   # 路人闲聊不补回，Lucifer 反馈补回
            ("wxid_p1", [20], True),              # 未回私聊补回，已回私聊 p2 跳过
        ])
        self.assertEqual(b.pending_boot, [])      # 已从 pending_boot 移出防重复

        # 同版本再次重启：去重不再重复提交
        b.pool.submit.reset_mock()
        with mock.patch.dict(os.environ, {"FUXI_BUILD": "v1"}):
            self.assertEqual(b._boot_catchup(), 0)
        b.pool.submit.assert_not_called()

        # 升级新版本重启（若仍没人回复）：允许重新评估一次
        with mock.patch.dict(os.environ, {"FUXI_BUILD": "v2"}):
            self.assertEqual(b._boot_catchup(), 2)

    def test_owner_replied_or_disabled_skips_catchup(self):
        tmp = tempfile.mkdtemp()
        b = self._bridge(tmp)
        now = int(time.time())
        b.reader.sessions.return_value = [{"username": "g@chatroom", "last_timestamp": now - 100}]
        b.reader.history.return_value = [
            self._msg("g@chatroom", 10, "任务栏有个小问题有阴影", sender="wxid_u1", age=600),
            self._msg("g@chatroom", 11, "收到我今晚修", sender="wxid_owner", age=300),
        ]
        self.assertEqual(b._boot_catchup(), 0)
        b.pool.submit.assert_not_called()

        # boot_catchup_min = 0 关闭补回
        b.boot_catchup_min = 0
        self.assertEqual(b._boot_catchup(), 0)


if __name__ == "__main__":
    unittest.main()
