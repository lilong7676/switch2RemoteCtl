#!/usr/bin/env python3
"""Scan the whole warehouse (all pages) into one inventory report.

The warehouse right-hand grid is paginated with L/R (9 pages in this save, page
dots verified 2026-10-25). The backpack (left grid) is the same on every page, so
it is scanned once. The page number and the cursor reset to 1 / first slot on
every open, which makes this deterministic.

Output: vision/data/inventory_full.json plus a printed summary. Unknown slots
(icons not in the catalog yet) are reported so they can be learned later with
learn_inventory.py.

Usage:
    python vision/scan_inventory.py              # scan current open warehouse
    python vision/scan_inventory.py --open       # open it with A first
    python vision/scan_inventory.py --no-ocr     # fast pass: names only
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
import badge_templates  # noqa: E402
from learn_inventory import (  # noqa: E402
    OBS_PASSWORD_DEFAULT,
    PI_URL_DEFAULT,
    connect_controller,
    ensure_warehouse_open,
    move,
)
from walk_to_storage import FastState  # noqa: E402

OUT_PATH = BASE_DIR / "data" / "inventory_full.json"


def slot_record(cell, catalog, read_badges):
    badge = cell.badge or {}
    star = badge.get("value")
    # Items without a quality star (bottles, boots, fish bones...) always sell at
    # the base price, i.e. as if they were quality 0.5.
    star_for_price = star
    if star_for_price is None and not badge.get("present", True):
        star_for_price = 0.5
    price = catalog.price_for(cell.item, star_for_price) if cell.item else None
    count = cell.count
    return {
        "col": cell.col,
        "row": cell.row,
        "item": cell.item,
        "item_id": cell.item_id,
        "similarity": round(cell.similarity, 3),
        "star": badge.get("value"),
        "star_color": badge.get("color"),
        "badge_present": badge.get("present", True),
        "count": count,
        "unit_price": price,
        "stack_value": (price * count) if (price is not None and count) else None,
    }


async def scan(args):
    reader = StateReader(ocr_enabled=True)
    fast = FastState(reader)
    grid = GridReader(reader)
    catalog = ItemCatalog.load()

    bridge = ObsBridge(args.obs_url, args.obs_password)
    await bridge.connect()
    source = args.obs_source or await bridge.find_capture_input()

    client = None
    index = None
    try:
        frame = await bridge.screenshot(source, fmt="png")
        if args.open:
            client, index = connect_controller(args.pi_url)
            args.no_reset = False
            if not await ensure_warehouse_open(
                bridge, source, fast, reader, client, index, args
            ):
                print("could not open the warehouse")
                return 1
            frame = await bridge.screenshot(source, fmt="png")
        elif fast.read(frame)["mode"] != "WAREHOUSE":
            print("warehouse is not open; rerun with --open")
            return 1

        pages = reader.read_warehouse_pages(frame) or {}
        total_pages = int(pages.get("total") or 1)
        print(f"warehouse pages: {pages} ; catalog items: {len(catalog.items)}")

        report = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "catalog_items": len(catalog.items),
            "pages": [],
        }

        for page in range(1, total_pages + 1):
            if page > 1:
                if args.arm_pages:
                    if client is None:
                        client, index = connect_controller(args.pi_url)
                    await move(client, index, "R", args.settle)
                frame = await bridge.screenshot(source, fmt="png")
            info = reader.read_warehouse_pages(frame) or {}
            print(f"[page {page}/{total_pages}] dots={info}")

            slots = grid.scan(frame, "WAREHOUSE", read_badges=not args.no_ocr, harvest=True)
            page_slots = []
            for cell in slots:
                if cell.grid != "warehouse":
                    continue
                page_slots.append(slot_record(cell, catalog, not args.no_ocr))
            unknown = [s for s in page_slots if not s["item"]]
            report["pages"].append(
                {"page": page, "slots": page_slots, "unknown": len(unknown)}
            )
            present = sum(1 for s in page_slots if s.get("badge_present", True))
            stars = sum(1 for s in page_slots if s["star"] is not None)
            print(
                f"  slots={len(page_slots)} unknown={len(unknown)} "
                f"counts={sum(1 for s in page_slots if s['count'] is not None)} "
                f"badges={present} stars={stars}"
            )
            if page == 1:
                backpack = []
                for cell in slots:
                    if cell.grid != "backpack":
                        continue
                    backpack.append(slot_record(cell, catalog, not args.no_ocr))
                report["backpack"] = backpack
                print(f"  backpack slots={len(backpack)}")

        badge_templates.store().save()
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(
            json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        print(f"saved {OUT_PATH}")
        return 0
    finally:
        if client is not None:
            try:
                client.stop_all()
            finally:
                client.close()
        await bridge.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--open", action="store_true", help="press A to open the warehouse")
    parser.add_argument("--arm-pages", action="store_true", default=True,
                        help="press R to flip pages (default true)")
    parser.add_argument("--no-pages", dest="arm_pages", action="store_false")
    parser.add_argument("--no-ocr", action="store_true", help="skip badge/count OCR")
    parser.add_argument("--settle", type=float, default=0.6)
    parser.add_argument("--pi-url", default=PI_URL_DEFAULT)
    parser.add_argument("--obs-url", default="ws://127.0.0.1:4455")
    parser.add_argument("--obs-password", default=OBS_PASSWORD_DEFAULT)
    parser.add_argument("--obs-source", default=None)
    args = parser.parse_args()
    return asyncio.run(scan(args))


if __name__ == "__main__":
    sys.exit(main())
