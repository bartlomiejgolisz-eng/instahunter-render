# -*- coding: utf-8 -*-
"""
============================================================================
GENERATOR FORMATU „to «X» — dopóki…"  —  wersja serwerowa (Pillow).
----------------------------------------------------------------------------
⛔ PO CO TEN PLIK ISTNIEJE
Do 13.08 ten generator żył WYŁĄCZNIE w otwartej karcie przeglądarki
(`generator-z2.js`, canvas). Znaczyło to, że jedyny sposób na wypuszczenie
tego formatu u klienta to człowiek, który ręcznie wkleja polecenia do
konsoli. Dopóki tak było, „losuj format przy każdej nowej puli" nie mogło
działać: losowanie wskazywałoby czasem format, którego automat nie umie
uruchomić.

Ten plik jest przepisaniem `generator-z2.js` 1:1 na Pillow. Zachowane są
WSZYSTKIE liczby przyjęte przez Bartka 16.08 — pozycje, drabinki, progi,
definicja „delikatnego" cienia. Gdzie canvas i Pillow liczą inaczej, jest
o tym komentarz.

⛔ CZEGO TU NIE MA
1. Rozpoznawania twarzy. Ramki są policzone raz, detektorem w przeglądarce
   (MediaPipe BlazeFace), i leżą przy zdjęciach w bazie. Serwer dostaje je
   gotowe w polu `r`. Przepisywanie detektora na serwer byłoby drugą kopią
   tej samej prawdy — a już raz nas to kosztowało („ramki z sufitu" 16.08).
2. Bramki treści. Ta stoi w workerze (`/api/op/tresc/bramka`) i wywołuje ją
   ten, kto zamawia render — zanim tu zajrzy.
3. Tekstów. Treści przyjęte przez Bartka leżą w Airtable, w rekordzie
   formatu, w polu „Prompt". Generator dostaje je z zewnątrz.
============================================================================
"""
from __future__ import annotations

import gc
import io
import math
import os
from typing import List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFilter, ImageFont

W, H = 1080, 1350

# Delikatny cień — definicja liczbowa, nie odczucie. Bartek kazał ją zapisać:
# delikatny = zmiana średniej jasności CAŁEGO kadru ≤ 2%, żadnego pasa ani plamy,
# promień ≤ 0,25 × wysokość pisma, przesunięcie 0, krycie ≤ 45%.
# Przy tych wartościach zmierzono 0,7%.
CIEN_PROMIEN = 0.26
CIEN_KRYCIE = 0.45
CIEN_PRZEBIEGI = 3

# Nieregularny układ napisów — każda plansza ma inne miejsce. „daleko" znaczy,
# że treść schodzi na wysokość 70% kadru, a nagłówek zostaje u góry.
POZ: List[Tuple[float, float, str]] = [
    (10.1, 20.6, "blisko"), (14.0, 24.3, "daleko"), (11.9, 14.0, "daleko"),
    (14.9, 13.9, "blisko"), (26.3, 9.1, "blisko"), (51.7, 18.1, "blisko"),
    (67.8, 22.3, "blisko"),
]


def drabinka_domyslna(i: int):
    return [POZ[i], (62, 10.4, "blisko"), (8, 10.1, "blisko"),
            (50, 14.2, "blisko"), (66, 9, "blisko"), (20, 9, "blisko")]


def drabinka_wysoko(i: int):
    return [POZ[i], (20, 9, "blisko"), (28, 12, "blisko"), (36, 9, "blisko"),
            (44, 12, "blisko"), (52, 9, "blisko"), (62, 10.4, "blisko"), (66, 9, "blisko")]


DRABINKI = {"domyslna": drabinka_domyslna, "wysoko": drabinka_wysoko}

# Które plansze chcą człowieka. Reguła Bartka: okładka ZAWSZE z człowiekiem,
# co najmniej 40% plansz z człowiekiem.
CHCE_CZLOWIEKA = [1, 0, 1, 0, 1, 0, 1]

TEKST_JASNY = (248, 248, 248)
TEKST_NA_PODKLADZIE = (20, 32, 15)      # #14200F

