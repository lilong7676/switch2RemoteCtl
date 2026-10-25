#!/usr/bin/env python3
"""Supervised walk: bedroom -> green mat (warehouse entrance).

Safety model
------------
* Default is DRY RUN. No controller input is sent unless --arm is given.
* Before arming, the game world must be positively identified (world_ok), which
  requires the in-game HUD date line to be readable.
* The walk stops the moment "A 查看仓库" shows up, and aborts on any mode that is
  not WORLD (HOME / MODAL / WAREHOUSE / BACKPACK / UNKNOWN).
* It never presses A. Reaching the mat only brings up the prompt.
* Every abort calls stop_all() and saves the frame that caused it (clean copy
  plus annotated copy).

Servo
-----
The game camera follows and rotates with the character, so a fixed joystick
direction does not stay valid for long (measured 2026-10-25). In live mode the
walk uses visual servoing instead: find the teal warehouse mat by colour in the
lower part of the frame, steer its horizontal offset toward the screen centre,
and always push "down" (toward the camera) until the prompt appears. Only the
last few known mat centres are reused when the character briefly covers the mat.

Usage
-----
    # offline state-machine test, no hardware, no input
    python vision/walk_to_storage.py --replay vision/samples/walk_to_storage.png \
        vision/samples/storage_prompt.png

    # live dry run through OBS (still no input)
    python vision/walk_to_storage.py --source obs

    # live, actually walking (needs explicit user authorization)
    python vision/walk_to_storage.py --source obs --arm
"""

import argparse
import asyncio
import sys
import time
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from obs_bridge import ObsBridge, default_password  # noqa: E402
from state_reader import StateReader  # noqa: E402

OUT_DIR = BASE_DIR / "logs" / "walk"
OBS_PASSWORD_DEFAULT = default_password()
PI_URL_DEFAULT = "http://192.168.5.19:8000"
NEUTRAL_MACRO = "L_STICK@+000+000 0.05s"

# Teal warehouse mat colour range (HSV), tuned on 2026-10-25 live frames.
# Wide on purpose: warm night lamps shift the mat's hue from ~80 (day) to ~60.
MAT_HUE = (52, 100)
MAT_SAT = (30, 220)
MAT_VAL = (60, 250)
MAT_MIN_AREA_RATIO = 0.0014  # of the full frame, ~2900 px at 1920x1080
MAT_MAX_AREA_RATIO = 0.09  # reject implausibly large blobs
MAT_ASPECT = (0.5, 2.2)  # the mat is roughly square in perspective
MAT_Y_FRAC = 0.42  # ignore the upper 42% of the frame
MAT_JUMP_LIMIT = 600.0  # px; prefer the candidate near the previous centre
ANCHOR_Y_FRAC = 0.58  # where the character's feet sit on screen


class FastState:
    """Cheap per-frame checks: no OCR, suitable for a ~20 Hz loop."""

    def __init__(self, reader):
        self.reader = reader

    def read(self, frame):
        gray = self.reader._clahe.apply(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY))
        mode, confidence = self.reader.detect_mode(frame, gray)
        storage_score, _ = self.reader.match(gray, "prompt_storage", "prompt_search")
        greet_score, _ = self.reader.match(gray, "prompt_greet", "prompt_search")
        return {
            "mode": mode,
            "confidence": confidence,
            "storage_prompt": round(storage_score, 3),
            "greet_prompt": round(greet_score, 3),
            "storage_visible": storage_score >= self.reader.thresholds["prompt"],
        }


