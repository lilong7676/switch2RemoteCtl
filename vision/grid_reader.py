#!/usr/bin/env python3
"""Grid item reader for the warehouse / backpack UI.

Goal: turn one screenshot of the item grid into
    slot -> (item name, star level, stack count, unit price)
without moving the cursor slot by slot. Item names are learned once (cursor on
the slot, read the detail panel) and remembered in vision/data/item_catalog.json
as a masked icon fingerprint, so later scans can recognise the whole grid.

Cell anatomy (1920x1080, ~112x116 px per cell):
    top-left   star icon (colour tier) + star number, e.g. "0.5", "7"
    centre     item sprite
    bottom-right stack count digits
The badge strip and the count corner are masked out of the fingerprint so the
same item matches regardless of star level or stack size.
"""

import base64
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from state_reader import StateReader, roi_to_pixels  # noqa: E402
import badge_templates  # noqa: E402
from item_prices import (  # noqa: E402
    QUALITY_LEVELS,
    base_price_from,
    normalize_digits,
    price_at,
    snap_badge_digits,
)

DATA_DIR = BASE_DIR / "data"
CATALOG_PATH = DATA_DIR / "item_catalog.json"

PATCH_SIZE = 24
ICON_CROP = (0.06, 0.06, 0.94, 0.94)  # fractions of the cell
BADGE_TOP_FRAC = 0.32  # mask rows above this
COUNT_X_FRAC = 0.55  # mask columns right of this...
COUNT_Y_FRAC = 0.68  # ...below this
OCCUPIED_STD = 18.0
SELECT_RING_FRAC = 0.12
MATCH_THRESHOLD = 0.86  # cosine similarity on masked patches
MATCH_FALLBACK = 0.78  # accept a slightly lower score only with a clear margin
MATCH_MARGIN = 0.08

# Measured 2026-10-25 (cell pitch 106x111, card 93x92, cell(0,0) at (149,268)).
# Rects are relative to the cell's top-left corner.
BADGE_RECT = (21, -13, 64, 29)   # star excluded (it overhangs to the left)
COUNT_RECT = (44, 60, 56, 42)    # right-aligned, overhangs the bottom-right corner
STAR_RECT = (-7, -14, 29, 31)
BADGE_VOCAB = tuple(QUALITY_LEVELS)  # 14 levels, 0.5 .. 7.0

MODES = {
    "WAREHOUSE": [
        ("backpack", "warehouse_left_grid", 8, 4),
        ("warehouse", "warehouse_right_grid", 6, 4),
    ],
    "BACKPACK": [
        ("backpack", "backpack_grid", 8, 4),
    ],
}


@dataclass
class Cell:
    grid: str
    col: int
    row: int
    rect: tuple
    occupied: bool = False
    selected: bool = False
    badge: dict = field(default_factory=dict)
    count: int | None = None
    item: str | None = None
    item_id: str | None = None
    similarity: float = 0.0


