#!/usr/bin/env python3
"""Read-only M1 state reader for the Switch 2 capture feed.

It recognizes the current UI mode, HUD, prompts, and inventory grid occupancy.
It NEVER sends controller input. No Socket.IO and no macro calls are present.

Examples:
    python vision/state_reader.py --image vision/samples/storage_prompt.png
    python vision/state_reader.py --image vision/samples/warehouse_ui.png
    python vision/state_reader.py --live --index 0
"""

import argparse
import json
import re
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
import yaml

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.yaml"
TEMPLATE_DIR = BASE_DIR / "templates" / "m1"
OCR_BIN = BASE_DIR / "bin" / "ocr_vision"
LOG_DIR = BASE_DIR / "logs"

MODES = ("HOME", "WORLD", "MODAL", "WAREHOUSE", "BACKPACK", "UNKNOWN")
BUTTON_TOKENS = {"A", "B", "X", "Y", "L", "R", "ZL", "ZR", "+", "-", "PLUS", "MINUS"}


def load_config():
    with CONFIG_PATH.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def roi_to_pixels(roi, width, height):
    x, y, w, h = roi
    x1 = max(0, int(round(x * width)))
    y1 = max(0, int(round(y * height)))
    x2 = min(width, int(round((x + w) * width)))
    y2 = min(height, int(round((y + h) * height)))
    return x1, y1, x2, y2


def normalize_text(text):
    return re.sub(r"\s+", "", str(text)).strip()


def parse_price(text):
    """Parse "90G" style values; tolerate G being misread as 6/0/Q/O."""
    raw = normalize_text(text).upper()
    if not raw:
        return None
    match = re.fullmatch(r"(\d+)\s*([G6QO0])", raw)
    if match:
        return int(match.group(1))
    match = re.search(r"(\d+)", raw)
    return int(match.group(1)) if match else None


def split_leading_button(text):
    match = re.match(r"^(ZL|ZR|A|B|X|Y|L|R|\+|-)\s*(.+)$", text)
    if match:
        return match.group(1), match.group(2)
    return None, text


def correct_hud_text(text):
    """Apply conservative fixes for known OCR confusions in the game font."""
    corrected = str(text).replace("週", "周")
    # HUD digits only; these substitutions are not used for item names.
    corrected = corrected.replace("O", "0").replace("o", "0")
    corrected = corrected.replace("春周", "春月")
    # The stylized 3 is sometimes read as 室 or 豆 in 春月13日.
    corrected = re.sub(r"([春夏秋冬]月\d)[室豆]", r"\g<1>3", corrected)
    # Normalize full-width parentheses and a stray slash after the time.
    corrected = corrected.replace("（", "(").replace("）", ")")
    corrected = re.sub(r"(\d{1,2}[.:]\d{2})\s*/\s*", r"\1 ", corrected)
    return corrected


class OcrRunner:
    def __init__(self, enabled=True):
        self.enabled = bool(enabled and OCR_BIN.exists())
        self.last_error = None

    def ocr_crop(self, crop, scale=1.0, lang=None):
        """Return OCR lines with coordinates in the (possibly scaled) crop."""
        if not self.enabled or crop is None or crop.size == 0:
            return []

        if scale != 1.0:
            crop = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)

        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as handle:
                temp_path = Path(handle.name)
            if not cv2.imwrite(str(temp_path), crop):
                self.last_error = "failed to write OCR temp image"
                return []

            command = [str(OCR_BIN), str(temp_path)]
            if lang:
                command.append(lang)
            proc = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if proc.returncode != 0:
                self.last_error = proc.stderr.strip() or f"OCR exit {proc.returncode}"
                return []

            lines = []
            for raw_line in proc.stdout.splitlines():
                parts = raw_line.split("\t")
                if len(parts) < 5:
                    continue
                try:
                    x, y, w, h = (float(value) for value in parts[:4])
                except ValueError:
                    continue
                text = parts[4].strip()
                if text:
                    lines.append({"x": x, "y": y, "w": w, "h": h, "text": text})
            return lines
        except Exception as exc:  # noqa: BLE001 - report OCR failures in state
            self.last_error = f"{type(exc).__name__}: {exc}"
            return []
        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink()
                except OSError:
                    pass


