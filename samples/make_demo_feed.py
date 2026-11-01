"""Builds the Mock Device Kit feed for `-demo`: a drawn 96-well plate and a moving tip, no real footage.
Mock Device Kit takes H.265 only. Run: .venv/bin/python samples/make_demo_feed.py
"""

import math
import subprocess
from pathlib import Path

import cv2
import numpy as np

W, H, FPS, SECONDS = 720, 1280, 15, 8
OUT = (Path(__file__).resolve().parent.parent / "ios" / "Meta-Global-AI-Developer-Hackathon"
       / "Meta-Global-AI-Developer-Hackathon" / "DemoFeed.mov")
ORIGIN, PITCH = (150, 430), 37


def well(row: int, col: int) -> tuple[int, int]:
    return ORIGIN[0] + col * PITCH, ORIGIN[1] + row * PITCH


def base() -> np.ndarray:
    img = np.full((H, W, 3), (72, 64, 58), np.uint8)
    cv2.rectangle(img, (100, 385), (620, 745), (235, 235, 230), -1)
    for r in range(8):
        for c in range(12):
            cv2.circle(img, well(r, c), 13, (190, 185, 178), -1)
    return img


def frame(plate: np.ndarray, t: float) -> np.ndarray:
    img = plate.copy()
    col = 1 + (math.sin(t * math.tau / SECONDS) + 1)  # back and forth over C2 to C4
    x, y = ORIGIN[0] + int(col * PITCH), well(2, 1)[1]
    cv2.line(img, (x, y), (x + 150, y - 420), (245, 245, 245), 26)
    cv2.circle(img, (x, y), 5, (40, 40, 210), -1)
    return img


plate = base()
ffmpeg = subprocess.Popen(
    ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{W}x{H}", "-r", str(FPS),
     "-i", "-", "-map_metadata", "-1", "-c:v", "libx265", "-preset", "medium", "-crf", "30", "-tag:v", "hvc1",
     "-pix_fmt", "yuv420p", "-x265-params", "log-level=error", str(OUT)], stdin=subprocess.PIPE)
for i in range(FPS * SECONDS):
    ffmpeg.stdin.write(frame(plate, i / FPS).tobytes())
ffmpeg.stdin.close()
assert ffmpeg.wait() == 0
print(OUT, OUT.stat().st_size)