class GridReader:
    def __init__(self, reader=None, catalog=None):
        self.reader = reader or StateReader(ocr_enabled=True)
        self.catalog = catalog or ItemCatalog.load()

    # ---------- geometry ----------

    def cells(self, frame, mode):
        out = []
        for grid_name, roi_name, cols, rows in MODES.get(mode, []):
            x1, y1, x2, y2 = roi_to_pixels(
                self.reader.rois[roi_name],
                frame.shape[1],
                frame.shape[0],
            )
            cw = (x2 - x1) / cols
            ch = (y2 - y1) / rows
            for row in range(rows):
                for col in range(cols):
                    rect = (
                        int(x1 + col * cw),
                        int(y1 + row * ch),
                        int(x1 + (col + 1) * cw),
                        int(y1 + (row + 1) * ch),
                    )
                    out.append(Cell(grid_name, col, row, rect))
        for cell in out:
            cell.occupied = self.is_occupied(frame, cell.rect)
            cell.selected = self.is_selected(frame, cell.rect)
        return out

    # ---------- per-cell primitives ----------

    @staticmethod
    def _crop(frame, rect):
        x1, y1, x2, y2 = rect
        return frame[y1:y2, x1:x2]

    def is_occupied(self, frame, rect):
        cell = self._crop(frame, rect)
        if cell.size == 0:
            return False
        h, w = cell.shape[:2]
        inner = cell[int(h * 0.18):int(h * 0.82), int(w * 0.18):int(w * 0.82)]
        gray = cv2.cvtColor(inner, cv2.COLOR_BGR2GRAY)
        return float(gray.std()) > OCCUPIED_STD

    def is_selected(self, frame, rect):
        """The selected slot draws a blue rounded ring around the cell."""
        cell = self._crop(frame, rect)
        if cell.size == 0:
            return False
        h, w = cell.shape[:2]
        b, g, r = cv2.split(cell.astype(np.int16))
        blue = ((b - r > 25) & (b > 140)).astype(np.uint8)
        ring = 0
        border = max(2, int(min(h, w) * SELECT_RING_FRAC))
        ring += blue[:border, :].sum() + blue[-border:, :].sum()
        ring += blue[:, :border].sum() + blue[:, -border:].sum()
        perimeter = 2 * (h + w) * border
        return ring / max(perimeter, 1) > 0.18

    @staticmethod
    def _crop_rel(frame, cell_rect, rel):
        """Crop by offset from a cell corner, allowing overhang outside the cell.

        Badges and stack counts are drawn slightly outside the card, so cropping
        inside the cell loses the top of the glyph.
        """
        dx, dy, w, h = rel
        x1 = cell_rect[0] + dx
        y1 = cell_rect[1] + dy
        x2 = x1 + w
        y2 = y1 + h
        height, width = frame.shape[:2]
        x1, y1 = max(0, x1), max(0, y1)
        x2, y2 = min(width, x2), min(height, y2)
        if x2 <= x1 or y2 <= y1:
            return None
        return frame[y1:y2, x1:x2]

    def star_tier(self, frame, rect):
        """Blue / purple / rainbow tier from the star icon colour mix."""
        star = self._crop_rel(frame, rect, STAR_RECT)
        if star is None or star.size == 0:
            return "unknown"
        hsv = cv2.cvtColor(star, cv2.COLOR_BGR2HSV)
        hue = hsv[:, :, 0].astype(int)
        sat = hsv[:, :, 1].astype(int)
        val = hsv[:, :, 2].astype(int)
        colored = (sat > 70) & (val > 110)
        total = max(1, int(colored.sum()))
        blue = ((hue >= 85) & (hue <= 125) & colored).sum() / total
        purple = ((hue >= 126) & (hue <= 165) & colored).sum() / total
        warm = (((hue < 35) | (hue > 165)) & colored).sum() / total
        green = ((hue >= 35) & (hue < 85) & colored).sum() / total
        if blue >= 0.60:
            return "blue"
        if purple >= 0.50:
            return "purple"
        if warm >= 0.20 and green >= 0.15:
            return "rainbow"
        return "unknown"

    def badge(self, frame, rect, read_ocr=True, harvest=False):
        """Star tier + badge number (star value). Rainbow stars are always 7."""
        tier = self.star_tier(frame, rect)
        if tier == "rainbow":
            return {"value": 7.0, "raw": "rainbow", "color": tier, "present": True}
        value = None
        raw = ""
        if read_ocr and self.reader.ocr.enabled:
            crop = self._crop_rel(frame, rect, BADGE_RECT)
            if crop is not None and crop.size:
                # Vision drops isolated hollow glyphs on busy backgrounds; the
                # grey + upscale + white-pad recipe was measured to be the most
                # reliable variant for these badges.
                gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
                for source in (gray, 255 - gray):  # normal + inverted glyph polarity
                    for scale in (6.0, 4.0, 3.0):
                        image = cv2.resize(
                            source, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC
                        )
                        image = cv2.copyMakeBorder(
                            image, 28, 28, 28, 28, cv2.BORDER_CONSTANT, value=255
                        )
                        lines = self.reader.ocr.ocr_crop(image, scale=1.0, lang="en-US")
                        raw = " ".join(line["text"] for line in lines).strip()
                        if any(ch.isdigit() for ch in raw):
                            break
                    if any(ch.isdigit() for ch in raw):
                        break
            value = snap_badge_digits(normalize_digits(raw))
            if value is None:
                value = parse_star_value(raw)
            # Blue-tier stars are 0.5..3.5 in practice; OCR drops a leading "0"
            # often enough that a lone "5" must be read as 0.5, not 5.0.
            if value == 5.0 and tier == "blue":
                value = 0.5
            glyph = badge_templates.glyph_patch(
                cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if crop is not None else None
            )
            if value is None and glyph is not None:
                # Vision never reads a lone "1"; fall back to labelled templates,
                # restricted to the value range that each star colour covers.
                # Blue stars cover every non-rainbow quality (a blue badge at
                # quality 5 is normal), so no value filtering here.
                fallback, score = badge_templates.store().match(
                    glyph, threshold=0.86
                )
                if fallback is not None:
                    value = fallback
                    raw = f"{raw or '?'}|template:{score:.2f}"
            elif value is not None and harvest and glyph is not None:
                badge_templates.store().add(value, glyph)
        # "no badge" is a legitimate state (some items never show a star tier);
        # distinguish it from "badge exists but OCR failed".
        present = bool(value is not None or tier != "unknown" or raw.strip())
        return {"value": value, "raw": raw, "color": tier, "present": present}

    def count(self, frame, rect, read_ocr=True):
        """Stack count from the bottom-right corner; 1 when the UI hides it."""
        crop = self._crop_rel(frame, rect, COUNT_RECT)
        if not read_ocr or not self.reader.ocr.enabled:
            return None
        for scale in (4.0, 5.0):
            lines = self.reader.ocr.ocr_crop(crop, scale=scale, lang="en-US")
            text = " ".join(line["text"] for line in lines)
            digits = "".join(ch for ch in text if ch.isdigit())
            if digits:
                return int(digits[:3])
        return 1  # verified: every empty OCR result was a count of 1

    def patch(self, frame, rect):
        """Masked icon fingerprint: badge strip and count corner zeroed."""
        cell = self._crop(frame, rect)
        x1, y1, x2, y2 = ICON_CROP
        h, w = cell.shape[:2]
        crop = cell[int(h * y1):int(h * y2), int(w * x1):int(w * x2)]
        patch = cv2.resize(
            crop, (PATCH_SIZE, PATCH_SIZE), interpolation=cv2.INTER_AREA
        ).astype(np.float32)
        mask = np.ones((PATCH_SIZE, PATCH_SIZE), np.float32)
        # Mask every corner overlay: badge strip, stack count, and both bottom
        # corners (the selection bracket sits in the bottom-left corner, and a
        # learned patch must match the same cell whether or not it is selected).
        mask[: int(PATCH_SIZE * BADGE_TOP_FRAC), :] = 0.0
        mask[int(PATCH_SIZE * 0.62):, :int(PATCH_SIZE * 0.30)] = 0.0
        mask[int(PATCH_SIZE * COUNT_Y_FRAC):, int(PATCH_SIZE * COUNT_X_FRAC):] = 0.0
        return patch, mask

    # ---------- scanning ----------

    def scan(self, frame, mode, read_badges=True, harvest=False):
        """Recognise the whole grid in one pass."""
        slots = []
        for cell in self.cells(frame, mode):
            if not cell.occupied:
                continue
            cell.badge = (
                self.badge(frame, cell.rect, harvest=harvest) if read_badges else {}
            )
            cell.count = self.count(frame, cell.rect)
            patch, mask = self.patch(frame, cell.rect)
            match = self.catalog.match(patch, mask)
            if match:
                cell.item, cell.similarity, cell.item_id = match
            slots.append(cell)
        return slots

    def read_selected_detail(self, frame, mode):
        """Read the detail panel for the currently selected slot."""
        detail = self.reader.read_item_detail(frame, mode) or {}
        selected = [c for c in self.cells(frame, mode) if c.selected]
        if selected:
            cell = selected[0]
            detail["grid"] = cell.grid
            detail["col"] = cell.col
            detail["row"] = cell.row
            detail["badge"] = self.badge(frame, cell.rect)
            detail["count"] = self.count(frame, cell.rect)
            detail["patch"], detail["mask"] = self.patch(frame, cell.rect)
        return detail


