#!/usr/bin/env python3
"""Learn only the grid cells whose icons are still unknown.

Full-page learning walks all 24 slots even when most items are already known.
This script scans first, clusters the unknown cells by icon fingerprint (the same
sprite usually fills several slots), then walks only one representative per
cluster and stores it in the catalog.

Navigation is deterministic inside the right-hand grid: the cursor starts at
(0,0) after open + R*(page-1) + 8x RIGHT, and moving "up/down first, then
left/right" never hits a panel-jump edge.

Usage:
    python vision/learn_unknown.py --arm
    python vision/learn_unknown.py --arm --pages 1 2 3
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
from walk_to_storage import FastState  # noqa: E402

CLUSTER_THRESHOLD = 0.93


async def go_to(client, index, current, target, settle):
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
    for key in keys:
        await move(client, index, key, settle)
    return target


def cluster_unknown(grid, catalog, frame, cells):
    """Return one representative cell per distinct unknown icon."""
    unknown = [c for c in cells if c.occupied and c.item is None]
    reps = []
    for cell in unknown:
        patch, mask = grid.patch(frame, cell.rect)
        placed = False
        for rep in reps:
            score = catalog.similarity(patch, mask, rep["patch"], rep["mask"])
            if score >= CLUSTER_THRESHOLD:
                rep["members"].append((cell.col, cell.row))
                placed = True
                break
        if not placed:
            reps.append(
                {
                    "cell": cell,
                    "patch": patch,
                    "mask": mask,
                    "members": [(cell.col, cell.row)],
                }
            )
    return reps


async def learn_unknown(args):
    reader = StateReader(ocr_enabled=True)
    fast = FastState(reader)
    grid = GridReader(reader)
    catalog = ItemCatalog.load()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = DATA_DIR / f"learn_unknown_{stamp}.jsonl"
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
        client, index = connect_controller(args.pi_url)
        log(f"controller index={index}")

        # Reset page + cursor, then read the page count from the dots.
        args.no_reset = False
        if not await ensure_warehouse_open(bridge, source, fast, reader, client, index, args):
            log("ABORT: warehouse not open")
            return 1
        frame = await bridge.screenshot(source, fmt="png")
        total_pages = int((reader.read_warehouse_pages(frame) or {}).get("total") or 1)
        pages = args.pages or list(range(1, total_pages + 1))
        log(f"pages to visit: {pages} (warehouse has {total_pages})")

        learned = 0
        for page in pages:
            # deterministic entry: reopen (resets page+cursor), flip to page, enter grid
            if page != 1:
                client.macro("B 0.10s", index=index)
                await asyncio.sleep(1.3)
                client.macro("A 0.10s", index=index)
                await asyncio.sleep(1.5)
                for _ in range(page - 1):
                    await move(client, index, "R", args.settle)
            for _ in range(8):
                await move(client, index, "DPAD_RIGHT", args.settle)

            frame = await bridge.screenshot(source, fmt="png")
            info = reader.read_warehouse_pages(frame) or {}
            slots = grid.scan(frame, "WAREHOUSE", read_badges=False)
            cells = [c for c in slots if c.grid == "warehouse"]
            reps = cluster_unknown(grid, catalog, frame, cells)
            log(f"page {page}: warehouse unknown={sum(1 for c in cells if c.item is None)} "
                f"clusters={len(reps)} dots={info}")

            current = (0, 0)
            for rep in reps:
                target = (rep["cell"].col, rep["cell"].row)
                await go_to(client, index, current, target, args.settle)
                current = target
                frame = await bridge.screenshot(source, fmt="png")
                detail = reader.read_item_detail(frame, "WAREHOUSE") or {}
                badge = grid.badge(frame, rep["cell"].rect)
                count = grid.count(frame, rep["cell"].rect)
                patch, mask = grid.patch(frame, rep["cell"].rect)
                name = (detail.get("name") or "").strip() or None
                record = {
                    "page": page,
                    "col": target[0],
                    "row": target[1],
                    "members": rep["members"],
                    "name": name,
                    "price": detail.get("price"),
                    "star": badge.get("value"),
                    "star_color": badge.get("color"),
                    "count": count,
                }
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
                    learned += 1
                else:
                    record["learn_failed"] = True
                log(json.dumps(record, ensure_ascii=False))

        catalog.save()
        log(f"saved catalog: {catalog.path} ({len(catalog.items)} items, learned {learned})")
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
    parser.add_argument("--arm", action="store_true", help="actually move / press keys")
    parser.add_argument("--pages", nargs="+", type=int, default=None)
    parser.add_argument("--settle", type=float, default=0.5)
    parser.add_argument("--pi-url", default=PI_URL_DEFAULT)
    parser.add_argument("--obs-url", default="ws://127.0.0.1:4455")
    parser.add_argument("--obs-password", default=OBS_PASSWORD_DEFAULT)
    parser.add_argument("--obs-source", default=None)
    args = parser.parse_args()
    return asyncio.run(learn_unknown(args))


if __name__ == "__main__":
    sys.exit(main())
