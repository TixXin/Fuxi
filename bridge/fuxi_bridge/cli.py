"""调试命令行。不会打印密钥本身。

  python -m fuxi_bridge.cli keys    --db-root <db_storage> [--pid N]
  python -m fuxi_bridge.cli tables  --db-root <db_storage> --db message/message_0.db --work <dir>
  python -m fuxi_bridge.cli send    --chat 文件传输助手 --text 你好
  python -m fuxi_bridge.cli open    --chat 文件传输助手 -n 3 --fresh   # 只打开不发送，测耗时
"""

from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys

from . import keyscan, sqlcipher


def _pid(args) -> int:
    pid = args.pid or keyscan.find_wechat_pid()
    if not pid:
        sys.exit("找不到微信进程（是否与微信同 pid 命名空间？）")
    return pid


def cmd_keys(args) -> None:
    keys = keyscan.extract_keys(_pid(args), args.db_root)
    for rel in sorted(keys):
        print(f"OK  {rel}  ({len(keys[rel].hex()) // 2} 字节)")


def cmd_tables(args) -> None:
    rels = [args.db]
    keys = keyscan.match_keys(keyscan.scan_candidates(_pid(args)), args.db_root, rels)
    if args.db not in keys:
        sys.exit("该库没解出密钥")
    os.makedirs(args.work, mode=0o700, exist_ok=True)
    out = os.path.join(args.work, os.path.basename(args.db))
    frames = sqlcipher.snapshot(os.path.join(args.db_root, args.db), keys[args.db], out)
    con = sqlite3.connect(f"file:{out}?mode=ro", uri=True)
    rows = con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
    print(f"WAL 合并帧 {frames}；表 {len(rows)} 张")
    for (name,) in rows:
        n = con.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
        print(f"  {name}  {n}")
    con.close()
    if not args.keep:
        os.remove(out)


def cmd_send(args) -> None:
    from .sender import Sender

    s = Sender(display=args.display)
    if args.image:
        s.send_image(args.chat, args.image)
    else:
        s.send_text(args.chat, args.text)


def cmd_open(args) -> None:
    from .sender import Sender

    s = Sender(display=args.display)
    for i in range(args.n):
        dt = s.probe_open(args.chat)
        print(f"第 {i + 1} 次：{dt:.2f} 秒")
        if args.fresh:
            s.forget_chat()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(prog="fuxi_bridge.cli")
    sub = p.add_subparsers(dest="cmd", required=True)

    k = sub.add_parser("keys")
    k.add_argument("--db-root", required=True)
    k.add_argument("--pid", type=int)
    k.set_defaults(fn=cmd_keys)

    t = sub.add_parser("tables")
    t.add_argument("--db-root", required=True)
    t.add_argument("--db", required=True)
    t.add_argument("--work", required=True)
    t.add_argument("--pid", type=int)
    t.add_argument("--keep", action="store_true")
    t.set_defaults(fn=cmd_tables)

    s = sub.add_parser("send")
    s.add_argument("--chat", required=True)
    s.add_argument("--text")
    s.add_argument("--image")
    s.add_argument("--display", default=":1")
    s.set_defaults(fn=cmd_send)

    o = sub.add_parser("open", help="只打开会话并核对标题（不发送），打印耗时")
    o.add_argument("--chat", required=True)
    o.add_argument("-n", type=int, default=1)
    o.add_argument("--fresh", action="store_true", help="每次都重新搜索（不走「已在会话」捷径）")
    o.add_argument("--display", default=":1")
    o.set_defaults(fn=cmd_open)

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
