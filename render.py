"""
InstaHunter — renderer karuzel (brand-aware, uniwersalny szablon)
=================================================================
W pełni programowy render PNG 1080x1350 (4:5), zero kosztu.
Rdzeń mikro-usługi renderu (patrz app.py + specyfikacje/09-karuzele-build.md).

Cel jakościowy: dopasowanie do zaakceptowanego prototypu
`karuzele-proto-brand/b_01..b_08.png` — czysta (BEZ poświaty) typografia,
dużo oddechu, auto-dopasowanie fontu (tekst NIGDY nie przepełnia kadru),
akcent koloru na pojedynczych słowach, różnorodność form slajdów
(okładka / statement / numerowany / punktowany / CTA-karta).

Wejście: brand (kolory/akcent/handle/font) + treść slajdów (sparsowana z tokenów)
+ opcjonalne zdjęcie okładki. Wyjście: lista plików PNG (po jednym na slajd).

Zależności: tylko Pillow (brak cairo -> łatwe do hostowania).
Font produkcyjny: Space Grotesk (bundlowany w fonts/).
"""

from __future__ import annotations
import os
from dataclasses import dataclass
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageOps, ImageEnhance

W, H = 1080, 1350  # kanwa 4:5 Instagram
MARGIN = 84        # lewy/prawy margines treści
BAR = 12           # szerokość paska akcentu z lewej

# ---------- FONTY ----------
FONT_DIR_CANDIDATES = [
    os.path.join(os.path.dirname(__file__), "fonts"),  # bundlowany Space Grotesk — priorytet
    "/usr/share/fonts/truetype/google-fonts",
    "/usr/share/fonts/truetype/poppins",
]


def _find_font(*names):
    for d in FONT_DIR_CANDIDATES:
        for n in names:
            p = os.path.join(d, n)
            if os.path.exists(p):
                return p
    return "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


FONT_BOLD = _find_font("SpaceGrotesk-Bold.ttf", "Poppins-Bold.ttf")
FONT_HEAVY = _find_font("SpaceGrotesk-Bold.ttf", "Poppins-Bold.ttf")
FONT_MED = _find_font("SpaceGrotesk-Medium.ttf", "Poppins-Medium.ttf")
FONT_BODY = _find_font("SpaceGrotesk-Regular.ttf", "DMSans-Regular.ttf", "Poppins-Regular.ttf")
FONT_LIGHT = _find_font("SpaceGrotesk-Light.ttf", "SpaceGrotesk-Regular.ttf", "Poppins-Light.ttf")


def _font_ok(p):
    try:
        ImageFont.truetype(p, 20)
        return True
    except Exception:
        return False


# Czcionka "zwykła" do stories natywnych (biały boks + tekst jak wpisany w apce IG).
# Preferuj systemowy DejaVu (neutralny, nie-firmowy); fallback Space Grotesk (bundlowany).
_DEJAVU = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_PLAIN = _DEJAVU if _font_ok(_DEJAVU) else FONT_BOLD

_FCACHE = {}


def _f(path, size):
    k = (path, size)
    if k not in _FCACHE:
        _FCACHE[k] = ImageFont.truetype(path, size)
    return _FCACHE[k]


# ---------- BRAND ----------
@dataclass
class Brand:
    """Zmienne z brandbooka klienta (mapowane z Profilu w Airtable)."""
    bg: str = "#111008"          # tło (ciepła czerń)
    bg_alt: str = "#F5EFE2"      # rezerwa (tło jasne) — nieużywane w wariancie dark
    accent: str = "#E8402A"      # koral (akcent, max ~20%)
    taupe: str = "#8A7A6A"       # tekst pomocniczy / drugi plan
    white: str = "#FFFFFF"       # tekst główny
    handle: str = "@bartekaihunter"
    glow: bool = False           # zostawione dla zgodności API; render jest CZYSTY (bez poświaty)
    ornaments: bool = True       # cienkie geometryczne kółka
    font_heavy: str = FONT_HEAVY
    font_bold: str = FONT_BOLD
    font_med: str = FONT_MED
    font_body: str = FONT_BODY


# ---------- KOLORY / TEKST ----------
def hex2rgb(h):
    h = str(h).lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _mix(c, other, t):
    return tuple(int(a * (1 - t) + b * t) for a, b in zip(c, other))


def _parse_rich(text):
    """Zamienia tekst z markerami *akcent* na listę (słowo, is_accent).
    Token będący samą interpunkcją doklejamy do poprzedniego słowa (bez spacji),
    żeby *akcent* tuż przed przecinkiem nie tworzył dziury."""
    words = []
    for i, part in enumerate((text or "").split("*")):
        acc = (i % 2 == 1)
        for w in part.split():
            if words and all(c in ",.;:!?)”\"'…»" for c in w):
                prev_w, prev_acc = words[-1]
                words[-1] = (prev_w + w, prev_acc)
            else:
                words.append((w, acc))
    return words


def _wrap_rich(draw, words, font, max_w=None):
    """Zawija listę (słowo, acc) do szerokości treści; zwraca listę linii."""
    if max_w is None:
        max_w = W - MARGIN - MARGIN
    space = draw.textlength(" ", font=font)
    lines, cur, cur_w = [], [], 0.0
    for w, acc in words:
        ww = draw.textlength(w, font=font)
        add = ww + (space if cur else 0)
        if cur and cur_w + add > max_w:
            lines.append(cur)
            cur, cur_w = [(w, acc)], ww
        else:
            cur.append((w, acc))
            cur_w += add
    if cur:
        lines.append(cur)
    return lines


def _line_w(draw, line, font):
    space = draw.textlength(" ", font=font)
    return sum(draw.textlength(w, font=font) for w, _ in line) + space * max(0, len(line) - 1)


