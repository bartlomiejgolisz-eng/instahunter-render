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
    type: str                       # cover | content | cta
    title: Optional[str] = None
    subtitle: Optional[str] = None
    tagline: Optional[str] = None
    count: Optional[int] = None
    number: Optional[int] = None
    heading: Optional[str] = None
    body: Optional[str] = None
    cta: Optional[str] = None


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
    job_id: Optional[str] = None    # do re-renderu tej samej karty (nadpisuje)


def _download(url: str) -> Optional[Image.Image]:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "InstaHunter/1.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return Image.open(io.BytesIO(r.read())).convert("RGB")
    except Exception:
        return None


def _download_bytes(url: str) -> Optional[bytes]:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "InstaHunter/1.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.read()
    except Exception:
        return None


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

    slides = [s.model_dump() for s in req.slides]
    paths = R.render_carousel(brand, slides, out_dir, photos=photos or None)
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


def parse_carousel_tokens(raw: str):
    """Surowy blok tokenów Claude -> (slides[dict], caption, temat).

    Tokeny (z prompt-karuzele-TRESC): TEMAT, SLAJD, TYTUL, PODTYTUL, TAGLINE,
    NAGLOWEK, TRESC, CTA, END, CAPTION (+ opcjonalne TYP, NUMER, LICZBA).
    Typ slajdu wnioskowany: TYTUL -> cover; CTA -> cta; reszta -> content.
    Content numerowane automatycznie 1..N, cover.count = liczba content.
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
                cur[tok] = val
    if cur is not None:
        raw_slides.append(cur)

    n_content = sum(1 for s in raw_slides
                    if "TYTUL" not in s and "CTA" not in s
                    and s.get("TYP", "").lower() != "cover"
                    and s.get("TYP", "").lower() != "cta")

    def _int(v):
        v = (v or "").strip()
        return int(v) if v.isdigit() else None

    slides, num = [], 0
    for s in raw_slides:
        typ = s.get("TYP", "").lower()
        if "TYTUL" in s or typ == "cover":
            slides.append({
                "type": "cover",
                "title": s.get("TYTUL", ""),
                "subtitle": s.get("PODTYTUL", ""),
                "tagline": s.get("TAGLINE", ""),
                "count": _int(s.get("LICZBA")) or n_content,
            })
        elif "CTA" in s or typ == "cta":
            slides.append({
                "type": "cta",
                "heading": s.get("NAGLOWEK", ""),
                "body": s.get("TRESC", ""),
                "cta": s.get("CTA", ""),
            })
        else:
            num += 1
            slides.append({
                "type": "content",
                "number": _int(s.get("NUMER")) or num,
                "heading": s.get("NAGLOWEK", ""),
                "body": s.get("TRESC", ""),
            })
    return slides, caption, temat


def build_readable(slides):
    """Czytelna, przyjazna klientowi wersja treści karuzeli (bez tokenów, bez em-dash).

    Trafia do widocznego pola „Scenariusz / treść"; surowe tokeny idą do ukrytego
    pola technicznego (potrzebne tylko do re-renderu).
    """
    lines = []
    for s in slides:
        t = s.get("type")
        if t == "cover":
            lines.append("OKŁADKA")
            if s.get("title"):
                lines.append(s["title"])
            if s.get("subtitle"):
                lines.append(s["subtitle"])
            if s.get("tagline"):
                lines.append(s["tagline"])
        elif t == "cta":
            head = s.get("heading", "")
            lines.append("CTA" + (": " + head if head else ""))
            if s.get("body"):
                lines.append(s["body"])
            if s.get("cta"):
                lines.append("👉 " + s["cta"])
        else:
            num = s.get("number") or (len([l for l in lines if l.startswith("SLAJD")]) + 1)
            head = s.get("heading", "")
            lines.append("SLAJD " + str(num) + (": " + head if head else ""))
            if s.get("body"):
                lines.append(s["body"])
        lines.append("")
    return "\n".join(lines).strip()


def _hex_or(default: str, v: str):
    v = (v or "").strip()
    if not v:
        return default
    return v if v.startswith("#") else "#" + v


@app.post("/render_tokens")
async def render_tokens_endpoint(
    request: Request,
    x_api_key: str = Header(default=""),
    bg: str = "", bg_alt: str = "", accent: str = "", taupe: str = "",
    white: str = "", handle: str = "", glow: bool = True, ornaments: bool = True,
    photo: List[str] = Query(default=[]),
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
        glow=glow, ornaments=ornaments,
    )

    photos = []
    for u in photo:
        img = _download(u)
        if img is not None:
            photos.append(img)

    paths = R.render_carousel(brand, slides, out_dir, photos=photos or None)
    urls = [f"{BASE_URL}/static/{job}/{os.path.basename(p)}" for p in paths]
    title = next((s.get("title", "") for s in slides if s.get("type") == "cover"), "")
    readable = build_readable(slides)
    return JSONResponse({
        "job_id": job, "count": len(urls), "slides": urls,
        "title": title, "temat": temat, "caption": caption,
        "readable": readable,
    })


def _vision_bytes(raw: bytes):
    """Przygotowuje bajty do wizji: downscale do max 1200px dłuższego boku, JPEG."""
    try:
        im = Image.open(io.BytesIO(raw)).convert("RGB")
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
        raise HTTPException(status_code=422, detail="nie udało się pobrać zdjęcia")
    try:
        img = Image.open(io.BytesIO(raw)).convert("RGB")
    except Exception:
        raise HTTPException(status_code=422, detail="nieprawidłowy plik obrazu")

    w, h = img.size
    orientation = R.orientation_of(w, h)
    det_ok = R.cover_photo_ok(img)

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
    v = _vision_eval(_vision_bytes(raw), "image/jpeg")
    if v is None:
        return _out("tak", f"Proporcje i rozdzielczość OK ({orientation}, {w}x{h}). "
                           "Ocena wizualna pominięta (brak klucza wizji).")
    if "_error" in v:
        return _out("tak", f"Proporcje i rozdzielczość OK ({orientation}, {w}x{h}). "
                           f"Wizja niedostępna: {v['_error']}")
    suitable = "tak" if v.get("suitable") else "nie"
    reason = (v.get("reason") or "").strip() or f"Ocena AI: {suitable}."
    return _out(suitable, reason)
