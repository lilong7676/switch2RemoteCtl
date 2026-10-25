#!/usr/bin/env python3
"""Headless capture-card smoke test: resolution, FPS, and a reference frame.

The capture card is exposed by macOS as an AVFoundation camera. This script
does not call cv2.imshow(), so it cannot get stuck on a GUI window.
"""

import argparse
import time
from pathlib import Path

import cv2


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--index",
        type=int,
        default=0,
        help="AVFoundation camera index (default: 0)",
    )
    parser.add_argument(
        "--frames",
        type=int,
        default=300,
        help="number of frames to read (default: 300)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).resolve().parent / "reference.png",
        help="reference frame output path",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    cap = cv2.VideoCapture(args.index, cv2.CAP_AVFOUNDATION)
    if not cap.isOpened():
        raise SystemExit(
            f"cannot open AVFoundation camera index {args.index}; "
            "check macOS Camera permission, try another index, "
            "use a USB 3 port, and close OBS/Zoom/QuickTime"
        )

    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1920)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 1080)
    cap.set(cv2.CAP_PROP_FPS, 60)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

    print(
        "reported:",
        int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
        "x",
        int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
        "@",
        cap.get(cv2.CAP_PROP_FPS),
        "fps",
    )

    frames = 0
    last_frame = None
    started = time.perf_counter()

    for _ in range(args.frames):
        ok, frame = cap.read()
        if not ok:
            print(f"read failed at frame {frames + 1}")
            break
        frames += 1
        last_frame = frame

    elapsed = time.perf_counter() - started
    cap.release()

    if frames == 0 or last_frame is None:
        raise SystemExit("no frames captured")

    actual_fps = frames / elapsed if elapsed > 0 else 0.0
    print(f"frames: {frames}")
    print(f"elapsed: {elapsed:.3f} s")
    print(f"actual fps: {actual_fps:.1f}")
    print(f"actual resolution: {last_frame.shape[1]}x{last_frame.shape[0]}")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(args.output), last_frame):
        raise SystemExit(f"failed to write {args.output}")

    print(f"saved: {args.output}")
    print(f"mean: {last_frame.mean():.2f}")
    print(f"std: {last_frame.std():.2f}")

    if actual_fps < 55:
        print("WARNING: measured FPS is below 55; do not report this as 60 fps")
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