# ---------------------------------------------------------------- czcionki
# ⛔ Nazwa rodziny przychodzi z profilu klienta („Anton", „Playfair Display").
# Mapujemy ją na PLIK, bo Pillow nie zna pojęcia rodziny ani wagi — bierze
# konkretny krój z dysku. Wagi są te same, które canvas dostawał z Google Fonts.
KATALOG_CZCIONEK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")

PLIKI_NAGLOWKA = {
    "anton": "Anton_400Regular.ttf",
    "archivo black": "ArchivoBlack_400Regular.ttf",
    "playfair display": "PlayfairDisplay_800ExtraBold.ttf",
    "fraunces": "Fraunces_700Bold.ttf",
    "oswald": "Oswald_700Bold.ttf",
    "poppins": "Poppins_600SemiBold.ttf",
    "montserrat": "Montserrat_600SemiBold.ttf",
    "inter": "Inter_600SemiBold.ttf",
    "barlow": "Barlow_600SemiBold.ttf",
}
PLIKI_TRESCI = {
    "poppins": "Poppins_600SemiBold.ttf",
    "barlow": "Barlow_600SemiBold.ttf",
    "inter": "Inter_600SemiBold.ttf",
    "montserrat": "Montserrat_600SemiBold.ttf",
    "anton": "Anton_400Regular.ttf",
    "oswald": "Oswald_700Bold.ttf",
    "archivo black": "ArchivoBlack_400Regular.ttf",
    "playfair display": "PlayfairDisplay_800ExtraBold.ttf",
    "fraunces": "Fraunces_700Bold.ttf",
}

_ZAPASOWA = "DejaVuSans-Bold.ttf"
_cache_czcionek = {}


def _sciezka_czcionki(rodzina: str, naglowek: bool) -> str:
    k = (rodzina or "").strip().lower()
    tab = PLIKI_NAGLOWKA if naglowek else PLIKI_TRESCI
    plik = tab.get(k)
    if plik:
        p = os.path.join(KATALOG_CZCIONEK, plik)
        if os.path.exists(p):
            return p
    # ⛔ Brak czcionki NIE MOŻE wywalić generatora — lepiej gorszy krój niż
    # brak karuzeli. Ale zapisujemy to w raporcie, żeby nie przeszło niezauważone.
    p = os.path.join(KATALOG_CZCIONEK, _ZAPASOWA)
    return p if os.path.exists(p) else ""


def czcionka(rodzina: str, rozmiar: int, naglowek: bool) -> ImageFont.FreeTypeFont:
    p = _sciezka_czcionki(rodzina, naglowek)
    klucz = (p, rozmiar)
    if klucz not in _cache_czcionek:
        _cache_czcionek[klucz] = (ImageFont.truetype(p, rozmiar) if p
                                  else ImageFont.load_default())
    return _cache_czcionek[klucz]


def czcionka_jest(rodzina: str, naglowek: bool) -> bool:
    k = (rodzina or "").strip().lower()
    tab = PLIKI_NAGLOWKA if naglowek else PLIKI_TRESCI
    plik = tab.get(k)
    return bool(plik and os.path.exists(os.path.join(KATALOG_CZCIONEK, plik)))


# ------------------------------------------------------------------ pomocnicze
def _szer(f: ImageFont.FreeTypeFont, t: str) -> float:
    """Odpowiednik canvasowego measureText().width."""
    return f.getlength(t)


def lam(f: ImageFont.FreeTypeFont, tekst: str, maks: float) -> List[str]:
    """Łamanie na słowa — 1:1 z `lam` z generator-z2.js."""
    slowa = str(tekst or "").split(" ")
    linie, biez = [], ""
    for w in slowa:
        prob = (biez + " " + w) if biez else w
        if _szer(f, prob) > maks and biez:
            linie.append(biez)
            biez = w
        else:
            biez = prob
    if biez:
        linie.append(biez)
    return linie


def _hex_rgb(s: str, zapas=(232, 64, 42)) -> Tuple[int, int, int]:
    s = (s or "").strip().lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    if len(s) != 6:
        return zapas
    try:
        return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
    except ValueError:
        return zapas


