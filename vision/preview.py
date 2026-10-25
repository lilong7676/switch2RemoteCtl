#!/usr/bin/env python3
"""Live OpenCV preview window for the USB capture card.

This process owns the UVC device. Do not run OBS (or another capture app)
at the same time, otherwise macOS will make them compete for the device.

Keys:
    q / ESC  quit
    s        save a timestamped snapshot next to this script
"""

import argparse
import time
from datetime import datetime
from pathlib import Path

import cv2


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", type=int, default=0, help="AVFoundation camera index")
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--fps", type=float, default=60.0)
    parser.add_argument(
        "--scale",
        type=float,
        default=0.6667,
        help="display scale; capture stays at full resolution (default: 0.6667)",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=0.0,
        help="auto-exit after N seconds; 0 means wait for q/ESC",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    output_dir = Path(__file__).resolve().parent

    cap = cv2.VideoCapture(args.index, cv2.CAP_AVFOUNDATION)
    if not cap.isOpened():
        raise SystemExit(
            f"cannot open AVFoundation camera index {args.index}; "
            "check Camera permission, close OBS/Zoom/QuickTime, and try another index"
        )

    cap.set(cv2.CAP_PROP_FRAME_WIDTH, args.width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, args.height)
    cap.set(cv2.CAP_PROP_FPS, args.fps)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or args.width
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or args.height
    display_size = (max(1, int(width * args.scale)), max(1, int(height * args.scale)))

    title = "Switch 2 Capture - q/ESC quit, s snapshot"
    cv2.namedWindow(title, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(title, *display_size)

    print(f"opened index {args.index}: {width}x{height} @ {cap.get(cv2.CAP_PROP_FPS):.2f} fps")
    print(f"display: {display_size[0]}x{display_size[1]}")
    print("keys: q/ESC quit, s save snapshot")

    frames = 0
    started = time.perf_counter()
    fps = 0.0
    deadline = started + args.duration if args.duration > 0 else None

    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                print("read failed")
                break

            frames += 1
            now = time.perf_counter()
            if now - started >= 0.5:
                fps = frames / (now - started)
                frames = 0
                started = now

            display = frame
            if args.scale != 1.0:
                display = cv2.resize(frame, display_size, interpolation=cv2.INTER_AREA)

            text = f"{width}x{height}  {fps:5.1f} fps"
            cv2.rectangle(display, (8, 8), (300, 40), (0, 0, 0), -1)
            cv2.putText(
                display,
                text,
                (16, 32),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.7,
                (0, 255, 0),
                2,
                cv2.LINE_AA,
            )

            cv2.imshow(title, display)
            key = cv2.waitKey(1) & 0xFF
            if key in (27, ord("q")):
                break
            if key == ord("s"):
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                path = output_dir / f"snapshot_{stamp}.png"
                cv2.imwrite(str(path), frame)
                print(f"saved {path}")

            if deadline is not None and now >= deadline:
                break

            if cv2.getWindowProperty(title, cv2.WND_PROP_VISIBLE) < 1:
                break
    finally:
        cap.release()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
