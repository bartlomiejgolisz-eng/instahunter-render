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
import os, io, uuid, time, urllib.request
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
    return JSONResponse({
        "job_id": job, "count": len(urls), "slides": urls,
        "title": title, "temat": temat, "caption": caption,
    })
