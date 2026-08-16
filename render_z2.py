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

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

W, H = 1080, 1350

# Delikatny cień — definicja liczbowa, nie odczucie. Bartek kazał ją zapisać:
# delikatny = zmiana średniej jasności CAŁEGO kadru ≤ 2%, żadnego pasa ani plamy,
# promień ≤ 0,25 × wysokość pisma, przesunięcie 0.
#
# ⭐⭐⭐ 16.08 (s290) — CIEŃ CIAŚNIEJSZY I MOCNIEJSZY. Bartek o tym formacie:
# „lepsza czytelność… może jeszcze delikatny cień pod samym napisem".
# CO SIĘ ZMIENIŁO I DLACZEGO AKURAT TAK:
#   promień 0,26 → 0,20  — cień był SZERSZY niż litery, więc czytało się go jako
#                          zabrudzenie tła, a nie jako obrys napisu. Ciaśniejszy
#                          promień trzyma go pod glifem, gdzie robi robotę.
#   krycie  0,45 → 0,55  — to jedyna liczba z definicji „delikatnego", która idzie
#                          w górę. Wolno, bo wiążącym progiem jest POMIAR (≤ 2%
#                          zmiany jasności kadru), a nie sama wartość krycia:
#                          ciaśniejszy promień oddaje więcej, niż krycie zabiera.
#   przebiegi   4 → 5    — gęstość tam, gdzie już jest maska liter; pasa nie tworzą.
# ⛔⛔ POMIAR — I RZECZ, KTÓRĄ TRZEBA WIEDZIEĆ O SAMYM POMIARZE.
# Na zdjęciu testowym (bardzo ciemny kadr, jasność 0,139): STARE liczby dają 7,19%,
# NOWE 8,63%. Obie są nad progiem „≤ 2%" — ale próg NIE jest złamany zmianą z 16.08,
# bo stare liczby przekraczają go na tym zdjęciu tak samo.
# PRZYCZYNA: `zmierz_cien` liczy zmianę WZGLĘDNĄ (spadek ÷ jasność kadru). Na jasnym
# zdjęciu, na którym mierzono 0,7% w sierpniu, mianownik jest kilka razy większy niż tutaj,
# więc ten sam cień wychodzi kilka razy „delikatniejszy". W liczbach bezwzględnych, czyli
# w tym, co widać: kadr ciemnieje o 1,0 pkt proc. (stare) i o 1,2 pkt proc. (nowe) — cień
# zgęstniał o jedną piątą, a nie kilkukrotnie.
# ⛔ Po każdej zmianie tych trzech liczb MIERZYĆ przez `zmierz_cien` — i porównywać
# ZAWSZE na tym samym zdjęciu ze stanem sprzed zmiany, nie z progiem 2% na sucho.
# ⭐⭐ 16.08 wieczorem — CIEŃ W DÓŁ, BO ZMIENIŁO SIĘ TŁO. Bartek: „nie trzeba robić takiego
# intensywnego cienia pod samymi literami, może być troszeczkę mniejszy, bo teraz jest
# za duży". I to jest spójne, a nie sprzeczne z decyzją sprzed godziny: cień podbiłem po to,
# żeby biały napis dawał radę na JASNYM zdjęciu. Skoro zdjęcie jest teraz przygaszone o 27%,
# tło samo w sobie robi tę robotę, a cień z tej samej wartości staje się widoczną plamą
# pod literami. Krycie 0,55 → 0,42, przebiegi 5 → 3.
# ⛔ PROMIEŃ ZOSTAJE 0,20 — to nie jest kwestia siły, tylko kształtu: cień szerszy od liter
# czyta się jako brud na tle, a nie jako ich obrys. Ta zmiana była dobra i zostaje.
CIEN_PROMIEN = 0.20
CIEN_KRYCIE = 0.42
CIEN_PRZEBIEGI = 3