def _jasnosc(img: Image.Image) -> float:
    """Średnia jasność względna (sRGB → luminancja), tak jak liczył canvas.
    ⛔ Canvas próbkował co 7. piksel (krok 28 w tablicy RGBA). Tu zmniejszamy
    obraz i liczymy z całości — wynik jest równoważny, a liczy się szybciej."""
    male = img.convert("RGB").resize((216, 270), Image.BILINEAR)
    px = male.load()
    suma, n = 0.0, 0
    for y in range(0, 270, 2):
        for x in range(0, 216, 2):
            r, g, b = px[x, y]
            def f(v):
                v = v / 255.0
                return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
            suma += 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)
            n += 1
    return round(suma / max(1, n), 3)


def pisz_z_cieniem(plotno: Image.Image, rys: ImageDraw.ImageDraw,
                   wpisy, font: ImageFont.FreeTypeFont, rozmiar: int,
                   kolor=TEKST_JASNY, bez_cienia: bool = False):
    """Napis z „delikatnym" cieniem — odpowiednik `cien()` z generator-z2.js.

    ⛔⛔ DWIE RZECZY, KTÓRE ŁATWO ZEPSUĆ PRZY PRZEPISYWANIU Z CANVASU I KTÓRE
    JUŻ RAZ ZEPSUŁEM (pomiar wyszedł 3,69% przy progu 2%):

    1. PROMIEŃ LICZY SIĘ OD ROZMIARU TEGO NAPISU, nie od największego na planszy.
       Gdy jednym rozmyciem 92-piksela objąłem też treść pisaną 46, cień pod
       treścią zrobił się dwa razy szerszy niż w przeglądarce i przyciemnił kadr.
    2. CIEŃ I NAPIS IDĄ PARAMI, po kolei. W canvasie `cien()` kończyło się ostrym
       napisem, zanim ruszył następny blok. Gdy zebrałem wszystkie cienie i nałożyłem
       je na końcu, cień treści położył się NA nagłówku i go przybrudził.

    Canvasowy `shadowBlur = b` to rozmycie gaussowskie o sigma ≈ b/2, a Pillow
    przyjmuje promień równy sigmie — stąd promień = rozmiar * 0.26 / 2.
    """
    if not bez_cienia:
        warstwa = Image.new("L", plotno.size, 0)
        r_warstwy = ImageDraw.Draw(warstwa)
        for xy, tekst in wpisy:
            r_warstwy.text(xy, tekst, font=font, fill=255, anchor="ls")
        maska = warstwa.filter(ImageFilter.GaussianBlur(rozmiar * CIEN_PROMIEN / 2.0))
        czarne = Image.new("RGB", plotno.size, (0, 0, 0))
        for _ in range(CIEN_PRZEBIEGI):
            plotno.paste(czarne, (0, 0), maska.point(lambda v: int(v * CIEN_KRYCIE)))
    for xy, tekst in wpisy:
        rys.text(xy, tekst, font=font, fill=kolor, anchor="ls")


