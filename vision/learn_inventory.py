#!/usr/bin/env python3
"""Learn the item catalog by walking the grid cursor once.

The warehouse cursor resets to slot (0,0) every time the UI opens (verified
2026-10-25), and DPAD_RIGHT wraps from the last column of a row to the first
column of the next row. So a plain sequence of RIGHT presses visits the whole
grid in row-major order, which makes the learning pass deterministic.

For every visited slot we read the detail panel (name / description / price),
the cell badge (star level) and the stack count, and store the masked icon
fingerprint in vision/data/item_catalog.json. Later scans recognise the whole
grid from one screenshot without moving the cursor.

Usage
-----
    python vision/learn_inventory.py --grid backpack --cells 24 --arm
    python vision/learn_inventory.py --grid backpack --cells 24          # dry run
"""

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import cv2

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from obs_bridge import ObsBridge, default_password  # noqa: E402
from state_reader import StateReader  # noqa: E402
from grid_reader import GridReader, ItemCatalog  # noqa: E402
from walk_to_storage import FastState, verify_arrival_prompt  # noqa: E402

DATA_DIR = BASE_DIR / "data"
FRAME_DIR = DATA_DIR / "learning_frames"
OBS_PASSWORD_DEFAULT = default_password()
PI_URL_DEFAULT = "http://192.168.5.19:8000"

# Cursor paths, measured on 2026-10-25:
#  * the warehouse UI resets the cursor to the backpack's first slot on open
#  * DOWN/UP move inside one column, RIGHT/LEFT move inside one row
#  * RIGHT on the backpack's last column jumps to the warehouse grid, same row
#  * the warehouse grid (6 cols) wraps RIGHT from its last column to the next row
PATH_BACKPACK = (
    [(col, 0) for col in range(8)]
    + [(7, 1)]
    + [(col, 1) for col in range(6, -1, -1)]
    + [(0, 2)]
    + [(col, 2) for col in range(1, 8)]
)
PATH_WAREHOUSE = (
    [(col, 0) for col in range(6)]
    + [(5, 1)]
    + [(col, 1) for col in range(4, -1, -1)]
    + [(0, 2)]
    + [(col, 2) for col in range(1, 6)]
    + [(5, 3)]
    + [(col, 3) for col in range(4, -1, -1)]
)
# moves to reach the warehouse grid from the fresh (0,0) backpack cursor:
# 7x RIGHT to the backpack's last column, then one more RIGHT enters warehouse (0,0)
ENTRY_WAREHOUSE = ["DPAD_RIGHT"] * 8


def path_for(grid_name):
    return PATH_BACKPACK if grid_name == "backpack" else PATH_WAREHOUSE


def moves_between(current, nxt):
    (c1, r1), (c2, r2) = current, nxt
    if r1 == r2:
        return ["DPAD_RIGHT"] if c2 > c1 else ["DPAD_LEFT"]
    return ["DPAD_DOWN"] if r2 > r1 else ["DPAD_UP"]


def connect_controller(url):
    from switch_client import SwitchClient

    client = SwitchClient(url)
    client.connect(wait_timeout=8)
    index = client.wait_connected(timeout=5)
    return client, index


async def move(client, index, key, settle):
    client.macro(f"{key} 0.06s", index=index)
    await asyncio.sleep(settle)


async def ensure_warehouse_open(bridge, source, fast, reader, client, index, args):
    """Open the warehouse and force the cursor back to slot (0,0).

    The UI resets the cursor on every open (verified 2026-10-25), so we always
    close and reopen: the learning pass maps step -> grid slot by construction
    and would otherwise pair item names with the wrong icons.
    """
    frame = await bridge.screenshot(source, fmt="png")
    state = fast.read(frame)
    if state["mode"] == "WAREHOUSE" and not args.no_reset:
        print("closing warehouse to reset the cursor ...")
        client.macro("B 0.10s", index=index)
        await asyncio.sleep(1.3)
        frame = await bridge.screenshot(source, fmt="png")
        state = fast.read(frame)
    if state["mode"] == "WAREHOUSE":
        return True
    if state["mode"] != "WORLD" or not state["storage_visible"]:
        print(f"cannot open warehouse: mode={state['mode']} prompt={state['storage_prompt']:.3f}")
        return False
    verified, text = verify_arrival_prompt(reader, frame)
    if not verified:
        print(f"cannot open warehouse: prompt OCR={text!r}")
        return False
    print("opening warehouse with A ...")
    for attempt in range(3):
        client.macro("A 0.10s", index=index)
        await asyncio.sleep(1.4)
        frame = await bridge.screenshot(source, fmt="jpg", quality=90)
        if fast.read(frame)["mode"] == "WAREHOUSE":
            return True
        print(f"  A attempt {attempt + 1} did not open the UI, retrying")
    return False


