#!/usr/bin/env python3
"""Template fallback for the item badge (quality star number).

macOS Vision reads the badge numbers well except for a lone narrow "1", which it
almost never detects. The glyphs are rendered with one fixed font and size, so a
nearest-template classifier over harvested badge crops closes that gap: every
successful OCR read contributes a labelled template, and failures are matched
against the pool.

The glyph patch is binarised (Otsu, glyph = white), cropped to its bounding box
and resized to a fixed size, so width differences between "1" and "0.5" do not
matter.
"""

import base64
import json
from pathlib import Path

import cv2
import numpy as np

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_PATH = BASE_DIR / "data" / "badge_templates.json"

PATCH_SIZE = (40, 28)  # w, h
MAX_SAMPLES_PER_VALUE = 12
MATCH_THRESHOLD = 0.90


def glyph_patch(gray_crop, normalize_aspect=True):
    """Isolate the badge number glyph and normalise it.

    The badge crop also contains a card border line and star remnants, so:
      1. rows that are mostly dark (the card's top border) are painted out;
      2. the left star remnant strip is trimmed;
      3. Otsu + largest connected components -> tight glyph bbox;
      4. scale by height only (keeping aspect) and paste centred on a fixed
         canvas, so a lone "1" is not stretched like a wide "0.5".
    """
    if gray_crop is None or gray_crop.size == 0:
        return None
    if gray_crop.ndim == 3:
        gray_crop = cv2.cvtColor(gray_crop, cv2.COLOR_BGR2GRAY)
    height, width = gray_crop.shape[:2]
    image = gray_crop.copy()
    dark = image < 130
    row_ratio = dark.mean(axis=1)
    for row in np.where(row_ratio > 0.65)[0]:
        image[row, :] = 255
    # trim star remnants on the left edge
    trim = max(2, int(width * 0.10))
    image = image[:, trim:]
    dark = image < 130
    col_ratio = dark.mean(axis=0)
    for col in np.where(col_ratio > 0.85)[0]:
        image[:, col] = 255
    _, binary = cv2.threshold(image, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(binary, 8)
    if count <= 1:
        return None
    # keep the two largest components (digits and the decimal point)
    order = sorted(range(1, count), key=lambda i: -stats[i, cv2.CC_STAT_AREA])[:2]
    keep = [i for i in order if stats[i, cv2.CC_STAT_AREA] >= 18]
    if not keep:
        return None
    xs, ys = [], []
    for i in keep:
        x, y, w, h = stats[i, cv2.CC_STAT_LEFT], stats[i, cv2.CC_STAT_TOP], stats[i, cv2.CC_STAT_WIDTH], stats[i, cv2.CC_STAT_HEIGHT]
        xs += [x, x + w]
        ys += [y, y + h]
    x1, x2, y1, y2 = min(xs), max(xs), min(ys), max(ys)
    if x2 - x1 < 4 or y2 - y1 < 8:
        return None
    crop = binary[y1:y2, x1:x2]
    target_w, target_h = PATCH_SIZE
    if normalize_aspect:
        scale = target_h / max(1, crop.shape[0])
        new_w = max(2, int(round(crop.shape[1] * scale)))
        new_w = min(new_w, target_w)
        resized = cv2.resize(crop, (new_w, target_h), interpolation=cv2.INTER_AREA)
        canvas = np.zeros((target_h, target_w), dtype=np.uint8)
        offset = (target_w - new_w) // 2
        canvas[:, offset:offset + new_w] = resized
        patch = canvas
    else:
        patch = cv2.resize(crop, PATCH_SIZE, interpolation=cv2.INTER_AREA)
    return (patch.astype(np.float32) / 255.0)


def _encode(patch):
    data = np.clip(patch * 255, 0, 255).astype(np.uint8)
    return base64.b64encode(data.tobytes()).decode("ascii")


def _decode(text):
    raw = np.frombuffer(base64.b64decode(text), dtype=np.uint8)
    return raw.reshape(PATCH_SIZE[1], PATCH_SIZE[0]).astype(np.float32) / 255.0


def similarity(a, b):
    va = a.ravel() - a.mean()
    vb = b.ravel() - b.mean()
    denom = float(np.linalg.norm(va) * np.linalg.norm(vb))
    if denom <= 1e-6:
        return 0.0
    return float((va * vb).sum() / denom)


class BadgeTemplateStore:
    def __init__(self, path=DEFAULT_PATH):
        self.path = Path(path)
        self.samples = {}
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self.samples = data.get("values", {})
            except (ValueError, OSError):
                self.samples = {}

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({"values": self.samples}, ensure_ascii=False), encoding="utf-8"
        )

    def count(self):
        return sum(len(v) for v in self.samples.values())

    def add(self, value, patch):
        """Add a labelled template, skipping near-duplicates."""
        if value is None or patch is None:
            return False
        key = f"{float(value):g}"
        bucket = self.samples.setdefault(key, [])
        for encoded in bucket:
            if similarity(patch, _decode(encoded)) >= 0.97:
                return False
        if len(bucket) >= MAX_SAMPLES_PER_VALUE:
            return False
        bucket.append(_encode(patch))
        return True

    def match(self, patch, threshold=MATCH_THRESHOLD, allowed=None):
        """Return (value, score) of the closest template, or (None, best)."""
        if patch is None:
            return None, 0.0
        best_value, best_score = None, 0.0
        for key, bucket in self.samples.items():
            if allowed is not None:
                try:
                    if not allowed(float(key)):
                        continue
                except ValueError:
                    continue
            for encoded in bucket:
                score = similarity(patch, _decode(encoded))
                if score > best_score:
                    best_score, best_value = score, float(key)
        if best_value is not None and best_score >= threshold:
            return best_value, best_score
        return None, best_score


_STORE = None


def store():
    global _STORE
    if _STORE is None:
        _STORE = BadgeTemplateStore()
    return _STORE


if __name__ == "__main__":
    s = store()
    print(f"badge templates: {s.count()} samples across {len(s.samples)} values")
    for value, bucket in sorted(s.samples.items(), key=lambda kv: float(kv[0])):
        print(f"  {value}: {len(bucket)}")
