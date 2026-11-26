#!/usr/bin/env python3
"""Draw the sync program's icon: a gold compass in a ring on the window header's navy.

Writes agent/adventuresync/assets/icon.png and icon-16/32/48.png (the window's icon: Tk cannot
read the .ico Pillow writes, whose pictures are stored as PNG) and icon.ico (the Windows
program's, 16 to 256 px, which Windows reads). Drawn at 1024 px and scaled down, so it stays
sharp small.

    python scripts/make-icon.py
"""
import math
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "agent" / "adventuresync" / "assets"
N = 1024
GOLD, GOLD_DARK, GOLD_DEEP = (240, 196, 92), (196, 144, 48), (150, 104, 28)


def draw() -> Image.Image:
    img = Image.new("RGBA", (N, N), (0, 0, 0, 0))
    # navy rounded square, lighter at the top
    back = Image.new("RGBA", (N, N))
    top, bottom = (46, 62, 104), (20, 28, 48)
    for y in range(N):
        t = y / (N - 1)
        back.paste(tuple(round(a + (b - a) * t) for a, b in zip(top, bottom)) + (255,), (0, y, N, y + 1))
    mask = Image.new("L", (N, N), 0)
    ImageDraw.Draw(mask).rounded_rectangle((24, 24, N - 24, N - 24), radius=210, fill=255)
    img.paste(back, (0, 0), mask)
    d = ImageDraw.Draw(img)
    c = N / 2
    # the ring, with tick marks at the four quarters
    r = 352
    d.ellipse((c - r, c - r, c + r, c + r), outline=GOLD_DARK, width=38)
    for k in range(4):
        a = math.pi / 2 * k + math.pi / 4
        x, y = math.cos(a), math.sin(a)
        d.line((c + x * (r - 40), c + y * (r - 40), c + x * (r + 4), c + y * (r + 4)), fill=GOLD_DARK, width=26)

    def star(length: float, inner: float, turn: float, light: tuple, dark: tuple) -> None:
        """A four-pointed star; each point in a light and a dark half, so it looks raised."""
        for k in range(4):
            a = turn + math.pi / 2 * k
            tip = (c + math.cos(a) * length, c + math.sin(a) * length)
            left = (c + math.cos(a - math.pi / 4) * inner, c + math.sin(a - math.pi / 4) * inner)
            right = (c + math.cos(a + math.pi / 4) * inner, c + math.sin(a + math.pi / 4) * inner)
            d.polygon([tip, left, (c, c)], fill=light)
            d.polygon([tip, right, (c, c)], fill=dark)

    star(250, 62, math.pi / 4, GOLD_DARK, GOLD_DEEP)              # the small diagonal points behind
    star(318, 84, -math.pi / 2, GOLD, GOLD_DARK)                  # north, east, south, west in front
    d.ellipse((c - 30, c - 30, c + 30, c + 30), fill=(20, 28, 48))
    d.ellipse((c - 16, c - 16, c + 16, c + 16), fill=GOLD)
    return img


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    big = draw()
    big.resize((256, 256), Image.LANCZOS).save(OUT / "icon.png")
    for s in (16, 32, 48):
        big.resize((s, s), Image.LANCZOS).save(OUT / f"icon-{s}.png")
    sizes = [16, 24, 32, 48, 64, 128, 256]
    frames = [big.resize((s, s), Image.LANCZOS) for s in sizes]
    frames[-1].save(OUT / "icon.ico", sizes=[(s, s) for s in sizes], append_images=frames[:-1])
    print("wrote", OUT / "icon.png", "and", OUT / "icon.ico")


if __name__ == "__main__":
    main()
