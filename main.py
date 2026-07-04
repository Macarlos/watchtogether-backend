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
import asyncio
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

# ── Mapping: our mood/genre chips -> Watchmode's own genre ids ──
# Confirmed via a live call to /genres/. Most chips now map directly to a
# real Watchmode genre (clearer and more accurate than fuzzy "mood" guessing);
# a few (feel-good, mind-bending, nostalgic) stay as genre combinations.
MOOD_TO_GENRES = {
    "comedy":     [4],
    "thriller":   [17],
    "romance":    [14],
    "horror":     [11],
    "action":     [1],
    "scifi":      [15],
    "fantasy":    [9],
    "animated":   [3],
    "documentary":[6],
    "crime":      [5],
    "history":    [10],
    "war":        [18],
    "western":    [19],
    "mystery":    [13],
    "family":     [8],
    "musical":    [12, 32],   # Music, Musical
    "feelgood":   [4, 8],     # Comedy, Family
    "mindbend":   [13, 15],   # Mystery, Science Fiction
    "nostalgic":  [7],        # Drama (rough approximation)
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

    def run(params, path="list-titles"):
        try:
            r = httpx.get(f"{WATCHMODE_BASE}/{path}/", params=params, timeout=15)
            return {"status": r.status_code, "body": r.json() if r.status_code == 200 else r.text}
        except Exception as e:
            return {"status": "error", "body": str(e)}

    base = {"apiKey": WATCHMODE_API_KEY, "types": "movie", "regions": "US"}

    result = {
        "test_1_no_filters": run(dict(base)),
        "test_2_netflix_only": run({**base, "source_ids": "203"}),
        "test_3_genre_only": run({**base, "genres": "4"}),
        "real_genre_list": run({"apiKey": WATCHMODE_API_KEY}, path="genres"),
        "test_tv_series": run({"apiKey": WATCHMODE_API_KEY, "types": "tv_series", "regions": "US", "sort_by": "popularity_desc"}),
        # Comparing comma vs pipe for combining Netflix (203) + Disney+ (372).
        # If comma means AND (must be on both), the count will be tiny.
        # If pipe means OR (on either), the count should be much larger.
        "test_sources_comma": run({**base, "source_ids": "203,372"}),
        "test_sources_pipe": run({**base, "source_ids": "203|372"}),
    }

    # Pull out just the first title's raw fields so we can see exactly
    # what list-titles actually returns (vs. what we assumed it returns).
    try:
        first_title = result["test_1_no_filters"]["body"]["titles"][0]
        result["sample_title_raw_fields"] = first_title
        sample_id = first_title["id"]
        result["sample_title_details_raw"] = run(
            {"apiKey": WATCHMODE_API_KEY}, path=f"title/{sample_id}/details"
        )
        result["sample_title_sources_raw"] = run(
            {"apiKey": WATCHMODE_API_KEY, "regions": "US"}, path=f"title/{sample_id}/sources"
        )
        result["all_sources_list_raw"] = run(
            {"apiKey": WATCHMODE_API_KEY}, path="sources"
        )
        tv_title = result["test_tv_series"]["body"]["titles"][0]
        result["sample_tv_raw_fields"] = tv_title
        result["sample_tv_details_raw"] = run(
            {"apiKey": WATCHMODE_API_KEY}, path=f"title/{tv_title['id']}/details"
        )
        # Checking whether Watchmode has a cast/crew endpoint at all, and
        # whether it includes photos, before building any UI around it.
        result["sample_cast_crew_raw"] = run(
            {"apiKey": WATCHMODE_API_KEY}, path=f"title/{sample_id}/cast-crew"
        )
    except Exception as e:
        result["sample_title_raw_fields"] = f"couldn't extract: {e}"

    return result


@app.get("/api/discover")
async def discover(
    platforms: str = Query("", description="Comma-separated platform ids, e.g. netflix,hbo"),
    moods: str = Query("", description="Comma-separated mood ids, e.g. comedy,romance"),
    time_budget: Optional[str] = Query(None, description="90 | 120 | long"),
    region: str = Query("US", description="ISO country code — Watchmode's free plan only supports US"),
    limit: int = Query(16, description="How many enriched results to return — keep moderate to conserve API credits"),
    page: int = Query(1, description="Watchmode results page — used for 'load more' without repeating titles already seen"),
    content_type: str = Query("movie", description="'movie' or 'tv_series'"),
):
    if not WATCHMODE_API_KEY:
        raise HTTPException(status_code=500, detail="WATCHMODE_API_KEY is not configured on the server.")

    source_ids = [PLATFORM_TO_WATCHMODE[p] for p in platforms.split(",") if p in PLATFORM_TO_WATCHMODE]
    genre_ids = set()
    for m in moods.split(","):
        for g in MOOD_TO_GENRES.get(m, []):
            genre_ids.add(g)

    # ── Stage 1: cheap filtered list (1 credit) ──
    list_params = {
        "apiKey": WATCHMODE_API_KEY,
        "types": content_type if content_type in ("movie", "tv_series") else "movie",
        "regions": region,
        "sort_by": "popularity_desc",
        "page": page,
    }
    if source_ids:
        list_params["source_ids"] = ",".join(str(s) for s in source_ids)
    if genre_ids:
        list_params["genres"] = ",".join(str(g) for g in genre_ids)

    async with httpx.AsyncClient(timeout=15) as client:
        r = None
        last_error = None
        for attempt in range(3):  # this call had zero retry before — a bare failure here killed the whole request
            try:
                r = await client.get(f"{WATCHMODE_BASE}/list-titles/", params=list_params)
                if r.status_code == 200:
                    break
                last_error = f"Watchmode error {r.status_code}: {r.text}"
            except httpx.HTTPError as e:
                last_error = f"Watchmode request failed: {e}"
            r = None
            await asyncio.sleep(0.8)

        if r is None:
            raise HTTPException(status_code=502, detail=last_error or "Watchmode request failed after retries")

        candidates = r.json().get("titles", [])
        if not candidates:
            return {"results": [], "count": 0}

        # A tight time_budget filter (90 or 120 min) discards a lot of candidates,
        # so pull a bigger pool up front in that case. "long" no longer filters
        # by runtime at all (see fits_time_budget below), so it doesn't need this.
        # Kept moderate (not pushed higher) — too many concurrent detail-fetch
        # requests at once was overloading Render's free-tier 0.1 CPU instance
        # and causing the list-titles call itself to time out.
        if time_budget in ("90", "120"):
            buffer_size = min(limit * 3, len(candidates), 30)
        else:
            buffer_size = min(limit * 2, len(candidates), 24)
        candidate_ids = [c["id"] for c in candidates[:buffer_size]]

        # ── Stage 2: enrich in parallel, but capped in concurrency — firing all
        # of them at once was too much load for a 0.1 CPU instance and was
        # causing the earlier list-titles call itself to time out. ──
        semaphore = asyncio.Semaphore(8)

        async def fetch_details(title_id):
            async with semaphore:
                for attempt in range(2):  # one retry — a handful of these were failing silently under load
                    try:
                        dr = await client.get(
                            f"{WATCHMODE_BASE}/title/{title_id}/details/",
                            params={"apiKey": WATCHMODE_API_KEY},
                        )
                        if dr.status_code == 200:
                            return dr.json()
                    except httpx.HTTPError:
                        pass
            return None

        detail_results = await asyncio.gather(*[fetch_details(tid) for tid in candidate_ids])

    def fits_time_budget(minutes):
        if not minutes or not time_budget:
            return True
        if time_budget == "90":
            return minutes <= 100
        if time_budget == "120":
            return 90 <= minutes <= 140
        # "long" (All night) means plenty of time available, not specifically
        # a single very long film — so it applies no runtime constraint at all,
        # same as no time filter. Filtering for 130+ minutes here was starving
        # results, since most popular titles are 90-140 minutes.
        return True

    results = []
    for d in detail_results:
        if len(results) >= limit:
            break
        if not d or not fits_time_budget(d.get("runtime_minutes")):
            continue

        results.append({
            "id": d.get("id"),
            "title": d.get("title"),
            "year": d.get("year"),
            "end_year": d.get("end_year"),
            "overview": d.get("plot_overview", ""),
            "will_you_like_this": d.get("will_you_like_this", ""),
            "poster_url": d.get("poster"),
            "backdrop_url": d.get("backdrop"),
            "genres": d.get("genre_names", []),
            "runtime_minutes": d.get("runtime_minutes"),
            "rating": d.get("user_rating"),
            "critic_score": d.get("critic_score"),
            "content_rating": d.get("us_rating"),
            "trailer_url": d.get("trailer"),
            "watchmode_id": d.get("id"),
        })

    return {"results": results, "count": len(results)}


# ── In-memory cache of Watchmode's full source list (id -> name/logo/type). ──
# This list is small (~280 entries) and effectively static, so we fetch it
# once per server process rather than on every request.
_sources_cache = None

async def get_sources_lookup(client):
    global _sources_cache
    if _sources_cache is not None:
        return _sources_cache
    r = await client.get(f"{WATCHMODE_BASE}/sources/", params={"apiKey": WATCHMODE_API_KEY})
    if r.status_code != 200:
        return {}
    lookup = {}
    for s in r.json():
        lookup[s.get("id")] = {
            "name": s.get("name"),
            "logo_url": s.get("logo_100px"),
        }
    _sources_cache = lookup
    return lookup


@app.get("/api/movie/{title_id}/providers")
async def movie_providers(title_id: int, region: str = "US"):
    if not WATCHMODE_API_KEY:
        raise HTTPException(status_code=500, detail="WATCHMODE_API_KEY is not configured on the server.")

    async with httpx.AsyncClient(timeout=15) as client:
        try:
            r = await client.get(
                f"{WATCHMODE_BASE}/title/{title_id}/sources/",
                params={"apiKey": WATCHMODE_API_KEY, "regions": region},
            )
        except httpx.HTTPError as e:
            raise HTTPException(status_code=502, detail=f"Watchmode request failed: {e}")
        if r.status_code != 200:
            raise HTTPException(status_code=502, detail=f"Watchmode error {r.status_code}: {r.text}")

        sources = r.json()
        logos = await get_sources_lookup(client)

    def clean(s):
        logo = logos.get(s.get("source_id"), {}).get("logo_url")
        return {
            "name": s.get("name"),
            "logo_url": logo,
            "price": s.get("price"),
            "format": s.get("format"),
            "web_url": s.get("web_url"),
        }

    subscription = [clean(s) for s in sources if s.get("type") == "sub"]
    rent = [clean(s) for s in sources if s.get("type") == "rent"]
    buy = [clean(s) for s in sources if s.get("type") == "buy"]
    free = [clean(s) for s in sources if s.get("type") == "free"]

    # De-duplicate by name within each group (Watchmode sometimes lists the
    # same platform twice for different formats, e.g. HD and 4K).
    def dedupe(items):
        seen = set()
        out = []
        for i in items:
            if i["name"] in seen:
                continue
            seen.add(i["name"])
            out.append(i)
        return out

    return {
        "subscription": dedupe(subscription),
        "rent": dedupe(rent),
        "buy": dedupe(buy),
        "free": dedupe(free),
    }