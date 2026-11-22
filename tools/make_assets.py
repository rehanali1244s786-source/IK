"""Generates the logo, icon, splash screen and installer pictures (needs: pip install pillow)."""
import os
from PIL import Image, ImageDraw, ImageFont
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "assets")
os.makedirs(OUT, exist_ok=True)
BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
REG = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
if os.name == "nt":
    BOLD, REG = "C:/Windows/Fonts/segoeuib.ttf", "C:/Windows/Fonts/segoeui.ttf"
def font(p, s):
    try: return ImageFont.truetype(p, s)
    except OSError: return ImageFont.load_default()
NAVY, NAVY2, GREEN, GREEN2, WHITE = (15, 23, 42), (30, 58, 138), (34, 197, 94), (22, 163, 74), (255, 255, 255)

def gradient(w, h, c1, c2, vertical=True):
    g = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(g)
    n = h if vertical else w
    for i in range(n):
        t = i / max(1, n - 1)
        c = tuple(int(c1[k] + (c2[k] - c1[k]) * t) for k in range(3))
        d.line([(0, i), (w, i)] if vertical else [(i, 0), (i, h)], fill=c)
    return g

def logo(size=1024):
    """Rounded square, blue gradient, white arrow dropping into a green tray."""
    im = gradient(size, size, NAVY2, NAVY).convert("RGBA")
    m = Image.new("L", (size, size), 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, size - 1, size - 1], radius=size // 5, fill=255)
    im.putalpha(m)
    d = ImageDraw.Draw(im)
    s = size / 100
    # arrow
    d.rounded_rectangle([42 * s, 16 * s, 58 * s, 52 * s], radius=3 * s, fill=WHITE)
    d.polygon([(26 * s, 48 * s), (74 * s, 48 * s), (50 * s, 74 * s)], fill=WHITE)
    # green tray
    d.rounded_rectangle([20 * s, 78 * s, 80 * s, 88 * s], radius=4 * s, fill=GREEN)
    d.rounded_rectangle([20 * s, 70 * s, 28 * s, 88 * s], radius=3 * s, fill=GREEN)
    d.rounded_rectangle([72 * s, 70 * s, 80 * s, 88 * s], radius=3 * s, fill=GREEN)
    return im

L = logo()
L.resize((256, 256), Image.LANCZOS).save(os.path.join(OUT, "logo.png"))
L.save(os.path.join(OUT, "icon.ico"), sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])

# ---- splash 560x320 ----
W, H, k = 560, 320, 2
sp = gradient(W * k, H * k, NAVY, (17, 40, 100), vertical=False)
d = ImageDraw.Draw(sp)
sp.paste(L.resize((150 * k, 150 * k), Image.LANCZOS), (40 * k, 60 * k), L.resize((150 * k, 150 * k), Image.LANCZOS))
d.text((215 * k, 85 * k), "IK", font=font(BOLD, 40 * k), fill=GREEN)
d.text((215 * k, 130 * k), "DOWNLOADER", font=font(BOLD, 38 * k), fill=WHITE)
d.text((217 * k, 188 * k), "Fast multi-connection download manager", font=font(REG, 15 * k), fill=(190, 205, 230))
d.rectangle([0, (H - 8) * k, W * k, H * k], fill=(10, 16, 32))
d.rectangle([0, (H - 8) * k, int(W * k * .34), H * k], fill=GREEN)
d.text((40 * k, (H - 38) * k), "Loading...", font=font(REG, 12 * k), fill=(150, 165, 195))
sp.resize((W, H), Image.LANCZOS).save(os.path.join(OUT, "splash.png"))

# ---- installer pictures (bmp) ----
def wizard(w, h):
    k = 2
    im = gradient(w * k, h * k, NAVY2, NAVY)
    d = ImageDraw.Draw(im)
    lg = L.resize((96 * k, 96 * k), Image.LANCZOS)
    im.paste(lg, ((w - 96) // 2 * k, 40 * k), lg)
    d.text((w // 2 * k, 170 * k), "IK", font=font(BOLD, 26 * k), fill=GREEN, anchor="mm")
    d.text((w // 2 * k, 200 * k), "DOWNLOADER", font=font(BOLD, 15 * k), fill=WHITE, anchor="mm")
    d.rectangle([20 * k, (h - 60) * k, (w - 20) * k, (h - 57) * k], fill=GREEN)
    d.text((w // 2 * k, (h - 36) * k), "Version 2.4", font=font(REG, 11 * k), fill=(190, 205, 230), anchor="mm")
    return im.resize((w, h), Image.LANCZOS)
wizard(164, 314).save(os.path.join(OUT, "wizard.bmp"))
small = Image.new("RGB", (55, 58), (255, 255, 255))
lg = L.resize((50, 50), Image.LANCZOS); small.paste(lg, (2, 4), lg)
small.save(os.path.join(OUT, "wizard_small.bmp"))
print("assets written to", os.path.abspath(OUT))
