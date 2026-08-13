"""
InstaHunter — mikro-usługa renderu karuzel (HTTP, dla Make)
===========================================================
FastAPI opakowujące render.py. Make woła POST /render z brandem + slajdami
(+ URL-e zdjęć klienta), usługa renderuje 8 PNG, hostuje je pod /static/...
i zwraca listę URL-i. Make mapuje URL-e do pola załącznika w Airtable
(Content Plan) — Airtable sam pobiera pliki z URL.

Uruchomienie lokalne:   uvicorn app:app --host 0.0.0.0 --port 8080
Deploy: Dockerfile (Fly.io / Render.com / Railway — darmowy tier wystarcza).

Bezpieczeństwo: nagłówek X-API-Key musi zgadzać się z env RENDER_API_KEY.
"""
from __future__ import annotations
import os, io, uuid, time, urllib.request, json, base64
from typing import List, Optional
import re
from fastapi import FastAPI, HTTPException, Header, Request, Query
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from PIL import Image

import render as R

BASE_URL = os.environ.get("BASE_URL", "http://localhost:8080")
API_KEY = os.environ.get("RENDER_API_KEY", "dev-key")
STATIC_DIR = os.environ.get("STATIC_DIR", os.path.join(os.path.dirname(__file__), "static"))
os.makedirs(STATIC_DIR, exist_ok=True)

app = FastAPI(title="InstaHunter Carousel Renderer", version="1.0")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class Slide(BaseModel):
    type: str                       # cover | content | list | stat | chart | cta
    title: Optional[str] = None
    subtitle: Optional[str] = None
    tagline: Optional[str] = None
    count: Optional[int] = None
    number: Optional[int] = None
    heading: Optional[str] = None
    body: Optional[str] = None
    cta: Optional[str] = None
    kicker: Optional[str] = None
    items: Optional[List[str]] = None
    figure: Optional[str] = None
    label: Optional[str] = None
    bars: Optional[list] = None


class BrandIn(BaseModel):
    bg: str = "#111008"
    bg_alt: str = "#F5EFE2"
    accent: str = "#E8402A"
    taupe: str = "#8A7A6A"
    white: str = "#FFFFFF"
    handle: str = "@klient"
    glow: bool = True
    ornaments: bool = True


class RenderReq(BaseModel):
    brand: BrandIn
    slides: List[Slide]
    photo_urls: List[str] = []      # rotacja zdjęć; [0] -> okładka (Format A)
    avatar_url: Optional[str] = None  # zdjęcie profilowe klienta -> okrągły awatar
    job_id: Optional[str] = None    # do re-renderu tej samej karty (nadpisuje)


# ⛔⛔ TWARDY SUFIT NA POBIERANY PLIK — 13.08 wieczorem, po trzech restartach z OOM.
# CO SIĘ STAŁO (zmierzone w logach): scenariusz „4e. Ocena zdjęć" bierze co godzinę
# 50 rekordów bez oceny i posyła KAŻDY na /eval_photo. Wśród nich siedziało 26 FILMÓW
# i 3 dokumenty — dla nich endpoint zawsze zwraca 422, więc znacznik „Oceniono" nigdy
# się nie zapisywał i te same pliki wracały CO GODZINĘ, w kółko. A `r.read()` wciągał
# każdy z nich DO PAMIĘCI W CAŁOŚCI, zanim ktokolwiek sprawdził, że to nie jest zdjęcie.
# Kilka filmów naraz = ponad 512 MB = restart instancji.
# ⭐ Zasada: NAJPIERW sprawdzamy, czy plik w ogóle ma prawo tu być, POTEM go czytamy.
MAKS_PLIK_BAJTY = int(os.environ.get("MAKS_PLIK_BAJTY", 20 * 1024 * 1024))


