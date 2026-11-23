"""搜索结果定位与标题核对：用合成截图，不含任何真实聊天内容。"""

import struct
import unittest

from fuxi_bridge.sender import _Shot, title_matches

W, H = 1760, 1280
WHITE, LIST_BG, GREEN, DARK, HOVER = (250, 250, 250), (238, 238, 240), (7, 193, 96), (40, 40, 40), (237, 237, 237)


class Canvas:
    def __init__(self, bpp=24):
        self.bpp = bpp
        self.px = [[WHITE] * W for _ in range(H)]

    def rect(self, x0, y0, x1, y1, color):
        for y in range(y0, y1):
            row = self.px[y]
            for x in range(x0, x1):
                row[x] = color

    def xwd(self) -> bytes:
        step = self.bpp // 8
        bpl = W * 4
        hdr = struct.pack(">25I", 100, 7, 2, 24, W, H, 0, 0, 32, 0, 32, self.bpp, bpl,
                          4, 0xFF0000, 0xFF00, 0xFF, 8, 256, 0, W, H, 0, 0, 0)
        data = bytearray()
        for row in self.px:
            line = bytearray()
            for r, g, b in row:
                line += bytes([b, g, r]) + (b"\x00" if step == 4 else b"")
            data += line + b"\x00" * (bpl - len(line))
        return hdr + data


def dropdown(lines, bottom=740, bpp=24):
    """lines: [(y, x_start, color, hover)]；画一个下拉框，下方是会话列表底色。"""
    c = Canvas(bpp)
    c.rect(146, bottom, 600, H, LIST_BG)          # 下拉框下方：会话列表
    for y, x0, color, hover in lines:
        if hover:
            c.rect(146, y - 30, 786, y + 40, HOVER)
        c.rect(x0, y, x0 + 160, y + 24, color)    # 一行文字
    return _Shot(c.xwd())


class LocateTest(unittest.TestCase):
    def test_contact_exact_match_over_group(self):
        # 联系人标题全绿（完全匹配）；群聊标题黑色、副标题「包含:」黑+绿；网络联想从 214 起
        s = dropdown([(260, 258, GREEN, True), (430, 258, DARK, False), (478, 258, DARK, False),
                      (945, 214, GREEN, False)])
        self.assertEqual(s.exact_match_row(), (400, 271))

    def test_web_suggestion_is_not_local(self):
        s = dropdown([(231, 214, GREEN, True), (299, 214, GREEN, False)])
        self.assertIsNone(s.exact_match_row())

    def test_ambiguous_two_exact(self):
        s = dropdown([(260, 258, GREEN, False), (400, 258, GREEN, False)])
        self.assertIsNone(s.exact_match_row())

    def test_ignores_list_behind_dropdown(self):
        s = dropdown([(800, 258, GREEN, False)], bottom=740)
        self.assertIsNone(s.exact_match_row())

    def test_32bpp(self):
        s = dropdown([(660, 258, GREEN, False)], bpp=32)
        self.assertEqual(s.exact_match_row(), (400, 671))


class TitleCropTest(unittest.TestCase):
    def test_title_bgr(self):
        c = Canvas()
        c.rect(700, 100, 760, 120, (10, 20, 30))
        a = _Shot(c.xwd()).title_bgr()
        self.assertEqual(a.shape, (70, 790, 3))
        self.assertEqual(tuple(a[25, 100]), (30, 20, 10))   # BGR


class OcrEngineTest(unittest.TestCase):
    def test_engine_loads_and_runs(self):
        from fuxi_bridge.sender import _ocr

        a = _Shot(Canvas().xwd()).title_bgr()       # 空白标题栏
        res, _ = _ocr()(a)
        self.assertFalse(res)                       # 没有文字，但引擎能跑


class TitleTest(unittest.TestCase):
    def test_cases(self):
        cases = [
            ("浮屿用户交流群", "浮屿用户交流群(23)                号 …", True),
            ("文件传输助手", "文件传输助手        名>", True),
            ("煜哥", "浮屿用户交流群(23)   号", False),
            ("小明", "小明", True),
            ("小明", "小", False),
            ("浮屿", "浮屿用户交流群(23)", False),
            ("浮屿用户交流群", "浮屿用户交流…", True),
            ("A🔥群", "A群", True),
            ("", "x", False),
            ("小明", "", False),
        ]
        for target, ocr, want in cases:
            with self.subTest(target=target, ocr=ocr):
                self.assertEqual(title_matches(target, ocr), want)


if __name__ == "__main__":
    unittest.main()
