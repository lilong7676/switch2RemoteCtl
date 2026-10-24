#!/usr/bin/env python3
"""连接保持与简单输入测试。

用法：
    .venv/bin/python scripts/test_link.py --seconds 60

默认在连接后每 2 秒按一次 DPAD_RIGHT（在主菜单上只是移动选中项，不会打开菜单）。
按 Ctrl-C 结束并释放适配器。
"""
import argparse
import signal
import sys
import time
from pathlib import Path

import nuxbt


def _sigterm(_signum, _frame):
    raise KeyboardInterrupt


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=60.0,
                        help="连接后保持并发送输入的时长（秒）")
    parser.add_argument("--interval", type=float, default=2.0,
                        help="两次输入的间隔（秒）")
    parser.add_argument("--button", default="DPAD_RIGHT",
                        help="要按的按键名，默认 DPAD_RIGHT")
    parser.add_argument("--address", default=None,
                        help="可选：强制重连的 Switch MAC（默认等待新配对）")
    parser.add_argument("--timeout", type=float, default=180.0,
                        help="等待连接的超时（秒）")
    args = parser.parse_args()

    button = getattr(nuxbt.Buttons, args.button)
    nx = nuxbt.Nuxbt()
    # nuxbt 自己注册的 SIGTERM handler 会抛 SystemExit 并打印 traceback；
    # 覆盖成 KeyboardInterrupt，让 finally 能正常清理子进程。
    signal.signal(signal.SIGTERM, _sigterm)
    idx = None
    try:
        kwargs = {}
        if args.address:
            kwargs["reconnect_address"] = args.address
        idx = nx.create_controller(nuxbt.PRO_CONTROLLER, **kwargs)
        print("controller created, waiting for Switch...", flush=True)

        deadline = time.monotonic() + args.timeout
        while nx.state[idx]["state"] != "connected":
            if nx.state[idx]["state"] == "crashed":
                print("controller crashed:", nx.state[idx]["errors"], flush=True)
                return 1
            if time.monotonic() > deadline:
                print("timeout waiting for connection", flush=True)
                return 2
            time.sleep(0.2)

        print("connected to", nx.state[idx].get("last_connection"), flush=True)
        mac = nx.state[idx].get("last_connection")
        if mac:
            logs_dir = Path(__file__).resolve().parent.parent / "logs"
            logs_dir.mkdir(exist_ok=True)
            (logs_dir / "last_mac").write_text(mac + "\n")
        start = time.monotonic()
        count = 0
        while time.monotonic() - start < args.seconds:
            nx.press_buttons(idx, [button], down=0.08, up=0.2)
            count += 1
            print(f"sent {args.button} #{count} "
                  f"({time.monotonic() - start:.1f}s)", flush=True)
            time.sleep(args.interval)
        print("done, still connected:", nx.state[idx]["state"], flush=True)
        return 0
    except KeyboardInterrupt:
        print("interrupted", flush=True)
        return 130
    finally:
        if idx is not None:
            try:
                nx.remove_controller(idx)
            except Exception as exc:  # noqa: BLE001
                print("remove_controller failed:", exc, flush=True)
        try:
            nx._on_exit()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    sys.exit(main())
