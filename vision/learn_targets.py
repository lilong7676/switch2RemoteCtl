#!/usr/bin/env python3
"""Learn specific warehouse cells with verified navigation.

Why this exists: learn_unknown.py walks the cursor with blind key presses. A
single dropped press silently shifts every later target, which is how a few
cells ended up "learned" but still unrecognised (the icon fingerprint was taken
from the wrong cell).

This script verifies every step: it captures a frame before and after each press
and finds the cells whose border ring changed. The selected slot draws a blue
bracket ring, so after a move exactly two cells change (old + new). If the
expected neighbour is the strongest change, the step is confirmed; if nothing
changed the press was dropped and is retried; anything else aborts the target
and restarts from the deterministic entry point.

Usage:
    python vision/learn_targets.py --arm --targets 2:1:1 5:2:1 5:3:1 5:3:3 6:3:3 8:2:1 8:4:1
    python vision/learn_targets.py --arm --unknown     # auto-detect unknown cells
"""

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from obs_bridge import ObsBridge  # noqa: E402
from state_reader import StateReader  # noqa: E402
from grid_reader import GridReader, ItemCatalog  # noqa: E402
from learn_inventory import (  # noqa: E402
    DATA_DIR,
    OBS_PASSWORD_DEFAULT,
    PI_URL_DEFAULT,
    connect_controller,
    ensure_warehouse_open,
    move,
)
from walk_to_storage import FastState  # noqa: E402

RING = 10           # border ring thickness in px
MOVE_RATIO = 2.0    # best change must beat the median by this factor


