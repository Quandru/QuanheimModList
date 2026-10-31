"""Images for the Discord post (see docs/discord-layout.md).

- banner(...)       the header image, made for each patch (date, mod count, Updated / New / Removed counts)
- server_banner()   the dark red "Server Update" image, when the server's Valheim version changes
- make_static()     regenerates tools/assets/rune-rule.png and spacer.png; run by hand only if they change:
                        python3 tools/images.py static

Needs Pillow (`pip install pillow`). Fonts come from Google Fonts on first use and are cached in .patchnotes/fonts.
"""
import os, re, sys, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.join(HERE, "assets")
FONT_CACHE = os.path.join(os.getcwd(), ".patchnotes", "fonts")
FONTS = {  # name: (Google Fonts css2 family spec, weight)
    "cinzel": "Cinzel:wght@700",
    "sans": "Noto+Sans:wght@400",
    "runic": "Noto+Sans+Runic",
}
RUNES = "ᛩᚢᚪᚾᚻᛖᛁᛗ"   # QUANHEIM in Anglo-Saxon runes

W = 1080                # 2x the width Discord shows (about 540 px), so images stay sharp
BG, TEXT, MUTED, ZERO = "#16191d", "#ece3cf", "#a9a291", "#5a5850"
RED_BG, RED_MUTED = "#3a1214", "#c9a9a0"
BRONZE, FRAME, EMBER = (199, 154, 75), (150, 116, 58), (224, 129, 47)


def font(name, size):
    from PIL import ImageFont
    os.makedirs(FONT_CACHE, exist_ok=True)
    path = os.path.join(FONT_CACHE, name + ".ttf")
    if not os.path.exists(path):
        req = urllib.request.Request(f"https://fonts.googleapis.com/css2?family={FONTS[name]}",
                                     headers={"User-Agent": "Mozilla/4.0"})   # old UA gets plain .ttf links
        css = urllib.request.urlopen(req, timeout=30).read().decode()
        url = re.search(r"url\((https://fonts\.gstatic\.com/[^)]+\.ttf)\)", css).group(1)
        with urllib.request.urlopen(url, timeout=30) as r, open(path, "wb") as f:
            f.write(r.read())
    return ImageFont.truetype(path, size)


def _frame(bg, name_line=True):
    """A banner-sized canvas: transparent outside a rounded frame on the very edge, and (optionally) the QUANHEIM line."""
    from PIL import Image, ImageDraw
    h = 300
    img = Image.new("RGBA", (W, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, W - 1, h - 1], radius=16, fill=bg, outline=FRAME, width=6)
    if not name_line:
        return img, d
    x, y = 56, 58
    rune, cap = font("runic", 24), font("cinzel", 24)
    label = "Q U A N H E I M"
    d.text((x, y), "ᛩ", font=rune, fill=BRONZE, anchor="lm")
    d.text((x + 34, y), label, font=cap, fill=BRONZE, anchor="lm")
    d.text((x + 46 + d.textlength(label, font=cap), y), "ᛩ", font=rune, fill=BRONZE, anchor="lm")
    return img, d


def banner(path, date, total, updated, new, removed, title="Update Notes"):
    """Header image for a mod update."""
    img, d = _frame(BG)
    x = 56
    d.text((x, 128), title, font=font("cinzel", 60), fill=TEXT, anchor="lm")
    d.text((x, 200), f"{date} · {total} mods", font=font("sans", 26), fill=MUTED, anchor="lm")
    cx = W - 70
    for val, lab in reversed([(updated, "UPDATED"), (new, "NEW"), (removed, "REMOVED")]):
        colw = max(d.textlength(lab, font=font("sans", 20)), 60)
        centre = cx - colw / 2
        d.text((centre, 128), str(val), font=font("cinzel", 60), fill=EMBER if val else ZERO, anchor="mm")
        d.text((centre, 182), lab, font=font("sans", 20), fill=MUTED, anchor="mm")
        cx -= colw + 36
    img.save(path)
    return path


def server_banner(path, version, previous=None, name_line=True):
    """Dark red image for a change in the server's Valheim version: same size, frame and title style as the header.
    name_line=False drops the small QUANHEIM line (used under the Update Notes header, which already has it) and
    centres the rest."""
    img, d = _frame(RED_BG, name_line)
    x, y = 56, (128 if name_line else 100)
    d.text((x, y), "Server Update", font=font("cinzel", 60), fill=TEXT, anchor="lm")
    was = f" (was {previous})" if previous else ""
    d.text((x, y + 68), f"The server has been updated to Valheim {version}{was}", font=font("cinzel", 26),
           fill=BRONZE, anchor="lm")
    d.text((x, y + 112), "Update Valheim in Steam before you join.", font=font("sans", 24), fill=RED_MUTED, anchor="lm")
    img.save(path)
    return path


def make_static():
    """The two fixed images, committed in tools/assets/."""
    from PIL import Image, ImageDraw
    os.makedirs(ASSETS, exist_ok=True)
    Image.new("RGBA", (W, 1), (0, 0, 0, 0)).save(os.path.join(ASSETS, "spacer.png"))
    h = 80   # the line sits in the middle; the rest is transparent breathing room
    img = Image.new("RGBA", (W, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    gold = BRONZE + (255,)
    f = font("runic", 28)
    mid, gap = W // 2, d.textlength(RUNES, font=f) / 2 + 22
    d.line([(0, h // 2), (mid - gap, h // 2)], fill=gold, width=3)
    d.line([(mid + gap, h // 2), (W - 1, h // 2)], fill=gold, width=3)
    d.text((mid, h // 2), RUNES, font=f, fill=gold, anchor="mm")
    img.save(os.path.join(ASSETS, "rune-rule.png"))


if __name__ == "__main__" and sys.argv[1:] == ["static"]:
    make_static()
    print("wrote", ASSETS)
