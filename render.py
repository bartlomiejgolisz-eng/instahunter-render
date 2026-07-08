"""
InstaHunter — renderer karuzel (Format A, brand-aware)
======================================================
W pełni programowy render PNG 1080x1350 (4:5), zero kosztu.
Rdzeń mikro-usługi renderu (patrz app.py + specyfikacje/09-karuzele-build.md).

Wejście: konfiguracja brandu (kolory/akcent/handle/font) + treść slajdów
(z modułu Claude, sparsowana z tokenów) + opcjonalne zdjęcie okładki (Format A).
Wyjście: lista plików PNG (po jednym na slajd).

Zależności: tylko Pillow (brak zależności systemowych cairo -> łatwe do hostowania).
Font produkcyjny: Space Grotesk (Google Fonts). W piaskownicy zastępnik Poppins.
"""

from __future__ import annotations
import os
import textwrap
from dataclasses import dataclass, field
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageOps, ImageEnhance

W, H = 1080, 1350  # kanwa 4:5 Instagram

# ---------- FONTY ----------
# Produkcja: podmienić na Space Grotesk 800 / DM Sans. Piaskownica: Poppins.
FONT_DIR_CANDIDATES = [
    os.path.join(os.path.dirname(__file__), "fonts"),  # bundlowane fonty marki (Space Grotesk) — priorytet
    "/usr/share/fonts/truetype/google-fonts",
    "/usr/share/fonts/truetype/poppins",
]


def _find_font(*names):
    for d in FONT_DIR_CANDIDATES:
        for n in names:
            p = os.path.join(d, n)
            if os.path.exists(p):
                return p
    # awaryjnie DejaVu (zawsze jest)
    return "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


FONT_BOLD = _find_font("SpaceGrotesk-Bold.ttf", "Poppins-Bold.ttf")
FONT_HEAVY = _find_font("SpaceGrotesk-Bold.ttf", "Poppins-Bold.ttf")
FONT_MED = _find_font("SpaceGrotesk-Medium.ttf", "Poppins-Medium.ttf")
FONT_BODY = _find_font("SpaceGrotesk-Regular.ttf", "DMSans-Regular.ttf", "Poppins-Regular.ttf")
FONT_LIGHT = _find_font("SpaceGrotesk-Light.ttf", "SpaceGrotesk-Regular.ttf", "Poppins-Light.ttf")


def _f(path, size):
    return ImageFont.truetype(path, size)


# ---------- BRAND ----------
@dataclass
class Brand:
    """Zmienne z brandbooka klienta (mapowane z Profilu w Airtable)."""
    bg: str = "#111008"          # tło hero/sales (ciepła czerń)
    bg_alt: str = "#F5EFE2"      # tło edukacyjne (krem) — niewykorzystane w Format A dark
    accent: str = "#E8402A"      # koral (akcent, max 20%)
    taupe: str = "#8A7A6A"       # drugi plan / tekst pomocniczy
    white: str = "#FFFFFF"       # tekst na ciemnym + glow
    handle: str = "@bartekaihunter"
    glow: bool = True            # biały outer glow pod tekstem
    ornaments: bool = True       # geometryczne kółka
    font_heavy: str = FONT_HEAVY
    font_bold: str = FONT_BOLD
    font_med: str = FONT_MED
    font_body: str = FONT_BODY


# ---------- POMOCNICZE ----------
def hex2rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _wrap(draw, text, font, max_w):
    """Zawijanie po słowach do zadanej szerokości (px)."""
    words = text.split()
    lines, cur = [], ""
    for wd in words:
        test = (cur + " " + wd).strip()
        if draw.textlength(test, font=font) <= max_w or not cur:
            cur = test
        else:
            lines.append(cur)
            cur = wd
    if cur:
        lines.append(cur)
    return lines


