#!/usr/bin/env python3
"""Open the warehouse from the green mat, with verified pre/post conditions.

Flow
----
1. Fast check: game world, and the "A 查看仓库" prompt template fires.
2. OCR the prompt area and require the text to actually mention 仓库. A template
   hit alone is not enough ("A 查看花瓶" scored 0.890 on 2026-10-25).
3. Only then press A (--arm required). Never presses A from an unverified state.
4. Poll frames until the WAREHOUSE mode template fires, then OCR the title.
5. Save clean + annotated snapshots and print the parsed inventory summary.

The warehouse UI pauses the game clock, so this is a safe place to run the
grid scanner afterwards.

Usage
-----
    python vision/open_warehouse.py --check          # dry run: verify preconditions
    python vision/open_warehouse.py --arm            # press A and verify
"""

import argparse
import asyncio
import sys
import time
from datetime import datetime
from pathlib import Path

import cv2

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from obs_bridge import ObsBridge, default_password  # noqa: E402
from state_reader import StateReader  # noqa: E402

OUT_DIR = BASE_DIR / "logs" / "warehouse"
OBS_PASSWORD_DEFAULT = default_password()
PI_URL_DEFAULT = "http://192.168.5.19:8000"

# Reuse the same prompt gating as the walk controller.
sys.path.insert(0, str(BASE_DIR))
from walk_to_storage import FastState, save_snapshot, verify_arrival_prompt  # noqa: E402


def connect_controller(url):
    from switch_client import SwitchClient

    client = SwitchClient(url)
    client.connect(wait_timeout=8)
    index = client.wait_connected(timeout=5)
    return client, index


async def open_warehouse(args, reader, fast):
    bridge = ObsBridge(args.obs_url, args.obs_password)
    await bridge.connect()
    source = args.obs_source or await bridge.find_capture_input()
    print(f"obs connected, source={source}")

    client = None
    index = None
    if args.arm:
        client, index = connect_controller(args.pi_url)
        print(f"controller index={index} connected")
    else:
        print("CHECK MODE: will not press A (pass --arm to open)")

    try:
        frame = await bridge.screenshot(source, fmt="png")
        state = fast.read(frame)
        print(
            f"pre: mode={state['mode']} warehouse_prompt={state['storage_prompt']:.3f} "
            f"greet={state['greet_prompt']:.3f}"
        )
        if state["mode"] != "WORLD":
            path = save_snapshot(reader, frame, state, "not_world_before_A")
            print(f"ABORT: not in game world ({state['mode']}); snapshot={path}")
            return 1
        if not state["storage_visible"]:
            path = save_snapshot(reader, frame, state, "no_storage_prompt")
            print(f"ABORT: A 查看仓库 not visible; snapshot={path}")
            return 1
        verified, prompt_text = verify_arrival_prompt(reader, frame)
        print(f"pre: prompt OCR = {prompt_text!r} verified={verified}")
        if not verified:
            path = save_snapshot(reader, frame, state, "prompt_not_warehouse")
            print(f"ABORT: prompt text is not the warehouse; snapshot={path}")
            return 1

        # Precondition screenshot, for the record.
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        cv2.imwrite(str(OUT_DIR / f"{stamp}_prompt_before_A.png"), frame)

        if not args.arm:
            print("CHECK OK: preconditions verified; A would be pressed now")
            return 0

        print("pressing A 0.10s ...")
        client.macro("A 0.10s", index=index)

        # Poll for the warehouse UI (title template + OCR confirmation).
        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline:
            await asyncio.sleep(0.4)
            frame = await bridge.screenshot(source, fmt="jpg", quality=90)
            state = fast.read(frame)
            if state["mode"] == "WAREHOUSE":
                png = await bridge.screenshot(source, fmt="png")
                full = reader.analyze(png)
                title_ok = full["mode"] == "WAREHOUSE"
                title_text, _ = reader.ocr_region(png, "title_search", scale=2.0)
                cv2.imwrite(str(OUT_DIR / f"{stamp}_warehouse_open.png"), png)
                cv2.imwrite(
                    str(OUT_DIR / f"{stamp}_warehouse_open_annotated.png"),
                    reader.annotate(png, full),
                )
                print(
                    f"OPENED: mode={full['mode']} title={title_text!r} "
                    f"inventory={full['inventory']}"
                )
                return 0 if title_ok else 2
            if state["mode"] in ("HOME", "MODAL", "UNKNOWN"):
                path = save_snapshot(reader, frame, state, "abort_while_opening")
                print(f"ABORT: mode became {state['mode']}; snapshot={path}")
                return 1

        path = save_snapshot(reader, frame, state, "open_timeout")
        print(f"ABORT: warehouse UI did not appear in {args.timeout}s; snapshot={path}")
        return 1
    finally:
        if client is not None:
            try:
                client.stop_all()
            finally:
                client.close()
        await bridge.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--arm", action="store_true", help="actually press A")
    parser.add_argument("--check", action="store_true", help="only verify preconditions")
    parser.add_argument("--timeout", type=float, default=6.0)
    parser.add_argument("--pi-url", default=PI_URL_DEFAULT)
    parser.add_argument("--obs-url", default="ws://127.0.0.1:4455")
    parser.add_argument("--obs-password", default=OBS_PASSWORD_DEFAULT)
    parser.add_argument("--obs-source", default=None)
    args = parser.parse_args()

    if not args.arm:
        args.check = True

    reader = StateReader(ocr_enabled=True)
    fast = FastState(reader)
    return asyncio.run(open_warehouse(args, reader, fast))


if __name__ == "__main__":
    sys.exit(main())