async def learn(args):
    reader = StateReader(ocr_enabled=True)
    fast = FastState(reader)
    grid = GridReader(reader)
    catalog = ItemCatalog.load(args.catalog)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    FRAME_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = DATA_DIR / f"learning_{stamp}.jsonl"
    log_handle = log_path.open("w", encoding="utf-8")

    def log(message):
        print(message)
        log_handle.write(json.dumps({"t": time.time(), "msg": message}, ensure_ascii=False) + "\n")
        log_handle.flush()

    bridge = ObsBridge(args.obs_url, args.obs_password)
    await bridge.connect()
    source = args.obs_source or await bridge.find_capture_input()

    client = None
    index = None
    try:
        if args.arm:
            client, index = connect_controller(args.pi_url)
            log(f"controller index={index}")
            if not await ensure_warehouse_open(bridge, source, fast, reader, client, index, args):
                log("ABORT: warehouse not open")
                return 1
        else:
            log("DRY RUN: no input will be sent")

        path = path_for(args.grid)
        total = min(args.cells, len(path))
        log(f"learning grid={args.grid} cells={total} catalog={args.catalog}")

        page = max(1, args.page)
        if args.grid == "warehouse" and args.arm:
            # Warehouse pages are flipped with R; the page number resets to 1 on
            # every open (verified), so open + (page-1) presses is deterministic.
            for _ in range(page - 1):
                await move(client, index, "R", args.settle)
            for key in ENTRY_WAREHOUSE:
                await move(client, index, key, args.settle)
            log(f"moved into the warehouse grid page={page} (cursor at 0,0)")
        elif args.grid == "warehouse":
            log(f"dry run: would open warehouse page {page} and move into the grid")

        for step in range(total):
            col, row = path[step]
            if step > 0 and args.arm:
                for key in moves_between(path[step - 1], path[step]):
                    await move(client, index, key, args.settle)
            frame = await bridge.screenshot(source, fmt="png")
            cells = grid.cells(frame, "WAREHOUSE")
            target = next(
                (
                    c
                    for c in cells
                    if c.grid == args.grid and c.col == col and c.row == row
                ),
                None,
            )
            if target is None:
                log(f"[{step:02d}] ({col},{row}) cell not found")
                break

            record = {
                "step": step,
                "page": page,
                "col": col,
                "row": row,
                "occupied": target.occupied,
            }
            if target.occupied:
                detail = reader.read_item_detail(frame, "WAREHOUSE") or {}
                badge = grid.badge(frame, target.rect)
                count = grid.count(frame, target.rect)
                patch, mask = grid.patch(frame, target.rect)
                record.update(
                    {
                        "name": detail.get("name"),
                        "price": detail.get("price"),
                        "description": detail.get("description"),
                        "star": badge.get("value"),
                        "star_raw": badge.get("raw"),
                        "star_color": badge.get("color"),
                        "count": count,
                    }
                )
                name = (detail.get("name") or "").strip() or None
                if name:
                    item_id, entry = catalog.learn(
                        name,
                        patch,
                        mask,
                        star=badge.get("value"),
                        price=detail.get("price"),
                        description=detail.get("description"),
                        log=log,
                    )
                    record["item_id"] = item_id
                    record["catalog_name"] = entry.get("name")
                cv2.imwrite(
                    str(FRAME_DIR / f"{stamp}_{step:02d}_{col}_{row}.png"),
                    frame[target.rect[1]:target.rect[3], target.rect[0]:target.rect[2]],
                )
            log(json.dumps(record, ensure_ascii=False))

        catalog.save()
        log(f"saved catalog: {args.catalog} ({len(catalog.items)} items)")
        return 0
    finally:
        if client is not None:
            try:
                client.stop_all()
            finally:
                client.close()
        await bridge.close()
        log_handle.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--arm", action="store_true", help="actually move the cursor")
    parser.add_argument("--grid", default="backpack", choices=["backpack", "warehouse"])
    parser.add_argument("--cells", type=int, default=24)
    parser.add_argument("--page", type=int, default=1, help="warehouse page (1-based)")
    parser.add_argument("--no-reset", action="store_true", help="keep the cursor where it is")
    parser.add_argument("--settle", type=float, default=0.45)
    parser.add_argument("--catalog", default=str(ItemCatalog.load().path))
    parser.add_argument("--pi-url", default=PI_URL_DEFAULT)
    parser.add_argument("--obs-url", default="ws://127.0.0.1:4455")
    parser.add_argument("--obs-password", default=OBS_PASSWORD_DEFAULT)
    parser.add_argument("--obs-source", default=None)
    args = parser.parse_args()
    return asyncio.run(learn(args))


if __name__ == "__main__":
    sys.exit(main())
