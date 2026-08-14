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
import math
import dataclasses
import threading
from dataclasses import dataclass
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageOps, ImageEnhance

W, H = 1080, 1350  # kanwa 4:5 Instagram

# kontekst renderu per-wątek (globalny mnożnik rozmiaru tekstu)
_ctx = threading.local()


def _ts():
    return getattr(_ctx, "text_scale", 1.0)
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


# ---------- BIBLIOTEKA FONTÓW (czcionka PER KLIENT z brandbooka) ----------
# Produkcja (Render) ma tylko to, co w repo fonts/. Każdy krój = pliki bold+regular w fonts/.
# Dokładamy kolejne kroje do fonts/ i do FONT_LIBRARY, gdy klient przynosi nowy font.
_FONT_DIR = os.path.join(os.path.dirname(__file__), "fonts")
# Każdy brand = PARA fontów, które się komponują: nagłówek (display) + tekst (body).
# Nazwa (z brandbooka) wskazuje krój wiodący; body dobrany tak, by ładnie pasował.
FONT_PAIRINGS = {
    "space grotesk": {"head": "SpaceGrotesk-Bold.ttf", "body": "Lato-Regular.ttf"},
    "montserrat":    {"head": "Montserrat-Bold.ttf",    "body": "Montserrat-Regular.ttf"},
    "lato":          {"head": "Lato-Bold.ttf",         "body": "Carlito-Regular.ttf"},
    "carlito":       {"head": "Carlito-Bold.ttf",      "body": "Lato-Regular.ttf"},
    "caladea":       {"head": "Caladea-Bold.ttf",      "body": "Lato-Regular.ttf"},  # serif + sans
    "dejavu":        {"head": "DejaVuSans-Bold.ttf",   "body": "Lato-Regular.ttf"},
}
# Aliasy nazw z brandbooków -> najbliższy metrycznie krój wiodący w bibliotece.
FONT_ALIASES = {
    "grotesk": "space grotesk", "spacegrotesk": "space grotesk", "space-grotesk": "space grotesk",
    "poppins": "space grotesk", "montserrat": "montserrat", "geometric": "space grotesk",
    "lato": "lato", "opensans": "lato", "open sans": "lato", "sans": "lato", "roboto": "lato",
    "calibri": "carlito", "carlito": "carlito",
    "cambria": "caladea", "caladea": "caladea", "georgia": "caladea", "times": "caladea",
    "times new roman": "caladea", "serif": "caladea", "playfair": "caladea",
    "arial": "dejavu", "helvetica": "dejavu", "dejavu": "dejavu",
}


def resolve_font_family(name):
    """Nazwa kroju (z brandbooka klienta) -> PARA ścieżek {head, body} do złożenia.
    Puste/nieznane -> domyślna para (Space Grotesk + Lato). Nigdy nie zawodzi."""
    key = (name or "").strip().lower()
    key = FONT_ALIASES.get(key, key)
    pair = FONT_PAIRINGS.get(key) or FONT_PAIRINGS["space grotesk"]

    def _path(fn):
        p = os.path.join(_FONT_DIR, fn)
        return p if os.path.exists(p) else FONT_BOLD
    return {"head": _path(pair["head"]), "body": _path(pair["body"])}


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
    # --- POKRĘTŁA WYGLĄDU (s98) — domyślne = dotychczasowy wygląd (zero regresji) ---
    accent_bar: bool = True         # pasek akcentu z lewej krawędzi
    bar_w: int = BAR                # grubość paska akcentu
    vignette: bool = True           # przyciemnienie rogów (winieta)
    vignette_strength: float = 1.0  # mnożnik siły winiety
    progress: bool = True           # pasek postępu (kropki u dołu) — karuzele
    shadow_strength: float = 1.0    # mnożnik cienia/aury pod tekstem
    text_scale: float = 1.0         # globalny mnożnik rozmiaru tekstu
    cover_scrim: float = 1.0        # mnożnik przyciemnienia zdjęcia okładki (karuzela)
    story_scrim: float = 1.0        # mnożnik przyciemnienia zdjęcia (stories)
    photo_blur: float = 0.0         # rozmycie (blur) zdjęcia w tle, px
    avatar_on: bool = True          # awatar na slajdach treści
    font_heavy: str = FONT_HEAVY
    font_bold: str = FONT_BOLD
    font_med: str = FONT_MED
    font_body: str = FONT_BODY
    font_family: str = ""        # nazwa kroju z brandbooka klienta (puste = Space Grotesk)

    def __post_init__(self):
        # PARA fontów per klient: nagłówek (head) + tekst (body) z brandbooka. Zawsze dwa
        # komponujące się kroje (nie jeden wszędzie). Puste = domyślna para Space Grotesk+Lato.
        fam = resolve_font_family(self.font_family)
        self.font_heavy = fam["head"]
        self.font_bold = fam["head"]
        self.font_med = fam["head"]
        self.font_body = fam["body"]


_BRAND_FIELDS = {f.name for f in dataclasses.fields(Brand)}


def _apply_look(brand, look):
    """Brand z nałożonymi overridami wyglądu per-slajd (tylko pola Brand)."""
    if not look:
        return brand
    over = {k: v for k, v in look.items() if k in _BRAND_FIELDS}
    if not over:
        return brand
    try:
        return dataclasses.replace(brand, **over)
    except Exception:
        return brand


# ---------- KOLORY / TEKST ----------
def hex2rgb(h):
    h = str(h).lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _mix(c, other, t):
    return tuple(int(a * (1 - t) + b * t) for a, b in zip(c, other))


# Scrim okładki fotograficznej — ZAWSZE CIEMNY, niezależny od koloru tła marki.
# (jasne brandy: tekst okładki jest biały, więc scrim nie może być beżowy/jasny).
COVER_SCRIM_RGB = (14, 13, 11)


def _luma(rgb):
    r, g, b = rgb
    return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255.0


def _is_light_bg(brand):
    """Czy tlo marki jest jasne (jasny brandbook, np. kremowe tlo)?"""
    try:
        return _luma(hex2rgb(brand.bg)) > 0.6
    except Exception:
        return False


def _ink_on_bg(brand):
    """Tekst GLOWNY na tle marki (auto-kontrast): jasne tlo -> ciemny atrament,
    ciemne tlo -> biel (dotychczasowe zachowanie, zero regresji na ciemnych)."""
    if _is_light_bg(brand):
        return (24, 22, 18)
    return hex2rgb(brand.white)


def _sec_on_bg(brand):
    """Tekst POMOCNICZY na tle marki (auto-kontrast): jasne tlo -> przyciemniony
    taupe (czytelny), ciemne tlo -> taupe jak dotad."""
    if _is_light_bg(brand):
        return _mix((24, 22, 18), hex2rgb(brand.taupe), 0.42)
    return hex2rgb(brand.taupe)


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
    _sc = _ts()
    if _sc and _sc != 1.0:
        size_hi = max(8, int(round(size_hi * _sc)))
        size_lo = max(8, int(round(size_lo * _sc)))
        if size_hi < size_lo:
            size_hi = size_lo
    words = _parse_rich(text)
    size = size_lo
    lines = _wrap_rich(draw, words, _f(font_path, size_lo), max_w)
    for size in range(size_hi, size_lo - 1, -step):
        font = _f(font_path, size)
        lines = _wrap_rich(draw, words, font, max_w)
        if len(lines) <= max_lines and all(_line_w(draw, ln, font) <= max_w for ln in lines):
            return font, lines, size
    return _f(font_path, size_lo), lines, size_lo


def _draw_rich(base, x, y, lines, font, white, accent, line_h, shadow=False, shadow_strength=1.0,
               hl_accent=False):
    """Rysuje wielolinijkowy tekst z akcentem per-słowo. Czysto (bez poświaty).
    shadow=True dodaje delikatny cień pod tekst (tylko okładka na zdjęciu).
    hl_accent=True (FIX ciemny akcent na zdjęciu): słowa akcentowane NIE są kolorowane
    ciemną czcionką, tylko dostają PODŚWIETLENIE (pigułka w kolorze marki + biały tekst)
    — kolor marki zostaje nietknięty, a słowo jest czytelne na każdym zdjęciu."""
    d = ImageDraw.Draw(base)
    space = d.textlength(" ", font=font)
    if shadow and shadow_strength > 0:
        _sa = max(0, min(255, int(170 * shadow_strength)))
        _sb = max(1, int(round(5 * min(2.0, shadow_strength))))
        sh = Image.new("RGBA", base.size, (0, 0, 0, 0))
        sd = ImageDraw.Draw(sh)
        yy = y
        for line in lines:
            xx = x
            for w, _ in line:
                sd.text((xx + 2, yy + 3), w, font=font, fill=(0, 0, 0, _sa))
                xx += sd.textlength(w, font=font) + space
            yy += line_h
        sh = sh.filter(ImageFilter.GaussianBlur(_sb))
        base.alpha_composite(sh)
    d = ImageDraw.Draw(base)
    yy = y
    for line in lines:
        xx = x
        if hl_accent:
            # najpierw pigułki pod CIĄGŁYMI seriami słów akcentowanych (jedna pigułka na serię)
            run_x, run_w = None, 0.0
            cx2 = xx
            runs = []
            for w, acc in line:
                ww = d.textlength(w, font=font)
                if acc:
                    if run_x is None:
                        run_x = cx2
                    run_w = (cx2 + ww) - run_x
                else:
                    if run_x is not None:
                        runs.append((run_x, run_w))
                        run_x, run_w = None, 0.0
                cx2 += ww + space
            if run_x is not None:
                runs.append((run_x, run_w))
            pad = max(10, int(font.size * 0.22))
            for rx, rw in runs:
                d.rounded_rectangle(
                    [rx - pad, yy - int(font.size * 0.08),
                     rx + rw + pad, yy + int(font.size * 1.16)],
                    radius=int(font.size * 0.30), fill=accent)
        for w, acc in line:
            if hl_accent and acc:
                d.text((xx, yy), w, font=font, fill=(255, 255, 255))
            else:
                d.text((xx, yy), w, font=font, fill=(accent if acc else white))
            xx += d.textlength(w, font=font) + space
        yy += line_h
    return yy