class StateReader:
    def __init__(self, ocr_enabled=True):
        self.config = load_config()
        self.width = int(self.config["capture"]["width"])
        self.height = int(self.config["capture"]["height"])
        self.rois = self.config["rois"]
        self.grids = self.config["grids"]
        self.thresholds = self.config["thresholds"]
        self.prompt_button_hints = self.config.get("prompt_button_hints", {})
        self.ocr = OcrRunner(ocr_enabled)
        # CLAHE keeps template matching usable in the game's darker interiors.
        self._clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        self.templates = self._load_templates()
        self._ocr_cache = {}
        self._ocr_cache_time = 0.0

    def _load_templates(self):
        names = [
            "title_warehouse_text",
            "title_backpack_text",
            "prompt_modal_close",
            "hud_sun",
            "hud_rain",
            "hud_wind",
            # Switch system UI anchors, so the HOME/menu screen is never
            # mistaken for the game world.
            "sys_battery",
            "sys_pad_battery",
            # Fast prompt anchors for the walk controller's stop condition.
            "prompt_storage",
            "prompt_greet",
        ]
        templates = {}
        for name in names:
            path = TEMPLATE_DIR / f"{name}.png"
            image = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
            if image is not None:
                templates[name] = self._clahe.apply(image)
        return templates

    def match(self, gray, template_name, roi_name):
        template = self.templates.get(template_name)
        if template is None:
            return 0.0, None

        x1, y1, x2, y2 = roi_to_pixels(self.rois[roi_name], self.width, self.height)
        search = gray[y1:y2, x1:x2]
        if search.shape[0] < template.shape[0] or search.shape[1] < template.shape[1]:
            return 0.0, None

        result = cv2.matchTemplate(search, template, cv2.TM_CCOEFF_NORMED)
        _, score, _, location = cv2.minMaxLoc(result)
        return float(score), (x1 + location[0], y1 + location[1])

    # ---------- HUD ----------

    def read_stamina(self, frame):
        x1, y1, x2, y2 = roi_to_pixels(self.rois["stamina_bar"], self.width, self.height)
        roi = frame[y1:y2, x1:x2]
        if roi.size == 0:
            return {"detected": False, "ratio": 0.0, "band": "unknown"}

        hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
        hue = hsv[:, :, 0]
        sat = hsv[:, :, 1]
        val = hsv[:, :, 2]

        green = (hue >= 60) & (hue <= 100)
        yellow = (hue >= 15) & (hue < 60)
        red = (hue < 15) | (hue >= 170)
        colored = (sat > 100) & (val > 30) & (green | yellow | red)

        roi_h, roi_w = colored.shape
        columns = colored.sum(axis=0)
        valid = np.where(columns >= max(2, int(roi_h * 0.25)))[0]
        if len(valid) == 0:
            return {"detected": False, "ratio": 0.0, "band": "unknown"}

        ratio = float((valid.max() - valid.min() + 1) / roi_w)
        pixels = hsv[colored]
        median_hue = float(np.median(pixels[:, 0])) if len(pixels) else -1.0

        if 60 <= median_hue <= 100:
            band = "green"
        elif 15 <= median_hue < 60:
            band = "yellow"
        elif median_hue < 15 or median_hue >= 170:
            band = "red"
        else:
            band = "unknown"

        return {
            "detected": True,
            "ratio": round(max(0.0, min(1.0, ratio)), 3),
            "band": band,
            "median_hue": round(median_hue, 1),
        }

    def parse_datetime_text(self, raw):
        corrected = correct_hud_text(raw)

        time_match = re.search(r"(\d{1,2})[.:](\d{2})\s*([APF])M", corrected, re.IGNORECASE)
        date_match = re.search(
            r"([春夏秋冬])月(\d{1,2})日\s*\(?[周週]([一二三四五六日])\)?",
            corrected,
        )
        if date_match is None:
            # Weekday can be unreadable; keep season/day if possible.
            date_match = re.search(r"([春夏秋冬])月(\d{1,2})日", corrected)

        result = {
            "raw": raw,
            "corrected": corrected,
            "time": None,
            "time_24h": None,
            "season": None,
            "day": None,
            "weekday": None,
            "date": None,
        }

        if time_match:
            hour = int(time_match.group(1))
            minute = int(time_match.group(2))
            period_letter = time_match.group(3).upper()
            period = "PM" if period_letter in ("P", "F") else "AM"
            result["time"] = f"{hour:02d}:{minute:02d} {period}"
            hour24 = hour % 12 + (12 if period == "PM" else 0)
            result["time_24h"] = f"{hour24:02d}:{minute:02d}"

        if date_match:
            season = date_match.group(1)
            day = int(date_match.group(2))
            weekday = None
            if date_match.lastindex and date_match.lastindex >= 3:
                weekday = f"周{date_match.group(3)}"
            result.update(
                {
                    "season": season,
                    "day": day,
                    "weekday": weekday,
                    "date": f"{season}月{day}日"
                    + (f"（{weekday}）" if weekday else ""),
                }
            )

        return result

    def read_datetime(self, frame):
        x1, y1, x2, y2 = roi_to_pixels(self.rois["datetime"], self.width, self.height)
        crop = frame[y1:y2, x1:x2]
        crop_gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)

        # Try the raw BGR crop first (best for bright scenes), then grayscale
        # 2x (best for dark interiors). Merge time/date if one variant misses.
        variants = [
            ("bgr_1x", crop, 1.0),
            ("gray_2x", crop_gray, 2.0),
            # Fallbacks only pay the OCR cost when the first two miss.
            ("gray_3x", crop_gray, 3.0),
            ("bgr_2x", crop, 2.0),
        ]

        merged = None
        for variant_name, variant_image, scale in variants:
            lines = self.ocr.ocr_crop(variant_image, scale=scale)
            raw = " ".join(line["text"] for line in lines).strip()
            result = self.parse_datetime_text(raw)
            result["ocr_variant"] = variant_name

            if result["time"] and result["date"]:
                return result

            if merged is None:
                merged = result
            else:
                for key in ("time", "time_24h", "season", "day", "weekday", "date"):
                    if not merged.get(key) and result.get(key):
                        merged[key] = result[key]

        return merged or {
            "raw": "",
            "corrected": "",
            "time": None,
            "time_24h": None,
            "season": None,
            "day": None,
            "weekday": None,
            "date": None,
        }

    def read_hud(self, frame, gray, use_ocr=True):
        weather_scores = {
            "sunny": self.match(gray, "hud_sun", "hud_search")[0],
            "rainy": self.match(gray, "hud_rain", "hud_search")[0],
        }
        ranked_weather = sorted(weather_scores.items(), key=lambda item: item[1], reverse=True)
        weather, weather_score = ranked_weather[0]
        second_weather_score = ranked_weather[1][1]
        margin = self.thresholds.get("weather_margin", 0.10)
        if (
            weather_score < self.thresholds["hud_icon"]
            or weather_score - second_weather_score < margin
        ):
            weather = "unknown"

        wind_score = self.match(gray, "hud_wind", "hud_search")[0]
        wind = "icon_detected" if wind_score >= self.thresholds["wind_icon"] else "unknown"

        return {
            "stamina": self.read_stamina(frame),
            "weather": weather,
            "weather_scores": {key: round(value, 3) for key, value in weather_scores.items()},
            "wind": wind,
            "wind_score": round(wind_score, 3),
            "datetime": self.read_datetime(frame) if use_ocr else {"raw": None},
        }

    # ---------- prompts ----------

    def read_prompts(self, frame, mode, use_ocr=True):
        if not use_ocr:
            return {"items": [], "raw_lines": []}

        if mode in ("WORLD", "MODAL"):
            roi = self.rois["prompt_search"]
            scale = 2.0
        else:
            # Bottom prompt strip for BACKPACK / WAREHOUSE.
            roi = [0.0, 0.89, 1.0, 0.11]
            scale = 2.0

        x1, y1, x2, y2 = roi_to_pixels(roi, self.width, self.height)
        crop = frame[y1:y2, x1:x2]
        lines = self.ocr.ocr_crop(crop, scale=scale)
        if not lines:
            return {"items": [], "raw_lines": []}

        raw_lines = [line["text"] for line in lines]
        glyphs = []
        labels = []
        for line in lines:
            token = normalize_text(line["text"]).upper()
            if token in BUTTON_TOKENS:
                glyphs.append(line)
            else:
                labels.append(line)

        items = []
        used_labels = set()
        for glyph in glyphs:
            glyph_center_y = glyph["y"] + glyph["h"] / 2
            candidates = []
            for index, label in enumerate(labels):
                if index in used_labels:
                    continue
                label_center_y = label["y"] + label["h"] / 2
                same_row = abs(label_center_y - glyph_center_y) <= max(glyph["h"], label["h"])
                to_right = label["x"] >= glyph["x"]
                if same_row and to_right:
                    candidates.append((abs(label_center_y - glyph_center_y), index, label))
            if candidates:
                _, index, label = min(candidates, key=lambda item: item[0])
                used_labels.add(index)
                items.append(
                    {
                        "button": normalize_text(glyph["text"]).upper(),
                        "label": normalize_text(label["text"]),
                        "text": f"{normalize_text(glyph['text']).upper()} {normalize_text(label['text'])}",
                        "button_source": "ocr",
                    }
                )
            else:
                items.append(
                    {
                        "button": normalize_text(glyph["text"]).upper(),
                        "label": None,
                        "text": normalize_text(glyph["text"]).upper(),
                        "button_source": "ocr",
                    }
                )

        # OCR sometimes merges the button glyph into the label, e.g. "B关闭".
        for item in items:
            if item.get("button") is None and item.get("label"):
                button, label = split_leading_button(item["label"])
                if button:
                    item["button"] = button
                    item["label"] = label
                    item["text"] = f"{button} {label}"
                    item["button_source"] = "ocr_merged"

        # Warehouse/backpack bottom bars have icon glyphs; use verified hints.
        known_texts = {item["text"] for item in items}
        unmatched = [
            (index, label)
            for index, label in enumerate(labels)
            if index not in used_labels
        ]
        for _, label in sorted(unmatched, key=lambda pair: pair[1]["x"]):
            text = normalize_text(label["text"])
            button, label_text = split_leading_button(text)
            source = "ocr_merged" if button else None
            if button is None:
                button = self.prompt_button_hints.get(mode, {}).get(label_text)
                source = "hint" if button else None
            item = {
                "button": button,
                "label": label_text,
                "text": f"{button} {label_text}" if button else label_text,
                "button_source": source,
            }
            if item["text"] not in known_texts:
                items.append(item)
                known_texts.add(item["text"])

        return {"items": items, "raw_lines": raw_lines}

    # ---------- inventory ----------

    @staticmethod
    def auto_expose(image, target=140.0, low=100.0, max_gain=4.0):
        """Brighten a uniformly dimmed crop (Switch idle dim / fade frames).

        Multiplicative gain keeps hue intact, so HSV checks stay valid. Frames
        that are already bright are returned untouched.
        """
        if image is None or image.size == 0:
            return image
        gray = (
            cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
            if image.ndim == 3
            else image
        )
        mean = float(gray.mean())
        if mean >= low or mean <= 1.0:
            return image
        gain = min(max_gain, target / mean)
        return np.clip(image.astype(np.float32) * gain, 0, 255).astype(np.uint8)

    def count_grid(self, frame, roi, cols, rows, capacity):
        patch_frame = self.auto_expose(frame)
        x1, y1, x2, y2 = roi_to_pixels(roi, self.width, self.height)
        cell_w = (x2 - x1) / cols
        cell_h = (y2 - y1) / rows
        used = 0
        for row in range(rows):
            for col in range(cols):
                cx1 = int(x1 + col * cell_w + cell_w * 0.18)
                cy1 = int(y1 + row * cell_h + cell_h * 0.18)
                cx2 = int(x1 + (col + 1) * cell_w - cell_w * 0.18)
                cy2 = int(y1 + (row + 1) * cell_h - cell_h * 0.18)
                patch = patch_frame[cy1:cy2, cx1:cx2]
                if patch.size and float(patch.std()) > self.thresholds["slot_std"]:
                    used += 1
        return {"used": used, "capacity": capacity, "full": used >= capacity}

    def read_inventory(self, frame, mode):
        if mode == "BACKPACK":
            grid = self.grids["backpack"]
            return {
                "kind": "backpack",
                "backpack": self.count_grid(
                    frame,
                    self.rois["backpack_grid"],
                    grid["cols"],
                    grid["rows"],
                    grid["capacity"],
                ),
            }

        if mode == "WAREHOUSE":
            left = self.grids["warehouse_left"]
            right = self.grids["warehouse_right"]
            return {
                "kind": "warehouse",
                "backpack": self.count_grid(
                    frame,
                    self.rois["warehouse_left_grid"],
                    left["cols"],
                    left["rows"],
                    left["capacity"],
                ),
                "warehouse": self.count_grid(
                    frame,
                    self.rois["warehouse_right_grid"],
                    right["cols"],
                    right["rows"],
                    right["capacity"],
                ),
            }

        return None

    # ---------- item detail / warehouse pages ----------

    def ocr_region(self, frame, roi_name, scale=3.0, gray=False, clahe=False):
        """OCR one configured ROI. Returns (text, line_count)."""
        x1, y1, x2, y2 = roi_to_pixels(
            self.rois[roi_name], self.width, self.height
        )
        crop = frame[y1:y2, x1:x2]
        if crop.size == 0:
            return "", 0
        image = self.auto_expose(crop)
        if gray or clahe:
            image = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        if clahe:
            image = self._clahe.apply(image)
        lines = self.ocr.ocr_crop(image, scale=scale)
        text = " ".join(line["text"] for line in lines).strip()
        return text, len(lines)

    def read_item_detail(self, frame, mode):
        """Name / description / price (and count for backpack) of the selected item."""
        if mode == "WAREHOUSE":
            name, _ = self.ocr_region(frame, "warehouse_item_name", scale=3.0)
            desc, _ = self.ocr_region(
                frame, "warehouse_item_desc", scale=3.0, gray=True, clahe=True
            )
            price_raw, _ = self.ocr_region(frame, "warehouse_item_price", scale=3.0)
        elif mode == "BACKPACK":
            name, _ = self.ocr_region(frame, "backpack_item_name", scale=3.0)
            desc, _ = self.ocr_region(
                frame, "backpack_item_desc", scale=3.0, gray=True, clahe=True
            )
            price_raw, _ = self.ocr_region(frame, "backpack_item_price", scale=3.0)
            count_raw, _ = self.ocr_region(frame, "backpack_item_count", scale=3.0)
        else:
            return None

        detail = {
            "name": name or None,
            "description": desc or None,
            "price": parse_price(price_raw),
            "price_raw": price_raw or None,
        }
        if mode == "BACKPACK":
            detail["count"] = parse_price(count_raw)
            detail["count_raw"] = count_raw or None
        return detail

    def read_warehouse_pages(self, frame):
        """Right-side page dots: one blue (current page) + N dark (other pages).

        Uses scale-invariant features (hue/saturation for the blue dot, contrast
        relative to the crop median for the dark dots) so a dimmed frame still
        parses after auto-exposure.
        """
        x1, y1, x2, y2 = roi_to_pixels(
            self.rois["warehouse_pages"], self.width, self.height
        )
        crop = frame[y1:y2, x1:x2]
        if crop.size == 0:
            return None
        exposed = self.auto_expose(crop)
        hsv = cv2.cvtColor(exposed, cv2.COLOR_BGR2HSV)
        hue, sat, val = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
        gray = cv2.cvtColor(exposed, cv2.COLOR_BGR2GRAY)
        median = float(np.median(gray))
        blue = (hue > 85) & (hue < 130) & (sat > 90) & (val > 70)
        dark = (gray < median - 22) & ~blue
        mask = ((blue | dark).astype(np.uint8)) * 255
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
        count, labels, stats, centroids = cv2.connectedComponentsWithStats(mask, 8)
        dots = []
        for index in range(1, count):
            x, y, width, height, area = stats[index]
            if 5 <= width <= 22 and 5 <= height <= 22 and area >= 25:
                is_blue = bool(blue[labels == index].mean() > 0.4)
                dots.append((float(centroids[index][0]), is_blue))
        if len(dots) < 2:
            return None
        dots.sort()
        current = next(
            (pos + 1 for pos, (_, is_blue) in enumerate(dots) if is_blue), None
        )
        return {"page": current, "total": len(dots)}

    # ---------- analysis ----------

    def detect_mode(self, frame, gray):
        # The Switch system UI (HOME screen, menus) shows a top-right battery
        # icon and a bottom-left controller battery widget. Both are absent from
        # the in-game HUD, so they are checked first: without this the HOME
        # screen's avatar row triggers a false stamina hit and looks like WORLD.
        battery_score, _ = self.match(gray, "sys_battery", "home_status_search")
        pad_score, _ = self.match(gray, "sys_pad_battery", "home_search")
        home_score = max(battery_score, pad_score)

        warehouse_score, _ = self.match(gray, "title_warehouse_text", "title_search")
        backpack_score, _ = self.match(gray, "title_backpack_text", "title_search")
        modal_score, _ = self.match(gray, "prompt_modal_close", "prompt_search")
        stamina = self.read_stamina(frame)
        wind_score, _ = self.match(gray, "hud_wind", "hud_search")

        if home_score >= self.thresholds["home"]:
            mode = "HOME"
        elif (
            warehouse_score >= self.thresholds["mode"]
            and warehouse_score > backpack_score
        ):
            mode = "WAREHOUSE"
        elif (
            backpack_score >= self.thresholds["mode"]
            and backpack_score > warehouse_score
        ):
            mode = "BACKPACK"
        elif modal_score >= self.thresholds["modal"]:
            mode = "MODAL"
        elif stamina["detected"] or wind_score >= self.thresholds["wind_icon"]:
            mode = "WORLD"
        else:
            mode = "UNKNOWN"

        confidence = {
            "home": round(home_score, 3),
            "system_battery": round(battery_score, 3),
            "system_pad": round(pad_score, 3),
            "warehouse_title": round(warehouse_score, 3),
            "backpack_title": round(backpack_score, 3),
            "modal_prompt": round(modal_score, 3),
            "wind_icon": round(wind_score, 3),
            "stamina": stamina["detected"],
        }
        return mode, confidence

    def analyze(self, frame, use_ocr=True):
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray = self._clahe.apply(gray)
        mode, confidence = self.detect_mode(frame, gray)

        state = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "mode": mode,
            "confidence": confidence,
            "hud": None,
            "prompts": {"items": [], "raw_lines": []},
            "inventory": None,
            "item": None,
            "pages": None,
            "world_ok": False,
            "ocr": {
                "enabled": self.ocr.enabled,
                "error": self.ocr.last_error,
            },
        }

        if mode in ("WORLD", "MODAL"):
            state["hud"] = self.read_hud(frame, gray, use_ocr=use_ocr)

        if mode == "HOME":
            # Switch system UI: game HUD/prompts/inventory do not apply here.
            return state

        state["prompts"] = self.read_prompts(frame, mode, use_ocr=use_ocr)
        state["inventory"] = self.read_inventory(frame, mode)
        if use_ocr and mode in ("WAREHOUSE", "BACKPACK"):
            state["item"] = self.read_item_detail(frame, mode)
            if mode == "WAREHOUSE":
                state["pages"] = self.read_warehouse_pages(frame)
        if mode == "WORLD":
            state["world_ok"] = self.world_is_safe(state["hud"])
        return state

    def world_is_safe(self, hud):
        """True only when the game world is positively identified.

        Gate for the walk controller. The game HUD date line ("春月16日") is the
        anchor: the clock digits sit on bright walls and are sometimes misread,
        but the season/day line is consistently readable and never appears on the
        Switch HOME screen or in system menus.
        """
        hud = hud or {}
        dt = hud.get("datetime") or {}
        if not dt.get("date"):
            return False
        stamina = hud.get("stamina") or {}
        if stamina.get("detected"):
            return True
        return float(hud.get("wind_score") or 0.0) >= self.thresholds["wind_icon"]

    # ---------- visualization ----------

    def annotate(self, frame, state):
        output = frame.copy()
        mode = state["mode"]
        color = {
            "HOME": (255, 200, 0),
            "WORLD": (0, 200, 0),
            "MODAL": (0, 180, 255),
            "WAREHOUSE": (255, 160, 0),
            "BACKPACK": (255, 100, 200),
            "UNKNOWN": (0, 0, 255),
        }.get(mode, (255, 255, 255))

        cv2.rectangle(output, (0, 0), (self.width, 46), (0, 0, 0), -1)
        cv2.putText(
            output,
            f"MODE: {mode}",
            (16, 32),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.9,
            color,
            2,
            cv2.LINE_AA,
        )

        if mode == "WORLD" and not state.get("world_ok"):
            cv2.putText(
                output,
                "UNVERIFIED - no game HUD",
                (self.width - 460, 32),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.7,
                (0, 0, 255),
                2,
                cv2.LINE_AA,
            )

        hud = state.get("hud") or {}
        stamina = hud.get("stamina") or {}
        if stamina.get("detected"):
            text = f"GUTS {stamina['ratio']:.2f} [{stamina['band']}]"
            cv2.putText(
                output,
                text,
                (16, 78),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.7,
                (0, 255, 0),
                2,
                cv2.LINE_AA,
            )

        dt = hud.get("datetime") or {}
        if dt.get("time") or dt.get("date"):
            text = "  ".join(
                part for part in (dt.get("time"), dt.get("date")) if part
            )
            cv2.putText(
                output,
                text,
                (self.width - 560, 78),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.7,
                (255, 255, 255),
                2,
                cv2.LINE_AA,
            )

        weather = hud.get("weather")
        if weather and weather != "unknown":
            cv2.putText(
                output,
                f"WEATHER: {weather}",
                (16, 112),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.6,
                (255, 255, 255),
                1,
                cv2.LINE_AA,
            )

        prompts = state["prompts"]["items"]
        if prompts:
            y = self.height - 24 * len(prompts) - 16
            for item in prompts:
                cv2.putText(
                    output,
                    item["text"],
                    (self.width - 360, y),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.65,
                    (0, 255, 255),
                    2,
                    cv2.LINE_AA,
                )
                y += 24

        inventory = state.get("inventory")
        if inventory:
            lines = []
            if inventory.get("backpack"):
                backpack = inventory["backpack"]
                lines.append(
                    f"BACKPACK {backpack['used']}/{backpack['capacity']}"
                    + (" FULL" if backpack["full"] else "")
                )
            if inventory.get("warehouse"):
                warehouse = inventory["warehouse"]
                lines.append(
                    f"WAREHOUSE {warehouse['used']}/{warehouse['capacity']}"
                    + (" PAGE FULL" if warehouse["full"] else "")
                )
            for index, text in enumerate(lines):
                cv2.putText(
                    output,
                    text,
                    (16, 150 + index * 28),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.7,
                    (255, 160, 0),
                    2,
                    cv2.LINE_AA,
                )

        item = state.get("item") or {}
        if item.get("name"):
            label = item["name"]
            if item.get("price"):
                label += f"  ({item['price']}G)"
            cv2.putText(
                output,
                label,
                (16, 238),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.7,
                (0, 255, 255),
                2,
                cv2.LINE_AA,
            )

        pages = state.get("pages") or {}
        if pages.get("total"):
            cv2.putText(
                output,
                f"PAGE {pages.get('page')}/{pages['total']}",
                (16, 274),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.7,
                (0, 255, 255),
                2,
                cv2.LINE_AA,
            )

        return output