def find_mat(frame, min_area=None, prefer_center=None):
    """Locate the teal warehouse mat in the lower part of the frame.

    Colour based on purpose: template matching on a mat that grows and rotates
    with the camera loses the target right when it matters most.
    """
    height, width = frame.shape[:2]
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    hue = hsv[:, :, 0]
    sat = hsv[:, :, 1]
    val = hsv[:, :, 2]
    mask = (
        (hue > MAT_HUE[0])
        & (hue < MAT_HUE[1])
        & (sat > MAT_SAT[0])
        & (sat < MAT_SAT[1])
        & (val > MAT_VAL[0])
        & (val < MAT_VAL[1])
    ).astype(np.uint8)
    mask[: int(height * MAT_Y_FRAC), :] = 0
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((11, 11), np.uint8))

    count, labels, stats, centroids = cv2.connectedComponentsWithStats(mask, 8)
    area_floor = (
        min_area if min_area is not None else MAT_MIN_AREA_RATIO * height * width
    )
    area_ceil = MAT_MAX_AREA_RATIO * height * width
    candidates = []
    for index in range(1, count):
        x, y, box_w, box_h, area = stats[index]
        if area < area_floor or area > area_ceil:
            continue
        aspect = box_w / max(box_h, 1)
        if not (MAT_ASPECT[0] <= aspect <= MAT_ASPECT[1]):
            continue
        candidates.append(
            {
                "center": (float(centroids[index][0]), float(centroids[index][1])),
                "bbox": (int(x), int(y), int(box_w), int(box_h)),
                "area": int(area),
            }
        )
    if not candidates:
        return None
    if prefer_center is not None:
        near = [
            c
            for c in candidates
            if ((c["center"][0] - prefer_center[0]) ** 2
                + (c["center"][1] - prefer_center[1]) ** 2) ** 0.5
            <= MAT_JUMP_LIMIT
        ]
        if near:
            return max(near, key=lambda c: c["area"])
    return max(candidates, key=lambda c: c["area"])


def servo_stick(mat_center, width, height, deadzone_x, deadzone_y):
    """Steer toward the mat in screen space.

    The follow camera means "walk toward the object on screen" is the right rule:
    pushing the stick toward the mat's side of the screen closes the gap, and the
    mat then drifts toward the character. Both axes are needed: the earlier
    version always pushed "down" and walked into the platform edge when the mat
    was actually beside the character.
    """
    anchor_x = width / 2.0
    anchor_y = height * ANCHOR_Y_FRAC
    dx = mat_center[0] - anchor_x
    dy = mat_center[1] - anchor_y
    x_component = 0 if abs(dx) <= deadzone_x else (100 if dx > 0 else -100)
    y_component = 0 if abs(dy) <= deadzone_y else (-100 if dy > 0 else 100)
    return "L_STICK@{:+04d}{:+04d}".format(x_component, y_component), dx, dy


def verify_arrival_prompt(reader, frame):
    """Confirm with OCR that the visible prompt really mentions the warehouse.

    Template matching alone treated "A 查看花瓶" as "A 查看仓库" (0.890), so a
    template hit is only a candidate: the text has to be checked.
    """
    text, _ = reader.ocr_region(frame, "prompt_search", scale=2.0)
    normalized = text.replace(" ", "")
    return ("查看仓库" in normalized or "仓库" in normalized), text


def describe(state, mat=None, lost=0):
    parts = [f"mode={state['mode']}", f"warehouse_prompt={state['storage_prompt']:.3f}"]
    if state["greet_prompt"] > 0.7:
        parts.append(f"greet={state['greet_prompt']:.3f}")
    if mat is not None:
        parts.append(
            f"mat=({mat['center'][0]:.0f},{mat['center'][1]:.0f}) area={mat['area']}"
        )
    elif lost:
        parts.append(f"mat_lost={lost}")
    return " ".join(parts)


def decide(state):
    """Return (action, reason) where action is 'arrived' / 'abort' / 'step'."""
    if state["storage_visible"]:
        return "arrived", "A 查看仓库 visible"
    mode = state["mode"]
    if mode == "WORLD":
        return "step", "world"
    if mode == "HOME":
        return "abort", "switch system menu"
    if mode == "MODAL":
        return "abort", "modal dialog open"
    if mode in ("WAREHOUSE", "BACKPACK"):
        return "abort", f"{mode} UI open"
    return "abort", f"mode {mode}"


def save_snapshot(reader, frame, state, tag):
    """Save the clean frame plus an annotated copy.

    The clean copy matters: annotate() draws text over the HUD area, which would
    poison any later OCR/review of the snapshot.
    """
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    clean = OUT_DIR / f"{stamp}_{tag}.png"
    cv2.imwrite(str(clean), frame)
    cv2.imwrite(
        str(OUT_DIR / f"{stamp}_{tag}_annotated.png"),
        reader.annotate(frame, _as_state_dict(state)),
    )
    return clean


