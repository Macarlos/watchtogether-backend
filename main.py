"""
Watch2Night — backend proxy
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
import random
import time
from collections import defaultdict
import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

app = FastAPI(title="Watch2Night API")

# ── Basic rate limiting ──
# In-memory per-IP counter — fine for a single Render instance (no separate
# workers, no external store needed). This isn't meant to stop a determined
# attacker, just to keep one bot or misbehaving client from silently burning
# through the whole month's Watchmode credit allowance in an afternoon.
RATE_LIMIT_WINDOW_SECONDS = 60
RATE_LIMIT_MAX_REQUESTS = 30  # generous for real browsing, tight for scripted hammering

_request_log = defaultdict(list)

class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # Render sits behind a proxy — the real client IP is in X-Forwarded-For,
        # not request.client.host (which would just be the proxy's own IP).
        forwarded = request.headers.get("x-forwarded-for", "")
        client_ip = forwarded.split(",")[0].strip() if forwarded else (
            request.client.host if request.client else "unknown"
        )

        now = time.time()
        timestamps = _request_log[client_ip]
        cutoff = now - RATE_LIMIT_WINDOW_SECONDS
        while timestamps and timestamps[0] < cutoff:
            timestamps.pop(0)

        if len(timestamps) >= RATE_LIMIT_MAX_REQUESTS:
            return JSONResponse(
                status_code=429,
                content={"detail": "Too many requests — please slow down and try again in a moment."},
            )

        timestamps.append(now)
        return await call_next(request)

app.add_middleware(RateLimitMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# ── Generic in-memory TTL cache ──
# Watchmode bills per API call, and /api/discover in particular can cost
# 15-25+ credits for a single request (see the enrichment loop below). Two
# visitors hitting the same filters — or the same visitor reloading, or the
# poster wallpaper firing on every page load — would otherwise each pay that
# cost separately. This cache lets identical requests within a short window
# share one Watchmode round-trip instead. In-memory only (resets on redeploy,
# not shared across instances) — the same accepted tradeoff as the existing
# rate limiter and stats counters, fine for Render's single-instance setup.
_ttl_cache = {}

def cache_get(key):
    entry = _ttl_cache.get(key)
    if entry is None:
        return None
    expires_at, value = entry
    if time.time() > expires_at:
        del _ttl_cache[key]
        return None
    return value

def cache_set(key, value, ttl_seconds):
    _ttl_cache[key] = (time.time() + ttl_seconds, value)
    # Simple cap so this can't grow unbounded over a long-running process —
    # if it ever gets large, just drop the oldest quarter of entries.
    if len(_ttl_cache) > 500:
        oldest_keys = sorted(_ttl_cache, key=lambda k: _ttl_cache[k][0])[:125]
        for k in oldest_keys:
            del _ttl_cache[k]

WATCHMODE_API_KEY = os.environ.get("WATCHMODE_API_KEY", "")
WATCHMODE_BASE = "https://api.watchmode.com/v1"

# ── Anonymous, aggregate-only usage stats ──
# No per-user data, no IPs, no identifiers — just running totals of which
# platforms/genres/content types get requested, purely from traffic that's
# already happening on existing endpoints (no new frontend calls needed).
# In-memory, so this resets on redeploy — same tradeoff as everything else
# on Render's free tier. Viewed via /api/debug-discover's "current_stats" key.
_stats = {
    "total_page_loads": 0,  # sessions, not unique users — no identifier is ever stored
    "total_discover_calls": 0,
    "total_search_calls": 0,
    "total_provider_checks": 0,
    "cache_hits": 0,  # requests served without spending any Watchmode credits
    "platform_counts": defaultdict(int),
    "genre_counts": defaultdict(int),
    "content_type_counts": defaultdict(int),
}

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


def build_result_from_details(d):
    """Maps a raw Watchmode /title/{id}/details/ response into the shape
    the frontend expects. Shared between /api/discover (many titles) and
    /api/title/{id} (a single title, used for shareable favorite links)."""
    return {
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
    }


@app.get("/")
def root():
    return {"status": "ok", "service": "Watch2Night API"}


@app.post("/api/ping")
def ping():
    """Anonymous session counter — increments once per page load. No cookie,
    no identifier, nothing that could distinguish one visitor from another;
    just a running tally of how many times the app has been opened."""
    _stats["total_page_loads"] += 1
    return {"ok": True}


@app.get("/api/debug-discover")
def debug_discover(stats_only: bool = Query(False, description="If true, skip the live Watchmode diagnostic calls and just return usage stats")):
    """
    TEMPORARY diagnostic endpoint — runs three isolated Watchmode calls
    server-side (using the already-configured key) so we can see which
    parameter is causing empty/failed results, without ever putting the
    API key in a browser URL. Delete this endpoint once things work.

    Also doubles as a viewer for anonymous aggregate usage stats (see
    "current_stats" in the response) — pass ?stats_only=true to check
    those without triggering the (credit-costing) diagnostic test calls.
    """
    if stats_only:
        return {"current_stats": _stats}

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
        # Checking the search endpoint before building text search on top of it.
        "test_search": run(
            {"apiKey": WATCHMODE_API_KEY, "search_field": "name", "search_value": "dune"},
            path="search"
        ),
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

    result["current_stats"] = _stats
    return result


@app.get("/api/discover")
async def discover(
    platforms: str = Query("", description="Comma-separated platform ids, e.g. netflix,hbo"),
    moods: str = Query("", description="Comma-separated mood ids, e.g. comedy,romance"),
    region: str = Query("US", description="ISO country code — Watchmode's free plan only supports US"),
    limit: int = Query(16, description="How many enriched results to return — keep moderate to conserve API credits"),
    page: int = Query(1, description="Watchmode results page — used for 'load more' without repeating titles already seen"),
    content_type: str = Query("movie", description="'movie' or 'tv_series'"),
):
    if not WATCHMODE_API_KEY:
        raise HTTPException(status_code=500, detail="WATCHMODE_API_KEY is not configured on the server.")

    _stats["total_discover_calls"] += 1
    _stats["content_type_counts"][content_type] += 1
    for p in platforms.split(","):
        if p:
            _stats["platform_counts"][p] += 1
    for m in moods.split(","):
        if m:
            _stats["genre_counts"][m] += 1

    cache_key = ("discover", platforms, moods, region, limit, page, content_type)
    cached = cache_get(cache_key)
    if cached is not None:
        _stats["cache_hits"] += 1
        return cached

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
            empty_result = {"results": [], "count": 0}
            cache_set(cache_key, empty_result, ttl_seconds=1200)
            return empty_result

        # Shuffle before slicing so two sessions with identical filters don't
        # always show the same popularity-sorted titles in the same order —
        # combined with the frontend starting each new session on a random
        # page, this makes repeated sessions feel meaningfully different.
        random.shuffle(candidates)

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

    results = []
    for d in detail_results:
        if len(results) >= limit:
            break
        if not d:
            continue
        results.append(build_result_from_details(d))

    response = {"results": results, "count": len(results)}
    # 20 min TTL — long enough that repeated/concurrent visitors with similar
    # filters (or the poster wallpaper, which always uses the same unfiltered
    # request) share one Watchmode round-trip, short enough that availability
    # data doesn't go stale for long.
    cache_set(cache_key, response, ttl_seconds=1200)
    return response


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


@app.get("/api/title/{title_id}")
async def get_title(title_id: int):
    """Fetches full details for one title by its Watchmode id — used when
    someone opens a shared favorites link, to resolve each id back into a
    real, current title (poster, description, ratings, etc.)."""
    if not WATCHMODE_API_KEY:
        raise HTTPException(status_code=500, detail="WATCHMODE_API_KEY is not configured on the server.")

    cache_key = ("title", title_id)
    cached = cache_get(cache_key)
    if cached is not None:
        _stats["cache_hits"] += 1
        return cached

    async with httpx.AsyncClient(timeout=15) as client:
        try:
            r = await client.get(
                f"{WATCHMODE_BASE}/title/{title_id}/details/",
                params={"apiKey": WATCHMODE_API_KEY},
            )
        except httpx.HTTPError as e:
            raise HTTPException(status_code=502, detail=f"Watchmode request failed: {e}")
        if r.status_code != 200:
            raise HTTPException(status_code=502, detail=f"Watchmode error {r.status_code}: {r.text}")

    result = build_result_from_details(r.json())
    # 6 hour TTL — title metadata (description, cast, poster) barely ever
    # changes, so this can be cached far longer than availability/pricing.
    cache_set(cache_key, result, ttl_seconds=21600)
    return result


@app.get("/api/search")
async def search_titles(query: str, content_type: str = Query("movie", description="'movie' or 'tv_series'")):
    """Text search for a specific title, used alongside swiping for when
    someone already knows what they want. Watchmode's /search/ endpoint
    returns lightweight matches (plus unrelated people_results, which we
    ignore) — we enrich the top candidates the same way /api/discover does,
    then keep only the ones matching the requested content type."""
    if not WATCHMODE_API_KEY:
        raise HTTPException(status_code=500, detail="WATCHMODE_API_KEY is not configured on the server.")

    _stats["total_search_calls"] += 1

    async with httpx.AsyncClient(timeout=15) as client:
        try:
            r = await client.get(
                f"{WATCHMODE_BASE}/search/",
                params={"apiKey": WATCHMODE_API_KEY, "search_field": "name", "search_value": query},
            )
        except httpx.HTTPError as e:
            raise HTTPException(status_code=502, detail=f"Watchmode request failed: {e}")
        if r.status_code != 200:
            raise HTTPException(status_code=502, detail=f"Watchmode error {r.status_code}: {r.text}")

        title_matches = [m for m in r.json().get("title_results", []) if m.get("resultType") == "title"][:15]
        if not title_matches:
            return {"results": [], "count": 0}

        semaphore = asyncio.Semaphore(6)

        async def fetch_one(title_id):
            cache_key = ("title_raw", int(title_id))
            cached = cache_get(cache_key)
            if cached is not None:
                _stats["cache_hits"] += 1
                return cached
            async with semaphore:
                for attempt in range(2):
                    try:
                        dr = await client.get(
                            f"{WATCHMODE_BASE}/title/{title_id}/details/",
                            params={"apiKey": WATCHMODE_API_KEY},
                        )
                        if dr.status_code == 200:
                            raw = dr.json()
                            cache_set(cache_key, raw, ttl_seconds=21600)
                            return raw
                    except httpx.HTTPError:
                        pass
            return None

        detail_results = await asyncio.gather(*[fetch_one(m["id"]) for m in title_matches])

    wanted_type = content_type if content_type in ("movie", "tv_series") else "movie"
    results = [
        build_result_from_details(d) for d in detail_results
        if d and d.get("type") == wanted_type
    ][:10]

    return {"results": results, "count": len(results)}


@app.get("/api/titles")
async def get_titles(ids: str):
    """Fetches multiple titles by id in one call, used for shared favorite
    links. Firing many separate /api/title/{id} requests from the browser
    at once was overloading Render's free-tier instance and causing most
    of them to silently fail — this does the same fetching server-side
    with capped concurrency and a retry per id, same pattern as /api/discover."""
    if not WATCHMODE_API_KEY:
        raise HTTPException(status_code=500, detail="WATCHMODE_API_KEY is not configured on the server.")

    id_list = [s.strip() for s in ids.split(",") if s.strip()]
    semaphore = asyncio.Semaphore(4)

    async with httpx.AsyncClient(timeout=15) as client:
        async def fetch_one(title_id):
            cache_key = ("title", int(title_id))
            cached = cache_get(cache_key)
            if cached is not None:
                _stats["cache_hits"] += 1
                return cached
            async with semaphore:
                for attempt in range(2):
                    try:
                        r = await client.get(
                            f"{WATCHMODE_BASE}/title/{title_id}/details/",
                            params={"apiKey": WATCHMODE_API_KEY},
                        )
                        if r.status_code == 200:
                            result = build_result_from_details(r.json())
                            cache_set(cache_key, result, ttl_seconds=21600)
                            return result
                    except httpx.HTTPError:
                        pass
            return None

        results = await asyncio.gather(*[fetch_one(tid) for tid in id_list])

    return {"results": [r for r in results if r]}


@app.get("/api/movie/{title_id}/providers")
async def movie_providers(title_id: int, region: str = "US"):
    if not WATCHMODE_API_KEY:
        raise HTTPException(status_code=500, detail="WATCHMODE_API_KEY is not configured on the server.")

    _stats["total_provider_checks"] += 1

    cache_key = ("providers", title_id, region)
    cached = cache_get(cache_key)
    if cached is not None:
        _stats["cache_hits"] += 1
        return cached

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

    response = {
        "subscription": dedupe(subscription),
        "rent": dedupe(rent),
        "buy": dedupe(buy),
        "free": dedupe(free),
    }
    # 1 hour TTL — pricing/availability doesn't shift fast enough to justify
    # spending a fresh credit on every single "final pick" view of the same title.
    cache_set(cache_key, response, ttl_seconds=3600)
    return response