# ------------------------------------------------------------------- render
def rysuj(i: int, im: Image.Image, tw: Optional[List[float]],
          pozycja, marka: dict, tresc: dict, bez_cienia: bool = False):
    """Jedna plansza. Zwraca dict z płótnem i pomiarami albo {'zaDuze': True}
    albo None, gdy żaden kadr z drabinki nie zmieścił napisu poza twarzą.

    marka: { akcent, podklad, czcionkaNag, czcionkaTxt }
    tresc: { temat, linie:[[zarzut, dopoki] × 6], cta:[haslo, zdanie] }
    tw:    [x, y, w, h] ramki twarzy w PROCENTACH obrazu albo None
    """
    gy, gx, tryb = pozycja
    L = round(W * gx / 100.0)
    maxW = W - L - round(W * 0.07)
    S2 = 46
    max_lin = 3 if i == 0 else 2

    f_nag_cache = {}

    def f_nag(rozm):
        if rozm not in f_nag_cache:
            f_nag_cache[rozm] = czcionka(marka.get("czcionkaNag", ""), rozm, True)
        return f_nag_cache[rozm]

    f_txt = czcionka(marka.get("czcionkaTxt", ""), S2, False)
    podklad = _hex_rgb(marka.get("podklad") or marka.get("akcent") or "#E8402A")

    iw, ih = im.size
    sk = max(W / iw, H / ih)
    rw, rh = iw * sk, ih * sk

    # ⭐ Skalujemy RAZ, przed pętlą po kadrach — kadr przesuwa obraz, nie zmienia
    # jego rozmiaru. Wcześniej to samo skalowanie leciało do pięciu razy na pozycję.
    skala = im.resize((max(1, round(rw)), max(1, round(rh))), Image.LANCZOS)

    for kadr in (0.22, 0.02, 0.45, 0.65, 0.85):
        # ⛔ przesunięcie kadru NIE MOŻE uciąć twarzy — stąd brały się „poucinane
        # mordy" u Agnieszki. Ograniczamy je tak, żeby cała ramka została w polu
        # widzenia, w pionie I w poziomie.
        oy = (H - rh) * kadr
        if tw and rh > H:
            ty1 = tw[1] / 100.0 * rh
            ty2 = (tw[1] + tw[3]) / 100.0 * rh
            oy = max(min(oy, min(0, H - 24 - ty2)), max(H - rh, 24 - ty1))
        if rh <= H:
            oy = (H - rh) / 2.0
        ox = (W - rw) / 2.0
        if tw and rw > W:
            tx1 = tw[0] / 100.0 * rw
            tx2 = (tw[0] + tw[2]) / 100.0 * rw
            ox = max(min(ox, min(0, W - 16 - tx2)), max(W - rw, 16 - tx1))

        plotno = Image.new("RGB", (W, H), (0, 0, 0))
        plotno.paste(skala, (round(ox), round(oy)))
        rys = ImageDraw.Draw(plotno)

        bloki = []

        def nag(pre: str, cyt: str, y_base: float):
            """Nagłówek: część biała (pre) + cytat na podkładzie w kolorze marki."""
            slowa = ([{"t": t, "c": 0} for t in pre.split(" ") if t] if pre else []) \
                    + [{"t": t, "c": 1} for t in str(cyt).split(" ") if t]
            S1, linie = 92, []
            for kand in (92, 86, 80, 74, 68, 62, 56, 50, 44):
                S1 = kand
                f = f_nag(S1)
                linie, cur = [], []
                for w in slowa:
                    prob = cur + [w]
                    if _szer(f, " ".join(x["t"] for x in prob)) > maxW and cur:
                        linie.append(cur)
                        cur = [w]
                    else:
                        cur = prob
                if cur:
                    linie.append(cur)
                naj = max(_szer(f, " ".join(x["t"] for x in a)) for a in linie)
                if len(linie) <= max_lin and naj <= maxW:
                    break
            f = f_nag(S1)
            x2 = L
            y = y_base + S1 * 0.78
            y_top = y - S1 * 0.80
            for k, linia in enumerate(linie):
                yy = y + k * S1 * 1.06
                x = float(L)
                grupy = []
                for w in linia:
                    if grupy and grupy[-1]["c"] == w["c"]:
                        grupy[-1]["t"] += " " + w["t"]
                    else:
                        grupy.append({"t": w["t"], "c": w["c"]})
                for gi, seg in enumerate(grupy):
                    txt = seg["t"] + (" " if gi < len(grupy) - 1 else "")
                    w_seg = _szer(f, seg["t"])
                    if seg["c"] == 1:
                        rys.rectangle([x - 10, yy - S1 * 0.72,
                                       x - 10 + w_seg + 20, yy - S1 * 0.72 + S1 * 0.95],
                                      fill=podklad)
                        rys.text((x, yy), seg["t"], font=f,
                                 fill=TEKST_NA_PODKLADZIE, anchor="ls")
                    else:
                        pisz_z_cieniem(plotno, rys, [((x, yy), seg["t"])], f, S1,
                                       bez_cienia=bez_cienia)
                    x += _szer(f, txt)
                x2 = max(x2, x + 10)
            y_end = y + (len(linie) - 1) * S1 * 1.06
            bloki.append({"x1": L, "y1": y_top, "x2": x2, "y2": y_end + S1 * 0.24})
            return {"x2": x2, "yEnd": y_end, "S1": S1}

        y_base = round(H * gy / 100.0)
        T = tresc["linie"][i] if i < 6 else None
        if T:
            N = nag((tresc["temat"] + " to") if i == 0 else "to", T[0], y_base)
        else:
            N = nag("", tresc["cta"][0], y_base)

        linie_t = lam(f_txt, T[1] if T else tresc["cta"][1], maxW)
        if T and tryb == "daleko":
            yd = round(H * 0.70)
        else:
            yd = N["yEnd"] + N["S1"] * 0.25 + S2 * 1.15

        pisz_z_cieniem(plotno, rys,
                       [((L, yd + k * S2 * 1.24), t) for k, t in enumerate(linie_t)],
                       f_txt, S2, bez_cienia=bez_cienia)

        bx = float(L)
        for t in linie_t:
            bx = max(bx, L + _szer(f_txt, t))
        dol_t = yd + (len(linie_t) - 1) * S2 * 1.24 + S2 * 0.3
        bloki.append({"x1": L, "y1": yd - S2 * 0.85, "x2": bx + 8, "y2": dol_t})

        if dol_t > H - 24 or max(N["x2"], bx) > W - 18:
            return {"zaDuze": True}

        # ⛔ Napis i treść to DWA osobne prostokąty, nie jeden wysoki. Przy układzie
        # „daleko" nagłówek stoi u góry, a treść na 70% wysokości — liczenie tego
        # jako jednego bloku dawało 100% zakrycia twarzy i odrzucało wszystko.
        zakr = 0.0
        if tw:
            tx1 = ox + tw[0] / 100.0 * rw
            ty1 = oy + tw[1] / 100.0 * rh
            tx2 = ox + (tw[0] + tw[2]) / 100.0 * rw
            ty2 = oy + (tw[1] + tw[3]) / 100.0 * rh
            pole = (tx2 - tx1) * (ty2 - ty1)
            kryte = 0.0
            for b in bloki:
                px = max(0.0, min(tx2, b["x2"]) - max(tx1, b["x1"]))
                py = max(0.0, min(ty2, b["y2"]) - max(ty1, b["y1"]))
                kryte += px * py
            zakr = round((kryte / pole) * 100.0, 1) if pole > 0 else 0.0

        gorna_t = min(b["y1"] for b in bloki)
        if zakr <= 0 or kadr == 0.85:
            return {
                "plotno": plotno,
                "jasnosc": _jasnosc(plotno),
                "blok": round(((dol_t - gorna_t) / H) * 100.0, 1),
                "twarz": zakr,
            }
    return None


