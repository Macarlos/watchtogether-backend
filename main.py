"""
Reel — backend proxy
Wraps Watchmode's search + streaming-availability data behind a simple
endpoint the frontend can call without exposing the Watchmode API key
in the browser.

Switched from TMDB to Watchmode because TMDB's free tier explicitly
excludes commercial use (including ad-supported apps) and requires a
$149/month commercial license. Watchmode's free tier (2,500 requests/
month, no card required) is built for exactly this use case.
"""

import os
import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional

app = FastAPI(title="Reel API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

WATCHMODE_API_KEY = os.environ.get("WATCHMODE_API_KEY", "")
WATCHMODE_BASE = "https://api.watchmode.com/v1"

# ── Mapping: our frontend platform ids -> Watchmode source ids ──
# Watchmode source ids are looked up once via /sources and hardcoded here,
# since they rarely change. Confirm these against /sources before launch.
PLATFORM_TO_WATCHMODE = {
    "netflix": 203,
    "hbo":     387,   # HBO Max / Max
    "disney":  372,
    "prime":   26,
    "apple":   371,
    "hulu":    157,
}

# ── Mapping: our mood chips -> closest Watchmode/TMDB-style genre ids ──
# Watchmode uses TMDB-compatible genre ids for movies, so this mapping
# carries over. These are approximations — refine later with keywords.
MOOD_TO_GENRES = {
    "feelgood":  [35, 10751],   # Comedy, Family
    "mindbend":  [9648, 878],   # Mystery, Sci-Fi
    "comedy":    [35],
    "thriller":  [53],
    "romance":   [10749],
    "nostalgic": [18],          # Drama (rough approximation)
}

GENRE_ID_TO_NAME = {
    35: "Comedy", 10751: "Family", 9648: "Mystery", 878: "Sci-Fi",
    53: "Thriller", 10749: "Romance", 18: "Drama",
}


@app.get("/")
def root():
    return {"status": "ok", "service": "Reel API"}


@app.get("/api/debug-discover")
def debug_discover():
    """
    TEMPORARY diagnostic endpoint — runs three isolated Watchmode calls
    server-side (using the already-configured key) so we can see which
    parameter is causing empty/failed results, without ever putting the
    API key in a browser URL. Delete this endpoint once things work.
    """
    if not WATCHMODE_API_KEY:
        raise HTTPException(status_code=500, detail="WATCHMODE_API_KEY is not configured on the server.")

    def run(params):
        try:
            r = httpx.get(f"{WATCHMODE_BASE}/list-titles/", params=params, timeout=15)
            return {"status": r.status_code, "body": r.json() if r.status_code == 200 else r.text}
        except Exception as e:
            return {"status": "error", "body": str(e)}

    base = {"apiKey": WATCHMODE_API_KEY, "types": "movie", "regions": "US"}

    return {
        "test_1_no_filters": run(dict(base)),
        "test_2_netflix_only": run({**base, "source_ids": "203"}),
        "test_3_genre_only": run({**base, "genres": "35"}),
    }


@app.get("/api/discover")
def discover(
    platforms: str = Query("", description="Comma-separated platform ids, e.g. netflix,hbo"),
    moods: str = Query("", description="Comma-separated mood ids, e.g. comedy,romance"),
    time_budget: Optional[str] = Query(None, description="90 | 120 | long"),
    region: str = Query("US", description="ISO country code — Watchmode's free plan only supports US"),
):
    if not WATCHMODE_API_KEY:
        raise HTTPException(status_code=500, detail="WATCHMODE_API_KEY is not configured on the server.")

    source_ids = [PLATFORM_TO_WATCHMODE[p] for p in platforms.split(",") if p in PLATFORM_TO_WATCHMODE]
    genre_ids = set()
    for m in moods.split(","):
        for g in MOOD_TO_GENRES.get(m, []):
            genre_ids.add(g)

    params = {
        "apiKey": WATCHMODE_API_KEY,
        "types": "movie",
        "regions": region,
        "sort_by": "popularity_desc",
    }
    if source_ids:
        params["source_ids"] = ",".join(str(s) for s in source_ids)
    if genre_ids:
        params["genres"] = ",".join(str(g) for g in genre_ids)

    try:
        r = httpx.get(f"{WATCHMODE_BASE}/list-titles/", params=params, timeout=15)
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Watchmode request failed: {e}")

    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Watchmode error {r.status_code}: {r.text}")

    data = r.json()
    titles = data.get("titles", [])[:20]

    results = []
    for t in titles:
        results.append({
            "id": t.get("id"),
            "title": t.get("title"),
            "year": t.get("year"),
            "poster_url": t.get("poster") or t.get("poster_url"),
            "genres": [GENRE_ID_TO_NAME.get(g, "") for g in (t.get("genre_ids") or []) if g in GENRE_ID_TO_NAME],
            "rating": t.get("imdb_rating") or t.get("critic_score"),
            "watchmode_id": t.get("id"),
        })

    return {"results": results, "count": len(results)}


@app.get("/api/movie/{title_id}/providers")
def movie_providers(title_id: int, region: str = "US"):
    if not WATCHMODE_API_KEY:
        raise HTTPException(status_code=500, detail="WATCHMODE_API_KEY is not configured on the server.")

    try:
        r = httpx.get(
            f"{WATCHMODE_BASE}/title/{title_id}/sources/",
            params={"apiKey": WATCHMODE_API_KEY, "regions": region},
            timeout=15,
        )
        r.raise_for_status()
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Watchmode request failed: {e}")

    sources = r.json()
    # Only keep subscription ("sub") sources for now — rent/buy can be added later.
    subs = [s for s in sources if s.get("type") == "sub"]
    return {
        "providers": [s.get("name") for s in subs],
        "links": [{"name": s.get("name"), "url": s.get("web_url")} for s in subs],
    }
