#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
IMAGES = ROOT / "data" / "images"


def draw_image(path: Path, size: tuple[int, int], color: str, label: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", size, color)
    draw = ImageDraw.Draw(image)
    draw.rectangle((8, 8, size[0] - 9, size[1] - 9), outline="white", width=4)
    draw.text((16, 16), label, fill="white")
    image.save(path)


def main() -> None:
    samples = [
        (IMAGES / "desktop" / "sample-wide.jpg", (640, 360), "#1d4ed8", "desktop jpg"),
        (IMAGES / "desktop" / "sample-wide.png", (480, 270), "#0f766e", "desktop png"),
        (IMAGES / "mobile" / "sample-tall.webp", (360, 640), "#b45309", "mobile webp"),
        (IMAGES / "mobile" / "sample-tall.jpeg", (270, 480), "#7c3aed", "mobile jpeg"),
        (IMAGES / "square.png", (320, 320), "#be123c", "square png"),
    ]
    for path, size, color, label in samples:
        draw_image(path, size, color, label)
        print(path)


if __name__ == "__main__":
    main()
