#!/usr/bin/env python3
"""Fill in missing base prices by reading the item panel once per item.

learn_targets.py reopens the warehouse for every target (~80 s each), which is
wasteful when many cells need a price. This script opens once per page, enters
the right-hand grid and walks between targets with relative, verified moves
(up/down first, then left/right) so a dropped key press is detected immediately.

Usage:
    python vision/learn_prices.py --arm --missing     # auto targets from inventory
    python vision/learn_prices.py --arm --targets 4:2:0 4:5:1
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
from learn_targets import press_verified, step_change  # noqa: E402
from walk_to_storage import FastState  # noqa: E402


def route_to(current, target):
    """Grid moves from current to target, vertical first (never hits a panel edge)."""
    (c1, r1), (c2, r2) = current, target
    keys = []
    if r2 > r1:
        keys += ["DPAD_DOWN"] * (r2 - r1)
    elif r2 < r1:
        keys += ["DPAD_UP"] * (r1 - r2)
    if c2 > c1:
        keys += ["DPAD_RIGHT"] * (c2 - c1)
    elif c2 < c1:
        keys += ["DPAD_LEFT"] * (c1 - c2)
    return keys


async def learn_prices(targets, args):
    reader = StateReader(ocr_enabled=True)
    fast = FastState(reader)
    grid = GridReader(reader)
    catalog = ItemCatalog.load()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = DATA_DIR / f"learn_prices_{stamp}.jsonl"
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

        if not targets:
            frame = await bridge.screenshot(source, fmt="png")
            total = int((reader.read_warehouse_pages(frame) or {}).get("total") or 1)
            inv = json.loads((DATA_DIR / "inventory_full.json").read_text(encoding="utf-8"))
            missing = {
                iid for iid, e in catalog.items.items() if e.get("base_price") is None
            }
            for page in range(1, total + 1):
                if page > 1:
                    await move(client, index, "R", args.settle)
                    frame = await bridge.screenshot(source, fmt="png")
                slots = grid.scan(frame, "WAREHOUSE", read_badges=False)
                for cell in slots:
                    if cell.grid == "warehouse" and cell.item_id in missing:
                        targets.append((page, cell.col, cell.row))
                        missing.discard(cell.item_id)
            log(f"auto targets: {targets}")

        args.no_reset = False
        if not await ensure_warehouse_open(bridge, source, fast, reader, client, index, args):
            log("ABORT: cannot open warehouse")
            return 1

        current_page = 1
        current = (0, 0)

        # expected item name per target, taken from the inventory scan
        inv = json.loads((DATA_DIR / "inventory_full.json").read_text(encoding="utf-8"))
        expected = {}
        for pg in inv["pages"]:
            for slot in pg["slots"]:
                if slot["item"]:
                    expected[(pg["page"], slot["col"], slot["row"])] = slot["item"]
        for slot in (inv.get("backpack") or []):
            if slot["item"]:
                expected.setdefault((1, slot["col"], slot["row"]), slot["item"])

        by_page = {}
        for page, col, row in targets:
            by_page.setdefault(page, []).append((col, row))

        async def enter(page):
            """Open the warehouse, flip to `page` and put the cursor on (0,0)."""
            client.macro("B 0.10s", index=index)
            await asyncio.sleep(1.1)
            client.macro("A 0.10s", index=index)
            await asyncio.sleep(1.3)
            for _ in range(page - 1):
                await move(client, index, "R", args.settle)
            for _ in range(8):
                await move(client, index, "DPAD_RIGHT", args.settle)
            return (0, 0)

        learned = 0
        skipped = 0
        for page in sorted(by_page):
            try:
                current = await enter(page)
                current_page = page
            except Exception as exc:  # noqa: BLE001
                log(f"page {page}: enter failed ({exc})")
                continue

            for target in sorted(by_page[page], key=lambda t: (t[1], t[0])):
                want = expected.get((page, target[0], target[1]))
                done = False
                for attempt in range(3):
                    try:
                        for key in route_to(current, target):
                            await move(client, index, key, args.settle)
                        current = target
                        frame = await bridge.screenshot(source, fmt="png")
                        detail = reader.read_item_detail(frame, "WAREHOUSE") or {}
                        name = (detail.get("name") or "").strip() or None
                        if want and name != want:
                            log(f"  page {page} {target}: got {name!r}, wanted {want!r} (attempt {attempt+1})")
                            current = await enter(page)
                            continue
                        badge = grid.badge(frame, next(
                            c for c in grid.cells(frame, "WAREHOUSE")
                            if c.grid == "warehouse" and c.col == target[0] and c.row == target[1]
                        ).rect)
                        patch, mask = grid.patch(frame, next(
                            c for c in grid.cells(frame, "WAREHOUSE")
                            if c.grid == "warehouse" and c.col == target[0] and c.row == target[1]
                        ).rect)
                        record = {
                            "page": page, "col": target[0], "row": target[1],
                            "name": name, "price": detail.get("price"),
                            "star": badge.get("value"),
                        }
                        if name and detail.get("price") is not None:
                            item_id, entry = catalog.learn(
                                name, patch, mask,
                                star=badge.get("value"),
                                price=detail.get("price"),
                                description=detail.get("description"),
                                log=log,
                            )
                            record["item_id"] = item_id
                            learned += 1
                            done = True
                        else:
                            record["failed"] = True
                            done = True
                        log(json.dumps(record, ensure_ascii=False))
                        break
                    except Exception as exc:  # noqa: BLE001
                        log(f"  page {page} {target}: attempt {attempt+1} failed ({exc})")
                        try:
                            current = await enter(page)
                        except Exception:  # noqa: BLE001
                            break
                else:
                    skipped += 1
                    log(f"  page {page} {target}: skipped after retries")

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
    parser.add_argument("--missing", action="store_true", help="auto from inventory")
    parser.add_argument("--settle", type=float, default=0.5)
    parser.add_argument("--pi-url", default=PI_URL_DEFAULT)
    parser.add_argument("--obs-url", default="ws://127.0.0.1:4455")
    parser.add_argument("--obs-password", default=OBS_PASSWORD_DEFAULT)
    parser.add_argument("--obs-source", default=None)
    args = parser.parse_args()
    targets = []
    for spec in (args.targets or []):
        page, col, row = (int(v) for v in spec.split(":"))
        targets.append((page, col, row))
    return asyncio.run(learn_prices(targets if not args.missing else [], args))


if __name__ == "__main__":
    sys.exit(main())