def _download_bytes(url: str) -> Optional[bytes]:
    """Pobiera plik z twardym sufitem rozmiaru. Za duży → None, i ani bajta więcej w RAM."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "InstaHunter/1.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            dl = r.headers.get("Content-Length")
            if dl and int(dl) > MAKS_PLIK_BAJTY:
                return None
            buf = io.BytesIO()
            while True:
                kawalek = r.read(262144)
                if not kawalek:
                    break
                buf.write(kawalek)
                if buf.tell() > MAKS_PLIK_BAJTY:
                    return None          # urywamy w pół pliku — nie dokańczamy pobierania
            return buf.getvalue()
    except Exception:
        return None


def _download(url: str) -> Optional[Image.Image]:
    raw = _download_bytes(url)
    if raw is None:
        return None
    try:
        return Image.open(io.BytesIO(raw)).convert("RGB")
    except Exception:
        return None


# ⛔ Ile ciężkich robót naraz. Render ma 512 MB; jedna karuzela z2 szczytuje ~220 MB,
# więc DWIE naraz to restart. Endpointy są synchroniczne, czyli FastAPI puszcza je
# w puli 40 wątków — bez tej bramki nic ich nie ogranicza poza pamięcią.
import threading as _th
_ZAMEK_CIEZKI = _th.BoundedSemaphore(1)


# ======================================================================
# OCENA ZDJĘĆ NA OKŁADKĘ (Format A) — deterministyczna siatka + wizja Claude
# (cz. 72). Deterministyka odrzuca kwadrat/poziom/za-małe; wizja ocenia
# twarz-nie-na-całą-klatkę + zapas na tekst u dołu + ostrość/jakość.
# ======================================================================
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
VISION_MODEL = os.environ.get("VISION_MODEL", "claude-haiku-4-5-20251001")

VISION_PROMPT = (
    "Oceniasz, czy ZDJĘCIE nadaje się na OKŁADKĘ pionowej karuzeli na Instagram "
    "(format 4:5, kadr 1080x1350). WAŻNE: na dole okładki nakładamy CIEMNY GRADIENT "
    "i dopiero na nim tytuł, więc tekst będzie czytelny niezależnie od tła w dolnej "
    "części. NIE odrzucaj zdjęcia tylko dlatego, że w dolnej 1/3 jest ubranie, ręce, "
    "tło czy inne detale, gradient je przykryje. "
    "Dobra okładka: to portret osoby, twarz w górnej lub środkowej części kadru, "
    "twarz NIE zajmuje całej klatki (jest trochę oddechu wokół), zdjęcie ostre i "
    "dobrej jakości. Typowy portret od pasa w górę jest OK. "
    "Zła okładka: twarz/głowa na całą klatkę (ekstremalne zbliżenie, wielka głowa), "
    "twarz umieszczona nisko w samej dolnej 1/3 (nachodzi na tytuł), zdjęcie rozmyte, "
    "bardzo ciemne lub prześwietlone, albo to w ogóle nie jest zdjęcie osoby "
    "(sam przedmiot/krajobraz bez tematu). Bądź rozsądny, nie przesadnie surowy. "
    "Odpowiedz WYŁĄCZNIE zwartym JSON, bez markdown: "
    '{"suitable": true/false, "face_full_frame": true/false, '
    '"face_too_low": true/false, "quality_ok": true/false, '
    '"reason": "jedno-dwa zdania po polsku, bez em-dash"}'
)


def _vision_eval(img_bytes: bytes, media_type: str) -> Optional[dict]:
    """Wywołuje Claude Vision. Zwraca dict werdyktu albo None (brak klucza/błąd)."""
    if not ANTHROPIC_API_KEY:
        return None
    try:
        b64 = base64.b64encode(img_bytes).decode("ascii")
        payload = {
            "model": VISION_MODEL,
            "max_tokens": 300,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "image", "source": {
                        "type": "base64", "media_type": media_type, "data": b64}},
                    {"type": "text", "text": VISION_PROMPT},
                ],
            }],
        }
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=45) as r:
            resp = json.loads(r.read().decode("utf-8"))
        text = "".join(b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text")
        m = re.search(r"\{.*\}", text, re.DOTALL)
        return json.loads(m.group(0)) if m else None
    except Exception as e:
        return {"_error": str(e)[:200]}


@app.get("/health")
def health():
    return {"ok": True, "ts": int(time.time())}


@app.post("/render")
def render_endpoint(req: RenderReq, x_api_key: str = Header(default="")):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="bad api key")
    job = req.job_id or uuid.uuid4().hex[:12]
    out_dir = os.path.join(STATIC_DIR, job)
    os.makedirs(out_dir, exist_ok=True)

    brand = R.Brand(bg=req.brand.bg, bg_alt=req.brand.bg_alt, accent=req.brand.accent,
                    taupe=req.brand.taupe, white=req.brand.white, handle=req.brand.handle,
                    glow=req.brand.glow, ornaments=req.brand.ornaments)

    photos = []
    for u in req.photo_urls:
        img = _download(u)
        if img is not None:
            photos.append(img)

    avatar = _download(req.avatar_url) if req.avatar_url else None

    slides = [s.model_dump() for s in req.slides]
    paths = R.render_carousel(brand, slides, out_dir, photos=photos or None, avatar=avatar)
    urls = [f"{BASE_URL}/static/{job}/{os.path.basename(p)}" for p in paths]
    return JSONResponse({"job_id": job, "count": len(urls), "slides": urls})


# ======================================================================
# /render_tokens — przyjmuje SUROWY blok tokenów Claude (text/plain body)
# + brand/photo/job w query params. Parsuje tokeny po stronie serwera,
# więc Make NIE buduje JSON (koniec problemu z cudzysłowami — cz.68 błąd #2)
# i NIE ekstrahuje pól (koniec pustych nagłówków — cz.68 błąd #1).
# Zwraca też caption/temat/title, żeby Make mapował kartę bez parsowania.
# ======================================================================
_TOKEN_RE = re.compile(r"\[\[([A-ZĄĆĘŁŃÓŚŹŻ_]+)\]\]")


# typy tokenów, które MOGĄ wystąpić wielokrotnie w jednym slajdzie -> lista
_REPEAT_TOKENS = {"PUNKT", "SLUPEK"}
# mapowanie wartości [[TYP]] (PL/EN) -> wewnętrzny typ renderu
_TYP_MAP = {
    "cover": "cover", "okladka": "cover", "okładka": "cover",
    "cta": "cta",
    "lista": "list", "list": "list",
    "statystyka": "stat", "stat": "stat",
    "wykres": "chart", "chart": "chart",
    "tresc": "content", "treść": "content", "content": "content",
}


def _parse_bar(line):
    """'Etykieta | 62 | tak' -> (label, value_int, highlight_bool). Odporne na braki."""
    parts = [p.strip() for p in str(line).split("|")]
    label = parts[0] if parts else ""
    val = 0
    if len(parts) > 1:
        digits = "".join(c for c in parts[1] if c.isdigit())
        val = int(digits) if digits else 0
    hi = False
    if len(parts) > 2:
        hi = parts[2].lower() in ("tak", "true", "1", "yes", "hi", "highlight")
    return (label, max(0, min(100, val)), hi)


def parse_carousel_tokens(raw: str):
    """Surowy blok tokenów Claude -> (slides[dict], caption, temat).

    Tokeny (z prompt-karuzele-TRESC): TEMAT, SLAJD, TYP, KICKER, TYTUL, PODTYTUL,
    TAGLINE, LICZBA, NUMER, NAGLOWEK, TRESC, FIGURA, ETYKIETA, PUNKT (xN),
    SLUPEK (xN), CTA, END, CAPTION.
    Typ slajdu z [[TYP]] (mapowanie PL/EN); fallback: TYTUL->cover, CTA->cta,
    FIGURA->stat, SLUPEK->chart, PUNKT->list, reszta->content.
    Slajdy treściowe numerowane automatycznie 1..N (jeśli brak NUMER),
    cover.count = LICZBA lub liczba slajdów-punktów.
    """
    matches = list(_TOKEN_RE.finditer(raw or ""))
    fields = []
    for i, m in enumerate(matches):
        tok = m.group(1)
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(raw)
        fields.append((tok, raw[start:end].strip()))

    raw_slides, caption, temat, cur = [], "", "", None
    mode = None
    for tok, val in fields:
        if tok == "TEMAT":
            temat = val
        elif tok == "SLAJD":
            if cur is not None:
                raw_slides.append(cur)
            cur = {}
            mode = "slide"
        elif tok == "CAPTION":
            if cur is not None:
                raw_slides.append(cur)
                cur = None
            caption = val
            mode = "caption"
        elif tok == "END":
            if cur is not None:
                raw_slides.append(cur)
                cur = None
            mode = None
        else:
            if mode == "slide" and cur is not None:
                if tok in _REPEAT_TOKENS:
                    cur.setdefault(tok, []).append(val)
                else:
                    cur[tok] = val
    if cur is not None:
        raw_slides.append(cur)

    def _typ_of(s):
        t = _TYP_MAP.get(s.get("TYP", "").strip().lower())
        if t:
            return t
        if "TYTUL" in s:
            return "cover"
        if "CTA" in s:
            return "cta"
        if "FIGURA" in s:
            return "stat"
        if "SLUPEK" in s:
            return "chart"
        if "PUNKT" in s:
            return "list"
        return "content"

    typed = [(_typ_of(s), s) for s in raw_slides]

    def _int(v):
        v = (v or "").strip()
        return int(v) if v.isdigit() else None

    slides, num = [], 0
    for typ, s in typed:
        if typ == "cover":
            slides.append({
                "type": "cover",
                "title": s.get("TYTUL", ""),
                "subtitle": s.get("PODTYTUL", ""),
                "tagline": s.get("TAGLINE", ""),
                # badge liczby TYLKO gdy jawnie podana (inaczej myliłaby się z „N błędów")
                "count": _int(s.get("LICZBA")),
            })
        elif typ == "cta":
            slides.append({
                "type": "cta",
                "heading": s.get("NAGLOWEK", ""),
                "body": s.get("TRESC", ""),
                "cta": s.get("CTA", ""),
            })
        elif typ == "stat":
            slides.append({
                "type": "stat",
                "kicker": s.get("KICKER", "") or None,
                "figure": s.get("FIGURA", ""),
                "label": s.get("ETYKIETA", ""),
                "body": s.get("TRESC", ""),
            })
        elif typ == "chart":
            slides.append({
                "type": "chart",
                "kicker": s.get("KICKER", "") or None,
                "heading": s.get("NAGLOWEK", ""),
                "bars": [_parse_bar(b) for b in s.get("SLUPEK", []) if str(b).strip()],
            })
        elif typ == "list":
            num += 1
            slides.append({
                "type": "list",
                "kicker": s.get("KICKER", "") or None,
                "number": _int(s.get("NUMER")),
                "heading": s.get("NAGLOWEK", ""),
                "items": [i for i in s.get("PUNKT", []) if str(i).strip()],
            })
        else:  # content
            num += 1
            slides.append({
                "type": "content",
                "kicker": s.get("KICKER", "") or None,
                "number": _int(s.get("NUMER")),
                "heading": s.get("NAGLOWEK", ""),
                "body": s.get("TRESC", ""),
            })
    return slides, caption, temat


def build_readable(slides):
    """Czytelna, przyjazna klientowi wersja treści karuzeli (bez tokenów, bez em-dash).

    Trafia do widocznego pola „Scenariusz / treść"; surowe tokeny idą do ukrytego
    pola technicznego (potrzebne tylko do re-renderu).
    """
    def _clean(v):
        # marker akcentu *słowo* jest tylko dla renderu -> w czytelnej wersji usuwamy gwiazdki
        return str(v or "").replace("*", "")

    lines = []
    n = 0
    for s in slides:
        t = s.get("type")
        if t == "cover":
            lines.append("OKŁADKA")
            for k in ("title", "subtitle", "tagline"):
                if s.get(k):
                    lines.append(_clean(s[k]))
        elif t == "cta":
            head = _clean(s.get("heading", ""))
            lines.append("CTA" + (": " + head if head else ""))
            if s.get("body"):
                lines.append(_clean(s["body"]))
            if s.get("cta"):
                lines.append("👉 " + _clean(s["cta"]))
        elif t == "stat":
            n += 1
            kick = _clean(s.get("kicker") or "")
            lines.append("SLAJD " + str(n) + (" (" + kick + ")" if kick else "") + " — STATYSTYKA")
            fig = _clean(s.get("figure", ""))
            lab = _clean(s.get("label", ""))
            lines.append((fig + " " + lab).strip())
            if s.get("body"):
                lines.append(_clean(s["body"]))
        elif t == "chart":
            n += 1
            kick = _clean(s.get("kicker") or "")
            head = _clean(s.get("heading", ""))
            lines.append("SLAJD " + str(n) + (" (" + kick + ")" if kick else "") +
                         " — WYKRES" + (": " + head if head else ""))
            for b in s.get("bars", []):
                lab = b[0] if isinstance(b, (list, tuple)) and b else ""
                val = b[1] if isinstance(b, (list, tuple)) and len(b) > 1 else ""
                lines.append("  • " + _clean(lab) + ": " + str(val) + "%")
        elif t == "list":
            n += 1
            kick = _clean(s.get("kicker") or "")
            head = _clean(s.get("heading", ""))
            lines.append("SLAJD " + str(n) + (" (" + kick + ")" if kick else "") +
                         " — LISTA" + (": " + head if head else ""))
            for it in s.get("items", []):
                lines.append("  ✓ " + _clean(it))
        else:  # content
            n += 1
            kick = _clean(s.get("kicker") or "")
            head = _clean(s.get("heading", ""))
            lines.append("SLAJD " + str(n) + (" (" + kick + ")" if kick else "") +
                         (": " + head if head else ""))
            if s.get("body"):
                lines.append(_clean(s["body"]))
        lines.append("")
    return "\n".join(lines).strip()


def _hex_or(default: str, v: str):
    v = (v or "").strip()
    if not v:
        return default
    return v if v.startswith("#") else "#" + v


def _parse_look(s: str):
    """JSON pokręteł wyglądu -> {'global': {...}, 'slides': {'1': {...}}}.
    Odporne na pusty/uszkodzony wkład (zwraca {})."""
    s = (s or "").strip()
    if not s:
        return {}
    try:
        d = json.loads(s)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _attach_slide_looks(items, per):
    """Dokleja override 'look' do slajdów/story wg 1-based indeksu (klucz str lub int)."""
    if not per or not isinstance(per, dict):
        return
    for i, it in enumerate(items, start=1):
        ov = per.get(str(i))
        if ov is None:
            ov = per.get(i)
        if isinstance(ov, dict) and ov:
            it["look"] = {**(it.get("look") or {}), **ov}


_LOOK_TOKEN_RE = re.compile(r"\[\[WYGLAD\]\](.*?)\[\[END\]\]", re.DOTALL)


def _extract_look_token(raw: str):
    """Wyłuskuje blok [[WYGLAD]] {json} [[END]] z tokenów Claude -> dict pokręteł ({} gdy brak/uszkodzony)."""
    m = _LOOK_TOKEN_RE.search(raw or "")
    if not m:
        return {}
    try:
        d = json.loads(m.group(1).strip())
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _merge_look(a: dict, b: dict):
    """Scala dwa configi pokręteł (b nadpisuje a): global + slides głęboko."""
    a = a or {}
    b = b or {}
    if not a:
        return b
    if not b:
        return a
    out = {"global": {**(a.get("global") or {}), **(b.get("global") or {})}}
    sl = {**(a.get("slides") or {})}
    for k, v in (b.get("slides") or {}).items():
        sl[k] = {**(sl.get(k) or {}), **(v or {})}
    if sl:
        out["slides"] = sl
    if not out.get("global"):
        out.pop("global", None)
    return out


@app.post("/render_tokens")
async def render_tokens_endpoint(
    request: Request,
    x_api_key: str = Header(default=""),
    bg: str = "", bg_alt: str = "", accent: str = "", taupe: str = "",
    white: str = "", handle: str = "", glow: bool = True, ornaments: bool = True,
    font: str = "",
    photo: List[str] = Query(default=[]),
    avatar: str = "",
    look: str = "",
    job_id: Optional[str] = None,
):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="bad api key")
    raw = (await request.body()).decode("utf-8", errors="replace")
    slides, caption, temat = parse_carousel_tokens(raw)
    if not slides:
        raise HTTPException(status_code=422, detail="no slides parsed from tokens")

    job = job_id or uuid.uuid4().hex[:12]
    out_dir = os.path.join(STATIC_DIR, job)
    os.makedirs(out_dir, exist_ok=True)

    brand = R.Brand(
        bg=_hex_or("#111008", bg), bg_alt=_hex_or("#F5EFE2", bg_alt),
        accent=_hex_or("#E8402A", accent), taupe=_hex_or("#8A7A6A", taupe),
        white=_hex_or("#FFFFFF", white), handle=(handle.strip() or "@klient"),
        glow=glow, ornaments=ornaments, font_family=font.strip(),
    )

    photos = []
    for u in photo:
        img = _download(u)
        if img is not None:
            photos.append(img)
    avatar_img = _download(avatar) if avatar.strip() else None

    look_cfg = _merge_look(_parse_look(look), _extract_look_token(raw))
    if look_cfg.get("global"):
        brand = R._apply_look(brand, look_cfg["global"])
    _attach_slide_looks(slides, look_cfg.get("slides"))

    paths = R.render_carousel(brand, slides, out_dir, photos=photos or None, avatar=avatar_img)
    urls = [f"{BASE_URL}/static/{job}/{os.path.basename(p)}" for p in paths]
    title = next((s.get("title", "") for s in slides if s.get("type") == "cover"), "")
    readable = build_readable(slides)
    return JSONResponse({
        "job_id": job, "count": len(urls), "slides": urls,
        "title": title, "temat": temat, "caption": caption,
        "readable": readable,
    })


@app.post("/render_story")
async def render_story_endpoint(
    request: Request,
    x_api_key: str = Header(default=""),
    bg: str = "", accent: str = "", taupe: str = "", white: str = "", handle: str = "",
    font: str = "",
    photo: List[str] = Query(default=[]),
    look: str = "",
    job_id: Optional[str] = None,
):
    """Render stories 1080x1920 z tekstów (surowy body, bloki [[STORY]]) + rotacja zdjęć.

    Body = jeden lub więcej bloków rozdzielonych [[STORY]]; tekst story może mieć
    marker *słowo* (akcent). Zdjęcia z query `photo` (rotacja po kolei). Zwraca URL-e PNG.
    """
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="bad api key")
    raw = (await request.body()).decode("utf-8", errors="replace")
    parts = re.split(r"\[\[STORY\]\]", raw)
    items = []
    for p in parts:
        if "[[TEKST]]" not in p:
            continue
        mfmt = re.search(r"\[\[FORMAT\]\]\s*([A-Za-z]+)", p)
        fmt = mfmt.group(1).lower() if mfmt else "tip"
        # strefa tekstu: [[STREFA]] dol|cala. dol = zdjęcie z twarzą (folder A) ->
        # tekst w dolnej połowie; cala = zdjęcie neutralne (folder B) / bez zdjęcia.
        mzone = re.search(r"\[\[STREFA\]\]\s*([A-Za-z]+)", p)
        zraw = mzone.group(1).lower() if mzone else ""
        if zraw in ("dol", "bottom"):
            zone = "bottom"
        elif zraw in ("cala", "full"):
            zone = "full"
        else:
            zone = None
        mtxt = re.search(r"\[\[TEKST\]\](.*?)(?:\[\[END\]\]|$)", p, re.DOTALL)
        text = (mtxt.group(1) if mtxt else "").strip()
        if text:
            item = {"text": text, "format": fmt}
            if zone:
                item["zone"] = zone
            items.append(item)
    if not items:
        raise HTTPException(status_code=422, detail="no stories parsed from body")

    job = job_id or uuid.uuid4().hex[:12]
    out_dir = os.path.join(STATIC_DIR, job)
    os.makedirs(out_dir, exist_ok=True)

    brand = R.Brand(
        bg=_hex_or("#111008", bg), accent=_hex_or("#E8402A", accent),
        taupe=_hex_or("#8A7A6A", taupe), white=_hex_or("#FFFFFF", white),
        handle=(handle.strip() or "@klient"), font_family=font.strip(),
    )
    photos = []
    for u in photo:
        img = _download(u)
        if img is not None:
            photos.append(img)

    look_cfg = _merge_look(_parse_look(look), _extract_look_token(raw))
    if look_cfg.get("global"):
        brand = R._apply_look(brand, look_cfg["global"])
    _attach_slide_looks(items, look_cfg.get("slides"))

    paths = R.render_stories(brand, items, out_dir, photos=photos or None)
    urls = [f"{BASE_URL}/static/{job}/{os.path.basename(p)}" for p in paths]
    return JSONResponse({"job_id": job, "count": len(urls), "stories": urls,
                         "texts": [it["text"] for it in items]})


def _vision_bytes(raw: bytes):
    """Przygotowuje bajty do wizji: downscale do max 1200px dłuższego boku, JPEG."""
    try:
        im = Image.open(io.BytesIO(raw))
        # ⭐ `draft` każe dekoderowi JPEG rozpakować obraz OD RAZU mniejszy (1/2, 1/4, 1/8).
        # Bez tego rozpakowujemy 36 MB tylko po to, żeby zaraz zejść do 1200 px.
        try:
            im.draft("RGB", (1200, 1200))
        except Exception:
            pass
        im = im.convert("RGB")
        long_side = max(im.size)
        if long_side > 1200:
            k = 1200 / long_side
            im = im.resize((int(im.width * k), int(im.height * k)), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=88)
        return buf.getvalue()
    except Exception:
        return raw


@app.api_route("/eval_photo", methods=["GET", "POST"])
def eval_photo_endpoint(url: str = Query(...), x_api_key: str = Header(default="")):
    """Ocena jednego zdjęcia pod okładkę Format A. Zwraca werdykt do zapisania w Airtable.

    Werdykt: suitable = "tak"/"nie", reason, orientation (pionowe/kwadratowe/poziome),
    width, height, evaluated_at. Make mapuje to na pola tabeli Zdjęcia klienta.
    """
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="bad api key")
    raw = _download_bytes(url)
    if raw is None:
        raise HTTPException(status_code=422, detail="nie udało się pobrać zdjęcia (albo jest większy niż %d MB — to nie jest zdjęcie na okładkę)" % (MAKS_PLIK_BAJTY // (1024 * 1024)))
    # ⭐⭐ SAM NAGŁÓWEK, BEZ DEKODOWANIA PIKSELI. `cover_photo_ok` i `orientation_of`
    # patrzą WYŁĄCZNIE na `img.size` — sprawdzone w render.py. Pełne `.convert("RGB")`
    # zdjęcia 4032×3024 to 36 MB w pamięci na każde wywołanie i było tu robione ZAWSZE,
    # także dla plików, które i tak zaraz odpadały jako poziome albo za małe.
    try:
        naglowek = Image.open(io.BytesIO(raw))
        w, h = naglowek.size
    except Exception:
        raise HTTPException(status_code=422, detail="nieprawidłowy plik obrazu")

    orientation = R.orientation_of(w, h)
    det_ok = R.cover_photo_ok(naglowek)
    try:
        naglowek.close()
    except Exception:
        pass

    def _out(suitable, reason):
        return JSONResponse({
            "suitable": suitable, "reason": reason, "orientation": orientation,
            "width": w, "height": h, "evaluated_at": int(time.time()),
        })

    # 1) SIATKA BEZPIECZEŃSTWA (deterministyczna) — twardy próg, oszczędza tokeny
    if not det_ok:
        if orientation == "poziome":
            why = f"Zdjęcie poziome ({w}x{h}). Okładka Format A wymaga pionowego."
        elif orientation == "kwadratowe":
            why = f"Zdjęcie kwadratowe ({w}x{h}). Kadr 4:5 wypycha twarz na środek i traci jakość."
        else:
            why = f"Za mała rozdzielczość ({w}x{h}) dla kadru 1080x1350 (za duży upscale)."
        return _out("nie", "Siatka bezpieczeństwa: " + why)

    # 2) WIZJA CLAUDE — twarz/zapas na tekst/jakość
    # ⛔ Dopiero TU dekodujemy piksele, i tylko dla zdjęć, które przeszły siatkę.
    with _ZAMEK_CIEZKI:
        male = _vision_bytes(raw)
    del raw
    v = _vision_eval(male, "image/jpeg")
    if v is None:
        return _out("tak", f"Proporcje i rozdzielczość OK ({orientation}, {w}x{h}). "
                           "Ocena wizualna pominięta (brak klucza wizji).")
    if "_error" in v:
        return _out("tak", f"Proporcje i rozdzielczość OK ({orientation}, {w}x{h}). "
                           f"Wizja niedostępna: {v['_error']}")
    suitable = "tak" if v.get("suitable") else "nie"
    reason = (v.get("reason") or "").strip() or f"Ocena AI: {suitable}."
    return _out(suitable, reason)


# ======================================================================
# FORMAT „to «X» — dopóki…"  —  render na serwerze  (13.08.2026)
# ----------------------------------------------------------------------
# ⛔ PO CO TO POWSTAŁO
# Ten format do dziś renderował się WYŁĄCZNIE w otwartej karcie przeglądarki
# (`generator-z2.js`). Znaczyło to, że automat nie umiał go uruchomić — a więc
# „losuj format przy każdej nowej puli treści" nie mogło działać: losowanie
# trafiałoby czasem na format, którego pipeline nie potrafi wywołać.
# Teraz oba formaty odpala się tak samo: jednym POST-em.
#
# ⛔ CZEGO TEN ENDPOINT NIE ROBI
# Nie liczy twarzy (ramki przychodzą gotowe z bazy, policzone raz detektorem)
# i nie sprawdza treści (bramka stoi w workerze i woła ją zamawiający).
# ======================================================================
import render_z2 as Z2


class Z2Zdjecie(BaseModel):
    u: str
    r: Optional[List[float]] = None        # [x, y, w, h] ramki twarzy w PROCENTACH


class Z2Zdjecia(BaseModel):
    ludzie: List[Z2Zdjecie] = []
    bez: List[Z2Zdjecie] = []


class Z2Req(BaseModel):
    temat: str
    linie: List[List[str]]                 # 6 × [zarzut, „dopóki…"]
    cta: List[str]                         # [hasło, zdanie]
    zdjecia: Z2Zdjecia
    czcionka_naglowka: str = ""
    czcionka_tresci: str = ""
    akcent: str = "#E8402A"
    podklad: str = ""
    drabinka: str = "domyslna"             # „domyslna" albo „wysoko" (Agnieszka)
    job_id: Optional[str] = None


@app.post("/render_z2")
def render_z2_endpoint(req: Z2Req, x_api_key: str = Header(default="")):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="bad api key")
    if len(req.linie) < 6:
        raise HTTPException(status_code=422, detail="format wymaga 6 linii „to «X» — dopóki…”")
    if len(req.cta) < 2:
        raise HTTPException(status_code=422, detail="cta musi mieć hasło i zdanie")

    marka = {
        "akcent": req.akcent,
        "podklad": (req.podklad or req.akcent),
        "czcionkaNag": req.czcionka_naglowka,
        "czcionkaTxt": req.czcionka_tresci,
    }
    tresc = {
        "temat": req.temat,
        "linie": [list(l)[:2] for l in req.linie[:6]],
        "cta": list(req.cta)[:2],
    }
    zdjecia = {
        "ludzie": [{"u": z.u, "r": z.r} for z in req.zdjecia.ludzie],
        "bez": [{"u": z.u, "r": z.r} for z in req.zdjecia.bez],
    }

    # ⛔ Brak czcionki nie wywala renderu (leci zapasowa), ale MUSI być widoczny
    # w odpowiedzi — inaczej klient dostałby karuzelę w cudzym kroju i nikt by
    # nie wiedział dlaczego.
    braki = [n for n, nag in ((req.czcionka_naglowka, True), (req.czcionka_tresci, False))
             if n and not Z2.czcionka_jest(n, nag)]

    # ⛔ TWARDY SUFIT NA PULĘ. Render ma 512 MB. Nawet po zmniejszeniu jedno zdjęcie
    # to ~9 MB w pamięci, więc przy pięćdziesięciu wejściowych adresach instancja
    # znów by padła. Format bierze 7 plansz i szuka najwyżej 12 kandydatów na planszę —
    # większa pula nic nie wnosi, a kosztuje pamięć.
    MAKS_ZDJEC = 20
    zdjecia["ludzie"] = zdjecia["ludzie"][:MAKS_ZDJEC]
    zdjecia["bez"] = zdjecia["bez"][:MAKS_ZDJEC]

    # ⛔ Jedna karuzela naraz. Dwie równoległe to 2 × ~220 MB = restart instancji.
    with _ZAMEK_CIEZKI:
        wyn = Z2.zrob_karuzele(marka, tresc, zdjecia,
                               {"drabinka": req.drabinka, "pobierz": _download})
    if "err" in wyn:
        return JSONResponse({"ok": False, "err": wyn["err"], "rap": wyn.get("rap", [])},
                            status_code=422)

    job = req.job_id or uuid.uuid4().hex[:12]
    out_dir = os.path.join(STATIC_DIR, job)
    os.makedirs(out_dir, exist_ok=True)
    urls = []
    for n, img in enumerate(wyn["plansze"], 1):
        nazwa = "z2-%d.jpg" % n
        img.save(os.path.join(out_dir, nazwa), "JPEG", quality=92)
        urls.append("%s/static/%s/%s" % (BASE_URL, job, nazwa))

    return JSONResponse({
        "ok": True, "job_id": job, "count": len(urls), "slides": urls,
        "ludzi": wyn["ludzi"], "rap": wyn["rap"],
        "brakujace_czcionki": braki,
    })