def _as_state_dict(fast):
    return {
        "mode": fast["mode"],
        "confidence": fast["confidence"],
        "hud": None,
        "prompts": {"items": [], "raw_lines": []},
        "inventory": None,
        "item": None,
        "pages": None,
        "world_ok": False,
    }


# ---------------------------------------------------------------- replay mode


def run_replay(args, reader, fast):
    paths = [Path(p) for p in args.replay]
    print(f"replay: {len(paths)} frame(s), dry-run only")
    pulses = 0
    for path in paths:
        frame = cv2.imread(str(path))
        if frame is None:
            raise SystemExit(f"cannot read {path}")
        state = fast.read(frame)
        mat = find_mat(frame, args.mat_min_area)
        action, reason = decide(state)
        print(f"  {path.name:38s} {describe(state, mat):72s} -> {action} ({reason})")
        if action == "arrived":
            verified, prompt_text = verify_arrival_prompt(reader, frame)
            print(f"       prompt OCR = {prompt_text!r} verified={verified}")
            if verified:
                print(f"STOP: arrived after {pulses} planned pulse(s)")
                return 0
            print("       arrival candidate rejected, would keep walking")
            action = "step"
        if action == "abort":
            print(f"ABORT: {reason} (would stop_all + snapshot)")
            return 1
        if args.servo and mat is not None:
            stick, dx, dy = servo_stick(
                mat["center"],
                frame.shape[1],
                frame.shape[0],
                args.mat_deadzone,
                args.mat_deadzone_y,
            )
            print(f"       servo would send {stick} (dx={dx:+.0f} dy={dy:+.0f})")
        else:
            print(f"       dry-run: would send L_STICK@{args.direction}")
        pulses += 1
    print(f"END OF FRAMES: no stop condition reached ({pulses} planned pulse(s))")
    return 2


# ------------------------------------------------------------------ live mode


def connect_controller(url):
    from switch_client import SwitchClient

    client = SwitchClient(url)
    client.connect(wait_timeout=8)
    index = client.wait_connected(timeout=5)
    return client, index