def _draw_glow_text(base, xy, lines, font, fill, line_h, glow_rgb=(255, 255, 255),
                    glow_radius=10, glow_op=110, align_left=True):
    """Rysuje wielolinijkowy tekst z miękką poświatą (glow) pod spodem."""
    x, y = xy
    # warstwa glow
    glow_layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow_layer)
    yy = y
    for ln in lines:
        gd.text((x, yy), ln, font=font, fill=glow_rgb + (glow_op,))
        yy += line_h
    glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(glow_radius))
    base.alpha_composite(glow_layer)
    # ostry tekst
    d = ImageDraw.Draw(base)
    yy = y
    for ln in lines:
        d.text((x, yy), ln, font=font, fill=fill)
        yy += line_h
    return yy


def _ornaments(base, brand):
    """Cienkie geometryczne kółka (tech/AI), niska widoczność."""
    if not brand.ornaments:
        return
    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    acc = hex2rgb(brand.accent)
    tp = hex2rgb(brand.taupe)
    # duże koło prawy-góra (akcent, delikatne)
    d.ellipse([W - 210, 120, W - 210 + 230, 120 + 230], outline=acc + (70,), width=3)
    # małe koło prawy-dół (taupe)
    d.ellipse([W - 150, H - 320, W - 150 + 150, H - 320 + 150], outline=tp + (90,), width=3)
    base.alpha_composite(layer)


def _accent_bar(base, brand):
    """Pionowy pasek akcentu na lewej krawędzi."""
    d = ImageDraw.Draw(base)
    d.rectangle([0, 0, 10, H], fill=hex2rgb(brand.accent))


def _header(base, brand, idx, total):
    d = ImageDraw.Draw(base)
    d.text((72, 62), brand.handle, font=_f(brand.font_med, 30), fill=hex2rgb(brand.taupe))
    pg = f"{idx:02d}/{total:02d}"
    w = d.textlength(pg, font=_f(brand.font_bold, 30))
    d.text((W - 72 - w, 62), pg, font=_f(brand.font_bold, 30), fill=hex2rgb(brand.accent))


def _progress(base, brand, idx, total):
    """Segmentowy pasek postępu na dole; aktywny segment koralowy."""
    d = ImageDraw.Draw(base)
    y = H - 70
    left, right = 72, W - 72
    gap = 14
    seg = (right - left - gap * (total - 1)) / total
    for i in range(total):
        x0 = left + i * (seg + gap)
        col = hex2rgb(brand.accent) if i == idx - 1 else hex2rgb(brand.taupe)
        # nieaktywne ściemnione
        if i != idx - 1:
            col = tuple(int(c * 0.55) for c in col)
        d.rounded_rectangle([x0, y, x0 + seg, y + 6], radius=3, fill=col)


def _count_badge(base, brand, number, x=72, y=250):
    """Koralowy zaokrąglony kwadrat z białą liczbą (okładka listy)."""
    d = ImageDraw.Draw(base)
    s = 150
    d.rounded_rectangle([x, y, x + s, y + s], radius=28, fill=hex2rgb(brand.accent))
    f = _f(brand.font_heavy, 96)
    tw = d.textlength(str(number), font=f)
    bb = f.getbbox(str(number))
    th = bb[3] - bb[1]
    d.text((x + (s - tw) / 2, y + (s - th) / 2 - bb[1]), str(number),
           font=f, fill=hex2rgb(brand.white))


def _big_numeral(base, brand, number, x=72, y=200):
    """Duża koralowa cyfra (slajdy treściowe)."""
    d = ImageDraw.Draw(base)
    d.text((x, y), str(number), font=_f(brand.font_heavy, 190), fill=hex2rgb(brand.accent))


# ---------- KADROWANIE ZDJĘĆ (Format A / B / C) ----------
def _cover_crop(img, w, h):
    return ImageOps.fit(img, (w, h), method=Image.LANCZOS, centering=(0.5, 0.42))