def print_state(state):
    print(json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True))


def append_jsonl(state):
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / f"state_{datetime.now():%Y%m%d}.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(state, ensure_ascii=False) + "\n")


def run_image(reader, path, use_ocr, annotate):
    frame = cv2.imread(str(path))
    if frame is None:
        raise SystemExit(f"cannot read image: {path}")

    state = reader.analyze(frame, use_ocr=use_ocr)
    state["source"] = str(path)
    print_state(state)

    if annotate:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        output = LOG_DIR / f"annotated_{path.stem}.png"
        cv2.imwrite(str(output), reader.annotate(frame, state))
        print(f"annotated: {output}")


def run_live(reader, index, use_ocr, annotate, duration=0.0):
    cap = cv2.VideoCapture(index, cv2.CAP_AVFOUNDATION)
    if not cap.isOpened():
        raise SystemExit(
            f"cannot open camera index {index}; close OBS and check Camera permission"
        )

    cap.set(cv2.CAP_PROP_FRAME_WIDTH, reader.width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, reader.height)
    cap.set(cv2.CAP_PROP_FPS, reader.config["capture"]["fps"])
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

    title = "State Reader (read-only) - q/ESC quit, s snapshot"
    cv2.namedWindow(title, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(title, 1280, 720)

    last_analysis = 0.0
    state = None
    last_mode = None
    deadline = time.perf_counter() + duration if duration > 0 else None
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                print("read failed")
                break

            now = time.perf_counter()
            if state is None or now - last_analysis >= 0.5:
                state = reader.analyze(frame, use_ocr=use_ocr)
                last_analysis = now
                append_jsonl(state)
                if state["mode"] != last_mode:
                    print(f"mode: {state['mode']}")
                    last_mode = state["mode"]

            annotated = reader.annotate(frame, state)
            display = cv2.resize(annotated, (1280, 720), interpolation=cv2.INTER_AREA)
            cv2.imshow(title, display)
            key = cv2.waitKey(1) & 0xFF
            if key in (27, ord("q")):
                break
            if key == ord("s"):
                LOG_DIR.mkdir(parents=True, exist_ok=True)
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                snapshot = LOG_DIR / f"state_snapshot_{stamp}.png"
                cv2.imwrite(str(snapshot), frame)
                print(f"saved {snapshot}")

            if cv2.getWindowProperty(title, cv2.WND_PROP_VISIBLE) < 1:
                break
            if deadline is not None and now >= deadline:
                break
    finally:
        cap.release()
        cv2.destroyAllWindows()

    if state is not None:
        print_state(state)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--image", type=Path, help="analyze one still image")
    source.add_argument("--live", action="store_true", help="read the capture card")
    parser.add_argument("--index", type=int, default=None, help="capture card index")
    parser.add_argument("--no-ocr", action="store_true", help="disable Vision OCR")
    parser.add_argument("--annotate", action="store_true", help="save annotated image")
    parser.add_argument(
        "--duration",
        type=float,
        default=0.0,
        help="live mode: auto-exit after N seconds; 0 means wait for q/ESC",
    )
    args = parser.parse_args()

    reader = StateReader(ocr_enabled=not args.no_ocr)

    if args.image:
        run_image(reader, args.image, use_ocr=not args.no_ocr, annotate=args.annotate)
    else:
        index = args.index
        if index is None:
            index = int(reader.config["capture"]["index"])
        run_live(
            reader,
            index,
            use_ocr=not args.no_ocr,
            annotate=args.annotate,
            duration=args.duration,
        )


if __name__ == "__main__":
    main()