def ring_signature(frame, cell, thickness=RING):
    x1, y1, x2, y2 = cell.rect
    gray = frame[y1:y2, x1:x2]
    if gray.ndim == 3:
        gray = cv2.cvtColor(gray, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape[:2]
    t = max(2, min(thickness, h // 3, w // 3))
    parts = [
        gray[:t, :].ravel(),
        gray[-t:, :].ravel(),
        gray[:, :t].ravel(),
        gray[:, -t:].ravel(),
    ]
    return np.concatenate(parts).astype(np.int16)


def step_change(before, after, cells, exclude=None):
    """Return (best_key, best_score, second_score) of border-ring changes."""
    scores = []
    for cell in cells:
        key = (cell.grid, cell.col, cell.row)
        if exclude is not None and key == exclude:
            continue
        sig_a = ring_signature(before, cell)
        sig_b = ring_signature(after, cell)
        scores.append((key, float(np.abs(sig_a - sig_b).mean())))
    scores.sort(key=lambda item: -item[1])
    if not scores:
        return None, 0.0, 0.0
    best_key, best = scores[0]
    second = scores[1][1] if len(scores) > 1 else 0.0
    return best_key, best, second


def delta_of(key, base):
    return (key[1] - base[1], key[2] - base[2])


async def press_verified(client, index, key, bridge, source, cells, current, settle, log):
    """Press one d-pad key and confirm the expected one-cell move."""
    before = await asyncio.wait_for(bridge.screenshot(source, fmt="jpg", quality=90), 10)
    for attempt in range(3):
        client.macro(f"{key} 0.09s", index=index)
        await asyncio.sleep(settle)
        after = await asyncio.wait_for(bridge.screenshot(source, fmt="jpg", quality=90), 10)
        best, score, second = step_change(
            before, after, cells, exclude=("warehouse", current[0], current[1])
        )
        expected_delta = {
            "DPAD_RIGHT": (1, 0), "DPAD_LEFT": (-1, 0),
            "DPAD_DOWN": (0, 1), "DPAD_UP": (0, -1),
        }[key]
        expected = ("warehouse", current[0] + expected_delta[0], current[1] + expected_delta[1])
        moved = score > 6.0 and score > second * 1.15
        if not moved:
            log(f"    press {key} dropped (score={score:.1f}); retrying")
            before = after
            continue
        if best == expected:
            return (expected[1], expected[2]), True
        log(f"    press {key} moved to {best} not {expected} (score={score:.1f}); retrying")
        before = after
    return current, False


async def navigate_verified(client, index, bridge, source, cells, target, settle, log):
    current = (0, 0)
    route = []
    if target[1] > current[1]:
        route += ["DPAD_DOWN"] * (target[1] - current[1])
    elif target[1] < current[1]:
        route += ["DPAD_UP"] * (current[1] - target[1])
    if target[0] > current[0]:
        route += ["DPAD_RIGHT"] * (target[0] - current[0])
    elif target[0] < current[0]:
        route += ["DPAD_LEFT"] * (current[0] - target[0])
    for key in route:
        current, ok = await press_verified(
            client, index, key, bridge, source, cells, current, settle, log
        )
        if not ok:
            return None
    return current


async def enter_grid(client, index, bridge, source, fast, settle, page, log):
    """Deterministic entry: reopen (resets page+cursor), flip page, enter grid."""
    client.macro("B 0.10s", index=index)
    await asyncio.sleep(1.2)
    frame = await bridge.screenshot(source, fmt="png")
    if fast.read(frame)["mode"] != "WORLD":
        log("    reopen: warehouse did not close, retrying")
    client.macro("A 0.10s", index=index)
    await asyncio.sleep(1.4)
    for _ in range(page - 1):
        await move(client, index, "R", settle)
    for _ in range(8):
        await move(client, index, "DPAD_RIGHT", settle)
    return True


async def learn(targets, args):
    reader = StateReader(ocr_enabled=True)
    fast = FastState(reader)
    grid = GridReader(reader)
    catalog = ItemCatalog.load()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = DATA_DIR / f"learn_targets_{stamp}.jsonl"
    handle = log_path.open("w", encoding="utf-8")

    def log(message):
        print(message)
        handle.write(json.dumps({"t": time.time(), "msg": message}, ensure_ascii=False) + "\n")
        handle.flush()

    bridge = ObsBridge(args.obs_url, args.obs_password)
    await bridge.connect()
    source = args.obs_source or await bridge.find_capture_input()
    client = None
    index = None
    try:
        client, index = connect_controller(args.pi_url)
        log(f"controller index={index}")

        args.no_reset = False
        if not await ensure_warehouse_open(bridge, source, fast, reader, client, index, args):
            log("ABORT: cannot open warehouse")
            return 1

        if not targets:
            # auto-detect: unknown slots on every page
            frame = await bridge.screenshot(source, fmt="png")
            total = int((reader.read_warehouse_pages(frame) or {}).get("total") or 1)
            targets = []
            for page in range(1, total + 1):
                if page > 1:
                    await move(client, index, "R", args.settle)
                    frame = await bridge.screenshot(source, fmt="png")
                slots = grid.scan(frame, "WAREHOUSE", read_badges=False)
                for cell in slots:
                    if cell.grid == "warehouse" and cell.item is None:
                        targets.append((page, cell.col, cell.row))
            log(f"auto targets: {targets}")

        learned = 0
        for (page, col, row) in targets:
            log(f"target page={page} ({col},{row})")
            await enter_grid(client, index, bridge, source, fast, args.settle, page, log)
            frame = await bridge.screenshot(source, fmt="png")
            cells = grid.cells(frame, "WAREHOUSE")
            got = await navigate_verified(
                client, index, bridge, source, cells, (col, row), args.settle, log
            )
            if got is None:
                log("    navigation failed, skipping target")
                continue
            frame = await bridge.screenshot(source, fmt="png")
            detail = reader.read_item_detail(frame, "WAREHOUSE") or {}
            name = (detail.get("name") or "").strip() or None
            badge = grid.badge(frame, next(c for c in cells if c.grid == "warehouse" and c.col == col and c.row == row).rect)
            count = grid.count(frame, next(c for c in cells if c.grid == "warehouse" and c.col == col and c.row == row).rect)
            patch, mask = grid.patch(frame, next(c for c in cells if c.grid == "warehouse" and c.col == col and c.row == row).rect)

            # validate the target contains the item the panel shows (when known)
            verify = None
            item_id = None
            if name:
                entry = next(
                    (i for i, e in catalog.items.items()
                     if e.get("name") == name or name in (e.get("aliases") or [])),
                    None,
                )
                if entry:
                    rescan = grid.scan(frame, "WAREHOUSE", read_badges=False)
                    hit = [c for c in rescan if c.grid == "warehouse" and c.item_id == entry]
                    verify = (entry in hit) if entry else None
                    item_id = entry
            record = {
                "page": page, "col": col, "row": row, "name": name,
                "price": detail.get("price"), "star": badge.get("value"),
                "count": count, "known_item": item_id, "verify": verify,
            }
            if name:
                item_id, entry = catalog.learn(
                    name, patch, mask, star=badge.get("value"),
                    price=detail.get("price"),
                    description=detail.get("description"), log=log,
                )
                record["item_id"] = item_id
                learned += 1
            else:
                record["learn_failed"] = True
            log(json.dumps(record, ensure_ascii=False))

        catalog.save()
        log(f"saved catalog ({len(catalog.items)} items, learned {learned})")
        return 0
    finally:
        if client is not None:
            try:
                client.stop_all()
            finally:
                client.close()
        await bridge.close()
        handle.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--arm", action="store_true")
    parser.add_argument("--targets", nargs="*", default=[], help="page:col:row list")
    parser.add_argument("--unknown", action="store_true", help="auto-detect unknown slots")
    parser.add_argument("--settle", type=float, default=0.55)
    parser.add_argument("--pi-url", default=PI_URL_DEFAULT)
    parser.add_argument("--obs-url", default="ws://127.0.0.1:4455")
    parser.add_argument("--obs-password", default=OBS_PASSWORD_DEFAULT)
    parser.add_argument("--obs-source", default=None)
    args = parser.parse_args()
    targets = []
    for spec in args.targets:
        page, col, row = (int(v) for v in spec.split(":"))
        targets.append((page, col, row))
    if args.unknown:
        targets = []
    return asyncio.run(learn(targets, args))


if __name__ == "__main__":
    sys.exit(main())
