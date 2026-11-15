"""Draw weekmenu/icon.png (128x128) and weekmenu/logo.png (250x100): a plate with a fork.

Run from weekmenu/ with Pillow installed (requirements-dev.txt):
    python tools/make_icons.py
No Albert Heijn branding. Drawn at 4x and scaled down for smooth edges.
"""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

GREEN = (47, 107, 58, 255)  # #2f6b3a
WHITE = (255, 255, 255, 255)
SCALE = 4
OUT = Path(__file__).resolve().parent.parent


def plate(d, cx, cy, r):
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=WHITE)
    ri = r * 0.72
    d.ellipse([cx - ri, cy - ri, cx + ri, cy + ri], outline=GREEN, width=max(2, int(r * 0.06)))


def fork(d, x, top, height, w):
    """A fork with three tines, stem centered on x."""
    tines_h = height * 0.42
    gap = w * 0.9
    tw = w * 0.34
    for i in (-1, 0, 1):
        tx = x + i * gap
        d.rounded_rectangle([tx - tw / 2, top, tx + tw / 2, top + tines_h], radius=tw / 2, fill=GREEN)
    d.rounded_rectangle([x - gap - tw / 2, top + tines_h * 0.6, x + gap + tw / 2, top + tines_h + tw],
                        radius=tw, fill=GREEN)
    d.rounded_rectangle([x - tw * 0.8, top + tines_h, x + tw * 0.8, top + height], radius=tw * 0.8, fill=GREEN)


def icon(size):
    s = size * SCALE
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, s - 1, s - 1], radius=s * 0.22, fill=GREEN)
    plate(d, s / 2, s / 2, s * 0.40)
    fork(d, s / 2, s * 0.27, s * 0.46, s * 0.07)
    return img.resize((size, size), Image.LANCZOS)


def logo(w, h):
    s = SCALE
    img = Image.new("RGBA", (w * s, h * s), (0, 0, 0, 0))
    mark = icon(h - 16).resize(((h - 16) * s, (h - 16) * s), Image.LANCZOS)
    img.alpha_composite(mark, (8 * s, 8 * s))
    d = ImageDraw.Draw(img)
    font = None
    for name in ("arialbd.ttf", "DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf"):
        try:
            font = ImageFont.truetype(name, 22 * s)
            break
        except OSError:
            continue
    font = font or ImageFont.load_default()
    d.text(((h + 4) * s, (h / 2) * s), "Weekmenu", font=font, fill=GREEN, anchor="lm")
    return img.resize((w, h), Image.LANCZOS)


if __name__ == "__main__":
    icon(128).save(OUT / "icon.png", optimize=True)
    logo(250, 100).save(OUT / "logo.png", optimize=True)
    print("icon.png and logo.png written")
