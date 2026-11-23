"""Draws the app icons into static/icons (run once; the PNGs are committed)."""
from pathlib import Path
from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "static" / "icons"
BLUE, WHITE = (10, 108, 189), (255, 255, 255)
S = 1024


def glyph(draw, cx, cy, scale):
    # Wi-Fi arcs + dot
    for i, r in enumerate((300, 205, 110)):
        r = r * scale
        w = int(52 * scale)
        draw.arc([cx - r, cy - r, cx + r, cy + r], 225, 315, fill=WHITE, width=w)
    d = 40 * scale
    draw.ellipse([cx - d, cy - d, cx + d, cy + d], fill=WHITE)
    # padlock, bottom right
    lx, ly = cx + 170 * scale, cy + 70 * scale
    bw, bh = 190 * scale, 150 * scale
    sh = 70 * scale
    draw.arc([lx - sh, ly - sh - 70 * scale, lx + sh, ly + sh - 70 * scale], 180, 360, fill=WHITE,
             width=int(34 * scale))
    draw.line([lx - sh + 17 * scale, ly - 70 * scale, lx - sh + 17 * scale, ly], fill=WHITE, width=int(34 * scale))
    draw.line([lx + sh - 17 * scale, ly - 70 * scale, lx + sh - 17 * scale, ly], fill=WHITE, width=int(34 * scale))
    draw.rounded_rectangle([lx - bw / 2, ly - 10 * scale, lx + bw / 2, ly + bh], radius=24 * scale, fill=WHITE)
    kr = 22 * scale
    draw.ellipse([lx - kr, ly + 45 * scale - kr, lx + kr, ly + 45 * scale + kr], fill=BLUE)
    draw.rectangle([lx - 9 * scale, ly + 45 * scale, lx + 9 * scale, ly + 105 * scale], fill=BLUE)


def make(rounded: bool, scale: float):
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0) if rounded else BLUE + (255,))
    draw = ImageDraw.Draw(img)
    if rounded:
        draw.rounded_rectangle([0, 0, S - 1, S - 1], radius=220, fill=BLUE)
    glyph(draw, S / 2 - 40 * scale, S / 2 + 110 * scale, scale)
    return img


OUT.mkdir(parents=True, exist_ok=True)
full = make(False, 1.0)       # iOS rounds the corners itself
full.convert("RGB").resize((180, 180), Image.LANCZOS).save(OUT / "apple-touch-icon.png")
rounded = make(True, 1.0)
for size in (192, 512):
    rounded.resize((size, size), Image.LANCZOS).save(OUT / f"icon-{size}.png")
make(False, 0.75).convert("RGB").resize((512, 512), Image.LANCZOS).save(OUT / "icon-maskable-512.png")
print("icons written to", OUT)