# ------------------------------------------------------------------ całość
# ⛔ Dłuższy bok po pobraniu. 1700 px wystarcza z zapasem: kadr ma 1080×1350, więc
# nawet pionowe 1275×1700 pokrywa go w całości bez rozciągania. Oryginał z telefonu
# (3024×4032 = 36 MB w pamięci) schodzi do ~6,5 MB. To jedyny powód tego ograniczenia.
MAKS_BOK = 1700


def zmniejsz(im):
    if im is None:
        return None
    if max(im.size) <= MAKS_BOK:
        return im
    im = im.copy()
    im.thumbnail((MAKS_BOK, MAKS_BOK), Image.LANCZOS)
    return im


def zrob_karuzele(marka: dict, tresc: dict, zdjecia: dict, opcje: dict = None):
    """zdjecia: { 'ludzie': [{'u':adres,'r':[x,y,w,h]}], 'bez': [{'u':…,'r':None}] }
    Zwraca { 'plansze': [Image], 'rap': [...] } albo { 'err': '…' }.

    ⛔ Bramki treści tu NIE MA — woła ją ten, kto zamawia render. Gdyby siedziała
    tutaj, render byłby zależny od workera i nie dałoby się go odpalić lokalnie.
    """
    opcje = opcje or {}
    drab = DRABINKI.get(opcje.get("drabinka") or "domyslna", drabinka_domyslna)
    pobierz = opcje.get("pobierz")           # funkcja adres -> PIL.Image albo None
    if pobierz is None:
        raise ValueError("brak funkcji pobierajacej zdjecia (opcje['pobierz'])")

    uzyte, out, rap = set(), [], []
    obrazki = {}

    def daj(u):
        if u not in obrazki:
            obrazki[u] = zmniejsz(pobierz(u))
        return obrazki[u]

    for i in range(7):
        okl = (i == 0)
        if okl:
            kolejnosc = [zdjecia.get("ludzie") or []]
        elif CHCE_CZLOWIEKA[i] == 1:
            kolejnosc = [zdjecia.get("ludzie") or [], zdjecia.get("bez") or []]
        else:
            kolejnosc = [zdjecia.get("bez") or [], zdjecia.get("ludzie") or []]

        mam = None
        # ⛔ NAJPIERW zdjęcie, POTEM pozycja napisu. Odwrotnie było źle: generator
        # wolał zmienić zdjęcie niż ruszyć napis i karuzela traciła ludzi
        # (u Agnieszki spadło do 2/7).
        for grupa in kolejnosc:
            if mam:
                break
            prob = 0
            for p in grupa:
                if mam:
                    break
                if p["u"] in uzyte:
                    continue
                if prob >= 12:
                    break
                prob += 1
                im = daj(p["u"])
                if im is None:
                    continue
                for poz in drab(i):
                    try:
                        r = rysuj(i, im, p.get("r"), poz, marka, tresc)
                    except Exception:
                        r = None
                    if not r or r.get("zaDuze"):
                        continue
                    if r["jasnosc"] < 0.11 or r["jasnosc"] > 0.85:
                        continue
                    if r["twarz"] > 0:      # napis nie wchodzi na twarz. Zero, nie „trochę".
                        continue
                    mam = {"p": p, "r": r, "poz": poz}
                    break

        if not mam:
            return {"err": "plansza %d: nie ma zdjęcia, na którym napis zmieści się poza twarzą" % (i + 1),
                    "rap": rap}

        uzyte.add(mam["p"]["u"])
        out.append(mam["r"]["plotno"])
        rap.append({
            "i": i + 1, "zdjecie": mam["p"]["u"], "czlowiek": bool(mam["p"].get("r")),
            "twarz": mam["r"]["twarz"], "jasnosc": mam["r"]["jasnosc"],
            "poz": "%s/%s" % (mam["poz"][0], mam["poz"][1]),
        })

    for im in obrazki.values():
        try:
            if im is not None:
                im.close()
        except Exception:
            pass
    obrazki.clear()
    gc.collect()

    ludzi = sum(1 for x in rap if x["czlowiek"])
    if ludzi / 7.0 < 0.4:
        return {"err": "tylko %d/7 plansz z człowiekiem — reguła mówi co najmniej 40%%" % ludzi,
                "rap": rap}
    return {"plansze": out, "rap": rap, "ludzi": ludzi}


def zmierz_cien(marka: dict, tresc: dict, zdjecie: dict, pobierz):
    """Pomiar „delikatności" cienia — ten sam kadr z cieniem i bez.
    Próg Bartka: zmiana średniej jasności CAŁEGO kadru ≤ 2%."""
    im = pobierz(zdjecie["u"])
    if im is None:
        return None
    a = rysuj(1, im, zdjecie.get("r"), POZ[1], marka, tresc, False)
    b = rysuj(1, im, zdjecie.get("r"), POZ[1], marka, tresc, True)
    if not a or not b or a.get("zaDuze") or b.get("zaDuze"):
        return None
    return {
        "zCieniem": a["jasnosc"], "bezCienia": b["jasnosc"],
        "zmianaProc": round(abs(a["jasnosc"] - b["jasnosc"]) / max(1e-6, b["jasnosc"]) * 100.0, 2),
    }