def _warm_grade(img, brand):
    """Ciepły grading + lekkie przyciemnienie pod brand."""
    img = ImageEnhance.Color(img).enhance(1.05)
    img = ImageEnhance.Contrast(img).enhance(1.04)
    img = ImageEnhance.Brightness(img).enhance(0.92)
    # ciepła warstwa mnożąca
    warm = Image.new("RGB", img.size, (46, 30, 18))
    img = Image.blend(img, Image.composite(img, warm, Image.new("L", img.size, 235)), 0.0)
    return img


def _bottom_scrim(base, brand, frac=0.6):
    """Gradient od dołu (ciemny) pod tekst na okładce foto."""
    grad = Image.new("L", (1, H), 0)
    for y in range(H):
        t = max(0.0, (y - H * (1 - frac)) / (H * frac))
        grad.putpixel((0, y), int(255 * min(1.0, t ** 1.4)))
    grad = grad.resize((W, H))
    bgcol = hex2rgb(brand.bg)
    scrim = Image.new("RGBA", (W, H))
    scrim.putalpha(grad)
    solid = Image.new("RGBA", (W, H), bgcol + (255,))
    solid.putalpha(grad)
    base.alpha_composite(solid)


# ---------- SLAJDY ----------
def render_cover(brand, title, subtitle, tagline, idx, total, count=None, photo=None):
    """Okładka (slajd 1). Format A = ze zdjęciem; bez photo = czysto tekstowa."""
    base = Image.new("RGBA", (W, H), hex2rgb(brand.bg) + (255,))
    if photo is not None:
        ph = _cover_crop(_warm_grade(photo.convert("RGB"), brand), W, H)
        base.paste(ph, (0, 0))
        base = base.convert("RGBA")
        _bottom_scrim(base, brand, frac=0.62)
    _accent_bar(base, brand)
    _ornaments(base, brand)
    _header(base, brand, idx, total)
    d = ImageDraw.Draw(base)

    if photo is not None:
        # tekst nisko na scrim
        y = 815
        tf = _f(brand.font_heavy, 82)
        lines = _wrap(d, title, tf, W - 150)
        y = _draw_glow_text(base, (72, y), lines, tf, hex2rgb(brand.white),
                            line_h=90, glow_radius=12, glow_op=120)
        y += 18
        sf = _f(brand.font_heavy, 66)
        slines = _wrap(d, subtitle, sf, W - 150)
        for ln in slines:
            d.text((72, y), ln, font=sf, fill=hex2rgb(brand.accent))
            y += 74
    else:
        yb = 250
        if count is not None:
            _count_badge(base, brand, count, y=yb)
            ty = yb + 210
        else:
            ty = 430
        tf = _f(brand.font_heavy, 86)
        lines = _wrap(d, title, tf, W - 150)
        ty = _draw_glow_text(base, (72, ty), lines, tf, hex2rgb(brand.white),
                            line_h=96, glow_radius=12, glow_op=120)
        ty += 22
        sf = _f(brand.font_heavy, 72)
        slines = _wrap(d, subtitle, sf, W - 150)
        for ln in slines:
            d.text((72, ty), ln, font=sf, fill=hex2rgb(brand.accent))
            ty += 82
        if tagline:
            ty += 40
            gf = _f(brand.font_med, 40)
            for ln in _wrap(d, tagline, gf, W - 200):
                d.text((72, ty), ln, font=gf, fill=hex2rgb(brand.taupe))
                ty += 52
    _progress(base, brand, idx, total)
    return base.convert("RGB")


def render_content(brand, number, heading, body, idx, total):
    """Slajd treściowy: duża koralowa cyfra + białe nagłówek + taupe treść."""
    base = Image.new("RGBA", (W, H), hex2rgb(brand.bg) + (255,))
    _accent_bar(base, brand)
    _ornaments(base, brand)
    _header(base, brand, idx, total)
    d = ImageDraw.Draw(base)
    if number is not None:
        _big_numeral(base, brand, number, y=195)
    # nagłówek (biały, glow)
    hf = _f(brand.font_heavy, 78)
    hy = 480
    hlines = _wrap(d, heading, hf, W - 150)
    hy = _draw_glow_text(base, (72, hy), hlines, hf, hex2rgb(brand.white),
                        line_h=86, glow_radius=11, glow_op=115)
    # treść (taupe)
    bf = _f(brand.font_med, 46)
    by = max(hy + 120, 880)
    for ln in _wrap(d, body, bf, W - 170):
        d.text((72, by), ln, font=bf, fill=hex2rgb(brand.taupe))
        by += 60
    _progress(base, brand, idx, total)
    return base.convert("RGB")