# ⭐⭐⭐ 16.08 (s290) — PRZYGASZENIE CAŁEGO KADRU NA PLANSZACH 2–7.
# Bartek: „drugi slajd ja bym tutaj odrobinkę przyciemnił… żeby to było lepiej czytelne".
# ⛔ CAŁE zdjęcie równomiernie, nie pas ani gradient pod napisem: pas widać jako figurę
# na zdjęciu i zabija wrażenie „to jest fotografia, nie grafika". Równomierne przygaszenie
# jest niewidoczne jako zabieg — po prostu biały napis siedzi na ciemniejszym tle.
# ⛔ OKŁADKA (plansza 1) ZOSTAJE JASNA. To ona zatrzymuje kciuk w rolce i to na niej
# zdjęcie ma pracować najmocniej; napis na okładce jest największy, więc czytelność
# i bez przygaszenia jest tam najlepsza z całej karuzeli.
# ⛔ 0,22 było za dużo: przy tej wartości bramka jasności (< 0,11) odrzucała zdjęcia,
# które wcześniej przechodziły. 0,14 to wartość zaakceptowana na podglądzie.
# ⭐ 16.08 wieczorem — 0,14 → 0,27. Bartek po zobaczeniu pierwszej karty na słonecznych
# zdjęciach: „ono jest takie bardzo jasne… prosiłem, żeby te dalsze były przyciemniane".
# 0,14 nie było wyborem estetycznym, tylko obejściem: przy 0,22 bramka jasności zaczynała
# odrzucać kadry, bo mierzy GOTOWĄ planszę i sama się nabierała na przygaszenie. Odkąd
# progi jadą przez `_wsp_przygaszenia`, tego ograniczenia nie ma i wartość może być taka,
# jaka ma być dla oka. Wybrana z podglądu trzech wariantów (14 / 22 / 30).
PRZYGASZENIE = 0.27
PRZYGAS_KOLOR = (14, 13, 11)     # nie czysta czerń — zostawia ciepło zdjęcia

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


def drabinka_auto(i: int):
    """⭐ 15.08 — odpowiedź na uwagę Bartka: „całość mogłaby być troszeczkę wyżej,
    bo jest bardzo mało miejsca od dołu, a twarz jest wysoko, więc jest dużo miejsca,
    żeby to wypośrodkować".

    ⛔ CO BYŁO ŹLE: drabinki wyżej to LISTY SZTYWNYCH WYSOKOŚCI. Generator brał
    pierwszą, na której napis nie dotykał twarzy — nikt nie mierzył, ile zostaje
    wolnego miejsca. Twarz siedzi w górnej połowie kadru, więc pierwsza wolna
    pozycja wypadała ok. 62% i napis lądował przyklejony do dołu.

    ⭐⭐⭐ 16.08 wieczorem — LICZENIE ŚRODKA WCHODZI NA WSZYSTKIE PLANSZE ZE ZDJĘCIEM
    CZŁOWIEKA. Bartek: „bym starał się dawać tam, żeby nie nachodziło na twarz…
    po prostu od twarzy do dalszej krawędzi, tak mniej więcej w połowie".
    ⛔ CO BYŁO ŹLE (a nie było widać, bo formalnie działało): reguła „napis nie dotyka
    twarzy" jest spełniona już przy 28 px od brody. Generator brał PIERWSZĄ wysokość
    z tabeli, która tego progu nie łamała — więc napis potrafił stanąć tuż pod twarzą
    albo tuż przy krawędzi kadru. Technicznie legalnie, wizualnie ciasno. Brakowało
    nie zakazu, tylko MIARY: ile tego wolnego pola w ogóle jest i gdzie jest jego środek.

    ⛔ ZDJĘCIA BEZ TWARZY ZOSTAJĄ NA TABELI POZ — i to jest świadomy kompromis, nie
    niedoróbka. Nie ma tam twarzy, od której liczyć, więc „środek wolnego pola" znaczyłby
    „środek kadru" i wszystkie takie plansze stanęłyby w jednym miejscu. Tabela daje
    formatowi rytm („każda plansza ma inne miejsce") i przeżywa dokładnie tam, gdzie
    liczenie nic nie wnosi.

    Za pozycją „auto" stoi stara drabinka jako awaryjna — gdyby wyliczone miejsce
    nie przeszło (za wysoki blok, twarz nietypowo nisko).
    """
    if i != 0:
        # ⛔ Lewy margines zostaje Z TABELI, zmienia się tylko wysokość. Rytm poziomy
        # (każda plansza inaczej odsunięta od lewej) jest drugą nogą tego układu i nie ma
        # powodu go ruszać — twarz ogranicza pion, nie poziom.
        return [(POZ[i][0], POZ[i][1], "auto")] + drabinka_domyslna(i)
    # ⭐ 15.08, druga uwaga Bartka: „jest troszeczkę przesunięte do prawej… zwykle się
    # jednak pisze bardziej od lewej". Okładka miała w POZ lewy margines 20,6% szerokości
    # (ok. 222 px) — wpisany ręcznie 16.08 i NIGDY nieliczony. Wcześniej tego nie było
    # widać, bo napis stał nisko, przy krawędzi kadru; po wyśrodkowaniu w pionie wyszedł
    # na czyste tło i odstęp od lewej zaczął rzucać się w oczy.
    # Teraz 9% z lewej przy stałych 7% z prawej — marginesy są prawie równe, a blok czyta
    # się jak tekst pisany od lewej, nie jak wciśnięty w prawy dolny róg.
    # ⛔ Dotyczy WYŁĄCZNIE okładki. Plansze 2–7 zostają na nieregularnym układzie POZ.
    return [(30, MARGINES_OKLADKI, "auto")] + drabinka_domyslna(0)


