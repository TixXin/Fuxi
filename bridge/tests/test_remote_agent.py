"""微信 ↔ 电脑端远程 Agent 实时多轮遥控测试：会话启动、长轮询、多轮续接、排队、打断、关窗退出、图片透传与 OwnerAgent 集成。"""

import os
import tempfile
import threading
import time
import unittest
from unittest import mock

from fuxi_bridge import control, events, feedback as fb, issues as isu, knowledge as kn, owner_agent as oa, remote_agent as ra, settings
from fuxi_bridge.app import Bridge
from fuxi_bridge.messages import Message
from fuxi_bridge.sending import SendJob


def make_bridge():
    d = tempfile.mkdtemp()
    b = object.__new__(Bridge)
    b.self_wxid = "wxid_bot"
    b.kernel_ready = False
    b.paused = False
    b.masters = ["wxid_owner"]
    b.cfg = settings.Settings(os.path.join(d, "settings.json"), {})
    b.cfg.set("owners", "wxid_owner")
    b.cfg.set("knowledge_repo", "TixXin/Fuyu")
    b.events = events.Events()
    b.names_cache = {"g@chatroom": "浮屿用户交流群", "wxid_owner": "煜哥", "wxid_u1": "群友A"}
    b.kernel = mock.Mock()
    b.knowledge = kn.Knowledge(os.path.join(d, "kn"))
    b.fuyu = mock.Mock()
    b.feedback = fb.Feedback(os.path.join(d, "fb"), complete=None, fuyu=b.fuyu)
    b.fixflow = isu.FixFlow(b.feedback, isu.Issues(lambda: "TixXin/Fuyu", token="tok"))
    b.reader = mock.Mock()
    b.reader.history.return_value = []
    b.images = mock.Mock()
    b.owner_chat = lambda: "wxid_owner"
    b._control_hashes = []
    b._sent = []

    def fake_send(job: SendJob):
        job.ok = True
        b._sent.append((job.chat, job.text))
        return job

    b.send = fake_send
    b.remote = ra.RemoteAgent(b, os.path.join(d, "remote"))
    b.control = control.Control(b)
    b.agent = oa.OwnerAgent(b, d)
    return b


def wait_sent(b, count: int, timeout: float = 2.0):
    deadline = time.monotonic() + timeout
    while len(b._sent) < count and time.monotonic() < deadline:
        time.sleep(0.02)
    return list(b._sent)