def render_cta(brand, heading, body, cta, idx, total, photo=None):
    """Slajd CTA (ostatni)."""
    base = Image.new("RGBA", (W, H), hex2rgb(brand.bg) + (255,))
    _accent_bar(base, brand)
    _ornaments(base, brand)
    _header(base, brand, idx, total)
    d = ImageDraw.Draw(base)
    hf = _f(brand.font_heavy, 78)
    hy = 360
    hy = _draw_glow_text(base, (72, hy), _wrap(d, heading, hf, W - 150), hf,
                        hex2rgb(brand.white), line_h=86, glow_radius=11, glow_op=115)
    hy += 20
    bf = _f(brand.font_med, 46)
    for ln in _wrap(d, body, bf, W - 170):
        d.text((72, hy), ln, font=bf, fill=hex2rgb(brand.taupe))
        hy += 60
    # przycisk CTA (koralowy pasek z tekstem)
    hy += 60
    cf = _f(brand.font_bold, 48)
    tw = d.textlength(cta, font=cf)
    pad = 40
    d.rounded_rectangle([72, hy, 72 + tw + pad * 2, hy + 96], radius=20,
                        fill=hex2rgb(brand.accent))
    d.text((72 + pad, hy + 20), cta, font=cf, fill=hex2rgb(brand.white))
    _progress(base, brand, idx, total)
    return base.convert("RGB")


# ---------- ORKIESTRACJA ----------
def render_carousel(brand, slides, out_dir, photos=None):
    """
    slides: lista dictów. Każdy:
      {"type":"cover","title","subtitle","tagline","count"(opc)}
      {"type":"content","number","heading","body"}
      {"type":"cta","heading","body","cta"}
    photos: lista ścieżek/obiektów Image do rotacji (Format A: pierwsze na okładkę).
    Zwraca: lista ścieżek PNG.
    """
    os.makedirs(out_dir, exist_ok=True)
    total = len(slides)
    paths = []
    cover_photo = None
    if photos:
        p = photos[0]
        cover_photo = Image.open(p) if isinstance(p, str) else p
    for i, s in enumerate(slides, start=1):
        t = s.get("type")
        if t == "cover":
            img = render_cover(brand, s.get("title", ""), s.get("subtitle", ""),
                               s.get("tagline", ""), i, total,
                               count=s.get("count"), photo=cover_photo)
        elif t == "cta":
            img = render_cta(brand, s.get("heading", ""), s.get("body", ""),
                             s.get("cta", ""), i, total)
        else:
            img = render_content(brand, s.get("number"), s.get("heading", ""),
                                 s.get("body", ""), i, total)
        fp = os.path.join(out_dir, f"slide_{i:02d}.png")
        img.save(fp, "PNG")
        paths.append(fp)
    return paths


def contact_sheet(paths, out_path, cols=4, thumb_w=360):
    imgs = [Image.open(p) for p in paths]
    th = int(thumb_w * H / W)
    rows = (len(imgs) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * thumb_w + (cols + 1) * 20,
                              rows * th + (rows + 1) * 20), (24, 22, 18))
    for i, im in enumerate(imgs):
        r, c = divmod(i, cols)
        t = im.resize((thumb_w, th), Image.LANCZOS)
        sheet.paste(t, (20 + c * (thumb_w + 20), 20 + r * (th + 20)))
    sheet.save(out_path, "PNG")
    return out_path
