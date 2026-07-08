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
from fastapi import FastAPI, HTTPException, Header
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