# ---------- ELEMENTY STAŁE ----------
def _accent_bar(base, brand):
    if not getattr(brand, "accent_bar", True):
        return
    bw = getattr(brand, "bar_w", BAR)
    ImageDraw.Draw(base).rectangle([0, 0, bw, H], fill=hex2rgb(brand.accent))


def _ornaments(base, brand, strong=False):
    """Geometryczne kółka (tech/AI) — widoczne, ale eleganckie (dwa koncentryczne).
    strong=True (okładka na zdjęciu) = wyraźniejsze, żeby nie zlały się z fotografią."""
    return  # kółka ozdobne USUNIĘTE (Bartek, s100): żadnych zdobień na karuzelach
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
    if not getattr(brand, "vignette", True):
        return
    st = getattr(brand, "vignette_strength", 1.0)
    v = Image.new("L", (W, H), 0)
    dv = ImageDraw.Draw(v)
    dv.ellipse([-W // 3, -H // 4, W + W // 3, H + H // 4], fill=60)
    v = v.filter(ImageFilter.GaussianBlur(160))
    dark = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    dark.putalpha(ImageOps.invert(v).point(lambda p: int(p * 0.28 * st)))
    base.alpha_composite(dark)


def _top_scrim(base, brand, frac=0.22):
    """Gradient od góry — czytelność górnego paska (handle/numer) na zdjęciu."""
    grad = Image.new("L", (1, H), 0)
    for y in range(H):
        t = max(0.0, 1.0 - y / (H * frac))
        grad.putpixel((0, y), int(238 * (t ** 1.25)))
    grad = grad.resize((W, H))
    solid = Image.new("RGBA", (W, H), COVER_SCRIM_RGB + (255,))
    solid.putalpha(grad)
    base.alpha_composite(solid)


def _draw_tracked(d, xy, text, font, fill, tracking=8):
    x, y = xy
    for ch in text:
        d.text((x, y), ch, font=font, fill=fill)
        x += d.textlength(ch, font=font) + tracking
    return x


def _kicker(base, brand, x, y, text):
    """Mały koralowy nagłówek 'eyebrow' WERSALIKAMI + krótka kreska (editorial).

    ⛔⛔ Bartek (14.08): w nadtytule lądowało całe zdanie („TRZY SYTUACJE, W KTÓRYCH
    FAKTORING WYCHODZI TANIEJ"), a duży nagłówek zostawał pusty („Kiedy to działa").
    Prompt tego już zabrania, ale renderer nie może na to liczyć: nadtytuł rysowany
    był JEDNĄ linią bez żadnego ograniczenia szerokości, więc długi tekst po prostu
    wyjeżdżał poza kadr i się urywał. Tu jest twarda zapora: zmniejszamy stopień
    i zacieśniamy światło, aż napis zmieści się w kadrze; dopiero na końcu ucinamy."""
    if not text:
        return y
    d = ImageDraw.Draw(base)
    txt = text.upper()
    dost = W - x - MARGIN - 80          # 80 px zostawiamy na kreskę za napisem
    rozmiar, tracking = 28, 8
    while rozmiar >= 18:
        f = _f(brand.font_bold, rozmiar)
        szer = sum(d.textlength(ch, font=f) + tracking for ch in txt)
        if szer <= dost:
            break
        rozmiar -= 2
        tracking = max(2, tracking - 1)
    f = _f(brand.font_bold, rozmiar)
    while len(txt) > 8 and sum(d.textlength(ch, font=f) + tracking for ch in txt) > dost:
        txt = txt[:-2]
    end = _draw_tracked(d, (x, y), txt, f, hex2rgb(brand.accent), tracking=tracking)
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
    handle_col = hex2rgb(brand.white) if shadow else _sec_on_bg(brand)
    d.text((MARGIN, 60), brand.handle, font=hf, fill=handle_col)
    pg_col = hex2rgb(brand.white) if shadow else hex2rgb(brand.accent)
    d.text((px, 60), pg, font=pf, fill=pg_col)


def _progress(base, brand, idx, total):
    if not getattr(brand, "progress", True):
        return
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
            col = _mix(hex2rgb(brand.bg), _ink_on_bg(brand), 0.32)
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
def _cover_crop(img, w, h, centering=(0.5, 0.4)):
    return ImageOps.fit(img, (w, h), method=Image.LANCZOS, centering=centering)


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
    solid = Image.new("RGBA", (W, H), COVER_SCRIM_RGB + (255,))
    solid.putalpha(grad)
    base.alpha_composite(solid)


# ---------- KADR OKŁADKI WG TWARZY ----------
def _metryka_okladki(d, brand, title, subtitle):
    """Liczy WYSOKOŚĆ bloku tekstu okładki (hook + chip podtytułu) razem z fontami.

    ⭐ Wydzielone, bo teraz potrzebujemy tego DWA razy i w tej kolejności:
    najpierw żeby wiedzieć, gdzie stanie napis (a więc jaki kadr wyciąć),
    potem żeby ten napis narysować. Jedna funkcja = zero szans na rozjazd."""
    tf, tl, _ = _fit_rich(d, title, brand.font_heavy, 104, 74, 3)
    lh = int(tf.size * 1.06)
    cf = None
    chip_h = 0
    sub_lines = []
    chip_lh = 0
    if subtitle:
        sub_txt = " ".join(w for w, _ in _parse_rich(subtitle))
        chip_max_w = W - 2 * MARGIN - 2 * 40
        cf, sub_lines, _ = _fit_rich(d, sub_txt, brand.font_bold, 68, 44, 1, max_w=chip_max_w)
        if len(sub_lines) > 1 or any(_line_w(d, ln, cf) > chip_max_w for ln in sub_lines):
            cf, sub_lines, _ = _fit_rich(d, sub_txt, brand.font_bold, 52, 34, 2, max_w=chip_max_w)
        chip_lh = int(cf.size) if len(sub_lines) == 1 else int(cf.size * 1.22)
        chip_h = chip_lh * len(sub_lines) + 2 * 22
    block_h = lh * len(tl) + (34 + chip_h if subtitle else 0)
    return {"tf": tf, "tl": tl, "lh": lh, "cf": cf, "sub_lines": sub_lines,
            "chip_lh": chip_lh, "chip_h": chip_h, "block_h": block_h}


def _plan_kadru(iw, ih, twarz, w, h, dol_bezpieczny, gora_min=36, max_zoom=2.4):
    """Sama MATEMATYKA kadru — bez dotykania pikseli, więc można nią TANIO sprawdzić
    kilkanaście zdjęć i wybrać to, które w ogóle nadaje się na okładkę.
    Zwraca (skala, ox, oy, y1_twarzy, y2_twarzy) albo None."""
    try:
        if not twarz or len(twarz) < 4 or iw <= 0 or ih <= 0:
            return None
        x, y, fw, fh = [float(v) for v in twarz[:4]]
        if fw <= 0 or fh <= 0:
            return None
        fx1, fy1 = x / 100.0 * iw, y / 100.0 * ih
        fx2, fy2 = (x + fw) / 100.0 * iw, (y + fh) / 100.0 * ih
        s_fit = max(w / float(iw), h / float(ih))
        cel = float(dol_bezpieczny)
        if cel <= gora_min + 60:
            return None
        if (fy2 - fy1) * s_fit > (cel - gora_min):
            return None                                  # twarz i tak się nie zmieści
        sk = s_fit
        if fy2 > 1:
            sk = max(sk, cel / fy2)                      # kadr nie wystaje górą
        if ih - fy2 > 1:
            sk = max(sk, (h - cel) / (ih - fy2))         # kadr nie wystaje dołem
        sk = min(sk, s_fit * max_zoom)
        rw, rh = int(math.ceil(iw * sk)), int(math.ceil(ih * sk))
        if rw < w or rh < h:
            return None
        oy = min(0.0, max(h - rh, cel - fy2 * sk))
        if oy + fy1 * sk < gora_min:                     # czubek głowy nie ucieka z kadru
            oy = min(0.0, max(h - rh, gora_min - fy1 * sk))
        ox = min(0.0, max(w - rw, w / 2.0 - (fx1 + fx2) / 2.0 * sk))
        lx = max(0, min(rw - w, int(round(-ox))))
        ly = max(0, min(rh - h, int(round(-oy))))
        g1, g2 = fy1 * sk - ly, fy2 * sk - ly
        if g2 > cel + 2:
            return None                                  # nie udało się — niech zdecyduje wywołujący
        return (sk, lx, ly, g1, g2)
    except Exception:
        return None


def _kadr_twarz_nad(img, twarz, w, h, dol_bezpieczny, gora_min=36, max_zoom=2.4):
    """Wycina kadr w×h tak, żeby CAŁA twarz stanęła NAD linią `dol_bezpieczny`.

    ⛔⛔ DLACZEGO STARE PODEJŚCIE NIE DZIAŁAŁO (zmierzone 14.08 na gosia-03): zdjęcie
    z telefonu 3:4 w kanwie 4:5 ma raptem ~90 px zapasu w pionie, więc `ImageOps.fit`
    z dowolnym `centering` daje praktycznie ten sam kadr. Render z ramką i bez ramki
    wyszedł niemal identyczny — napis leżał na brodzie w obu. Kod próbował wtedy
    PRZENIEŚĆ NAPIS (pod twarz albo nad twarz), a gdy nie mieścił się ani tu, ani tu,
    po cichu zostawiał go na twarzy. Czyli: dokładnie to, co widział Bartek.
    ⭐ POPRAWNA DŹWIGNIA: na okładce napis ma STAŁE miejsce u dołu, więc to ZDJĘCIE ma
    się dopasować do napisu. Dobieramy więc nie tylko przesunięcie, ale i SKALĘ —
    gdy zdjęcie nie ma zapasu, kadrujemy ciaśniej (zoom), aż twarz wejdzie nad napis.
    Zwraca (kadr, (y1, y2)) albo None, gdy fizycznie się nie da — wtedy działa stara
    ścieżka awaryjna. Bez ramki twarzy zwraca None i nic się nie zmienia."""
    try:
        if img is None:
            return None
        iw, ih = img.size
        plan = _plan_kadru(iw, ih, twarz, w, h, dol_bezpieczny, gora_min=gora_min, max_zoom=max_zoom)
        if plan is None:
            return None
        sk, lx, ly, g1, g2 = plan
        rw, rh = int(math.ceil(iw * sk)), int(math.ceil(ih * sk))
        skala = img.resize((rw, rh), Image.LANCZOS)
        return skala.crop((lx, ly, lx + w, ly + h)), (g1, g2)
    except Exception:
        return None


# ---------- SLAJDY ----------
def render_cover(brand, title, subtitle, tagline, idx, total, count=None, photo=None, title_shift=0,
                 twarz=None):
    """Okładka. Ze zdjęciem = pełnoklatkowe foto+scrim; bez = tekstowa z badge liczby.

    ⭐ `twarz` = ramka [x, y, w, h] w PROCENTACH zdjęcia (pole „Twarz — ramka" w bazie).
    Bartek (14.08): „mapowanie twarzy musi być też w karuzeli tokenowej i w stories".
    Stories dostały to wcześniej; tu robimy to samo dwoma ruchami: (1) kadr wycinamy
    tak, żeby twarz poszła do góry, (2) jeżeli mimo to blok tekstu wypada na twarzy —
    PRZENOSIMY TEKST, nie twarz. Bez ramki wszystko działa dokładnie jak dotąd."""
    base = Image.new("RGBA", (W, H), hex2rgb(brand.bg) + (255,))
    on_photo = photo is not None
    _tw = None
    _m = None
    _kadr_ok = False
    if on_photo:
        # ⭐ KOLEJNOŚĆ MA ZNACZENIE: najpierw mierzymy napis (ma stałe miejsce u dołu),
        # dopiero potem wycinamy kadr tak, żeby twarz stanęła NAD nim.
        _m = _metryka_okladki(ImageDraw.Draw(base), brand, title, subtitle)
        _y_doc = max(120, H - 158 - _m["block_h"] - int(title_shift or 0))
        _zrodlo = _warm_grade(photo.convert("RGB"))
        _wyc = _kadr_twarz_nad(_zrodlo, twarz, W, H, _y_doc - 34)
        if _wyc is not None:
            ph, _tw = _wyc
            _kadr_ok = True
        else:
            _cent = _kadr_wg_twarzy(twarz, domyslne=(0.5, 0.4), cel_y=0.30)
            ph = _cover_crop(_zrodlo, W, H, centering=_cent)
            _tw = _twarz_na_kadrze(_zrodlo, twarz, centering=_cent, sw=W, sh=H)
        _pb = getattr(brand, "photo_blur", 0.0)
        if _pb and _pb > 0:
            ph = ph.filter(ImageFilter.GaussianBlur(_pb))
        base.paste(ph, (0, 0))
        base = base.convert("RGBA")
        _cs = getattr(brand, "cover_scrim", 1.0)
        _bottom_scrim(base, brand, frac=min(0.95, 0.72 * _cs))
        _top_scrim(base, brand, frac=min(0.6, 0.28 * _cs))   # czytelność górnego paska
    else:
        _vignette(base, brand)
    _accent_bar(base, brand)
    _ornaments(base, brand, strong=on_photo)
    _header(base, brand, idx, total, shadow=on_photo)
    d = ImageDraw.Draw(base)
    white, accent, taupe = _ink_on_bg(brand), hex2rgb(brand.accent), _sec_on_bg(brand)

    if on_photo:
        white = hex2rgb(brand.white)  # na ciemnym scrim okładki tekst ZAWSZE biały
        # HOOK: cały biały (akcentowane słowa też białe — wyróżnienie wielkością, nie kolorem).
        # Kolor marki NIE jest zmieniany: wraca jako WYPEŁNIONY chip podtytułu (biały tekst na
        # akcencie = czytelny dla KAŻDEGO koloru marki, także ciemnego jak granat).
        _ss = getattr(brand, "shadow_strength", 1.0)
        tf, tl, lh = _m["tf"], _m["tl"], _m["lh"]
        cf, sub_lines, chip_lh, chip_h = _m["cf"], _m["sub_lines"], _m["chip_lh"], _m["chip_h"]
        block_h = _m["block_h"]
        y = max(120, H - 158 - block_h - int(title_shift or 0))
        # ⭐⭐ NAPIS SCHODZI Z TWARZY. Domyślnie blok stoi u dołu kadru — dokładnie tam,
        # gdzie na wielu zdjęciach z telefonu jest twarz. Jeżeli ramka twarzy mówi, że
        # tak jest, próbujemy najpierw POD twarzą, a jak nie ma miejsca — NAD nią,
        # dokładając wtedy przyciemnienie od góry, żeby biały tekst został czytelny.
        if _tw is not None and not _kadr_ok:
            f1, f2 = _tw
            if y < f2 and (y + block_h) > f1:
                pod = f2 + 34
                nad = f1 - 34 - block_h
                if pod + block_h <= H - 150:
                    y = int(pod)
                elif nad >= 170:
                    y = int(nad)
                    _top_scrim(base, brand, frac=0.66)
                    # przyciemnienie poszło NA narysowany już pasek górny — rysujemy go
                    # jeszcze raz, żeby handle i licznik nie zgasły
                    _header(base, brand, idx, total, shadow=on_photo)
                else:
                    # ⛔ TU KOD DOTĄD MILCZAŁ i zostawiał napis na twarzy. Skoro nie da się
                    # ani zejść pod twarz, ani wejść nad nią — przynajmniej dokładamy pełne
                    # przyciemnienie, żeby biały tekst był czytelny, a nie „szary na policzku".
                    _bottom_scrim(base, brand, frac=0.98)
        y = _draw_rich(base, MARGIN, y, tl, tf, white, white, lh, shadow=True, shadow_strength=_ss)
        if subtitle and cf is not None:
            cy = y + 34
            pad_x = 40
            dmes = ImageDraw.Draw(base)
            line_txts = [" ".join(w for w, _ in ln) for ln in sub_lines]
            tw = max(dmes.textlength(t, font=cf) for t in line_txts)
            cw = min(int(tw) + 2 * pad_x, W - 2 * MARGIN)
            rad = min(chip_h // 2, 46)
            # miękki cień pod chipem — lekko unosi go znad zdjęcia
            sh = Image.new("RGBA", base.size, (0, 0, 0, 0))
            ImageDraw.Draw(sh).rounded_rectangle(
                [MARGIN, cy, MARGIN + cw, cy + chip_h], radius=rad, fill=(0, 0, 0, 150))
            base.alpha_composite(sh.filter(ImageFilter.GaussianBlur(12)))
            dc = ImageDraw.Draw(base)
            dc.rounded_rectangle([MARGIN, cy, MARGIN + cw, cy + chip_h],
                                 radius=rad, fill=hex2rgb(brand.accent))
            ty = cy + 22 - int(cf.size * 0.06)
            for t in line_txts:
                dc.text((MARGIN + pad_x, ty), t, font=cf, fill=hex2rgb(brand.white))
                ty += chip_lh
    else:
        ink, sec = _ink_on_bg(brand), _sec_on_bg(brand)
        y = 235
        if count is not None:
            y = _count_badge(base, brand, count, y=y) + 120
        else:
            y = 430
        tf, tl, _ = _fit_rich(d, title, brand.font_heavy, 96, 60, 3)
        lh = int(tf.size * 1.06)
        y = _draw_rich(base, MARGIN, y, tl, tf, ink, accent, lh)
        if subtitle:
            sf, sl, _ = _fit_rich(d, subtitle, brand.font_heavy, 76, 48, 3)
            slh = int(sf.size * 1.1)
            y = _draw_rich(base, MARGIN, y + 26,
                           [[(w, True) for w, _ in ln] for ln in sl], sf, ink, accent, slh)
        if tagline:
            gf, gl, _ = _fit_rich(d, tagline, brand.font_med, 42, 32, 2)
            _draw_rich(base, MARGIN, y + 44, gl, gf, sec, accent, int(gf.size * 1.28))
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
    white, accent, taupe = _ink_on_bg(brand), hex2rgb(brand.accent), _sec_on_bg(brand)

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
        by = max(by, hbottom + 60)  # FIX: treść nigdy nie wjeżdża na nagłówek
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
    white, accent, taupe = _ink_on_bg(brand), hex2rgb(brand.accent), _sec_on_bg(brand)

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
    # FIX (napis na napisie): wiersze NIE są już sztywne (rowh=108) — każdy punkt
    # zajmuje tyle linii, ile realnie potrzebuje, a następny zaczyna się POD nim.
    # Rozmiar czcionki dobierany tak, żeby całość zmieściła się nad paskiem postępu.
    r = 20
    tx = MARGIN + 2 * r + 30
    top = hy + 66
    max_w = W - MARGIN - tx
    avail_h = (H - 140) - top  # dół: strefa paska postępu
    itf = None
    wrapped = []
    for size in (46, 43, 40, 37, 34):
        itf = _f(brand.font_med, size)
        lineh = int(size * 1.2)
        gap = 34
        wrapped = [_wrap_rich(d, _parse_rich(str(it)), itf, max_w) for it in items]
        total_h = sum(max(len(ls) * lineh, 2 * r + 8) + gap for ls in wrapped) - gap
        if total_h <= avail_h:
            break
    lineh = int(itf.size * 1.2)
    gap = 34
    cy = int(top)
    for it_lines in wrapped:
        _check(base, brand, MARGIN, cy, r=r)
        yy = cy - 6
        for line in it_lines:
            _draw_rich(base, tx, yy, [line], itf, white, accent, lineh)
            yy += lineh
        cy += max(len(it_lines) * lineh, 2 * r + 8) + gap
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
    white, accent, taupe = _ink_on_bg(brand), hex2rgb(brand.accent), _sec_on_bg(brand)

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
    white, accent, taupe = _ink_on_bg(brand), hex2rgb(brand.accent), _sec_on_bg(brand)

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
        d.text((MARGIN, by), label, font=lf, fill=white)
        track_y = by + 52
        d.rounded_rectangle([MARGIN, track_y, MARGIN + bar_w, track_y + 26], radius=13,
                            fill=_mix(hex2rgb(brand.bg), _ink_on_bg(brand), 0.22))
        fillw = int(bar_w * val / 100)
        col = accent if hi else _sec_on_bg(brand)
        if fillw > 26:
            d.rounded_rectangle([MARGIN, track_y, MARGIN + fillw, track_y + 26], radius=13,
                                fill=col)
        vtxt = f"{val}%"
        d.text((W - MARGIN - d.textlength(vtxt, font=vf), by - 2), vtxt, font=vf,
               fill=(accent if hi else white))
    _progress(base, brand, idx, total)
    return base.convert("RGB")


def _krzyzyk(base, brand, x, y, r=20, kolor=None):
    """Znacznik „nie" — kółko w kolorze drugoplanowym z białym iksem."""
    d = ImageDraw.Draw(base)
    kol = kolor or _mix(hex2rgb(brand.bg), _ink_on_bg(brand), 0.42)
    d.ellipse([x, y, x + 2 * r, y + 2 * r], fill=kol)
    a = r * 0.44
    cx, cy = x + r, y + r
    d.line([(cx - a, cy - a), (cx + a, cy + a)], fill=hex2rgb(brand.white), width=5)
    d.line([(cx - a, cy + a), (cx + a, cy - a)], fill=hex2rgb(brand.white), width=5)


def _karta(base, brand, box, sila=0.10, obwodka=None, promien=34):
    """Miękka karta pod treść — delikatnie jaśniejsze/ciemniejsze pole na tle marki."""
    d = ImageDraw.Draw(base)
    d.rounded_rectangle(box, radius=promien,
                        fill=_mix(hex2rgb(brand.bg), _ink_on_bg(brand), sila))
    if obwodka:
        d.rounded_rectangle(box, radius=promien, outline=obwodka, width=3)


def render_porownanie(brand, kicker, heading, lewa, punkty_l, prawa, punkty_p, idx, total,
                      avatar=None):
    """⭐ NOWY RODZAJ SLAJDU (Bartek, 14.08: „większość slajdów jest taka sama… dołóżmy
    jeszcze ze dwa, trzy rodzaje"). PORÓWNANIE DWÓCH KOLUMN: tak/nie, przed/po, mit/fakt.
    Lewa kolumna = to, co NIE działa (iksy, kolor drugoplanowy), prawa = to, co działa
    (ptaszki, kolor marki). Czyta się jednym rzutem oka — dokładnie dlatego, że nie jest
    to „napis na górze, wyjaśnienie na dole"."""
    base = Image.new("RGBA", (W, H), hex2rgb(brand.bg) + (255,))
    _vignette(base, brand)
    _accent_bar(base, brand)
    if avatar is None:
        _ornaments(base, brand)
    _header(base, brand, idx, total)
    _avatar(base, brand, avatar, W - MARGIN - 66, 205, 66)
    d = ImageDraw.Draw(base)
    white, accent, taupe = _ink_on_bg(brand), hex2rgb(brand.accent), _sec_on_bg(brand)

    y = _kicker(base, brand, MARGIN, 300, kicker) + 4 if kicker else 300
    if heading:
        hf, hl, _ = _fit_rich(d, heading, brand.font_heavy, 64, 44, 2)
        y = _draw_rich(base, MARGIN, y, hl, hf, white, accent, int(hf.size * 1.08))
    top = int(y + 60)

    przerwa = 36
    kol_w = (W - 2 * MARGIN - przerwa) / 2.0
    dol = H - 150
    kolumny = [
        (MARGIN, lewa, [i for i in (punkty_l or []) if str(i).strip()][:4], False),
        (MARGIN + kol_w + przerwa, prawa, [i for i in (punkty_p or []) if str(i).strip()][:4], True),
    ]
    pad = 30
    tekst_w = kol_w - 2 * pad - 52          # 52 px na znacznik
    # jeden wspólny stopień pisma dla OBU kolumn — inaczej wygląda to jak dwa różne slajdy
    rozmiar = 40
    for rozmiar in (40, 37, 34, 31, 28):
        f = _f(brand.font_med, rozmiar)
        lh = int(rozmiar * 1.24)
        naj = 0
        for _, tyt, punkty, _p in kolumny:
            wys = 96 if tyt else 20
            for it in punkty:
                lines = _wrap_rich(d, _parse_rich(str(it)), f, tekst_w)
                wys += max(len(lines) * lh, 46) + 26
            naj = max(naj, wys)
        if top + pad + naj <= dol - pad:
            break
    f = _f(brand.font_med, rozmiar)
    lh = int(rozmiar * 1.24)
    tf = _f(brand.font_bold, min(42, rozmiar + 4))

    # ⭐ karta ma wysokość TREŚCI, a nie całego kadru — inaczej pod krótką listą zostaje
    # pół slajdu pustki i wygląda to jak błąd renderu, a nie jak decyzja.
    wysokosci = []
    for _, tyt, punkty, _d in kolumny:
        wys = 96 if tyt else 20
        for it in punkty:
            wys += max(len(_wrap_rich(d, _parse_rich(str(it)), f, tekst_w)) * lh, 46) + 26
        wysokosci.append(wys - 26 + 2 * pad)
    kh = min(max(wysokosci), dol - top)
    _karta(base, brand, [MARGIN, top, MARGIN + kol_w, top + kh], sila=0.07)
    _karta(base, brand, [MARGIN + kol_w + przerwa, top, W - MARGIN, top + kh], sila=0.07,
           obwodka=accent)

    for x0, tyt, punkty, dobra in kolumny:
        cy = top + pad
        if tyt:
            txt = str(tyt).upper()
            while d.textlength(txt, font=tf) > kol_w - 2 * pad and len(txt) > 4:
                txt = txt[:-2]
            d.text((x0 + pad, cy), txt, font=tf, fill=(accent if dobra else taupe))
            cy += 96
        for it in punkty:
            lines = _wrap_rich(d, _parse_rich(str(it)), f, tekst_w)
            if dobra:
                _check(base, brand, x0 + pad, cy, r=17)
            else:
                _krzyzyk(base, brand, x0 + pad, cy, r=17)
            yy = cy - 6
            for line in lines:
                _draw_rich(base, x0 + pad + 52, yy, [line], f, white, accent, lh)
                yy += lh
            cy += max(len(lines) * lh, 46) + 26
    _progress(base, brand, idx, total)
    return base.convert("RGB")


def render_kroki(brand, kicker, heading, kroki, idx, total, avatar=None):
    """⭐ NOWY RODZAJ SLAJDU: OŚ KROKÓW 1 → 2 → 3. Numerowane kółka spięte pionową linią,
    przy każdym tytuł kroku i (opcjonalnie) jedno zdanie wyjaśnienia — `Tytuł | wyjaśnienie`.
    Inaczej niż lista z ptaszkami: tu widać KOLEJNOŚĆ, a nie zbiór."""
    base = Image.new("RGBA", (W, H), hex2rgb(brand.bg) + (255,))
    _vignette(base, brand)
    _accent_bar(base, brand)
    if avatar is None:
        _ornaments(base, brand)
    _header(base, brand, idx, total)
    _avatar(base, brand, avatar, W - MARGIN - 66, 205, 66)
    d = ImageDraw.Draw(base)
    white, accent, taupe = _ink_on_bg(brand), hex2rgb(brand.accent), _sec_on_bg(brand)

    y = _kicker(base, brand, MARGIN, 300, kicker) + 4 if kicker else 300
    if heading:
        hf, hl, _ = _fit_rich(d, heading, brand.font_heavy, 64, 44, 2)
        y = _draw_rich(base, MARGIN, y, hl, hf, white, accent, int(hf.size * 1.08))

    pozycje = []
    for k in (kroki or []):
        czesci = [c.strip() for c in str(k).split("|")]
        tytul = czesci[0] if czesci else ""
        opis = czesci[1] if len(czesci) > 1 else ""
        if tytul:
            pozycje.append((tytul, opis))
    pozycje = pozycje[:4]
    if not pozycje:
        _progress(base, brand, idx, total)
        return base.convert("RGB")

    r = 40
    tx = MARGIN + 2 * r + 34
    max_w = W - MARGIN - tx
    top = int(y + 70)
    dost = (H - 160) - top
    for st, so in ((52, 40), (48, 37), (44, 34), (40, 31), (36, 29)):
        ft = _f(brand.font_heavy, st)
        fo = _f(brand.font_med, so)
        lht, lho = int(st * 1.12), int(so * 1.28)
        blok = []
        wys = 0
        for tytul, opis in pozycje:
            lt = _wrap_rich(d, _parse_rich(tytul), ft, max_w)
            lo = _wrap_rich(d, _parse_rich(opis), fo, max_w) if opis else []
            h = max(len(lt) * lht + (len(lo) * lho + 14 if lo else 0), 2 * r + 6)
            blok.append((lt, lo, h))
            wys += h + 46
        wys -= 46
        if wys <= dost:
            break
    nf = _f(brand.font_heavy, int(r * 1.0))
    cy = top
    for i, (lt, lo, h) in enumerate(blok):
        # linia łącząca kroki — to ona robi z listy OŚ
        if i < len(blok) - 1:
            d.line([(MARGIN + r, cy + 2 * r + 6), (MARGIN + r, cy + h + 46 - 6)],
                   fill=_mix(hex2rgb(brand.bg), _ink_on_bg(brand), 0.30), width=4)
        d.ellipse([MARGIN, cy, MARGIN + 2 * r, cy + 2 * r], outline=accent, width=4)
        num = str(i + 1)
        nw = d.textlength(num, font=nf)
        d.text((MARGIN + r - nw / 2, cy + r - nf.size * 0.62), num, font=nf, fill=accent)
        yy = cy - 4
        for line in lt:
            _draw_rich(base, tx, yy, [line], ft, white, accent, lht)
            yy += lht
        if lo:
            yy += 14
            for line in lo:
                _draw_rich(base, tx, yy, [[(w, False) for w, _ in line]], fo, taupe, accent, lho)
                yy += lho
        cy += h + 46
    _progress(base, brand, idx, total)
    return base.convert("RGB")


def render_cytat(brand, cytat, autor, idx, total, avatar=None, kicker=None):
    """⭐ NOWY RODZAJ SLAJDU: CYTAT NA CAŁĄ PLANSZĘ. Jedno zdanie, duże, na środku,
    z wielkim znakiem cudzysłowu w kolorze marki. Slajd-oddech: przerywa rytm
    „napis na górze, wyjaśnienie na dole" i daje karuzeli miejsce, w którym oko odpoczywa."""
    base = Image.new("RGBA", (W, H), hex2rgb(brand.bg) + (255,))
    _vignette(base, brand)
    _accent_bar(base, brand)
    if avatar is None:
        _ornaments(base, brand)
    _header(base, brand, idx, total)
    _avatar(base, brand, avatar, W - MARGIN - 66, 205, 66)
    d = ImageDraw.Draw(base)
    white, accent, taupe = _ink_on_bg(brand), hex2rgb(brand.accent), _sec_on_bg(brand)

    txt = str(cytat or "").strip().strip('"\u201e\u201d\u201c')
    cf, cl, _ = _fit_rich(d, txt, brand.font_heavy, 86, 46, 6)
    lh = int(cf.size * 1.16)
    wys = lh * len(cl)
    # ⛔ FIX: cudzysłów rysowany „na oko" wchodził na pierwszą literę cytatu. Mierzymy
    # jego REALNY obrys (textbbox) i stawiamy go NAD tekstem, wyrównanego do marginesu.
    qf = _f(brand.font_heavy, 190)
    qtxt = "\u201d"
    try:
        qb = d.textbbox((0, 0), qtxt, font=qf)
    except Exception:
        qb = (0, 0, 120, 120)
    qw, qh = qb[2] - qb[0], qb[3] - qb[1]
    caly = qh + 26 + wys + 44 + (56 if autor else 0)
    gora = int(max(300, (H - caly) / 2))
    gora = min(gora, H - 190 - caly)
    d.text((MARGIN - qb[0], gora - qb[1]), qtxt, font=qf, fill=accent)
    y = _draw_rich(base, MARGIN, gora + qh + 26, cl, cf, white, accent, lh)
    d.line([(MARGIN, y + 44), (MARGIN + 110, y + 44)], fill=accent, width=5)
    if autor:
        af = _f(brand.font_med, 40)
        d.text((MARGIN, y + 74), str(autor), font=af, fill=taupe)
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
    white, accent, taupe = _ink_on_bg(brand), hex2rgb(brand.accent), _sec_on_bg(brand)

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
def render_carousel(brand, slides, out_dir, photos=None, avatar=None, twarze=None):
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
    cover_twarz = None
    if photos:
        # ⭐⭐ WYBÓR ZDJĘCIA NA OKŁADKĘ, A NIE „PIERWSZE Z BRZEGU".
        # Bartek (14.08): „napisy najeżdżają jej na twarz". Część zdjęć po prostu NIE NADAJE SIĘ
        # na okładkę: twarz jest duża i nisko, więc przy stałym miejscu napisu u dołu nie da się
        # jej ustawić wyżej żadnym kadrem. Zamiast renderować takie zdjęcie i psuć okładkę,
        # bierzemy PIERWSZE Z PULI, które przechodzi test geometrii (`_plan_kadru` — sama
        # matematyka, bez skalowania pikseli, więc sprawdzenie kilkunastu zdjęć jest darmowe).
        # Gdy żadne nie przejdzie — zostaje pierwsze sensowne i działają stare ścieżki awaryjne.
        _cs = next((_s for _s in slides if _s.get("type") == "cover"), None)
        _y_doc = None
        if _cs is not None:
            _eff0 = _apply_look(brand, _cs.get("look") or {})
            _stare_skalowanie = getattr(_ctx, "text_scale", 1.0)
            _ctx.text_scale = getattr(_eff0, "text_scale", 1.0)
            try:
                _mm = _metryka_okladki(ImageDraw.Draw(Image.new("RGB", (W, H))), _eff0,
                                       _cs.get("title", ""), _cs.get("subtitle", ""))
                try:
                    _ts = int((_cs.get("look") or {}).get("title_shift", 0) or 0)
                except (TypeError, ValueError):
                    _ts = 0
                _y_doc = max(120, H - 158 - _mm["block_h"] - _ts)
            finally:
                _ctx.text_scale = _stare_skalowanie
        _zapas = None
        for _i, _p in enumerate(photos):
            try:
                _c = Image.open(_p) if isinstance(_p, str) else _p
            except Exception:
                continue
            if not cover_photo_ok(_c):
                continue
            _tw = None
            if twarze:
                try:
                    _tw = twarze[_i]
                except (IndexError, TypeError):
                    _tw = None
            if _zapas is None:
                _zapas = (_c, _tw)
            if _y_doc is None or not _tw:
                continue
            if _plan_kadru(_c.size[0], _c.size[1], _tw, W, H, _y_doc - 34) is not None:
                _zapas = (_c, _tw)
                break
        if _zapas is not None:
            cover_photo, cover_twarz = _zapas
    av = None
    if avatar is not None:
        av = Image.open(avatar) if isinstance(avatar, str) else avatar
    for i, s in enumerate(slides, start=1):
        look = s.get("look") or {}
        eff = _apply_look(brand, look)
        _ctx.text_scale = getattr(eff, "text_scale", 1.0)
        av_use = av if getattr(eff, "avatar_on", True) else None
        try:
            tshift = int(look.get("title_shift", 0) or 0)
        except (TypeError, ValueError):
            tshift = 0
        t = s.get("type")
        if t == "cover":
            img = render_cover(eff, s.get("title", ""), s.get("subtitle", ""),
                               s.get("tagline", ""), i, total,
                               count=s.get("count"), photo=cover_photo, title_shift=tshift,
                               twarz=cover_twarz)
        elif t == "cta":
            # CTA: w kółku ZDJĘCIE PROFILOWE klienta (decyzja Bartka s103); zdjęcie
            # z okładki tylko awaryjnie, gdy brak profilowego.
            img = render_cta(eff, s.get("heading", ""), s.get("body", ""),
                             s.get("cta", ""), i, total,
                             photo=(av if av is not None else cover_photo))
        elif t == "list":
            img = render_list(eff, s.get("number"), s.get("heading", ""),
                              s.get("items", []), i, total, avatar=av_use,
                              kicker=s.get("kicker"))
        elif t == "stat":
            img = render_stat(eff, s.get("kicker"), s.get("figure", ""),
                              s.get("label", ""), s.get("body", ""), i, total, avatar=av_use)
        elif t == "chart":
            img = render_chart(eff, s.get("kicker"), s.get("heading", ""),
                               s.get("bars", []), i, total, avatar=av_use)
        elif t == "porownanie":
            img = render_porownanie(eff, s.get("kicker"), s.get("heading", ""),
                                    s.get("lewa", ""), s.get("punkty_l", []),
                                    s.get("prawa", ""), s.get("punkty_p", []),
                                    i, total, avatar=av_use)
        elif t == "kroki":
            img = render_kroki(eff, s.get("kicker"), s.get("heading", ""),
                               s.get("kroki", []), i, total, avatar=av_use)
        elif t == "cytat":
            img = render_cytat(eff, s.get("cytat", ""), s.get("autor", ""), i, total,
                               avatar=av_use, kicker=s.get("kicker"))
        else:
            img = render_content(eff, s.get("number"), s.get("heading", ""),
                                 s.get("body", ""), i, total, avatar=av_use,
                                 kicker=s.get("kicker"))
        fp = os.path.join(out_dir, f"slide_{i:02d}.png")
        img.save(fp, "PNG")
        paths.append(fp)
    _ctx.text_scale = 1.0
    return paths


# ---------- STORIES (1080x1920, format autentyczny) ----------
SW, SH = 1080, 1920  # kanwa 9:16 Instagram Story


def _kadr_wg_twarzy(twarz, domyslne=(0.5, 0.38), cel_y=0.26):
    """Zamienia ramkę twarzy [x, y, w, h] w PROCENTACH zdjęcia na `centering` dla ImageOps.fit,
    tak żeby środek twarzy wypadł w GÓRNEJ części kadru — tam, gdzie nie ma napisów.

    ⛔⛔ Bartek, 14.08: „mapowanie twarzy się nie sprawdziło, ponieważ nachodzi bardzo mocno
    na napis. Na twarz nie powinno tak być." Stories i karuzela tokenowa nie dostawały
    ramek twarzy w ogóle — kadr był zawsze wycinany na sztywno (0,38 wysokości), więc przy
    zdjęciu, na którym twarz siedzi nisko, tekst lądował prosto na niej.
    ⭐ Ramki są policzone detektorem i leżą w bazie („Twarz — ramka"). Tu ich używamy.
    Gdy ramki nie ma — zachowujemy się dokładnie jak dotąd, więc nic się nie psuje."""
    try:
        if not twarz or len(twarz) < 4:
            return domyslne
        x, y, w, h = [float(v) for v in twarz[:4]]
        if w <= 0 or h <= 0:
            return domyslne
        sx, sy = (x + w / 2.0) / 100.0, (y + h / 2.0) / 100.0
        # ImageOps.fit tnie nadmiar; `centering` mówi, KTÓRĄ część zachować.
        # Chcemy, żeby środek twarzy wypadł na `cel_y` wysokości kadru.
        cy = min(1.0, max(0.0, (sy - cel_y) / max(0.001, 1.0 - 0.0)))
        cx = min(1.0, max(0.0, sx))
        return (cx, cy)
    except Exception:
        return domyslne



def _twarz_na_kadrze(img, twarz, centering=(0.5, 0.38), sw=None, sh=None):
    """Gdzie w GOTOWYM kadrze wyląduje twarz — zwraca (y1, y2) w pikselach albo None.

    ⛔⛔ DLACZEGO NIE WYSTARCZY PRZESUNĄĆ KADRU: zdjęcie z telefonu ma zwykle 3:4, a kadr
    stories 9:16. Przy takich proporcjach `ImageOps.fit` przycina wyłącznie BOKI — w pionie
    zostaje całe zdjęcie, więc żadne `centering` nie ruszy twarzy ani o piksel. Sprawdzone
    na renderze: kadr z ramką i bez ramki wyszły identyczne co do piksela.
    ⭐ Dlatego robimy to samo, co format „to «X» — dopóki…": liczymy, gdzie twarz stoi
    w gotowym kadrze, i USUWAMY STAMTĄD NAPIS. Zdjęcia nie ruszamy."""
    try:
        if img is None or not twarz or len(twarz) < 4:
            return None
        sw = sw or SW
        sh = sh or SH
        iw, ih = img.size
        if iw <= 0 or ih <= 0:
            return None
        skala = max(sw / float(iw), sh / float(ih))
        rw, rh = iw * skala, ih * skala
        oy = (sh - rh) * float(centering[1] if len(centering) > 1 else 0.5)
        y1 = oy + (float(twarz[1]) / 100.0) * rh
        y2 = oy + ((float(twarz[1]) + float(twarz[3])) / 100.0) * rh
        if y2 <= y1:
            return None
        return (max(0.0, y1), min(float(sh), y2))
    except Exception:
        return None

def _story_crop(img, centering=(0.5, 0.38)):
    """Zdjęcie pełnoklatkowo w kadrze 9:16 (crop-to-fill). exif_transpose = poprawny obrót
    (telefony zapisują poziome/pionowe z EXIF; bez tego lądują bokiem)."""
    img = ImageOps.exif_transpose(img.convert("RGB"))
    return ImageOps.fit(img, (SW, SH), method=Image.LANCZOS, centering=centering)


def _story_scrim(base, brand, frac=0.55, strength=1.0):
    """Delikatny gradient od dołu — czytelność tekstu. ZAWSZE CZARNY (niezależny od koloru
    tła marki), żeby nie robił się np. niebieski przy granatowym brandzie."""
    grad = Image.new("L", (1, SH), 0)
    for y in range(SH):
        t = max(0.0, (y - SH * (1 - frac)) / (SH * frac))
        grad.putpixel((0, y), int(255 * min(1.0, t ** 1.6) * strength))
    grad = grad.resize((SW, SH))
    solid = Image.new("RGBA", (SW, SH), (0, 0, 0, 255))
    solid.putalpha(grad)
    base.alpha_composite(solid)


def _story_scrim_top(base, brand, frac=0.32, strength=0.95):
    """Gradient od GÓRY — gdy tekst siedzi wyżej (slajdy neutralne). ZAWSZE CZARNY."""
    grad = Image.new("L", (1, SH), 0)
    for y in range(SH):
        t = max(0.0, (SH * frac - y) / (SH * frac))
        grad.putpixel((0, y), int(255 * min(1.0, t ** 1.5) * strength))
    grad = grad.resize((SW, SH))
    solid = Image.new("RGBA", (SW, SH), (0, 0, 0, 255))
    solid.putalpha(grad)
    base.alpha_composite(solid)


def _sklej_zdania(wiersze):
    """Skleja osobne wiersze w jeden akapit, dbając o kropki między zdaniami.
    ⛔ Wiersz kończący się już znakiem przestankowym (. ! ? … : ;) albo myślnikiem
    zostaje bez zmian; do reszty dokładamy kropkę. Gwiazdki akcentu (*słowo*) są
    przezroczyste dla tej reguły — sprawdzamy ostatni ZNACZĄCY znak."""
    out = []
    for w in [str(x).strip() for x in (wiersze or []) if str(x).strip()]:
        rdzen = w.rstrip("*").rstrip()
        if rdzen and rdzen[-1] not in ".!?…:;–—,":
            gwiazdki = len(w) - len(w.rstrip("*"))
            w = rdzen + "." + ("*" * gwiazdki)
        out.append(w)
    return " ".join(out).strip()


def _draw_pill(base, x, y, text, font, fill, text_col, pad_x=44, pad_y=24, max_w=None):
    """Jeden zaokrąglony 'przycisk' (CTA/akcent) — jedyny wypełniony element, przez co
    się wyróżnia; reszta tekstu leży bezpośrednio na zdjęciu.

    ⛔⛔⛔ TU SIEDZIAŁ „ROZJECHANY SLAJD 4". Bartek, 14.08: „Sprawdź kontrahenta na bla
    bla bla, dane z — i tutaj nie widać. To jest złe." Pigułka liczyła szerokość jako
    tekst + marginesy i rysowała ją BEZ ŻADNEGO OGRANICZENIA. Przy dłuższym wezwaniu
    („Sprawdź kontrahenta na finmach.pl — dane z live API, za darmo") prostokąt wychodził
    poza kadr 1080 px, a napis urywał się w połowie słowa. Odtworzone co do piksela.
    ⭐ Teraz pigułka ZAWIJA tekst do dostępnej szerokości i rośnie w dół, a nie w bok."""
    d = ImageDraw.Draw(base)
    if max_w is None:
        max_w = base.size[0] - 2 * x
    dost = max(80, int(max_w) - 2 * pad_x)
    linie = _wrap_plain(d, text, font, dost)
    lh = int(font.size * 1.22)
    tw = max(int(d.textlength(l, font=font)) for l in linie) if linie else 0
    h = lh * max(1, len(linie)) + 2 * pad_y - int(font.size * 0.22)
    w = min(int(max_w), tw + 2 * pad_x)
    r = min(h // 2, int(font.size * 0.9))
    d.rounded_rectangle([x, y, x + w, y + h], radius=r, fill=fill)
    yy = y + pad_y - int(font.size * 0.06)
    for l in linie:
        d.text((x + pad_x, yy), l, font=font, fill=text_col)
        yy += lh
    return w, h


def _story_progress(base, brand, idx, total, y=54):
    """Pasek postępu USUNIĘTY (decyzja s85, 2026-07-24): brak paska na górze stories.
    Zostawione jako no-op dla zgodności wywołań."""
    return
    # --- kod paska wyłączony (zostawiony na wypadek przywrócenia) ---
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
                 kicker=None, cta=None, twarz=None):
    """FORMAT 1 (jedno zdjęcie przez całą serię, spójny szablon): pasek postępu u góry
    (element stories) + statement (duży, bold, *akcent*) w DOLNEJ CZĘŚCI (nie na samym
    dole) + opcjonalna linia dopowiedzenia + opcjonalny CTA-pill. BEZ handla i kickera
    (to nie karuzela). Tekst z \\n: 1. linia = statement, reszta = dopowiedzenie."""
    base = Image.new("RGBA", (SW, SH), hex2rgb(brand.bg) + (255,))
    if photo is not None:
        _spc = _warm_grade(_story_crop(photo, centering=_kadr_wg_twarzy(twarz)))
        _pb = getattr(brand, "photo_blur", 0.0)
        if _pb and _pb > 0:
            _spc = _spc.filter(ImageFilter.GaussianBlur(_pb))
        base.paste(_spc, (0, 0))
        base = base.convert("RGBA")
        _ss2 = getattr(brand, "story_scrim", 1.0)
        _story_scrim(base, brand, frac=0.62, strength=min(1.0, 1.0 * _ss2))
        _story_scrim_top(base, brand, frac=0.18, strength=min(1.0, 0.6 * _ss2))
    d = ImageDraw.Draw(base)
    white, accent = hex2rgb(brand.white), hex2rgb(brand.accent)
    margin = 88
    max_w = SW - 2 * margin

    # _story_progress(base, brand, idx, total)  # USUNIĘTY s85 — bez paska postępu na górze

    raw = [l.strip() for l in str(text or "").split("\n") if l.strip()]
    # Ostatnia linia w gwiazdkach (*...*) = koralowy GUZIK CTA (jak w referencji Formatu 2).
    if cta is None and len(raw) > 1 and raw[-1].startswith("*") and raw[-1].endswith("*") \
            and len(raw[-1]) > 2:
        cta = raw[-1].strip("*").strip()
        raw = raw[:-1]
    statement = raw[0] if raw else ""
    # ⛔⛔ BRAKUJĄCE KROPKI. Bartek, 14.08: „koniec zdania: tylko produkt pod jeden konkretny
    # cel. I dalej: bierzesz dokładnie tyle. Gdzie jest kropka między «cel» a «bierzesz»?"
    # Generator daje każde zdanie w osobnym wierszu, a render sklejał je gołą spacją —
    # wychodziło „…która czeka nietknięta Po spłacie limit odnawia się sam". To nie jest
    # sprawa modelu, tylko sklejania: wiersz, który nie kończy się znakiem przestankowym,
    # dostaje kropkę, zanim doklei się następny.
    body = _sklej_zdania(raw[1:])

    # ⛔⛔ BIAŁE NAPISY BYŁY ZA MAŁE — I TO NIE BYŁ PRZYPADEK, TYLKO REGUŁA.
    # Bartek, 14.08: „te napisy są bardzo małe… im więcej jest tych napisów, tym one będą
    # mniejsze. To jest błędny mechanizm." Dokładnie tak to działało: tekst pomocniczy miał
    # sztywny sufit TRZECH WIERSZY, więc każde dłuższe zdanie zjeżdżało do 36 px, żeby się
    # w nich zmieścić. Zamiast tego pozwalamy tekstowi zająć więcej wierszy i trzymamy
    # czytelny stopień pisma: 44 px zamiast 36 na dole skali, 54 zamiast 48 na górze.
    # Kadr ma 1920 px wysokości — miejsca jest pod dostatkiem, brakowało tylko zgody.
    sf, sl, _ = _fit_rich(d, statement, brand.font_bold, 94, 62, 5, max_w=max_w)
    slh = int(sf.size * 1.13)
    s_h = slh * max(1, len(sl))
    bf = bl = None
    b_h = 0
    if body:
        bf, bl, _ = _fit_rich(d, body, brand.font_body, 54, 44, 6, max_w=max_w)
        b_h = int(bf.size * 1.34) * len(bl)
    cta_font = _f(brand.font_bold, 42)
    # ⭐ Pigułka zawija się teraz w wiele wierszy, więc jej wysokość trzeba policzyć,
    #    a nie zgadnąć — inaczej blok wychodzi poza dolną krawędź.
    cta_h = 0
    cta_linie = []
    if cta:
        cta_linie = _wrap_plain(d, cta, cta_font, max_w - 2 * 44)
        cta_h = int(cta_font.size * 1.22) * max(1, len(cta_linie)) + 48 - int(cta_font.size * 0.22)

    gap_body, gap_cta = 24, 40
    total_h = s_h + (gap_body + b_h if body else 0) + (gap_cta + cta_h if cta else 0)

    # dolna część kadru, ale uniesione znad samego dołu
    y = int(SH * 0.86) - total_h
    # ⛔ Sufit 0,44 wysokości powodował, że dłuższy blok po prostu wychodził poza dół kadru.
    #    Blok ma prawo wjechać wyżej — dopiero wtedy zaczyna brakować miejsca naprawdę.
    y = max(int(SH * 0.26), y)

    # ⭐⭐⭐ NAPIS SCHODZI Z TWARZY. Bartek, 14.08: „mapowanie twarzy się nie sprawdziło,
    #     ponieważ nachodzi bardzo mocno na napis. Na twarz nie powinno tak być."
    #     Ta sama reguła co w formacie „to «X» — dopóki…": rusza się NAPIS, nie zdjęcie.
    _tw = _twarz_na_kadrze(photo, twarz, centering=_kadr_wg_twarzy(twarz)) if photo is not None else None
    if _tw:
        f1, f2 = _tw
        luz = 34
        def _koliduje(yy):
            return not (yy + total_h <= f1 - luz or yy >= f2 + luz)
        if _koliduje(y):
            pod = int(f2 + luz)                       # najpierw: pod twarzą
            nad = int(f1 - luz - total_h)             # potem: nad twarzą
            if pod + total_h <= int(SH * 0.94):
                y = pod
            elif nad >= int(SH * 0.06):
                y = nad
            # gdy ani pod, ani nad się nie mieści — zostaje jak było; lepszy napis
            # na skraju twarzy niż napis ucięty poza kadrem.
    x = margin
    _sst = getattr(brand, "shadow_strength", 1.0)
    # FIX (ciemny akcent marki niewidoczny na zdjęciu ze scrimem): jeśli kolor akcentu
    # jest ciemny, słowa akcentowane dostają podświetlenie (pigułka akcent + biały tekst).
    _hl = _luma(accent) < 80
    _draw_rich(base, x, y, sl, sf, white, accent, slh, shadow=True, shadow_strength=_sst,
               hl_accent=_hl)
    y += s_h
    if body:
        y += gap_body
        _draw_rich(base, x, y, bl, bf, white, accent, int(bf.size * 1.34), shadow=True,
                   shadow_strength=_sst, hl_accent=_hl)
        y += b_h
    if cta:
        y += gap_cta
        _draw_pill(base, x, y, cta, cta_font, accent, (255, 255, 255), max_w=max_w)

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
    _sc = _ts()
    if _sc and _sc != 1.0:
        hi = max(8, int(round(hi * _sc)))
        lo = max(8, int(round(lo * _sc)))
        if hi < lo:
            hi = lo
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


# Warianty UKŁADU środkowych (neutralnych) slajdów native — tekst ROZRZUCONY po całej
# wysokości kadru z DUŻYMI, nierównymi odstępami (nie sklejony w jednym miejscu): raz
# nagłówek wysoko z dużą przerwą, raz zgrupowany niżej. Lewy margines wspólny (bez
# przesunięć w poziomie — jak w referencjach). start = górna krawędź 1. elementu jako
# ułamek wysokości; gaps = odstępy między kolejnymi elementami. Dobierane po numerze slajdu.
_NATIVE_LAYOUTS = [
    {"start": 0.30, "gaps": [46, 300, 90]},   # nagłówek wysoko, duża przerwa, tekst nisko
    {"start": 0.33, "gaps": [44, 96, 16]},    # wysoko, umiarkowany rozrzut
    {"start": 0.52, "gaps": [26, 22, 20]},    # zgrupowane niżej
    {"start": 0.28, "gaps": [230, 60, 40]},   # nagłówek b. wysoko, duża przerwa, grupa
    {"start": 0.40, "gaps": [60, 180, 30]},   # środek, rozrzut
    {"start": 0.35, "gaps": [280, 46, 16]},   # wysoko, wielka przerwa, para nisko
    {"start": 0.47, "gaps": [30, 210, 24]},   # środek, boks blisko, przerwa, tekst nisko
]


def render_story_native(brand, photo, lines, out_dir, idx=1, zone="bottom", layout=None, seed=0, twarz=None):
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
        _npc = _story_crop(photo, centering=_kadr_wg_twarzy(twarz))
        _pb = getattr(brand, "photo_blur", 0.0)
        if _pb and _pb > 0:
            _npc = _npc.filter(ImageFilter.GaussianBlur(_pb))
        base.paste(_npc, (0, 0))
        base = base.convert("RGBA")
    top_anchor = (zone == "full")
    _ss3 = getattr(brand, "story_scrim", 1.0)
    if has_photo:
        if top_anchor:
            _story_scrim_top(base, brand, frac=0.66, strength=min(1.0, 0.9 * _ss3))
            _story_scrim(base, brand, frac=0.30, strength=min(1.0, 0.45 * _ss3))
        else:
            _story_scrim(base, brand, frac=0.70, strength=min(1.0, 0.95 * _ss3))
    else:
        _story_scrim(base, brand, frac=1.0, strength=min(1.0, 0.5 * _ss3))
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
            # NAGŁÓWEK — krój display (head)
            f, w = _fit_plain(d, txt, brand.font_bold, 84, 56, 3, max_w)
            lh = int(f.size * 1.12)
            h = lh * len(w)
        elif kind == "plain":
            # tekst bez tła — krój tekstowy (body), lżejszy kontrast z nagłówkiem
            f, w = _fit_plain(d, txt, brand.font_body, 56, 42, 3, max_w)
            lh = int(f.size * 1.18)
            h = lh * len(w)
        elif kind == "accent":
            # koralowy boks/CTA — krój display (head), mocny akcent
            f, w = _fit_plain(d, txt, brand.font_bold, 54, 40, 3, inner)
            lh = int(f.size * 1.12)
            h = lh * len(w) + 2 * 20
        else:  # box — krój tekstowy (body)
            f, w = _fit_plain(d, txt, brand.font_body, 54, 40, 3, inner)
            lh = int(f.size * 1.2)
            h = lh * len(w) + 2 * 20
        els.append((kind, w, f, lh, h))

    base_gap = 22
    n = len(els)
    sum_h = sum(e[4] for e in els)
    lay = layout or {}
    gaps = [base_gap] * max(0, n - 1)
    xoff = [0] * n
    if not top_anchor:
        # HOOK / CTA (zdjęcie z osobą): blok zakotwiczony GÓRĄ na stałej wysokości, żeby
        # 1. slajd i CTA były na tej samej (wyższej) wysokości; wciąż poniżej twarzy.
        total_h = sum_h + sum(gaps)
        y = int(SH * 0.56)
        y = min(y, SH - total_h - int(SH * 0.05))
        y = max(int(SH * 0.40), y)
    else:
        # SLAJD ŚRODKOWY (zdjęcie neutralne): ROZRZUCENIE po kadrze — duże, nierówne odstępy.
        # Wariant losowany deterministycznie per KARTA (seed) + numer slajdu, żeby układ
        # był RÓŻNY z karty na kartę (nie dwa sztywne wzory), zawsze premium i czytelny.
        if not lay:
            lay = _NATIVE_LAYOUTS[(seed + idx) % len(_NATIVE_LAYOUTS)]
        gaps = [int(v) for v in lay.get("gaps", [])][: max(0, n - 1)]
        if len(gaps) < n - 1:
            gaps += [90] * (n - 1 - len(gaps))
        if n >= 2 and els[-1][0] == "accent":
            gaps[-1] = 16  # akcent/CTA blisko poprzedniego elementu
        total_h = sum_h + sum(gaps[: n - 1])
        y = int(SH * lay.get("start", 0.34))
        y = min(y, SH - total_h - int(SH * 0.07))
        y = max(int(SH * 0.06), y)

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
        else:  # box (zawsze biały; koral zarezerwowany dla *akcentu*)
            _story_textbox(base, xx, y, w, f, (255, 255, 255, 255), ink)
            y += h
        if i < n - 1:
            y += gaps[i]

    os.makedirs(out_dir, exist_ok=True)
    fp = os.path.join(out_dir, f"story_{idx:02d}.png")
    base.convert("RGB").save(fp, "PNG")
    return fp


def render_stories(brand, items, out_dir, photos=None, seed=None, twarze=None):
    """items: lista dictów {'text', 'format'('tip'|'native'), 'photo'} albo str.
    format 'native' -> render_story_native (linie tekstu rozdzielone \\n; *linia* = akcent).
    format 'tip' (domyślny) -> render_story (zdjęcie + tekst brandowy nisko).
    photos: rotacja zdjęć. seed = wariacja układu native per KARTA (domyślnie z nazwy job).
    Zwraca listę ścieżek PNG."""
    os.makedirs(out_dir, exist_ok=True)
    if seed is None:
        # stabilny hash z nazwy joba -> ten sam układ przy re-renderze, różny między kartami
        seed = 0
        for c in os.path.basename(os.path.normpath(out_dir)):
            seed = (seed * 31 + ord(c)) % 100000
    photos = photos or []
    # ⭐ Ramki twarzy idą RÓWNOLEGLE do zdjęć — ta sama rotacja, ten sam indeks.
    #    Pusta lista = zachowanie jak dotąd (kadr na sztywno), więc nic się nie psuje.
    twarze = twarze or []
    paths = []
    for i, it in enumerate(items, start=1):
        ph, fmt, zone = None, "tip", None
        look = None
        if isinstance(it, dict):
            text = it.get("text", "")
            ph = it.get("photo")
            fmt = (it.get("format") or "tip").strip().lower()
            zone = (it.get("zone") or "").strip().lower() or None
            look = it.get("look")
        else:
            text = str(it)
        eff = _apply_look(brand, look or {})
        _ctx.text_scale = getattr(eff, "text_scale", 1.0)
        tw = it.get("twarz") if isinstance(it, dict) else None
        if ph is None and photos:
            k = (i - 1) % len(photos)
            ph = photos[k]
            if tw is None and k < len(twarze):
                tw = twarze[k]
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
            paths.append(render_story_native(eff, ph, lines, out_dir, idx=i, zone=zone, seed=seed, twarz=tw))
        else:
            paths.append(render_story(eff, ph, text, out_dir, idx=i, zone=zone, twarz=tw))
    _ctx.text_scale = 1.0
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