DRABINKI = {"domyslna": drabinka_domyslna, "wysoko": drabinka_wysoko,
            "auto": drabinka_auto}

# Ile miejsca zostawiamy pod twarzą i przy krawędziach kadru, gdy liczymy pozycję „auto".
# ⛔ 28 px to mniej więcej połowa wysokości pisma treści.
# ⭐⭐ 15.08 (s281) — MARGINES_DOLNY: 40 → 104 px. Bartek, po obejrzeniu okładki
# „Finansowanie pod okazję zakupową": „nie ma praktycznie wcale wolnej przestrzeni na
# dole pod napisem". Miał rację i 40 px było liczbą techniczną (zapas 24 px + cień),
# a nie marginesem kompozycyjnym. 104 px to ok. 7,7% wysokości kadru — tyle samo, co
# marginesy boczne (9% z lewej, 7% z prawej), więc napis oddycha tak samo z każdej strony.
MARGINES_OKLADKI = 9.0
ODSTEP_OD_TWARZY = 28.0
MARGINES_DOLNY = 104.0
# ⭐ 15.08 (s284) — 72 → 88 px. Bartek, po obejrzeniu podglądu z białym napisem:
# „proponuję, żeby przerwa była tak samo od dołu, jak i od góry. Od góry może być
# troszeczkę mniejsza, natomiast generalnie nie może być tak, że przy samej górze
# też się pojawia napis".
# ⭐ s285: te liczby znaczą PIKSELE OD KRAWĘDZI KADRU DO PIERWSZEGO PIKSELA NAPISU.
# Do s284 odnosiły się do nominalnego pudełka linii i myliły o ok. 44 px — dlatego
# „margines 88" dawał napis 44 px od krawędzi. Teraz blok mierzy `textbbox`, czyli
# faktyczny obrys liter razem z podkładem.
MARGINES_GORNY = 88.0
# Awaryjne, ciaśniejsze marginesy — używane dopiero wtedy, gdy przy pełnych blok nie
# mieści się ani nad twarzą, ani pod nią. Lepiej ciaśniejsza okładka niż brak karuzeli.
MARGINES_DOLNY_MIN = 72.0
MARGINES_GORNY_MIN = 64.0

# Które plansze chcą człowieka. Reguła Bartka: okładka ZAWSZE z człowiekiem,
# co najmniej 40% plansz z człowiekiem.
CHCE_CZLOWIEKA = [1, 0, 1, 0, 1, 0, 1]

# ⭐ 15.08 (s287) — stały nagłówek okładki nowego formatu. Po nim, na zakreślaczu,
# stoi nazwa zawodu — prosta i rodzajowa („trener personalny", nie „trener personalny
# przygotowujący do wypraw górskich"). Pilnuje tego bramka i krytyk.
NAGLOWEK_OKLADKI = "Najczęstsze pytania, które dostaję jako"

TEKST_JASNY = (248, 248, 248)

# ⭐⭐⭐ 15.08 (s283) — NAPIS NA ZAKREŚLACZU JEST ZAWSZE BIAŁY. DECYZJA BARTKA.
# „Mam wątpliwości, czy te napisy muszą być czarne… Ja wolałem, kiedy one były białe,
# zdecydowanie. Myślę, że to nawet może być reguła, że zawsze te napisy na tym
# zakreślaczu są białe. W ogóle ja bym nie robił nigdzie czarnych."
#
# Historia tej linijki, żeby nikt jej trzeci raz nie ruszał:
#   • do s281 — na sztywno ciemna zieleń #14200F (dobrana pod żółty i pomarańcz);
#   • s282 — liczona kontrastem WCAG, bo u Łukasza ciemne na granacie znikało;
#   • s283 — ZAWSZE BIAŁY. Bartek chce jednego wyglądu na wszystkich profilach,
#     nie wyglądu zależnego od koloru marki.
# ⛔ To jest wybór estetyczny, nie pomiar. Na jasnym żółtym biel ma niski kontrast
# liczbowo — Bartek to widział i tak zdecydował. Nie „poprawiać" tego z powrotem.
TEKST_NA_PODKLADZIE = TEKST_JASNY

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


# ⭐ 15.08 (s286) — TA SAMA SIATKA BEZPIECZEŃSTWA, CO W render.py.
# ⛔ Sprostowanie: profile mają pola czcionek WYPEŁNIONE POPRAWNIE (ozdobny w nagłówku,
# spokojny w tekście) — moje wcześniejsze „pięć z sześciu ma zamienione" brało się
# z pomylenia identyfikatorów pól przy odczycie. Ta lista jest zabezpieczeniem na
# przyszłość, dziś nie zmienia niczyjego wyglądu.
# ⛔ Dotyczy WYŁĄCZNIE tekstu do czytania. Nagłówki zostają dokładnie takie, jak w profilu.
OZDOBNE_NIE_DO_CZYTANIA = {"anton", "archivo black", "playfair display", "fraunces", "oswald"}
PARTNER_DO_CZYTANIA = {
    "anton": "inter", "archivo black": "inter", "oswald": "inter",
    "playfair display": "poppins", "fraunces": "poppins",
}


