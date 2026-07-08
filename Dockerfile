# InstaHunter — renderer karuzel (mikro-usługa)
FROM python:3.11-slim

# Fonty: Space Grotesk (produkcja) + Poppins/DejaVu (fallback). fontconfig do fc-list.
RUN apt-get update && apt-get install -y --no-install-recommends \
      fontconfig fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Kopiujemy całą zawartość repo (render.py, app.py, opcjonalnie fonts/).
# render.py szuka fontów w ./fonts (SpaceGrotesk-Bold.ttf itd.); brak = fallback DejaVu.
COPY . .
# Katalogi muszą istnieć nawet gdy puste (fonts opcjonalne, static tworzy runtime).
RUN mkdir -p fonts static

ENV BASE_URL="" RENDER_API_KEY="" STATIC_DIR="/app/static" PORT=8080
EXPOSE 8080
# Render/Railway wstrzykują $PORT — honorujemy go (fallback 8080 lokalnie).
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-8080}"]