class RemoteAgentTest(unittest.TestCase):
    def test_start_long_poll_reply_followup_queue_interrupt_and_close(self):
        b = make_bridge()
        r = b.remote
        r.heartbeat("mac", "MacBookPro")
        self.assertTrue(r.is_node_online("mac"))

        # 后台线程模拟 Mac 长轮询等待任务
        polled = []

        def poller():
            polled.extend(r.poll("mac", wait=3.0, host="MacBookPro"))

        t = threading.Thread(target=poller, daemon=True)
        t.start()
        time.sleep(0.05)

        sess, dm = r.start_session("mac", "TixXin/fuyu-plugins", "看下最新 PR 并给出意见", readonly=True)
        self.assertEqual(sess["id"], "R1")
        self.assertIn("只读审查模式", dm)
        self.assertNotIn("离线", dm)
        t.join(timeout=2.0)
        self.assertEqual(len(polled), 1)
        self.assertEqual(polled[0]["op"], "run")
        self.assertEqual(polled[0]["repo"], "TixXin/fuyu-plugins")
        self.assertTrue(polled[0]["readonly"])

        # 运行中主人又发了一条追问 → 自动排队
        q_msg = r.handle_private_message("顺便看下 manifest 权限声明合理不")
        self.assertIn("已排进 #R1 队列", q_msg)

        # 第一轮回复到达 → 私发主人，并自动把排队中的下一条下发给 Mac
        res = r.deliver_reply(
            sid="R1", seq=polled[0]["seq"], ok=True,
            text="审完了：这个 FreeControl 插件整体很干净，只有 icon 路径需要改下。",
            claude_session_id="csid-abc-123", duration_s=12.4, cost_usd=0.025,
        )
        self.assertTrue(res["queued_next"])
        sent = wait_sent(b, 1)
        self.assertEqual(sent[0][0], "wxid_owner")
        self.assertIn("FreeControl", sent[0][1])
        self.assertIn("MAC #R1 · 12s · $0.025", sent[0][1])

        # Mac 立刻领到排队的第 2 轮，且自动带上了 claude_session_id
        cmds2 = r.poll("mac", wait=0.1, host="MacBookPro")
        self.assertEqual(len(cmds2), 1)
        self.assertEqual(cmds2[0]["claude_session_id"], "csid-abc-123")
        self.assertIn("manifest", cmds2[0]["prompt"])

        # 打断当前轮
        stop_msg = r.handle_private_message("停一下")
        self.assertIn("已向 MAC（#R1）发送打断信号", stop_msg)
        cmds_stop = r.poll("mac", wait=0.1)
        self.assertEqual(cmds_stop[0]["op"], "cancel")

        # 退出并关窗
        exit_msg = r.handle_private_message("退出 agent")
        self.assertIn("已退出 MAC 远程会话 #R1", exit_msg)
        self.assertFalse(r.has_active())
        cmds_close = r.poll("mac", wait=0.1)
        self.assertEqual(cmds_close[0]["op"], "close")

    def test_idle_timeout_auto_closes_and_offline_warning(self):
        b = make_bridge()
        r = b.remote
        # 未连线时应给出离线提示
        _, dm = r.start_session("mac", "TixXin/Fuyu", "查下编译报错", readonly=False)
        self.assertIn("尚未连线", dm)
        r.deliver_reply("R1", 1, True, "查到了", claude_session_id="c1")
        wait_sent(b, 1)

        # 模拟 2 小时无互动 → tick 自动关闭并推送 close 指令
        r.active["last_at"] = time.time() - ra.IDLE_TIMEOUT - 10
        r.tick()
        self.assertFalse(r.has_active())
        sent = wait_sent(b, 2)
        self.assertIn("2 小时无互动", sent[1][1])
        cmds = r.poll("mac", wait=0.1)
        self.assertTrue(any(c["op"] == "close" for c in cmds))

    def test_chunk_splitting_preserves_readability(self):
        long_text = "\n\n".join([f"第{i}段：" + "测试内容" * 120 for i in range(6)])
        chunks = ra.split_chunks(long_text, limit=600)
        self.assertLessEqual(len(chunks), 4)
        self.assertIn("输出过长已截断", chunks[-1])

    def test_owner_agent_group_trigger_private_passthrough_and_image_forwarding(self):
        b = make_bridge()
        b.remote.heartbeat("mac", "MacBookPro")
        b.kernel.complete.return_value = (
            '{"mode":"act","calls":[{"tool":"remote_start","args":'
            '{"target":"mac","repo":"TixXin/fuyu-plugins","prompt":"把最新的 PR 看下给出意见","readonly":true}}]}'
        )
        # 群里 @浮汐 触发：群里只给简短确认，私聊发启动回执
        grp_reply = b.agent.maybe_handle("g@chatroom", {
            "chat_type": "group", "sender_id": "wxid_owner", "user_id": "wxid_owner",
            "text": "@浮汐 把最新的插件 PR 看下，把看法私发我", "at_me": True, "reply_to_bot": False,
            "self_names": ["浮汐"],
        })
        self.assertIn("已交给 MAC 处理（会话 #R1", grp_reply)
        sent = wait_sent(b, 1)
        self.assertEqual(sent[0][0], "wxid_owner")
        self.assertIn("已启动 MAC 远程会话 #R1", sent[0][1])

        # 先消费掉第 1 轮并回传，让会话进入等待回复态
        c1 = b.remote.poll("mac", wait=0.1)
        b.remote.deliver_reply("R1", c1[0]["seq"], True, "看法如下", claude_session_id="c99")
        wait_sent(b, 2)

        # 活跃期间，主人在私聊里发消息 + 附图：直接透传给 Mac，不再调用路由器模型
        b.kernel.complete.reset_mock()
        b.images.load.return_value = (b"\xff\xd8\xff\xe0IMGDATA", "full")
        img_msg = Message(chat="wxid_owner", local_id=10, sort_seq=10, type="image", sender="wxid_owner",
                          is_self=False, create_time=int(time.time()), text="[图片]", source="", shard="m0", media="m123")
        dm_reply = b.agent.maybe_handle("wxid_owner", {
            "chat_type": "private", "sender_id": "wxid_owner", "user_id": "wxid_owner",
            "text": "按我截的这个图帮我改一下", "at_me": False, "reply_to_bot": False,
            "self_names": ["浮汐"],
        }, batch=[img_msg])
        b.kernel.complete.assert_not_called()
        self.assertIn("已发给 MAC（#R1）继续执行（附 1 张图）", dm_reply)
        c2 = b.remote.poll("mac", wait=0.1)
        self.assertEqual(len(c2[0]["images"]), 1)
        self.assertEqual(c2[0]["claude_session_id"], "c99")

        # 非主人绝无法操控远程会话
        self.assertIsNone(b.agent.maybe_handle("wxid_u1", {
            "chat_type": "private", "sender_id": "wxid_u1", "user_id": "wxid_u1",
            "text": "退出 agent", "at_me": False, "reply_to_bot": False, "self_names": ["浮汐"],
        }))
        self.assertTrue(b.remote.has_active())

        # 主人私聊发「关闭终端」→ 结束会话，恢复正常聊天
        out_exit = b.agent.maybe_handle("wxid_owner", {
            "chat_type": "private", "sender_id": "wxid_owner", "user_id": "wxid_owner",
            "text": "关闭终端", "at_me": False, "reply_to_bot": False, "self_names": ["浮汐"],
        })
        self.assertIn("已退出 MAC 远程会话 #R1", out_exit)
        self.assertFalse(b.remote.has_active())

    def test_owner_continuous_group_command_without_at_and_deliver_to_group(self):
        b = make_bridge()
        b.remote.heartbeat("mac", "MacBookPro")
        # 刚在群里对话过（3 分钟内）→ 下一句不带 @ 也能触发 OwnerAgent
        b.agent.note_owner_reply("g@chatroom")
        b.kernel.complete.return_value = (
            '{"mode":"act","calls":[{"tool":"remote_start","args":'
            '{"target":"mac","repo":"TixXin/Fuyu","prompt":"查源码解释钥匙串弹窗","readonly":true,"deliver_to":"group"}}]}'
        )
        grp_reply = b.agent.maybe_handle("g@chatroom", {
            "chat_type": "group", "sender_id": "wxid_owner", "user_id": "wxid_owner",
            "text": "在mac 启动 claude 查下这个是什么原因 然后结果发群里", "at_me": False, "reply_to_bot": False,
            "self_names": ["浮汐"],
        })
        self.assertIn("查完结果会直接发到本群", grp_reply)
        c1 = b.remote.poll("mac", wait=0.1)
        b.remote.deliver_reply("R1", c1[0]["seq"], True, "源码核实结论：这是浮屿读取 Claude Code 凭据用于展示用量，点拒绝不影响主功能。")
        sent = wait_sent(b, 3)  # 启动私聊 + 群发结论 + 私聊回执
        group_msgs = [t for chat, t in sent if chat == "g@chatroom"]
        self.assertEqual(len(group_msgs), 1)
        self.assertIn("源码核实结论", group_msgs[0])
        self.assertNotIn("退出 agent", group_msgs[0])  # 群发不带主人控制尾注


if __name__ == "__main__":
    unittest.main()