def parse_star_value(text):
    """Parse the star number ("0.5", "7", "2.5") out of badge OCR noise."""
    if not text:
        return None
    cleaned = text.replace("O", "0").replace("o", "0").replace(" ", "")
    cleaned = cleaned.replace("。", ".").replace(",", ".")
    import re

    match = re.search(r"(\d{1,2}(?:\.\d)?)", cleaned)
    if not match:
        return None
    try:
        value = float(match.group(1))
    except ValueError:
        return None
    if value <= 0 or value > 7.5:  # quality range is 0.5..7.0
        return None
    return value


def parse_count(text):
    """Parse stack count digits, tolerating common glyph noise."""
    if not text:
        return None
    cleaned = text.replace("O", "0").replace("o", "0").replace(" ", "")
    import re

    match = re.search(r"(\d{1,4})", cleaned)
    if not match:
        return None
    return int(match.group(1))


def star_key(value):
    if value is None:
        return "unknown"
    return f"{value:g}"


class ItemCatalog:
    """Learned item knowledge: masked icon fingerprints + star -> price table.

    Entries are merged by icon fingerprint, not by OCR name: the same sprite can
    be read with slightly different names ("木片" vs "市片"), while two different
    items never share an icon.
    """

    MERGE_THRESHOLD = 0.95
    MAX_ICON_SAMPLES = 5

    def __init__(self, path=CATALOG_PATH):
        self.path = Path(path)
        self.items = {}
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.items = data.get("items", {})

    @classmethod
    def load(cls, path=CATALOG_PATH):
        return cls(path)

    # ---------- compact icon storage ----------

    @staticmethod
    def pack_patch(patch):
        array = np.clip(np.asarray(patch, dtype=np.float32), 0, 255).round().astype(
            np.uint8
        )
        return {
            "shape": list(array.shape),
            "b64": base64.b64encode(array.tobytes()).decode("ascii"),
        }

    @staticmethod
    def unpack_patch(icon):
        if not icon:
            return None
        if "b64" in icon:
            raw = base64.b64decode(icon["b64"])
            array = np.frombuffer(raw, dtype=np.uint8).reshape(icon["shape"])
            return array.astype(np.float32)
        if "patch" in icon:  # legacy float-list format
            return np.asarray(icon["patch"], dtype=np.float32)
        return None

    @staticmethod
    def unpack_mask(icon):
        if not icon:
            return None
        if "mask_b64" in icon:
            raw = base64.b64decode(icon["mask_b64"])
            array = np.frombuffer(raw, dtype=np.uint8).reshape(icon["mask_shape"])
            return array.astype(np.float32)
        if "mask" in icon:
            return np.asarray(icon["mask"], dtype=np.float32)
        return None

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 2, "items": self.items}
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # ---------- matching ----------

    @staticmethod
    def similarity(patch_a, mask_a, patch_b, mask_b):
        mask = (mask_a > 0) & (mask_b > 0)
        if mask.sum() < 50:
            return 0.0
        va = patch_a[mask]
        vb = patch_b[mask]
        va = va - va.mean(axis=0)
        vb = vb - vb.mean(axis=0)
        denom = float(np.linalg.norm(va) * np.linalg.norm(vb))
        if denom <= 1e-6:
            return 0.0
        return float((va * vb).sum() / denom)

    @staticmethod
    def _icons_of(entry):
        icons = entry.get("icons")
        if icons:
            return icons
        icon = entry.get("icon")
        return [icon] if icon else []

    def match(self, patch, mask, threshold=MATCH_THRESHOLD):
        """Best catalog match with a margin guard.

        The same sprite can score as low as ~0.81 across cells (quality variants,
        selection state, JPEG vs PNG), so a strict 0.86 cut rejects real hits.
        Accept those only when they clearly beat the runner-up, which keeps
        similar-looking items apart.
        """
        scores = []
        for item_id, entry in self.items.items():
            for icon in self._icons_of(entry):
                stored = self.unpack_patch(icon)
                stored_mask = self.unpack_mask(icon)
                if stored is None or stored_mask is None:
                    continue
                score = self.similarity(patch, mask, stored, stored_mask)
                scores.append((score, entry.get("name") or item_id, item_id))
        if not scores:
            return None
        scores.sort(key=lambda item: -item[0])
        best_score, best_name, best_id = scores[0]
        second = max(
            (score for score, _, item_id in scores if item_id != best_id), default=0.0
        )
        if best_score >= threshold:
            return best_name, best_score, best_id
        if best_score >= MATCH_FALLBACK and best_score - second >= MATCH_MARGIN:
            return best_name, best_score, best_id
        return None

    # ---------- learning ----------

    def _pack_icon(self, patch, mask):
        packed = self.pack_patch(patch)
        mask_array = (np.asarray(mask, dtype=np.float32) > 0).astype(np.uint8) * 255
        packed["mask_shape"] = list(mask_array.shape)
        packed["mask_b64"] = base64.b64encode(mask_array.tobytes()).decode("ascii")
        return packed

    def learn(self, name, patch, mask, star=None, price=None, category=None,
              description=None, log=None):
        """Merge into the entry with the same icon, or create a new one."""
        match = self.match(patch, mask, threshold=self.MERGE_THRESHOLD)
        if match:
            item_id = match[2]
            entry = self.items[item_id]
            entry["seen"] = entry.get("seen", 0) + 1
            entry["match_score_last"] = round(match[1], 4)
            icons = entry.setdefault("icons", self._icons_of(entry))
            if len(icons) < self.MAX_ICON_SAMPLES:
                icons.append(self._pack_icon(patch, mask))
        else:
            # Same item name but a different sprite (eggs come in several
            # colours): keep one entry and add the new icon as another sample.
            same_name = next(
                (
                    i
                    for i, e in self.items.items()
                    if name
                    and (e.get("name") == name or name in (e.get("aliases") or []))
                ),
                None,
            )
            if same_name:
                item_id = same_name
                entry = self.items[item_id]
                entry["seen"] = entry.get("seen", 0) + 1
                icons = entry.setdefault("icons", self._icons_of(entry))
                if len(icons) < self.MAX_ICON_SAMPLES:
                    icons.append(self._pack_icon(patch, mask))
                if log:
                    log(f"icon variant merged into {item_id} (name={name!r})")
                name = None  # skip alias handling below
            else:
                item_id = self._new_id(name)
                entry = {
                    "name": name,
                    "aliases": [],
                    "category": category,
                    "description": description,
                    "stars": {},
                    "icons": [self._pack_icon(patch, mask)],
                    "seen": 1,
                }
                self.items[item_id] = entry
                if log:
                    log(f"NEW item: {item_id} (name={name!r})")

        if name and name != entry.get("name"):
            aliases = entry.setdefault("aliases", [])
            if name not in aliases:
                aliases.append(name)
        if category and not entry.get("category"):
            entry["category"] = category
        if description and not entry.get("description"):
            entry["description"] = description
        if star is None and price is not None and not entry.get("stars") \
                and entry.get("base_price") is None:
            # Items with no quality star at all (bottles, boots, trash) always
            # sell at the base price, so the panel price is the base.
            entry["stars"] = {star_key(0.5): {"price": int(price)}}
            entry["base_price"] = float(price)
        if star is not None and price is not None:
            key = star_key(star)
            known = entry["stars"].get(key)
            if known and known.get("price") != int(price):
                if log:
                    log(
                        f"price conflict {item_id} star={key}: "
                        f"{known.get('price')} vs {price}"
                    )
            else:
                entry["stars"][key] = {"price": int(price)}
            # One observation is enough for every quality level: store the base.
            base = base_price_from(price, star)
            if base is not None:
                previous = entry.get("base_price")
                if previous is None:
                    entry["base_price"] = base
                elif abs(float(previous) - base) > 0.02 * max(base, 1.0):
                    if log:
                        log(
                            f"base price conflict {item_id}: "
                            f"{previous} vs {base} (star={key}, price={price})"
                        )
        return item_id, entry

    def _new_id(self, name):
        base = f"item_{len(self.items) + 1:03d}"
        if name:
            safe = "".join(ch for ch in name if ch.isalnum() or ch in "-_")[:16]
            if safe:
                base = f"{base}_{safe}"
        return base

    def price_for(self, name_or_id, star):
        entry = None
        if name_or_id in self.items:
            entry = self.items[name_or_id]
        else:
            for candidate in self.items.values():
                if candidate.get("name") == name_or_id:
                    entry = candidate
                    break
        if not entry:
            return None
        base = entry.get("base_price")
        if base is not None:
            predicted = price_at(base, star)
            if predicted is not None:
                return predicted
        value = entry.get("stars", {}).get(star_key(star)) or {}
        return value.get("price")


def summarize(slots, catalog=None):
    catalog = catalog or ItemCatalog.load()
    out = []
    for cell in slots:
        badge = cell.badge or {}
        price = None
        if cell.item:
            price = catalog.price_for(cell.item, badge.get("value"))
        out.append(
            {
                "grid": cell.grid,
                "col": cell.col,
                "row": cell.row,
                "item": cell.item,
                "item_id": cell.item_id,
                "similarity": round(cell.similarity, 3),
                "star": badge.get("value"),
                "star_color": badge.get("color"),
                "count": cell.count,
                "unit_price": price,
            }
        )
    return out


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    parser.add_argument("--mode", default="WAREHOUSE", choices=list(MODES))
    parser.add_argument("--no-ocr", action="store_true")
    args = parser.parse_args()

    frame = cv2.imread(args.image)
    if frame is None:
        raise SystemExit(f"cannot read {args.image}")
    grid = GridReader()
    slots = grid.scan(frame, args.mode, read_badges=not args.no_ocr)
    print(f"occupied slots: {len(slots)}")
    for row in summarize(slots):
        print(row)