# ⭐ 16.08 — TEN SAM MECHANIZM CO W `render.py`: krój spoza repo próbujemy ŚCIĄGNĄĆ
# z Google Fonts, zanim podstawimy cokolwiek zastępczego. Jedna funkcja, jeden cache,
# dwa formaty — nie dwie kopie tej samej logiki.
try:
    from render import _gf_pobierz as _gf_pobierz
except Exception:                                    # render.py niedostępny — stare zachowanie
    _gf_pobierz = None


def _sciezka_czcionki(rodzina: str, naglowek: bool) -> str:
    k = (rodzina or "").strip().lower()
    if not naglowek and k in OZDOBNE_NIE_DO_CZYTANIA:
        k = PARTNER_DO_CZYTANIA.get(k, "inter")
    tab = PLIKI_NAGLOWKA if naglowek else PLIKI_TRESCI
    plik = tab.get(k)
    if plik:
        p = os.path.join(KATALOG_CZCIONEK, plik)
        if os.path.exists(p):
            return p
    if k and _gf_pobierz is not None:
        wagi = (700, 800, 600, 500, 400) if naglowek else (400, 500, 300, 600)
        z_sieci = _gf_pobierz(rodzina, wagi)
        if z_sieci:
            return z_sieci
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


def _przygas(img: Image.Image) -> Image.Image:
    """Równomierne przygaszenie całego kadru o `PRZYGASZENIE`. Zdjęcie i nic więcej —
    wołane PRZED napisami, żeby biel liter i kolor zakreślacza zostały nietknięte.
    Gdyby przygaszać gotową planszę, przygasłby też napis i cały zabieg nie dałby
    ani grama kontrastu — tylko ciemniejszy obrazek."""
    if PRZYGASZENIE <= 0:
        return img
    return Image.blend(img, Image.new("RGB", img.size, PRZYGAS_KOLOR), PRZYGASZENIE)


def _wsp_przygaszenia() -> float:
    """⛔⛔ PO CO TO JEST — INACZEJ PRZYGASZENIE ZACZĘŁOBY ODRZUCAĆ ZDJĘCIA.
    Bramka wyboru zdjęcia (`jasnosc < 0,11` / `> 0,85`) mierzy GOTOWĄ planszę.
    Skoro plansze 2–7 są teraz przygaszone, ten sam pomiar wychodzi na nich niżej
    niż wczoraj — i zdjęcia, które wczoraj przechodziły, zaczęłyby wypadać jako
    „za ciemne", mimo że nikt ich nie zmienił. Dokładnie to zobaczyłem na podglądzie
    przy wartości 0,22.
    Ten współczynnik mówi, o ile sam pomiar spada od samego zabiegu (liczony raz,
    na szarym klinie 0–255), i o tyle przesuwamy progi. Efekt: przechodzą DOKŁADNIE
    te same zdjęcia co przed zmianą, tylko wychodzą ciemniejsze.
    ⛔ Liczone z `PRZYGASZENIE`, nie wpisane z ręki — po zmianie tamtej liczby
    ten współczynnik poprawia się sam."""
    if PRZYGASZENIE <= 0:
        return 1.0

    def _lum(im):
        s = 0.0
        dane = list(im.getdata())
        for r, g, b in dane:
            def f(v):
                v = v / 255.0
                return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
            s += 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)
        return s / max(1, len(dane))

    klin = Image.new("RGB", (256, 1))
    klin.putdata([(v, v, v) for v in range(256)])
    return round(_lum(_przygas(klin)) / max(1e-6, _lum(klin)), 3)