async def run_live(args, reader, fast):
    client = None
    index = None
    if args.arm:
        print(f"ARMED: will send joystick macros to {args.pi_url}")
        client, index = connect_controller(args.pi_url)
        print(f"controller index={index} connected")
    else:
        print("DRY RUN: no input will be sent (pass --arm to walk for real)")

    bridge = ObsBridge(args.obs_url, args.obs_password)
    await bridge.connect()
    source = args.obs_source or await bridge.find_capture_input()
    print(f"obs connected, source={source}")

    pulses = 0
    fallback_pulses = 0
    lost_streak = 0
    last_mat_center = None
    started = time.monotonic()
    try:
        # One full check with OCR: the walk is only allowed in a verified world.
        frame = await bridge.screenshot(source, width=args.width, fmt="png")
        full = reader.analyze(frame)
        print(
            f"initial: mode={full['mode']} world_ok={full['world_ok']} "
            f"date={(full.get('hud') or {}).get('datetime', {}).get('date')}"
        )
        if not full["world_ok"]:
            path = save_snapshot(reader, frame, fast.read(frame), "precheck_not_world")
            print(f"ABORT: world not verified (mode={full['mode']}); snapshot={path}")
            return 1

        while pulses < args.max_pulses and time.monotonic() - started < args.max_seconds:
            frame = await bridge.screenshot(
                source, width=args.width, fmt="jpg", quality=85
            )
            state = fast.read(frame)
            mat = (
                find_mat(frame, args.mat_min_area, last_mat_center)
                if args.servo
                else None
            )
            if mat is not None:
                last_mat_center = mat["center"]
                lost_streak = 0
            else:
                lost_streak += 1

            action, reason = decide(state)
            print(
                f"[{pulses:02d}] {describe(state, mat, lost_streak)} -> {action} ({reason})"
            )

            if action == "arrived":
                verified, prompt_text = verify_arrival_prompt(reader, frame)
                if not verified:
                    print(
                        f"       arrival candidate rejected: prompt OCR = {prompt_text!r}"
                    )
                else:
                    path = save_snapshot(reader, frame, state, "arrived")
                    print(f"STOP: arrived after {pulses} pulse(s); snapshot={path}")
                    print(f"       verified prompt: {prompt_text!r}")
                    print("Not pressing A. Take over and open the warehouse manually.")
                    return 0
            if action == "abort":
                path = save_snapshot(reader, frame, state, "abort")
                print(f"ABORT: {reason}; snapshot={path}")
                return 1

            if not args.servo:
                stick = f"L_STICK@{args.direction}"
                source_label = "fixed direction"
            elif last_mat_center is not None and lost_streak <= args.mat_memory:
                stick, dx, dy = servo_stick(
                    last_mat_center,
                    args.width,
                    args.height,
                    args.mat_deadzone,
                    args.mat_deadzone_y,
                )
                source_label = f"servo dx={dx:+.0f} dy={dy:+.0f}"
            elif last_mat_center is not None:
                path = save_snapshot(reader, frame, state, "mat_lost")
                print(f"ABORT: mat lost for {lost_streak} frames; snapshot={path}")
                return 1
            else:
                fallback_pulses += 1
                if fallback_pulses > args.max_fallback:
                    path = save_snapshot(reader, frame, state, "mat_never_seen")
                    print(
                        f"ABORT: mat not found after {fallback_pulses} fallback pulses; "
                        f"snapshot={path}"
                    )
                    return 1
                stick = f"L_STICK@{args.direction}"
                source_label = (
                    f"fallback direction (mat not seen yet, "
                    f"{fallback_pulses}/{args.max_fallback})"
                )

            print(f"       {source_label}: {stick} {args.pulse:.2f}s")

            if args.arm:
                try:
                    client.macro(f"{stick} {args.pulse:.2f}s", index=index)
                    client.macro(NEUTRAL_MACRO, index=index)
                except Exception as exc:  # socket / macro failure
                    print(f"ABORT: macro failed: {exc}")
                    return 1
            else:
                print("       dry-run: not sending")

            pulses += 1
            await asyncio.sleep(args.settle)

        print(f"ABORT: gave up after {pulses} pulse(s) without seeing the prompt")
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
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--source", choices=["obs"], default="obs")
    source.add_argument(
        "--replay", nargs="+", help="offline frame list; never touches hardware"
    )
    parser.add_argument("--arm", action="store_true", help="actually send joystick macros")
    parser.add_argument("--direction", default="000-100", help="fallback L_STICK payload (straight down)")
    parser.add_argument("--pulse", type=float, default=0.25, help="seconds per joystick pulse")
    parser.add_argument("--settle", type=float, default=0.4, help="seconds between pulses")
    parser.add_argument("--max-pulses", type=int, default=20)
    parser.add_argument("--max-seconds", type=float, default=25.0)
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--pi-url", default=PI_URL_DEFAULT)
    parser.add_argument("--obs-url", default="ws://127.0.0.1:4455")
    parser.add_argument("--obs-password", default=OBS_PASSWORD_DEFAULT)
    parser.add_argument("--obs-source", default=None)
    parser.add_argument("--servo", dest="servo", action="store_true", default=True)
    parser.add_argument("--no-servo", dest="servo", action="store_false")
    parser.add_argument("--mat-deadzone", type=float, default=60.0)
    parser.add_argument("--mat-deadzone-y", type=float, default=45.0)
    parser.add_argument(
        "--mat-memory", type=int, default=2, help="frames to reuse last mat centre"
    )
    parser.add_argument("--mat-min-area", type=int, default=None)
    parser.add_argument(
        "--max-fallback",
        type=int,
        default=3,
        help="max open-loop pulses before the mat must be visible",
    )
    args = parser.parse_args()

    reader = StateReader(ocr_enabled=True)
    fast = FastState(reader)

    if args.replay:
        return run_replay(args, reader, fast)
    return asyncio.run(run_live(args, reader, fast))


if __name__ == "__main__":
    sys.exit(main())