def _fit_rich(draw, text, font_path, size_hi, size_lo, max_lines, max_w=None, step=3):
    """Największy rozmiar, przy którym tekst mieści się w max_lines i szerokości."""
    if max_w is None:
        max_w = W - MARGIN - MARGIN
    words = _parse_rich(text)
    size = size_lo
    lines = _wrap_rich(draw, words, _f(font_path, size_lo), max_w)
    for size in range(size_hi, size_lo - 1, -step):
        font = _f(font_path, size)
        lines = _wrap_rich(draw, words, font, max_w)
        if len(lines) <= max_lines and all(_line_w(draw, ln, font) <= max_w for ln in lines):
            return font, lines, size
    return _f(font_path, size_lo), lines, size_lo


def _draw_rich(base, x, y, lines, font, white, accent, line_h, shadow=False):
    """Rysuje wielolinijkowy tekst z akcentem per-słowo. Czysto (bez poświaty).
    shadow=True dodaje delikatny cień pod tekst (tylko okładka na zdjęciu)."""
    d = ImageDraw.Draw(base)
    space = d.textlength(" ", font=font)
    if shadow:
        sh = Image.new("RGBA", base.size, (0, 0, 0, 0))
        sd = ImageDraw.Draw(sh)
        yy = y
        for line in lines:
            xx = x
            for w, _ in line:
                sd.text((xx + 2, yy + 3), w, font=font, fill=(0, 0, 0, 170))
                xx += sd.textlength(w, font=font) + space
            yy += line_h
        sh = sh.filter(ImageFilter.GaussianBlur(5))
        base.alpha_composite(sh)
    d = ImageDraw.Draw(base)
    yy = y
    for line in lines:
        xx = x
        for w, acc in line:
            d.text((xx, yy), w, font=font, fill=(accent if acc else white))
            xx += d.textlength(w, font=font) + space
        yy += line_h
    return yy


# ---------- ELEMENTY STAŁE ----------
def _accent_bar(base, brand):
    ImageDraw.Draw(base).rectangle([0, 0, BAR, H], fill=hex2rgb(brand.accent))


def _ornaments(base, brand, strong=False):
    """Geometryczne kółka (tech/AI) — widoczne, ale eleganckie (dwa koncentryczne).
    strong=True (okładka na zdjęciu) = wyraźniejsze, żeby nie zlały się z fotografią."""
    if not brand.ornaments:
        return
    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    acc = hex2rgb(brand.accent)
    tp = hex2rgb(brand.taupe)
    if strong:
        a1, a2, a3, w1, w3 = 220, 120, 190, 6, 5
        tp = hex2rgb(brand.white)
    else:
        a1, a2, a3, w1, w3 = 120, 55, 110, 4, 4
    # duże koło prawy-góra (akcent) + mniejsze koncentryczne
    d.ellipse([W - 250, 120, W - 250 + 280, 120 + 280], outline=acc + (a1,), width=w1)
    d.ellipse([W - 205, 165, W - 205 + 190, 165 + 190], outline=acc + (a2,), width=3)
    # koło prawy-dół
    d.ellipse([W - 170, H - 360, W - 170 + 200, H - 360 + 200], outline=tp + (a3,), width=w3)
    base.alpha_composite(layer)


