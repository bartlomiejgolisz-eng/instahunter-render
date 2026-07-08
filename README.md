# InstaHunter — usługa renderu karuzel

Centralna mikro-usługa (FastAPI + Pillow) renderująca karuzele Instagram 1080×1350 (Format A, brand-aware).
**Multi-tenant:** jedna instancja obsługuje wszystkich klientów; brand przekazywany per request z Airtable (patrz USTALENIA cz. 67).

## Endpointy
- `GET /health` → `{ok:true}`
- `POST /render` (nagłówek `X-API-Key: <RENDER_API_KEY>`) → renderuje N PNG, zwraca `{job_id, count, slides:[url...]}`
  - body: `{ brand:{bg,accent,taupe,white,handle,...}, slides:[...], photo_urls:[...], job_id? }`
  - `job_id` reużyty = re-render (nadpisuje) → obsługa „Zastosuj uwagi".

## Zmienne środowiskowe (Render.com → Environment)
- `RENDER_API_KEY` — dowolne długie hasło; ten sam wpisujemy w module HTTP w Make.
- `BASE_URL` — publiczny adres usługi (np. `https://instahunter-render.onrender.com`). Usługa buduje z niego URL-e PNG.

## Fonty
Wgraj do `fonts/` przed pushem: `SpaceGrotesk-Bold.ttf`, `SpaceGrotesk-Medium.ttf` (+ opcjonalnie `DMSans-Regular.ttf`).
Bez nich render używa DejaVu (fallback). Docelowo: biblioteka fontów per klient (ZADANIA).

## Deploy (Render.com, Docker)
Render wykrywa `Dockerfile`. Usługa nasłuchuje na `$PORT`. Darmowy tier „zasypia" po bezczynności (~30 s zimny start) — dla 2 karuzel/tydz. bez znaczenia.
Uwaga: dysk efemeryczny — PNG-i żyją tylko do restartu; Airtable pobiera je od razu po renderze (pole załącznika trzyma własną kopię), więc to nie problem.

## Lokalnie
```
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8080
```
