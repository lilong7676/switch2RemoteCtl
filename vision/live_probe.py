#!/usr/bin/env python3
"""Read-only live probe through OBS (camera bridge), feeding the M1 state reader.

Waits for the obs-websocket server to come up, grabs frames from the capture
source, runs StateReader.analyze() on them, prints one compact line per frame and
writes annotated snapshots on mode changes.

Never sends controller input.
"""

import argparse
import json
import socket
import sys
import time
from datetime import datetime
from pathlib import Path

import cv2

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from obs_bridge import ObsBridge, ObsError, default_password  # noqa: E402
from state_reader import StateReader  # noqa: E402

OUT_DIR = BASE_DIR / "logs" / "live"

DEFAULT_PASSWORD = default_password()


def wait_for_port(host, port, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return True
        except OSError:
            time.sleep(1.0)
    return False


def summarize(state):
    hud = state.get("hud") or {}
    stamina = hud.get("stamina") or {}
    dt = hud.get("datetime") or {}
    prompts = state.get("prompts") or {}
    inventory = state.get("inventory") or {}
    parts = [f"mode={state['mode']}"]
    if state["mode"] == "WORLD":
        parts.append(f"world_ok={state.get('world_ok')}")
    if stamina.get("detected"):
        parts.append(f"guts={stamina['ratio']:.2f}/{stamina['band']}")
    if hud.get("weather"):
        parts.append(f"weather={hud['weather']}")
    if dt.get("time"):
        parts.append(f"time={dt['time']}")
    if dt.get("date"):
        parts.append(f"date={dt['date']}")
    labels = [p.get("text") for p in prompts.get("items", [])]
    if labels:
        parts.append("prompts=" + "/".join(labels))
    if inventory:
        left = inventory.get("backpack") or {}
        right = inventory.get("warehouse") or {}
        bits = []
        if left:
            bits.append(f"bag {left.get('used')}/{left.get('capacity')}")
        if right:
            bits.append(f"wh {right.get('used')}/{right.get('capacity')}")
        if bits:
            parts.append("inv=" + ",".join(bits))
    return " | ".join(parts)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="ws://127.0.0.1:4455")
    parser.add_argument("--password", default=DEFAULT_PASSWORD)
    parser.add_argument("--source", default=None, help="OBS source name; auto if omitted")
    parser.add_argument("--duration", type=float, default=20.0, help="seconds to probe")
    parser.add_argument("--interval", type=float, default=0.4, help="seconds between frames")
    parser.add_argument("--wait-server", type=float, default=0.0, help="wait this long for port 4455")
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--no-ocr", action="store_true")
    args = parser.parse_args()

    if args.wait_server > 0:
        print(f"waiting up to {args.wait_server:.0f}s for obs-websocket on 4455 ...", flush=True)
        if not wait_for_port("127.0.0.1", 4455, args.wait_server):
            print("TIMEOUT: obs-websocket never opened port 4455", flush=True)
            return 10
        print("port 4455 open", flush=True)

    reader = StateReader(ocr_enabled=not args.no_ocr)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    import asyncio

    async def run():
        bridge = ObsBridge(args.url, args.password)
        await bridge.connect()
        print("obs-websocket connected", flush=True)
        source = args.source or await bridge.find_capture_input()
        print(f"source={source}", flush=True)

        version = await bridge.request("GetVersion")
        print(f"obs={version.get('obsVersion')} ws={version.get('obsWebSocketVersion')}", flush=True)

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_path = OUT_DIR / f"probe_{stamp}.jsonl"
        last_mode = None
        counts = {}
        started = time.monotonic()
        try:
            while time.monotonic() - started < args.duration:
                cycle = time.monotonic()
                frame = await bridge.screenshot(source, width=args.width)
                state = reader.analyze(frame)
                mode = state["mode"]
                counts[mode] = counts.get(mode, 0) + 1
                print(
                    f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] "
                    f"shape={frame.shape[1]}x{frame.shape[0]} {summarize(state)}",
                    flush=True,
                )
                with log_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(state, ensure_ascii=False) + "\n")

                if mode != last_mode:
                    annotated = reader.annotate(frame, state)
                    shot = OUT_DIR / f"{stamp}_mode_{mode}_{int(time.time() * 1000) % 100000}.png"
                    cv2.imwrite(str(shot), annotated)
                    print(f"  saved {shot}", flush=True)
                    last_mode = mode

                elapsed = time.monotonic() - cycle
                time.sleep(max(0.0, args.interval - elapsed))
        finally:
            await bridge.close()

        print(f"probe done, frames by mode: {counts}", flush=True)
        print(f"log={log_path}", flush=True)
        return 0

    try:
        return asyncio.run(run())
    except ObsError as exc:
        print(f"OBS ERROR: {exc}", flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