def _vignette(base, brand):
    """Subtelna głębia: delikatne przyciemnienie krawędzi (premium)."""
    v = Image.new("L", (W, H), 0)
    dv = ImageDraw.Draw(v)
    dv.ellipse([-W // 3, -H // 4, W + W // 3, H + H // 4], fill=60)
    v = v.filter(ImageFilter.GaussianBlur(160))
    dark = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    dark.putalpha(ImageOps.invert(v).point(lambda p: int(p * 0.28)))
    base.alpha_composite(dark)


def _top_scrim(base, brand, frac=0.22):
    """Gradient od góry — czytelność górnego paska (handle/numer) na zdjęciu."""
    grad = Image.new("L", (1, H), 0)
    for y in range(H):
        t = max(0.0, 1.0 - y / (H * frac))
        grad.putpixel((0, y), int(238 * (t ** 1.25)))
    grad = grad.resize((W, H))
    solid = Image.new("RGBA", (W, H), hex2rgb(brand.bg) + (255,))
    solid.putalpha(grad)
    base.alpha_composite(solid)


def _draw_tracked(d, xy, text, font, fill, tracking=8):
    x, y = xy
    for ch in text:
        d.text((x, y), ch, font=font, fill=fill)
        x += d.textlength(ch, font=font) + tracking
    return x


def _kicker(base, brand, x, y, text):
    """Mały koralowy nagłówek 'eyebrow' WERSALIKAMI + krótka kreska (editorial)."""
    if not text:
        return y
    d = ImageDraw.Draw(base)
    f = _f(brand.font_bold, 28)
    end = _draw_tracked(d, (x, y), text.upper(), f, hex2rgb(brand.accent), tracking=8)
    cy = y + 15
    d.line([(end + 6, cy), (end + 70, cy)], fill=hex2rgb(brand.accent), width=3)
    return y + 52


def _header(base, brand, idx, total, shadow=False):
    hf = _f(brand.font_med, 30)
    pg = f"{idx:02d}/{total:02d}"
    pf = _f(brand.font_bold, 30)
    px = W - MARGIN - ImageDraw.Draw(base).textlength(pg, font=pf)
    if shadow:  # cień pod tekstem na okładce fotograficznej (czytelność na każdym tle)
        sh = Image.new("RGBA", base.size, (0, 0, 0, 0))
        sd = ImageDraw.Draw(sh)
        sd.text((MARGIN + 1, 62), brand.handle, font=hf, fill=(0, 0, 0, 210))
        sd.text((px + 1, 62), pg, font=pf, fill=(0, 0, 0, 210))
        base.alpha_composite(sh.filter(ImageFilter.GaussianBlur(4)))
    d = ImageDraw.Draw(base)
    handle_col = hex2rgb(brand.white) if shadow else hex2rgb(brand.taupe)
    d.text((MARGIN, 60), brand.handle, font=hf, fill=handle_col)
    d.text((px, 60), pg, font=pf, fill=hex2rgb(brand.accent))


def _progress(base, brand, idx, total):
    d = ImageDraw.Draw(base)
    y = H - 66
    left, right = MARGIN, W - MARGIN
    gap = 14
    seg = (right - left - gap * (total - 1)) / max(1, total)
    for i in range(total):
        x0 = left + i * (seg + gap)
        if i == idx - 1:
            col = hex2rgb(brand.accent)
        else:
            col = _mix(hex2rgb(brand.bg), hex2rgb(brand.taupe), 0.45)
        d.rounded_rectangle([x0, y, x0 + seg, y + 6], radius=3, fill=col)


def _count_badge(base, brand, number, x=MARGIN, y=235, s=150):
    """Koralowy zaokrąglony kwadrat z białą liczbą (okładka listy)."""
    d = ImageDraw.Draw(base)
    d.rounded_rectangle([x, y, x + s, y + s], radius=30, fill=hex2rgb(brand.accent))
    f = _f(brand.font_heavy, int(s * 0.62))
    tw = d.textlength(str(number), font=f)
    bb = f.getbbox(str(number))
    th = bb[3] - bb[1]
    d.text((x + (s - tw) / 2, y + (s - th) / 2 - bb[1]), str(number),
           font=f, fill=hex2rgb(brand.white))
    return y + s


def _big_numeral(base, brand, number, x=MARGIN, y=196, size=168):
    """Duża koralowa cyfra (slajdy numerowane)."""
    d = ImageDraw.Draw(base)
    f = _f(brand.font_heavy, size)
    d.text((x, y), str(number), font=f, fill=hex2rgb(brand.accent))
    bb = d.textbbox((x, y), str(number), font=f)
    return bb[3]


def _check(base, brand, x, y, r=22):
    """Koralowy znacznik listy (kółko z białym ptaszkiem)."""
    d = ImageDraw.Draw(base)
    d.ellipse([x, y, x + 2 * r, y + 2 * r], fill=hex2rgb(brand.accent))
    cx, cy = x + 2 * r * 0.32, y + 2 * r * 0.52
    d.line([(cx, cy), (cx + r * 0.32, cy + r * 0.38)], fill=hex2rgb(brand.white), width=5)
    d.line([(cx + r * 0.32, cy + r * 0.38), (cx + r * 0.9, cy - r * 0.4)],
           fill=hex2rgb(brand.white), width=5)


def _circle(photo, d, center=(0.5, 0.42)):
    """Zdjęcie przycięte do koła (RGBA, przezroczyste tło poza kołem)."""
    im = ImageOps.fit(photo.convert("RGB"), (d, d), method=Image.LANCZOS, centering=center)
    mask = Image.new("L", (d, d), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, d - 1, d - 1], fill=255)
    out = Image.new("RGBA", (d, d), (0, 0, 0, 0))
    out.paste(im, (0, 0), mask)
    return out


def _avatar(base, brand, photo, cx, cy, r, ring_w=6, center=(0.5, 0.42)):
    """Okrągły awatar ze zdjęcia + koralowy pierścień. Element powtarzalny."""
    if photo is None:
        return
    d = 2 * r
    base.alpha_composite(_circle(photo, d, center), (int(cx - r), int(cy - r)))
    ImageDraw.Draw(base).ellipse([cx - r, cy - r, cx + r, cy + r],
                                 outline=hex2rgb(brand.accent), width=ring_w)


# ---------- SIATKA BEZPIECZEŃSTWA OKŁADKI (deterministyczny guard) ----------
COVER_MAX_UPSCALE = 1.35
COVER_MAX_RATIO = 0.95


def orientation_of(w, h):
    if h <= 0 or w <= 0:
        return "nieznane"
    r = w / h
    if r <= 0.95:
        return "pionowe"
    if r < 1.1:
        return "kwadratowe"
    return "poziome"


def cover_photo_ok(img):
    try:
        w, h = img.size
    except Exception:
        return False
    if w <= 0 or h <= 0:
        return False
    if (w / h) > COVER_MAX_RATIO:
        return False
    if max(W / w, H / h) > COVER_MAX_UPSCALE:
        return False
    return True


# ---------- ZDJĘCIE OKŁADKI ----------
def _cover_crop(img, w, h):
    return ImageOps.fit(img, (w, h), method=Image.LANCZOS, centering=(0.5, 0.4))


def _warm_grade(img):
    img = ImageEnhance.Color(img).enhance(1.04)
    img = ImageEnhance.Contrast(img).enhance(1.03)
    img = ImageEnhance.Brightness(img).enhance(0.9)
    return img


def _bottom_scrim(base, brand, frac=0.62):
    grad = Image.new("L", (1, H), 0)
    for y in range(H):
        t = max(0.0, (y - H * (1 - frac)) / (H * frac))
        grad.putpixel((0, y), int(255 * min(1.0, t ** 1.5)))
    grad = grad.resize((W, H))
    solid = Image.new("RGBA", (W, H), hex2rgb(brand.bg) + (255,))
    solid.putalpha(grad)
    base.alpha_composite(solid)


# ---------- SLAJDY ----------
def render_cover(brand, title, subtitle, tagline, idx, total, count=None, photo=None):
    """Okładka. Ze zdjęciem = pełnoklatkowe foto+scrim; bez = tekstowa z badge liczby."""
    base = Image.new("RGBA", (W, H), hex2rgb(brand.bg) + (255,))
    on_photo = photo is not None
    if on_photo:
        ph = _cover_crop(_warm_grade(photo.convert("RGB")), W, H)
        base.paste(ph, (0, 0))
        base = base.convert("RGBA")
        _bottom_scrim(base, brand, frac=0.72)
        _top_scrim(base, brand, frac=0.28)   # czytelność górnego paska na dowolnym zdjęciu
    else:
        _vignette(base, brand)
    _accent_bar(base, brand)
    _ornaments(base, brand, strong=on_photo)
    _header(base, brand, idx, total, shadow=on_photo)
    d = ImageDraw.Draw(base)
    white, accent, taupe = hex2rgb(brand.white), hex2rgb(brand.accent), hex2rgb(brand.taupe)

    if on_photo:
        # HOOK: duży i wyraźny (na 1. slajdzie ma być najmocniejszy)
        tf, tl, _ = _fit_rich(d, title, brand.font_heavy, 104, 74, 3)
        lh = int(tf.size * 1.06)
        sf, sl = None, None
        if subtitle:
            sf, sl, _ = _fit_rich(d, subtitle, brand.font_heavy, 68, 48, 2)
        block_h = lh * len(tl) + (int(sf.size * 1.12) * len(sl) + 18 if subtitle else 0)
        y = H - 165 - block_h
        y = _draw_rich(base, MARGIN, y, tl, tf, white, accent, lh, shadow=True)
        if subtitle:
            _draw_rich(base, MARGIN, y + 18,
                       [[(w, True) for w, _ in ln] for ln in sl], sf, white, accent,
                       int(sf.size * 1.12), shadow=True)
    else:
        y = 235
        if count is not None:
            y = _count_badge(base, brand, count, y=y) + 120
        else:
            y = 430
        tf, tl, _ = _fit_rich(d, title, brand.font_heavy, 96, 60, 3)
        lh = int(tf.size * 1.06)
        y = _draw_rich(base, MARGIN, y, tl, tf, white, accent, lh)
        if subtitle:
            sf, sl, _ = _fit_rich(d, subtitle, brand.font_heavy, 76, 48, 3)
            slh = int(sf.size * 1.1)
            y = _draw_rich(base, MARGIN, y + 26,
                           [[(w, True) for w, _ in ln] for ln in sl], sf, white, accent, slh)
        if tagline:
            gf, gl, _ = _fit_rich(d, tagline, brand.font_med, 42, 32, 2)
            _draw_rich(base, MARGIN, y + 44, gl, gf, taupe, accent, int(gf.size * 1.28))
    _progress(base, brand, idx, total)
    return base.convert("RGB")


def render_content(brand, number, heading, body, idx, total, avatar=None, kicker=None):
    """Slajd treściowy: eyebrow (lub duża cyfra) + czysty biały nagłówek + taupe treść.
    Bez numeru i kickera = 'statement' (nagłówek większy, wyżej). Awatar w prawym górnym rogu."""
    base = Image.new("RGBA", (W, H), hex2rgb(brand.bg) + (255,))
    _vignette(base, brand)
    _accent_bar(base, brand)
    if avatar is None:
        _ornaments(base, brand)
    _header(base, brand, idx, total)
    _avatar(base, brand, avatar, W - MARGIN - 66, 205, 66)
    d = ImageDraw.Draw(base)
    white, accent, taupe = hex2rgb(brand.white), hex2rgb(brand.accent), hex2rgb(brand.taupe)

    if kicker:
        _kicker(base, brand, MARGIN, 360, kicker)
        hf, hl, _ = _fit_rich(d, heading, brand.font_heavy, 82, 50, 3)
        hy = 445
    elif number is not None:
        _big_numeral(base, brand, number, y=300, size=150)
        hf, hl, _ = _fit_rich(d, heading, brand.font_heavy, 72, 48, 3)
        hy = 520
    else:
        hf, hl, _ = _fit_rich(d, heading, brand.font_heavy, 82, 54, 4)
        hy = 360
    lh = int(hf.size * 1.1)
    hbottom = _draw_rich(base, MARGIN, hy, hl, hf, white, accent, lh)

    if body:
        bf, bl, _ = _fit_rich(d, body, brand.font_med, 46, 34, 4)
        blh = int(bf.size * 1.34)
        by = max(hbottom + 120, 900)
        by = min(by, H - 150 - blh * len(bl))
        _draw_rich(base, MARGIN, by, [[(w, False) for w, _ in ln] for ln in bl],
                   bf, taupe, accent, blh)
    _progress(base, brand, idx, total)
    return base.convert("RGB")


def render_list(brand, number, heading, items, idx, total, avatar=None, kicker=None):
    """Slajd punktowany (framework / 'wart zapisania'): nagłówek + koralowe ptaszki."""
    base = Image.new("RGBA", (W, H), hex2rgb(brand.bg) + (255,))
    _vignette(base, brand)
    _accent_bar(base, brand)
    if avatar is None:
        _ornaments(base, brand)
    _header(base, brand, idx, total)
    _avatar(base, brand, avatar, W - MARGIN - 66, 205, 66)
    d = ImageDraw.Draw(base)
    white, accent, taupe = hex2rgb(brand.white), hex2rgb(brand.accent), hex2rgb(brand.taupe)

    hy = 300
    if kicker:
        hy = _kicker(base, brand, MARGIN, 300, kicker) + 4
    elif number is not None:
        _big_numeral(base, brand, number, y=280, size=130)
        hy = 470
    hf, hl, _ = _fit_rich(d, heading, brand.font_heavy, 68, 46, 2)
    hy = _draw_rich(base, MARGIN, hy, hl, hf, white, accent, int(hf.size * 1.08))

    items = [i for i in (items or []) if str(i).strip()][:5]
    if not items:
        _progress(base, brand, idx, total)
        return base.convert("RGB")
    # punkty blisko siebie, wyrównane do góry (elegancko, nie rozstrzelone)
    itf = _f(brand.font_med, 46)
    r = 20
    tx = MARGIN + 2 * r + 30
    rowh = 108
    top = hy + 66
    space = d.textlength(" ", font=itf)
    max_w = W - MARGIN - tx
    for i, it in enumerate(items):
        cy = int(top + i * rowh)
        _check(base, brand, MARGIN, cy, r=r)
        words = _parse_rich(str(it))
        line, lw = [], 0.0
        yy = cy - 6
        for w, acc in words:
            ww = d.textlength(w, font=itf)
            if line and lw + ww + space > max_w:
                _draw_rich(base, tx, yy, [line], itf, white, accent, int(itf.size * 1.2))
                yy += int(itf.size * 1.2)
                line, lw = [(w, acc)], ww
            else:
                line.append((w, acc))
                lw += ww + (space if len(line) > 1 else 0)
        if line:
            _draw_rich(base, tx, yy, [line], itf, white, accent, int(itf.size * 1.2))
    _progress(base, brand, idx, total)
    return base.convert("RGB")


def render_stat(brand, kicker, figure, label, body, idx, total, avatar=None):
    """Slajd statystyki: duża koralowa liczba/% + biały label + taupe kontekst."""
    base = Image.new("RGBA", (W, H), hex2rgb(brand.bg) + (255,))
    _vignette(base, brand)
    _accent_bar(base, brand)
    if avatar is None:
        _ornaments(base, brand)
    _header(base, brand, idx, total)
    _avatar(base, brand, avatar, W - MARGIN - 66, 205, 66)
    d = ImageDraw.Draw(base)
    white, accent, taupe = hex2rgb(brand.white), hex2rgb(brand.accent), hex2rgb(brand.taupe)

    y = _kicker(base, brand, MARGIN, 360, kicker) if kicker else 360
    # wielka liczba
    ff, fl, _ = _fit_rich(d, figure, brand.font_heavy, 300, 150, 1)
    y = _draw_rich(base, MARGIN, y + 10, fl, ff, accent, accent, int(ff.size * 1.0))
    if label:
        lf, ll, _ = _fit_rich(d, label, brand.font_heavy, 76, 48, 3)
        y = _draw_rich(base, MARGIN, y + 24, ll, lf, white, accent, int(lf.size * 1.1))
    if body:
        bf, bl, _ = _fit_rich(d, body, brand.font_med, 46, 34, 3)
        by = min(max(y + 60, 980), H - 150 - int(bf.size * 1.34) * len(bl))
        _draw_rich(base, MARGIN, by, [[(w, False) for w, _ in ln] for ln in bl],
                   bf, taupe, accent, int(bf.size * 1.34))
    _progress(base, brand, idx, total)
    return base.convert("RGB")


def render_chart(brand, kicker, heading, bars, idx, total, avatar=None):
    """Slajd wykresu: poziome słupki. bars = [(label, value_0_100, highlight_bool)]."""
    base = Image.new("RGBA", (W, H), hex2rgb(brand.bg) + (255,))
    _vignette(base, brand)
    _accent_bar(base, brand)
    if avatar is None:
        _ornaments(base, brand)
    _header(base, brand, idx, total)
    _avatar(base, brand, avatar, W - MARGIN - 66, 205, 66)
    d = ImageDraw.Draw(base)
    white, accent, taupe = hex2rgb(brand.white), hex2rgb(brand.accent), hex2rgb(brand.taupe)

    y = _kicker(base, brand, MARGIN, 300, kicker) + 4 if kicker else 300
    hf, hl, _ = _fit_rich(d, heading, brand.font_heavy, 64, 44, 2)
    y = _draw_rich(base, MARGIN, y, hl, hf, white, accent, int(hf.size * 1.1))

    bars = (bars or [])[:4]
    if not bars:
        _progress(base, brand, idx, total)
        return base.convert("RGB")
    top = y + 70
    rowh = min(150, (H - 200 - top) / len(bars))
    bar_w = W - MARGIN - MARGIN
    lf = _f(brand.font_med, 38)
    vf = _f(brand.font_bold, 40)
    for i, item in enumerate(bars):
        label, val = item[0], max(0, min(100, item[1]))
        hi = item[2] if len(item) > 2 else False
        by = int(top + i * rowh)
        d.text((MARGIN, by), label, font=lf, fill=hex2rgb(brand.white))
        track_y = by + 52
        d.rounded_rectangle([MARGIN, track_y, MARGIN + bar_w, track_y + 26], radius=13,
                            fill=_mix(hex2rgb(brand.bg), hex2rgb(brand.taupe), 0.3))
        fillw = int(bar_w * val / 100)
        col = accent if hi else _mix(hex2rgb(brand.taupe), hex2rgb(brand.white), 0.15)
        if fillw > 26:
            d.rounded_rectangle([MARGIN, track_y, MARGIN + fillw, track_y + 26], radius=13,
                                fill=col)
        vtxt = f"{val}%"
        d.text((W - MARGIN - d.textlength(vtxt, font=vf), by - 2), vtxt, font=vf,
               fill=(accent if hi else white))
    _progress(base, brand, idx, total)
    return base.convert("RGB")


def render_cta(brand, heading, body, cta, idx, total, photo=None):
    """Slajd CTA (ostatni): karta z koralową ramką + okrągły awatar (powrót zdjęcia
    z okładki) + linia 'Obserwuj po więcej'."""
    base = Image.new("RGBA", (W, H), hex2rgb(brand.bg) + (255,))
    _vignette(base, brand)
    _accent_bar(base, brand)
    if photo is None:
        _ornaments(base, brand)
    _header(base, brand, idx, total)
    d = ImageDraw.Draw(base)
    white, accent, taupe = hex2rgb(brand.white), hex2rgb(brand.accent), hex2rgb(brand.taupe)

    card = [MARGIN - 12, 250, W - MARGIN + 12, 950]
    d.rounded_rectangle(card, radius=42, outline=accent, width=3)
    cx = MARGIN + 44
    inner_w = (card[2] - 44) - cx
    if photo is not None:
        _avatar(base, brand, photo, W // 2, 400, 116, ring_w=6)
        ctop = 570
        h_hi, h_lines = 74, 2
    else:
        ctop = 340
        h_hi, h_lines = 84, 3
    hf, hl, _ = _fit_rich(d, heading, brand.font_heavy, h_hi, 50, h_lines, max_w=inner_w)
    y = _draw_rich(base, cx, ctop, hl, hf, white, accent, int(hf.size * 1.08))
    # pomiń osobną linię CTA, jeśli hasło jest już wplecione w nagłówek (bez dublowania)
    def _norm(t):
        return "".join(c.lower() for c in (t or "") if c.isalnum())
    if cta and _norm(cta) and _norm(cta) in _norm(heading):
        cta = ""
    if cta:
        cf, cl, _ = _fit_rich(d, cta, brand.font_bold, 52, 38, 2, max_w=inner_w)
        y = _draw_rich(base, cx, y + 8,
                       [[(w, True) for w, _ in ln] for ln in cl], cf, white, accent,
                       int(cf.size * 1.1))
    if body:
        # AUTO-FIT do ramki karty: treść nie może wyjść pod dolną krawędź (card[3]).
        avail = (card[3] - 40) - (y + 34)
        if avail >= 44:
            max_body_lines = max(1, min(4, int(avail / 48)))
            bf, bl, _ = _fit_rich(d, body, brand.font_med, 44, 28, max_body_lines, max_w=inner_w)
            _draw_rich(base, cx, y + 34, [[(w, False) for w, _ in ln] for ln in bl],
                       bf, taupe, accent, int(bf.size * 1.3))

    follow = f"Obserwuj po więcej: {brand.handle}"
    ff, fl, _ = _fit_rich(d, follow, brand.font_bold, 44, 32, 2)
    _draw_rich(base, MARGIN, 1035, [[(w, False) for w, _ in ln] for ln in fl],
               ff, white, accent, int(ff.size * 1.2))
    _progress(base, brand, idx, total)
    return base.convert("RGB")


# ---------- ORKIESTRACJA ----------
def render_carousel(brand, slides, out_dir, photos=None, avatar=None):
    """
    slides: lista dictów. Każdy:
      {"type":"cover","title","subtitle","tagline","count"(opc)}
      {"type":"content","number","heading","body"}
      {"type":"list","number"(opc),"heading","items":[...]}
      {"type":"cta","heading","body","cta"}
    photos: lista ścieżek/Image (pierwsze na okładkę, jeśli pionowe; wraca też w kółku na CTA).
    avatar: ścieżka/Image zdjęcia profilowego klienta -> okrągły awatar na slajdach treści.
    Zwraca: lista ścieżek PNG.
    """
    os.makedirs(out_dir, exist_ok=True)
    total = len(slides)
    paths = []
    cover_photo = None
    if photos:
        p = photos[0]
        cand = Image.open(p) if isinstance(p, str) else p
        cover_photo = cand if cover_photo_ok(cand) else None
    av = None
    if avatar is not None:
        av = Image.open(avatar) if isinstance(avatar, str) else avatar
    for i, s in enumerate(slides, start=1):
        t = s.get("type")
        if t == "cover":
            img = render_cover(brand, s.get("title", ""), s.get("subtitle", ""),
                               s.get("tagline", ""), i, total,
                               count=s.get("count"), photo=cover_photo)
        elif t == "cta":
            img = render_cta(brand, s.get("heading", ""), s.get("body", ""),
                             s.get("cta", ""), i, total, photo=cover_photo)
        elif t == "list":
            img = render_list(brand, s.get("number"), s.get("heading", ""),
                              s.get("items", []), i, total, avatar=av,
                              kicker=s.get("kicker"))
        elif t == "stat":
            img = render_stat(brand, s.get("kicker"), s.get("figure", ""),
                              s.get("label", ""), s.get("body", ""), i, total, avatar=av)
        elif t == "chart":
            img = render_chart(brand, s.get("kicker"), s.get("heading", ""),
                               s.get("bars", []), i, total, avatar=av)
        else:
            img = render_content(brand, s.get("number"), s.get("heading", ""),
                                 s.get("body", ""), i, total, avatar=av,
                                 kicker=s.get("kicker"))
        fp = os.path.join(out_dir, f"slide_{i:02d}.png")
        img.save(fp, "PNG")
        paths.append(fp)
    return paths


# ---------- STORIES (1080x1920, format autentyczny) ----------
SW, SH = 1080, 1920  # kanwa 9:16 Instagram Story


def _story_crop(img, centering=(0.5, 0.38)):
    """Zdjęcie pełnoklatkowo w kadrze 9:16 (crop-to-fill). exif_transpose = poprawny obrót
    (telefony zapisują poziome/pionowe z EXIF; bez tego lądują bokiem)."""
    img = ImageOps.exif_transpose(img.convert("RGB"))
    return ImageOps.fit(img, (SW, SH), method=Image.LANCZOS, centering=centering)


def _story_scrim(base, brand, frac=0.55, strength=1.0):
    """Delikatny gradient od dołu — czytelność tekstu bez agencyjnego wyglądu."""
    grad = Image.new("L", (1, SH), 0)
    for y in range(SH):
        t = max(0.0, (y - SH * (1 - frac)) / (SH * frac))
        grad.putpixel((0, y), int(255 * min(1.0, t ** 1.6) * strength))
    grad = grad.resize((SW, SH))
    solid = Image.new("RGBA", (SW, SH), hex2rgb(brand.bg) + (255,))
    solid.putalpha(grad)
    base.alpha_composite(solid)


def _story_scrim_top(base, brand, frac=0.32, strength=0.95):
    """Gradient od GÓRY — gdy tekst siedzi wyżej (slajdy neutralne)."""
    grad = Image.new("L", (1, SH), 0)
    for y in range(SH):
        t = max(0.0, (SH * frac - y) / (SH * frac))
        grad.putpixel((0, y), int(255 * min(1.0, t ** 1.5) * strength))
    grad = grad.resize((SW, SH))
    solid = Image.new("RGBA", (SW, SH), hex2rgb(brand.bg) + (255,))
    solid.putalpha(grad)
    base.alpha_composite(solid)


def _draw_pill(base, x, y, text, font, fill, text_col, pad_x=44, pad_y=24):
    """Jeden zaokrąglony 'przycisk' (CTA/akcent) — jedyny wypełniony element, przez co
    się wyróżnia; reszta tekstu leży bezpośrednio na zdjęciu."""
    d = ImageDraw.Draw(base)
    tw = int(d.textlength(text, font=font))
    h = int(font.size * 1.0) + 2 * pad_y
    w = tw + 2 * pad_x
    d.rounded_rectangle([x, y, x + w, y + h], radius=h // 2, fill=fill)
    d.text((x + pad_x, y + pad_y - int(font.size * 0.06)), text, font=font, fill=text_col)
    return w, h


def _story_progress(base, brand, idx, total, y=54):
    """Segmentowy pasek postępu u góry (jak w stories) — element brandowy Formatu 1."""
    if total < 2:
        return
    accent, white = hex2rgb(brand.accent), (255, 255, 255)
    d = ImageDraw.Draw(base, "RGBA")
    m, gap, h = 84, 12, 7
    seg = (SW - 2 * m - gap * (total - 1)) / total
    for i in range(total):
        x0 = int(m + i * (seg + gap))
        x1 = int(x0 + seg)
        col = accent + (255,) if i < idx else white + (90,)
        d.rounded_rectangle([x0, y, x1, y + h], radius=h // 2, fill=col)


def render_story(brand, photo, text, out_dir, idx=1, zone="bottom", total=4,
                 kicker=None, cta=None):
    """FORMAT 1 (jedno zdjęcie przez całą serię, spójny szablon): pasek postępu u góry
    (element stories) + statement (duży, bold, *akcent*) w DOLNEJ CZĘŚCI (nie na samym
    dole) + opcjonalna linia dopowiedzenia + opcjonalny CTA-pill. BEZ handla i kickera
    (to nie karuzela). Tekst z \\n: 1. linia = statement, reszta = dopowiedzenie."""
    base = Image.new("RGBA", (SW, SH), hex2rgb(brand.bg) + (255,))
    if photo is not None:
        base.paste(_warm_grade(_story_crop(photo)), (0, 0))
        base = base.convert("RGBA")
        _story_scrim(base, brand, frac=0.62, strength=1.0)
        _story_scrim_top(base, brand, frac=0.18, strength=0.6)
    d = ImageDraw.Draw(base)
    white, accent = hex2rgb(brand.white), hex2rgb(brand.accent)
    margin = 88
    max_w = SW - 2 * margin

    _story_progress(base, brand, idx, total)

    raw = [l.strip() for l in str(text or "").split("\n") if l.strip()]
    statement = raw[0] if raw else ""
    body = " ".join(raw[1:]).strip()

    sf, sl, _ = _fit_rich(d, statement, brand.font_bold, 94, 54, 4, max_w=max_w)
    slh = int(sf.size * 1.13)
    s_h = slh * max(1, len(sl))
    bf = bl = None
    b_h = 0
    if body:
        bf, bl, _ = _fit_rich(d, body, brand.font_body, 48, 36, 3, max_w=max_w)
        b_h = int(bf.size * 1.34) * len(bl)
    cta_font = _f(brand.font_bold, 42)
    cta_h = int(cta_font.size) + 48 if cta else 0

    gap_body, gap_cta = 24, 40
    total_h = s_h + (gap_body + b_h if body else 0) + (gap_cta + cta_h if cta else 0)

    # dolna część kadru, ale uniesione znad samego dołu
    y = int(SH * 0.80) - total_h
    y = max(int(SH * 0.44), y)
    x = margin
    _draw_rich(base, x, y, sl, sf, white, accent, slh, shadow=True)
    y += s_h
    if body:
        y += gap_body
        _draw_rich(base, x, y, bl, bf, white, accent, int(bf.size * 1.34), shadow=True)
        y += b_h
    if cta:
        y += gap_cta
        _draw_pill(base, x, y, cta, cta_font, accent, (255, 255, 255))

    os.makedirs(out_dir, exist_ok=True)
    fp = os.path.join(out_dir, f"story_{idx:02d}.png")
    base.convert("RGB").save(fp, "PNG")
    return fp


def _wrap_plain(d, text, font, max_w):
    words = str(text).split()
    lines, cur = [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if d.textlength(t, font=font) <= max_w or not cur:
            cur = t
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines or [""]


def _fit_plain(d, text, path, hi, lo, max_lines, max_w):
    for s in range(hi, lo - 1, -3):
        f = _f(path, s)
        w = _wrap_plain(d, text, f, max_w)
        if len(w) <= max_lines:
            return f, w
    f = _f(path, lo)
    return f, _wrap_plain(d, text, f, max_w)


def _story_textbox(base, x, y, wrapped, font, fill, text_col, pad_x=32, pad_y=20,
                   radius=24, line_gap=1.12):
    """Zaokrąglony boks tekstowy (autentyczny styl IG/Canva), lewa krawędź w x."""
    d = ImageDraw.Draw(base, "RGBA")
    line_h = int(font.size * line_gap)
    tw = max(d.textlength(l, font=font) for l in wrapped)
    bw, bh = int(tw) + 2 * pad_x, line_h * len(wrapped) + 2 * pad_y
    d.rounded_rectangle([x, y, x + bw, y + bh], radius=radius, fill=fill)
    ty = y + pad_y
    for l in wrapped:
        d.text((x + pad_x, ty), l, font=font, fill=text_col)
        ty += line_h
    return bw, bh


def render_story_native(brand, photo, lines, out_dir, idx=1, zone="bottom", layout=None):
    """FORMAT 2 (autentyczny, storytellingowy): zdjęcie + WIĘCEJ tekstu w MIKSIE stylów,
    jakby klient sam zrobił w Canvie/IG. 1. linia = nagłówek (duży, bez tła, cień).
    Kolejne linie = białe boksy (ciemny tekst). `~linia~` = bez tła (biały tekst).
    `*linia*` = boks akcentowy (koral). Miks boks/bez-tła = naturalny wygląd i czytelność
    także na jasnym tle.

    zone: 'bottom' = zdjęcie z TWARZĄ (folder A) -> nisko, gradient od dołu.
    'full' = zdjęcie NEUTRALNE (folder B) -> wyżej, więcej miejsca na tekst."""
    base = Image.new("RGBA", (SW, SH), hex2rgb(brand.bg) + (255,))
    has_photo = photo is not None
    if has_photo:
        base.paste(_story_crop(photo), (0, 0))
        base = base.convert("RGBA")
    top_anchor = (zone == "full")
    if has_photo:
        if top_anchor:
            _story_scrim_top(base, brand, frac=0.66, strength=0.9)
            _story_scrim(base, brand, frac=0.30, strength=0.45)
        else:
            _story_scrim(base, brand, frac=0.70, strength=0.95)
    else:
        _story_scrim(base, brand, frac=1.0, strength=0.5)
    d = ImageDraw.Draw(base)
    white, accent, ink = hex2rgb(brand.white), hex2rgb(brand.accent), (22, 20, 17)
    margin = 76
    max_w = SW - 2 * margin
    inner = max_w - 2 * 32  # szerokość tekstu w boksie

    # parsowanie linii na (kind, wrapped, font, line_h)
    raw = [str(l).strip() for l in lines if str(l).strip()]
    els = []  # (kind, wrapped, font, line_h, h)
    for i, s in enumerate(raw):
        if s.startswith("*") and s.endswith("*") and len(s) > 2:
            kind, txt = "accent", s.strip("*").strip()
        elif s.startswith("~") and s.endswith("~") and len(s) > 2:
            kind, txt = "plain", s.strip("~").strip()
        elif i == 0:
            kind, txt = "head", s
        else:
            kind, txt = "box", s
        if kind == "head":
            f, w = _fit_plain(d, txt, brand.font_bold, 84, 56, 3, max_w)
            lh = int(f.size * 1.12)
            h = lh * len(w)
        elif kind == "plain":
            f, w = _fit_plain(d, txt, brand.font_bold, 58, 44, 3, max_w)
            lh = int(f.size * 1.16)
            h = lh * len(w)
        else:  # box / accent
            f, w = _fit_plain(d, txt, brand.font_bold, 56, 40, 3, inner)
            lh = int(f.size * 1.12)
            h = lh * len(w) + 2 * 20
        els.append((kind, w, f, lh, h))

    base_gap = 22
    n = len(els)
    sum_h = sum(e[4] for e in els)
    lay = layout or {}
    # domyślne odstępy/przesunięcia
    gaps = [base_gap] * (n - 1)
    xoff = [0] * n
    if not top_anchor:
        total_h = sum_h + sum(gaps)
        y = SH - int(SH * 0.15) - total_h
        y = max(int(SH * 0.42), y)
        y = min(y, SH - total_h - int(SH * 0.04))
    else:
        # slajd neutralny: skupisko o kontrolowanym rytmie (nie rozciągane na całą wysokość)
        g = lay.get("gaps")
        if g:
            gaps = [int(v) for v in g][: n - 1]
            gaps += [base_gap] * (n - 1 - len(gaps))
        o = lay.get("xoff")
        if o:
            xoff = [int(v) for v in o][:n]
            xoff += [0] * (n - len(xoff))
        vpos = lay.get("vpos", 0.5)
        total_h = sum_h + sum(gaps[: n - 1])
        y = int(SH * vpos) - total_h // 2
        y = max(int(SH * 0.10), min(y, SH - total_h - int(SH * 0.06)))

    for i, (kind, w, f, lh, h) in enumerate(els):
        ew = max(int(d.textlength(l, font=f)) for l in w)
        if kind in ("accent", "box"):
            ew += 64
        xx = margin + (xoff[i] if i < len(xoff) else 0)
        xx = min(xx, SW - margin - ew)
        xx = max(margin, xx)
        if kind in ("head", "plain"):
            for l in w:
                # miękka, ale mocna ciemna aura pod napisem bez tła — czytelność na DOWOLNYM
                # (także jasnym/zabieganym) zdjęciu neutralnym, bez twardego boksu
                sh = Image.new("RGBA", base.size, (0, 0, 0, 0))
                sd = ImageDraw.Draw(sh)
                for dx, dy in ((0, 0), (0, 3)):
                    sd.text((xx + dx, y + dy), l, font=f, fill=(0, 0, 0, 235))
                base.alpha_composite(sh.filter(ImageFilter.GaussianBlur(17)))
                sh2 = Image.new("RGBA", base.size, (0, 0, 0, 0))
                ImageDraw.Draw(sh2).text((xx, y + 1), l, font=f, fill=(0, 0, 0, 205))
                base.alpha_composite(sh2.filter(ImageFilter.GaussianBlur(6)))
                ImageDraw.Draw(base).text((xx, y), l, font=f, fill=white)
                y += lh
        elif kind == "accent":
            _story_textbox(base, xx, y, w, f, accent + (255,), (255, 255, 255))
            y += h
        else:  # box
            _story_textbox(base, xx, y, w, f, (255, 255, 255, 255), ink)
            y += h
        if i < n - 1:
            y += gaps[i]

    os.makedirs(out_dir, exist_ok=True)
    fp = os.path.join(out_dir, f"story_{idx:02d}.png")
    base.convert("RGB").save(fp, "PNG")
    return fp


def render_stories(brand, items, out_dir, photos=None):
    """items: lista dictów {'text', 'format'('tip'|'native'), 'photo'} albo str.
    format 'native' -> render_story_native (linie tekstu rozdzielone \\n; *linia* = akcent).
    format 'tip' (domyślny) -> render_story (zdjęcie + tekst brandowy nisko).
    photos: rotacja zdjęć. Zwraca listę ścieżek PNG."""
    os.makedirs(out_dir, exist_ok=True)
    photos = photos or []
    paths = []
    for i, it in enumerate(items, start=1):
        ph, fmt, zone = None, "tip", None
        if isinstance(it, dict):
            text = it.get("text", "")
            ph = it.get("photo")
            fmt = (it.get("format") or "tip").strip().lower()
            zone = (it.get("zone") or "").strip().lower() or None
        else:
            text = str(it)
        if ph is None and photos:
            ph = photos[(i - 1) % len(photos)]
        if isinstance(ph, str):
            try:
                ph = Image.open(ph)
            except Exception:
                ph = None
        # domyślna strefa: jest zdjęcie -> 'bottom' (bezpieczne dla twarzy),
        # brak zdjęcia -> 'full' (tekst na tle brandu może zająć środek)
        if zone not in ("bottom", "full"):
            zone = "bottom" if ph is not None else "full"
        if fmt == "native":
            lines = [l for l in str(text).split("\n")]
            paths.append(render_story_native(brand, ph, lines, out_dir, idx=i, zone=zone))
        else:
            paths.append(render_story(brand, ph, text, out_dir, idx=i, zone=zone))
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