PRZYGAS_LUM = _wsp_przygaszenia()


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
    tresc: { zawod, linie:[[pytanie, odpowiedz] × 5], cta:[haslo, zdanie] }
    tw:    [x, y, w, h] ramki twarzy w PROCENTACH obrazu albo None
    """
    gy, gx, tryb = pozycja
    L = round(W * gx / 100.0)
    maxW = W - L - round(W * 0.07)
    S2 = 52
    # ⭐⭐ 15.08 (s283) — CTA (plansza 7) dostaje trzecią linię nagłówka, tak jak okładka.
    # Bartek: „Call to Action… ten napis w markerze jest taki bardzo pomniejszony".
    # Przyczyna: hasło CTA to najdłuższy nagłówek w całej karuzeli („Sprawdź kontrahenta
    # za darmo na finmach.pl — link w bio"), a przy limicie DWÓCH linii jedyny sposób,
    # żeby się zmieścił, to zejście stopniem pisma aż do 44–50 px. Przy trzech liniach
    # drabinka zatrzymuje się kilka stopni wyżej i hasło jest normalnej wielkości.
    max_lin = 3 if i in (0, 6) else 2

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

        def zloz(y_base: float):
            """Składa CAŁĄ planszę przy zadanej wysokości napisu.

            ⭐ Wydzielone 15.08 z ciała pętli, żeby dało się złożyć planszę PRÓBNIE
            (zmierzyć, jak wysoki wyszedł blok tekstu), a potem złożyć ją drugi raz
            już na policzonej wysokości. Bez tego nie da się niczego wyśrodkować:
            wysokości bloku nie znamy, zanim nie dobierzemy stopnia pisma i łamania.
            Stopień pisma i łamanie NIE zależą od y_base, więc jedna próba wystarcza.
            """
            plotno = Image.new("RGB", (W, H), (0, 0, 0))
            plotno.paste(skala, (round(ox), round(oy)))
            # ⛔ Przygaszenie IDZIE TU — po zdjęciu, przed literami. Patrz `_przygas`.
            if i >= 1:
                plotno = _przygas(plotno)
            rys = ImageDraw.Draw(plotno)

            bloki = []

            def nag(pre: str, cyt: str, y_naglowka: float, cudzyslow: bool = True):
                """Nagłówek: część biała (pre) + cytat na podkładzie w kolorze marki.

                ⭐⭐ 15.08 — CUDZYSŁÓW. Ustalenie „to, co zakreślone, ma stać
                w cudzysłowie" zapadło 14.08, ale wylądowało WYŁĄCZNIE w tekście
                promptu w Airtable — w tym pliku nie było ani jednego znaku cytatu.
                Bartek brał za cudzysłów sam podkład w kolorze marki.
                Znaki doklejamy do pierwszego i ostatniego słowa cytatu, żeby jechały
                razem z tekstem przy łamaniu linii; podkład rysujemy pod ZMIERZONYM
                słowem, więc rozszerza się sam i cudzysłów mieści się w środku.
                ⛔ Hasło CTA (plansza 7) nie dostaje cudzysłowu — to nie jest cudza
                wypowiedź, tylko wezwanie od klienta.
                """
                cyt_slowa = [t for t in str(cyt).split(" ") if t]
                if cudzyslow and cyt_slowa:
                    cyt_slowa[0] = "„" + cyt_slowa[0]
                    cyt_slowa[-1] = cyt_slowa[-1] + "”"
                slowa = ([{"t": t, "c": 0} for t in pre.split(" ") if t] if pre else []) \
                        + [{"t": t, "c": 1} for t in cyt_slowa]
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
                y = y_naglowka + S1 * 0.78
                # ⭐⭐⭐ 15.08 (s285) — MIERZYMY ATRAMENT, NIE PUDEŁKO LINII.
                # Do s284 górna krawędź bloku brała się z wzoru `y - 0,80 × stopień pisma`,
                # czyli z NOMINALNEJ linii pisma. Prawdziwe litery (wersaliki, „ł", cudzysłów,
                # polskie ogonki) wychodzą ponad to nawet o 44 px przy stopniu 92. Efekt:
                # margines nazywał się 88, a napis stał 44 px od krawędzi — i tak Bartek
                # dostał okładkę Łukasza z napisem „przy samej górze", mimo że reguła
                # marginesu formalnie była spełniona.
                # `textbbox` z tym samym anchorem, którym rysujemy, zwraca faktyczny obrys
                # liter. Od tej chwili liczby w stałych MARGINES_* znaczą to, co widać.
                ink_y1, ink_y2 = 10 ** 9, -10 ** 9
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
                            gora_p = yy - S1 * 0.72
                            rys.rectangle([x - 10, gora_p,
                                           x - 10 + w_seg + 20, gora_p + S1 * 0.95],
                                          fill=podklad)
                            rys.text((x, yy), seg["t"], font=f,
                                     fill=TEKST_NA_PODKLADZIE, anchor="ls")
                            # ⛔ Podkład to element WIDOCZNY — do granic bloku wchodzi
                            # razem z literami, bo to on dotyka krawędzi kadru.
                            ink_y1 = min(ink_y1, gora_p)
                            ink_y2 = max(ink_y2, gora_p + S1 * 0.95)
                        else:
                            pisz_z_cieniem(plotno, rys, [((x, yy), seg["t"])], f, S1,
                                           bez_cienia=bez_cienia)
                        bb = rys.textbbox((x, yy), seg["t"], font=f, anchor="ls")
                        ink_y1 = min(ink_y1, bb[1])
                        ink_y2 = max(ink_y2, bb[3])
                        x += _szer(f, txt)
                    x2 = max(x2, x + 10)
                y_end = y + (len(linie) - 1) * S1 * 1.06
                if ink_y1 > ink_y2:          # pusty nagłówek — nie ma czego mierzyć
                    ink_y1, ink_y2 = y - S1 * 0.80, y_end + S1 * 0.24
                bloki.append({"x1": L, "y1": ink_y1, "x2": x2, "y2": ink_y2})
                return {"x2": x2, "yEnd": y_end, "S1": S1}

            # ⭐⭐⭐ 15.08 (s287) — NOWA TREŚĆ FORMATU: „NAJCZĘSTSZE PYTANIA, KTÓRE DOSTAJĘ JAKO…".
            # Decyzja Bartka: warstwa WIZUALNA tego formatu zostaje w całości („to mapowanie
            # jest fajnie zrobione… nie nachodzi na twarz, nie jest za blisko krawędzi"),
            # a treść wymieniamy na inną. Stary sens („<TEMAT> to «zarzut» — dopóki…")
            # przestaje istnieć: nie ma tematu, nie ma zarzutu, nie ma dolnej linii „dopóki".
            #
            # UKŁAD PLANSZ:
            #   1        okładka  — biały nagłówek + ZAWÓD na zakreślaczu, BEZ linii pod spodem
            #   2–6      pytanie  — PYTANIE w cudzysłowie na zakreślaczu + ODPOWIEDŹ pod spodem
            #   7        CTA      — bez zmian
            # ⛔ Pytań jest PIĘĆ, nie sześć: okładka oddała swoje miejsce nagłówkowi.
            if i == 0:
                # ⛔ 16.08 (s289) — ZAWÓD BEZ CUDZYSŁOWU. Zobaczone na pierwszym prawdziwym
                # renderze: okładka wychodziła „«pośrednik kredytowy»" — a cudzysłów mówi
                # „ktoś to powiedział". Na planszach 2–6 to prawda (tam stoi cytat z wiadomości
                # klienta) i cudzysłów ZOSTAJE. Na okładce zawód jest po prostu nazwą — w
                # cudzysłowie czyta się jak ironia albo jak podważanie. Sam zakreślacz,
                # krój i miejsce bez zmian, więc warstwa wizualna zostaje 1:1.
                N = nag(NAGLOWEK_OKLADKI, tresc.get("zawod", ""), y_base, cudzyslow=False)
                linie_t = []          # ⛔ okładka nie ma dolnej linii. Bartek: „to możemy usunąć"
            elif i <= 5:
                T = tresc["linie"][i - 1]
                N = nag("", T[0], y_base)          # pytanie klienta, w cudzysłowie
                linie_t = lam(f_txt, T[1], maxW)   # odpowiedź
            else:
                N = nag("", tresc["cta"][0], y_base, cudzyslow=False)
                linie_t = lam(f_txt, tresc["cta"][1], maxW)

            # ⭐⭐⭐ 16.08 (s290) — ODPOWIEDŹ ZAWSZE POD PYTANIEM. Układ „daleko" ZDJĘTY.
            # Bartek: „raczej zawsze jest po prostu odpowiedź pod pytaniem".
            # ⛔ CO ROBIŁ UKŁAD „DALEKO" I DLACZEGO BYŁ ZŁY: na planszach 2 i 3 pytanie
            # stało u góry kadru, a odpowiedź zjeżdżała na 70% wysokości — między nimi
            # zostawała pusta połowa zdjęcia. Wyglądało to jak dwa niepowiązane napisy
            # na jednym obrazku, a czytelnik nie ma jak wiedzieć, że dolny jest
            # odpowiedzią na górny. W formacie, którego CAŁY sens to „pytanie → odpowiedź",
            # rozdzielanie tej pary jest kosztem, którego nie kupuje żadna korzyść;
            # rytm kompozycji i tak trzyma się na tym, że każda plansza startuje z innej
            # wysokości (POZ) i ma inny margines lewy.
            # ⛔ Wartość „daleko" ZOSTAJE w tabeli POZ, żeby nie ruszać siedmiu krotek
            # i ich pozycji — po prostu nic już nie znaczy przy składaniu.
            yd = N["yEnd"] + N["S1"] * 0.25 + S2 * 1.15

            pisz_z_cieniem(plotno, rys,
                           [((L, yd + k * S2 * 1.24), t) for k, t in enumerate(linie_t)],
                           f_txt, S2, bez_cienia=bez_cienia)

            bx = float(L)
            for t in linie_t:
                bx = max(bx, L + _szer(f_txt, t))
            # ⭐ 15.08 (s285) — treść też mierzona atramentem, nie wzorem.
            # ⭐ 15.08 (s287) — OKŁADKA NIE MA JUŻ TREŚCI POD NAGŁÓWKIEM. Gdy `linie_t`
            # jest puste, NIE dokładamy drugiego prostokąta (pusty blok psułby i pomiar
            # zakrycia twarzy, i wyśrodkowanie) — dolną krawędzią całości jest sam nagłówek.
            if linie_t:
                t_y1, t_y2 = 10 ** 9, -10 ** 9
                for k, t in enumerate(linie_t):
                    bb = rys.textbbox((L, yd + k * S2 * 1.24), t, font=f_txt, anchor="ls")
                    t_y1 = min(t_y1, bb[1])
                    t_y2 = max(t_y2, bb[3])
                dol_t = t_y2
                bloki.append({"x1": L, "y1": t_y1, "x2": bx + 8, "y2": t_y2})
            else:
                dol_t = bloki[-1]["y2"]

            return {"plotno": plotno, "bloki": bloki, "N": N, "bx": bx,
                    "dol_t": dol_t, "gorna_t": min(b["y1"] for b in bloki)}

        y_base = round(H * gy / 100.0)
        r0 = zloz(y_base)

        # ⭐⭐ 15.08 — WYŚRODKOWANIE NA OKŁADCE (pozycja „auto").
        # Bartek: „mogłoby być całość troszeczkę wyżej, bo jest bardzo mało miejsca
        # od dołu, a twarz jest wysoko — jest dużo miejsca, żeby to wypośrodkować".
        # Dotąd nikt nie mierzył wolnego pola: drabinka podawała sztywne wysokości,
        # a generator brał pierwszą, na której napis nie dotykał twarzy.
        # Teraz: bierzemy dolną krawędź twarzy, liczymy wolne pole do dołu kadru
        # i wstawiamy w jego środek zmierzony blok tekstu.
        # ⭐⭐ 15.08 (s281) — DWA POLA ZAMIAST JEDNEGO, I ŻADNEGO DOKLEJANIA DO KRAWĘDZI.
        # Co było źle w s280: liczyliśmy WYŁĄCZNIE pole POD twarzą, a gdy blok się w nim
        # nie mieścił, dosuwaliśmy go do dolnej krawędzi (`dol - wys`). Na zdjęciu, gdzie
        # twarz siedzi nisko, wychodziła okładka z napisem przyklejonym do dołu — dokładnie
        # to, co Bartek zobaczył na „Finansowanie pod okazję zakupową".
        # Teraz: mierzymy OBA wolne pola (nad twarzą i pod twarzą), bierzemy to, w którym
        # blok naprawdę się mieści z marginesami, a przy dwóch pasujących — większe.
        # Jak nie mieści się w żadnym nawet przy ciaśniejszych marginesach, ta pozycja
        # PRZEPADA (`zaDuze`) i drabinka próbuje dalej: inny kadr, inna pozycja, inne zdjęcie.
        # ⛔ Nigdy więcej dosuwania do krawędzi — lepiej inne zdjęcie niż zła kompozycja.
        # ⛔ 16.08 wieczorem — WARUNEK `tw or i == 0`, nie samo `tryb == "auto"`.
        # Plansze 2–7 dostają dziś „auto" jako pierwszą próbę, ale liczenie środka ma sens
        # WYŁĄCZNIE względem twarzy. Bez ramki twarzy „środek wolnego pola" to po prostu
        # środek kadru — wszystkie plansze bez człowieka stanęłyby w jednym miejscu.
        # Takie plansze spadają niżej, do gałęzi z tabelą POZ, i zachowują rytm formatu.
        # ⛔ Okładka (i == 0) liczy się ZAWSZE, także bez twarzy — tak działa od 15.08
        # i Bartek to zaakceptował; nie ruszam tego przy okazji innej zmiany.
        if tryb == "auto" and (tw or i == 0):
            wys = r0["dol_t"] - r0["gorna_t"]
            ty1f = (oy + tw[1] / 100.0 * rh) if tw else 0.0
            ty2f = (oy + (tw[1] + tw[3]) / 100.0 * rh) if tw else 0.0

            def pola(mg, md):
                """Wolne pola [(góra, dół)] przy zadanych marginesach kadru."""
                pod = (max(mg, ty2f + ODSTEP_OD_TWARZY) if tw else mg, H - md)
                if not tw:
                    return [pod]
                nad = (mg, min(H - md, ty1f - ODSTEP_OD_TWARZY))
                return [pod, nad]

            wybrane = None
            for mg, md in ((MARGINES_GORNY, MARGINES_DOLNY),
                           (MARGINES_GORNY_MIN, MARGINES_DOLNY_MIN)):
                pasuje = [(g, d) for (g, d) in pola(mg, md) if d - g >= wys]
                if pasuje:
                    wybrane = max(pasuje, key=lambda t: t[1] - t[0])
                    break
            if wybrane is None:
                return {"zaDuze": True}

            gora, dol = wybrane
            cel = gora + (dol - gora - wys) / 2.0
            przesun = cel - r0["gorna_t"]
            if abs(przesun) > 1:
                r0 = zloz(y_base + przesun)

        # ⭐⭐⭐ 15.08 (s283) — MARGINES DOLNY OBOWIĄZUJE NA KAŻDEJ PLANSZY, NIE TYLKO
        # NA OKŁADCE. Bartek: „tam tak jakby nie obowiązywała ta zasada, że musi być
        # jakaś przerwa pomiędzy dołem… to jest taka sama zasada na każdym slajdzie,
        # ale zauważyłem, że to się najczęściej zdarza na tych ostatnich slajdach".
        #
        # Dlaczego akurat ostatnie: plansze 2–7 stoją na sztywnych wysokościach z POZ,
        # a plansza 7 startuje najniżej ze wszystkich (67,8% wysokości kadru). Przy
        # dłuższej treści blok schodził poniżej marginesu i nikt tego nie sprawdzał —
        # jedyny warunek, jaki tam był, to twarde `dol_t > H - 24`, czyli „byle nie
        # wyszło poza kadr".
        #
        # Teraz: jeżeli blok schodzi poniżej `H - MARGINES_DOLNY`, PODNOSIMY go o brakującą
        # różnicę — ale tylko tyle, ile pozwala górny margines. Rytm formatu zostaje
        # (każda plansza dalej startuje z innej wysokości), znika samo doklejanie do dołu.
        # ⛔ Do 16.08 stało tu `elif tryb == "blisko"` — bo w układzie „daleko" treść
        # siedziała na sztywnych 70% wysokości i podnoszenie `y_base` rozjechałoby planszę.
        # Układu „daleko" już nie ma (odpowiedź zawsze pod pytaniem), więc powód zniknął,
        # a warunek zostawiony na miejscu wyłączałby margines dolny na planszach 2 i 3 —
        # jedynych dwóch, które go teraz potrzebują tak samo jak reszta.
        else:
            nadmiar = r0["dol_t"] - (H - MARGINES_DOLNY)
            if nadmiar > 1:
                zapas_u_gory = r0["gorna_t"] - MARGINES_GORNY
                podnies = min(nadmiar, max(0.0, zapas_u_gory))
                if podnies > 1:
                    r0 = zloz(y_base - podnies)
            # ⭐ 15.08 (s284) — i w drugą stronę: napis nie dotyka GÓRNEJ krawędzi.
            # Bartek: „nie może być tak, że przy samej górze też się pojawia napis".
            brak_u_gory = MARGINES_GORNY - r0["gorna_t"]
            if brak_u_gory > 1:
                zapas_u_dolu = (H - MARGINES_DOLNY) - r0["dol_t"]
                opusc = min(brak_u_gory, max(0.0, zapas_u_dolu))
                if opusc > 1:
                    r0 = zloz(y_base + opusc)

        plotno = r0["plotno"]
        bloki = r0["bloki"]
        N = r0["N"]
        dol_t = r0["dol_t"]
        bx = r0["bx"]

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
    """⭐⭐ 15.08 (s282) — OBRÓT Z EXIF. Zdjęcia z telefonu bardzo często leżą
    w pliku poziomo, a pionowo ustawia je dopiero znacznik EXIF Orientation.
    Przeglądarka ten znacznik honoruje, Pillow — NIE. Skutek u Łukasza (15.08):
    okładka i plansza 4 wyszły położone na boku.
    ⛔ To psuło też RAMKĘ TWARZY: ramki liczy przeglądarka (MediaPipe) na obrazie
    już obróconym, więc procenty odnosiły się do innego układu niż ten, na którym
    render stawiał napis. Stąd napisy w poprzek sylwetki na tych kadrach.
    `exif_transpose` obraca piksele i usuwa znacznik — od tego momentu obie strony
    patrzą na ten sam obraz."""
    if im is None:
        return None
    try:
        im = ImageOps.exif_transpose(im)
    except Exception:
        pass
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
                    # ⛔ Progi przesunięte o współczynnik przygaszenia na planszach 2–7 —
                    # inaczej sam zabieg wywalałby zdjęcia jako „za ciemne". Patrz
                    # `_wsp_przygaszenia`.
                    _wsp = PRZYGAS_LUM if i >= 1 else 1.0
                    if r["jasnosc"] < 0.11 * _wsp or r["jasnosc"] > 0.85 * _wsp:
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
