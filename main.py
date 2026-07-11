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
import re
import asyncio
import random
import time
from urllib.parse import quote_plus
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

# ── Movie of the Night ("Streaming Availability API") — replacing Watchmode ──
# Stage 1 of the migration: this powers /api/discover only. Watchmode is left
# untouched for search/providers/title endpoints until later stages.
MOTN_API_KEY = os.environ.get("MOTN_API_KEY", "")
MOTN_BASE = "https://api.movieofthenight.com/v4"

# ── Language support, split by who actually handles the translation ──
# MOTN's own output_language parameter covers en/es/fr/de directly — real
# official regional titles and data from their own database, not machine-
# translated guesses. Confirmed via their docs: MOTN does NOT support pt/hi/pl
# at all (no amount of parameter-passing changes that), so for those three we
# fall back to Groq for just the overview text — titles/genres stay in
# English there, since machine-translating official release titles risks
# confidently wrong results.
MOTN_NATIVE_LANGUAGES = {"en", "es", "fr", "de"}
GROQ_TRANSLATE_LANGUAGES = {"pt", "hi", "pl"}
LANGUAGE_NAMES = {"pt": "Portuguese", "hi": "Hindi", "pl": "Polish"}

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_BASE = "https://api.groq.com/openai/v1"
# gpt-oss-20b — Groq's recommended fast/cheap model as of their June 2026
# deprecation of llama-3.1-8b-instant. Plenty capable for a short synopsis.
GROQ_MODEL = "openai/gpt-oss-20b"

async def translate_overview_via_groq(text, target_lang):
    """Translates a movie/show overview into a language MOTN doesn't support
    natively. Deliberately narrow in scope — only called when someone views
    a title's full details (not per swipe-deck card), since Groq's free tier
    caps at 30 requests/minute and this needs to never be the bottleneck.
    Falls back to the original English text on any failure — a missing
    translation is far better than a broken detail page."""
    if not GROQ_API_KEY or not text:
        return text

    cache_key = ("groq_overview", hash(text), target_lang)
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    language_name = LANGUAGE_NAMES.get(target_lang, target_lang)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                f"{GROQ_BASE}/chat/completions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                json={
                    "model": GROQ_MODEL,
                    "messages": [
                        {
                            "role": "system",
                            "content": f"You translate movie and TV show synopses into {language_name}. Reply with ONLY the translated text — no quotes, no commentary, no explanation.",
                        },
                        {"role": "user", "content": text},
                    ],
                    "temperature": 0.3,
                },
            )
            if r.status_code == 200:
                translated = r.json()["choices"][0]["message"]["content"].strip()
                if translated:
                    # Long TTL — a synopsis translation never changes once done.
                    cache_set(cache_key, translated, ttl_seconds=2592000)  # 30 days
                    return translated
    except Exception:
        pass
    return text

# Catalog ids are identical to what Watch2Night already uses internally —
# confirmed via a live call to /countries/us, no mapping needed at all.
# (netflix, prime, disney, hbo, hulu, apple all match directly.)

# ── Mapping: our mood/genre chips -> Movie of the Night's genre ids ──
# Confirmed via a live call to /genres. Almost everything matches Watchmode's
# naming directly; three differences: "animation" not "animated", "music"
# only (no separate "musical" genre), and no analogous change for the rest.
MOOD_TO_MOTN_GENRES = {
    "comedy":     ["comedy"],
    "thriller":   ["thriller"],
    "romance":    ["romance"],
    "horror":     ["horror"],
    "action":     ["action"],
    "scifi":      ["scifi"],
    "fantasy":    ["fantasy"],
    "animated":   ["animation"],
    "documentary":["documentary"],
    "crime":      ["crime"],
    "history":    ["history"],
    "war":        ["war"],
    "western":    ["western"],
    "mystery":    ["mystery"],
    "family":     ["family"],
    "musical":    ["music"],
    "feelgood":   ["comedy", "family"],
    "mindbend":   ["mystery", "scifi"],
    "drama":      ["drama"],
}

# Confirmed-valid order_by values (seen directly in MOTN's own docs/examples).
# Randomizing this per session replaces the old "random page 1-5" trick for
# variety — MOTN's cursor-based pagination can't jump to a random page, but
# a randomized sort order gives a genuinely different top-of-list each time,
# while still allowing clean sequential cursor-walking for "load more".
MOTN_ORDER_BY_CHOICES = ["popularity_1year", "rating"]

# ── Anonymous, aggregate-only usage stats ──
# No per-user data, no IPs, no identifiers — just running totals of which
# platforms/genres/content types get requested, purely from traffic that's
# already happening on existing endpoints (no new frontend calls needed).
# In-memory, so this resets on redeploy — same tradeoff as everything else
# on Render's free tier. Viewed via /api/debug-discover's "current_stats" key.
_stats = {
    "total_page_loads": 0,  # sessions, not unique users — no identifier is ever stored
    "total_discover_calls": 0,
    "total_motn_api_calls": 0,  # real HTTP calls to Movie of the Night — 1 call = 1 request, no per-title enrichment cost
    "total_search_calls": 0,
    "total_provider_checks": 0,
    "cache_hits": 0,  # requests served without spending any Watchmode credits
    "platform_counts": defaultdict(int),
    "genre_counts": defaultdict(int),
    "content_type_counts": defaultdict(int),
}

# ── Monthly credit budget safety net ──
# Set via Render's environment variables — defaults to the free tier's 2,500.
# Bump this to your actual plan size (e.g. 40000) once/if upgraded. This is
# an estimate, not a precise billing counter (resets on redeploy, doesn't
# know Watchmode's actual billing-cycle boundary) — it exists to keep a
# runaway day from silently exhausting the whole month's quota with zero
# warning, not to track spend to the credit.
MONTHLY_CREDIT_BUDGET = int(os.environ.get("MONTHLY_CREDIT_BUDGET", "2500"))
BUDGET_SOFT_THRESHOLD = 0.85  # cut off decorative/non-essential usage (the wallpaper) first
BUDGET_HARD_THRESHOLD = 0.97  # cut off everything, including real browsing — better a
                              # friendly "extra busy" message than the raw 502s Watchmode
                              # itself returns once the quota is actually gone

_credits_used = 0

def record_credits(n):
    global _credits_used
    _credits_used += n

def budget_ok(hard=False):
    threshold = BUDGET_HARD_THRESHOLD if hard else BUDGET_SOFT_THRESHOLD
    return _credits_used < MONTHLY_CREDIT_BUDGET * threshold

def budget_info():
    return {
        "estimated_credits_used": _credits_used,
        "monthly_credit_budget": MONTHLY_CREDIT_BUDGET,
        "percent_used": round(100 * _credits_used / MONTHLY_CREDIT_BUDGET, 1) if MONTHLY_CREDIT_BUDGET else 0,
        "conservation_mode": not budget_ok(),  # decorative/wallpaper requests paused
        "hard_paused": not budget_ok(hard=True),  # everything paused, including real browsing
    }

def build_result_from_motn_show(show):
    """Maps a raw Movie of the Night 'show' object into the exact same shape
    build_result_from_details produces, so the frontend needs zero changes
    for this stage of the migration. A couple of fields have no MOTN
    equivalent (will_you_like_this, critic_score, content_rating) — left as
    None/empty rather than faking data; the frontend already handles these
    being absent gracefully (e.g. "Series" instead of a runtime).

    Confirmed via MOTN's own Show object reference (the full field list is
    explicitly documented): content_rating genuinely isn't part of their
    schema — not a gap in our integration, it's just not data they provide.
    """
    is_series = show.get("showType") == "series"
    poster_set = show.get("imageSet", {}).get("verticalPoster", {})
    backdrop_set = show.get("imageSet", {}).get("horizontalBackdrop", {})
    title = show.get("title")
    year = show.get("firstAirYear") if is_series else show.get("releaseYear")

    # MOTN's schema confirmed has no trailer field at all (unlike Watchmode).
    # Rather than show nothing, link to a YouTube search for it — same
    # "point them at a search rather than nothing" pattern already used for
    # the JustWatch fallback link when streaming info is missing.
    trailer_url = None
    if title:
        query = f"{title} {year} official trailer" if year else f"{title} official trailer"
        trailer_url = f"https://www.youtube.com/results?search_query={quote_plus(query)}"

    return {
        "id": show.get("id"),
        "title": title,
        "year": year,
        "end_year": show.get("lastAirYear") if is_series else None,
        "overview": show.get("overview", ""),
        "will_you_like_this": "",  # no MOTN equivalent
        "poster_url": poster_set.get("w480") or poster_set.get("w360"),
        "backdrop_url": backdrop_set.get("w720") or backdrop_set.get("w480"),
        "genres": [g.get("name") for g in show.get("genres", []) if g.get("name")],
        "runtime_minutes": None if is_series else show.get("runtime"),
        "rating": round(show["rating"] / 10, 1) if show.get("rating") is not None else None,
        "critic_score": None,  # MOTN has one unified rating, not separate user/critic scores
        "content_rating": None,  # confirmed absent from MOTN's schema
        "trailer_url": trailer_url,
        "watchmode_id": show.get("imdbId"),  # IMDb id — field name kept for frontend compatibility; MOTN's /shows/{id} lookup accepts this directly
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


# ── Supported regions & country auto-detection ──
# Watchmode's free tier allows choosing up to 3 countries; these are the
# ones configured on the account. Anything else falls back to US.
SUPPORTED_REGIONS = {"US", "CA", "IN", "GB", "AU", "MX", "ES", "BR", "FR", "DE", "PL"}
DEFAULT_REGION = "US"

# For the later UI-translation stage — which of the 7 supported languages
# each region should show. Not used yet (stage 1 is regions/platforms only),
# but defined alongside the region list since they're naturally paired.
REGION_TO_LANGUAGE = {
    "US": "en", "CA": "en", "IN": "en", "GB": "en", "AU": "en",
    "MX": "es", "ES": "es",
    "BR": "pt",
    "FR": "fr",
    "DE": "de",
    "PL": "pl",
}

@app.get("/api/detect-region")
async def detect_region(request: Request):
    """Best-effort IP -> country -> supported region lookup, purely to avoid
    making the visitor pick a country manually. Always falls back to
    DEFAULT_REGION on any failure — this is a nice-to-have, never something
    that should be allowed to break the app if the geolocation service is
    slow, down, or wrong."""
    forwarded = request.headers.get("x-forwarded-for", "")
    client_ip = forwarded.split(",")[0].strip() if forwarded else (
        request.client.host if request.client else None
    )

    if not client_ip or client_ip in ("unknown", "127.0.0.1", "localhost"):
        return {"region": DEFAULT_REGION, "detected_country": None}

    cache_key = ("geo", client_ip)
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    try:
        async with httpx.AsyncClient(timeout=3) as client:
            r = await client.get(f"https://free.freeipapi.com/api/json/{client_ip}")
        if r.status_code == 200:
            data = r.json()
            country_code = (data.get("countryCode") or "").upper()
            region = country_code if country_code in SUPPORTED_REGIONS else DEFAULT_REGION
            result = {"region": region, "detected_country": country_code or None}
            # Cache per IP for a while — this is a decorative feature, not
            # something that needs to be re-checked on every single request,
            # and it keeps us comfortably under freeipapi's free rate limit.
            cache_set(cache_key, result, ttl_seconds=3600)
            return result
    except Exception:
        pass

    return {"region": DEFAULT_REGION, "detected_country": None}


@app.get("/api/platforms")
async def get_platforms(region: str = "US"):
    """Real per-country platform list. Different countries have genuinely
    different top streaming services — confirmed via live checks against
    MOTN's /countries endpoint before building this (e.g. Hulu is US-only;
    the UK has iPlayer/ITVX; Australia has Stan; Spain/Poland have
    SkyShowtime instead of Paramount+). Netflix/Prime/Disney+/HBO/Apple TV
    are universal across every region checked so far."""
    region = region.upper() if region.upper() in SUPPORTED_REGIONS else DEFAULT_REGION

    if not MOTN_API_KEY:
        raise HTTPException(status_code=500, detail="MOTN_API_KEY is not configured on the server.")

    cache_key = ("platforms", region)
    cached = cache_get(cache_key)
    if cached is not None:
        _stats["cache_hits"] += 1
        return cached

    async with httpx.AsyncClient(timeout=15) as client:
        try:
            r = await client.get(
                f"{MOTN_BASE}/countries/{region.lower()}",
                headers={"X-API-Key": MOTN_API_KEY},
            )
        except httpx.HTTPError as e:
            raise HTTPException(status_code=502, detail=f"MOTN request failed: {e}")
        if r.status_code != 200:
            raise HTTPException(status_code=502, detail=f"MOTN error {r.status_code}: {r.text}")

        _stats["total_motn_api_calls"] += 1

    data = r.json()
    services = data.get("services", [])[:8]  # top 8 by popularity, per MOTN's own ordering
    platforms = []
    for s in services:
        images = s.get("imageSet", {})
        platforms.append({
            "id": s.get("id"),
            "label": s.get("name"),
            "logo_url": images.get("darkThemeImage") or images.get("whiteImage"),
        })

    response = {"platforms": platforms}
    # Long TTL — a country's set of top streaming services barely ever
    # changes, so this can be cached for a very long time.
    cache_set(cache_key, response, ttl_seconds=86400)
    return response


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
        return {"current_stats": _stats, "budget": budget_info()}

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
    result["budget"] = budget_info()
    return result


@app.get("/api/discover")
async def discover(
    platforms: str = Query("", description="Comma-separated platform ids, e.g. netflix,hbo"),
    moods: str = Query("", description="Comma-separated mood ids, e.g. comedy,romance"),
    region: str = Query("US", description="ISO country code — must be one of the 3 countries configured (US, CA, IN)"),
    limit: int = Query(16, description="How many results to return (MOTN's own page size is fixed ~20; this just truncates)"),
    page: int = Query(1, description="Our own sequential page number — MOTN uses cursor pagination under the hood, walked automatically"),
    content_type: str = Query("movie", description="'movie' or 'tv_series'"),
    order_by: str = Query("popularity_1year", description="Sort order — the frontend randomizes this once per session for variety, since MOTN can't jump to a random page"),
    language: str = Query("en", description="UI language — only en/es/fr/de get passed to MOTN as output_language; pt/hi/pl aren't supported by MOTN so this stays English for the swipe deck (overview translation happens only on the detail view)"),
):
    if not MOTN_API_KEY:
        raise HTTPException(status_code=500, detail="MOTN_API_KEY is not configured on the server.")

    motn_language = language if language in MOTN_NATIVE_LANGUAGES else "en"

    region = region.upper() if region.upper() in SUPPORTED_REGIONS else DEFAULT_REGION
    order_by = order_by if order_by in MOTN_ORDER_BY_CHOICES else MOTN_ORDER_BY_CHOICES[0]
    motn_content_type = "series" if content_type == "tv_series" else "movie"

    _stats["total_discover_calls"] += 1
    _stats["content_type_counts"][content_type] += 1
    for p in platforms.split(","):
        if p:
            _stats["platform_counts"][p] += 1
    for m in moods.split(","):
        if m:
            _stats["genre_counts"][m] += 1

    # Platform ids now vary by region (see /api/platforms) rather than a
    # fixed set of 6 — basic sanitization instead of a hardcoded allowlist.
    # MOTN itself just ignores unrecognized catalog ids, so this only needs
    # to guard against obviously malformed input, not validate real ids.
    catalogs = [p for p in platforms.split(",") if p and re.fullmatch(r"[a-z0-9_]+", p)]
    genre_ids = []
    seen_genres = set()
    for m in moods.split(","):
        for g in MOOD_TO_MOTN_GENRES.get(m, []):
            if g not in seen_genres:
                seen_genres.add(g)
                genre_ids.append(g)

    result_cache_key = ("discover_result", tuple(sorted(catalogs)), tuple(genre_ids), region, motn_content_type, order_by, page, limit, motn_language)
    cached = cache_get(result_cache_key)
    if cached is not None:
        _stats["cache_hits"] += 1
        # Shuffle a copy, not the cached list itself — the cache stays a
        # stable, reusable canonical order; each individual response gets
        # its own fresh shuffle so repeat visits with the same filters
        # (or the same order_by, since there are only 2 choices) don't see
        # an identical lineup every time.
        shuffled = cached["results"].copy()
        random.shuffle(shuffled)
        return {"results": shuffled, "count": len(shuffled)}

    # The poster wallpaper always calls with no platform/mood filter — cache
    # that combination far longer since it's purely decorative.
    is_unfiltered = not platforms and not moods
    discover_ttl = 14400 if is_unfiltered else 1200  # 4 hours vs 20 minutes

    page_cache_base = ("motn_page", tuple(sorted(catalogs)), tuple(genre_ids), region, motn_content_type, order_by, motn_language)

    async def fetch_motn_page(cursor):
        params = {"country": region.lower(), "show_type": motn_content_type}
        if catalogs:
            params["catalogs"] = ",".join(catalogs)
        if genre_ids:
            params["genres"] = ",".join(genre_ids)
            params["genres_relation"] = "or"
        if order_by:
            params["order_by"] = order_by
        if motn_language != "en":
            params["output_language"] = motn_language
        if cursor:
            params["cursor"] = cursor

        last_error = None
        async with httpx.AsyncClient(timeout=15) as client:
            for attempt in range(3):
                try:
                    r = await client.get(
                        f"{MOTN_BASE}/shows/search/filters",
                        params=params,
                        headers={"X-API-Key": MOTN_API_KEY},
                    )
                    if r.status_code == 200:
                        _stats["total_motn_api_calls"] += 1
                        return r.json()
                    last_error = f"MOTN error {r.status_code}: {r.text}"
                except httpx.HTTPError as e:
                    last_error = f"MOTN request failed: {e}"
                await asyncio.sleep(0.8)
        raise HTTPException(status_code=502, detail=last_error or "MOTN request failed after retries")

    # Walk the cursor chain sequentially up to the requested page. In normal
    # use the frontend always requests pages in order (1, then 2, then 3...),
    # so every page except the very first is almost always already cached
    # from a previous request — this loop only does real work on a cache miss.
    page_num = 1
    cursor = None
    page_data = None
    while page_num <= page:
        this_page_key = page_cache_base + (page_num,)
        cached_page = cache_get(this_page_key)
        if cached_page is not None:
            page_data = cached_page
        else:
            page_data = await fetch_motn_page(cursor)
            cache_set(this_page_key, page_data, ttl_seconds=discover_ttl)
        cursor = page_data.get("nextCursor")
        if page_num < page and not page_data.get("hasMore"):
            break  # ran out of pages before reaching the requested one
        page_num += 1

    shows = (page_data or {}).get("shows", [])
    results = [build_result_from_motn_show(s) for s in shows[:limit]]
    response = {"results": results, "count": len(results)}
    cache_set(result_cache_key, response, ttl_seconds=discover_ttl)

    shuffled = results.copy()
    random.shuffle(shuffled)
    return {"results": shuffled, "count": len(shuffled)}



@app.get("/api/title/{title_id}")
async def get_title(title_id: str, language: str = Query("en", description="UI language")):
    """Fetches full details for one title by its IMDb id — used when someone
    opens a shared favorites link, to resolve each id back into a real,
    current title (poster, description, ratings, etc.). Also the endpoint
    the frontend re-fetches from when viewing a title's full details in a
    language MOTN doesn't natively support, to get a Groq-translated overview."""
    if not MOTN_API_KEY:
        raise HTTPException(status_code=500, detail="MOTN_API_KEY is not configured on the server.")

    motn_language = language if language in MOTN_NATIVE_LANGUAGES else "en"
    needs_groq = language in GROQ_TRANSLATE_LANGUAGES

    cache_key = ("title", title_id, motn_language, language if needs_groq else None)
    cached = cache_get(cache_key)
    if cached is not None:
        _stats["cache_hits"] += 1
        return cached

    async with httpx.AsyncClient(timeout=15) as client:
        try:
            title_params = {}
            if motn_language != "en":
                title_params["output_language"] = motn_language
            r = await client.get(
                f"{MOTN_BASE}/shows/{title_id}",
                params=title_params,
                headers={"X-API-Key": MOTN_API_KEY},
            )
        except httpx.HTTPError as e:
            raise HTTPException(status_code=502, detail=f"MOTN request failed: {e}")
        if r.status_code != 200:
            raise HTTPException(status_code=502, detail=f"MOTN error {r.status_code}: {r.text}")

        _stats["total_motn_api_calls"] += 1

    result = build_result_from_motn_show(r.json())

    if needs_groq and result.get("overview"):
        result["overview"] = await translate_overview_via_groq(result["overview"], language)

    # 6 hour TTL — title metadata (description, cast, poster) barely ever
    # changes, so this can be cached far longer than availability/pricing.
    cache_set(cache_key, result, ttl_seconds=21600)
    return result


@app.get("/api/search")
async def search_titles(
    query: str,
    content_type: str = Query("movie", description="'movie' or 'tv_series'"),
    language: str = Query("en", description="UI language — only en/es/fr/de get passed to MOTN"),
):
    """Text search for a specific title, used alongside swiping for when
    someone already knows what they want."""
    if not MOTN_API_KEY:
        raise HTTPException(status_code=500, detail="MOTN_API_KEY is not configured on the server.")

    _stats["total_search_calls"] += 1
    wanted_type = "series" if content_type == "tv_series" else "movie"
    motn_language = language if language in MOTN_NATIVE_LANGUAGES else "en"

    async with httpx.AsyncClient(timeout=15) as client:
        try:
            search_params = {"title": query, "show_type": wanted_type}
            if motn_language != "en":
                search_params["output_language"] = motn_language
            r = await client.get(
                f"{MOTN_BASE}/shows/search/title",
                params=search_params,
                headers={"X-API-Key": MOTN_API_KEY},
            )
        except httpx.HTTPError as e:
            raise HTTPException(status_code=502, detail=f"MOTN request failed: {e}")
        if r.status_code != 200:
            raise HTTPException(status_code=502, detail=f"MOTN error {r.status_code}: {r.text}")

        _stats["total_motn_api_calls"] += 1
        # Defensive about response shape here — confirmed the filters endpoint
        # wraps results in {"shows": [...]}, but hadn't specifically verified
        # title search does the same before writing this, so handling both
        # possibilities rather than assuming.
        parsed = r.json()
        shows = parsed if isinstance(parsed, list) else parsed.get("shows", [])

    # Defensive filter in case show_type isn't fully respected server-side —
    # matches the old Watchmode implementation's same defensive habit.
    results = [
        build_result_from_motn_show(s) for s in shows
        if s.get("showType") == wanted_type
    ][:10]

    return {"results": results, "count": len(results)}


@app.get("/api/titles")
async def get_titles(ids: str, language: str = Query("en", description="UI language — only en/es/fr/de get passed to MOTN. No Groq translation here (a shared list can contain many titles at once, which could burst past Groq's rate limit) — that only happens on individual detail views.")):
    """Fetches multiple titles by IMDb id in one call, used for shared
    favorite links. Firing many separate /api/title/{id} requests from the
    browser at once was overloading Render's free-tier instance and causing
    most of them to silently fail — this does the same fetching server-side
    with capped concurrency and a retry per id, same pattern as /api/discover."""
    if not MOTN_API_KEY:
        raise HTTPException(status_code=500, detail="MOTN_API_KEY is not configured on the server.")

    motn_language = language if language in MOTN_NATIVE_LANGUAGES else "en"
    id_list = [s.strip() for s in ids.split(",") if s.strip()]
    semaphore = asyncio.Semaphore(4)

    async with httpx.AsyncClient(timeout=15) as client:
        async def fetch_one(title_id):
            cache_key = ("title", title_id, motn_language, None)
            cached = cache_get(cache_key)
            if cached is not None:
                _stats["cache_hits"] += 1
                return cached
            async with semaphore:
                for attempt in range(2):
                    try:
                        title_params = {"output_language": motn_language} if motn_language != "en" else {}
                        r = await client.get(
                            f"{MOTN_BASE}/shows/{title_id}",
                            params=title_params,
                            headers={"X-API-Key": MOTN_API_KEY},
                        )
                        if r.status_code == 200:
                            _stats["total_motn_api_calls"] += 1
                            result = build_result_from_motn_show(r.json())
                            cache_set(cache_key, result, ttl_seconds=21600)
                            return result
                    except httpx.HTTPError:
                        pass
            return None

        results = await asyncio.gather(*[fetch_one(tid) for tid in id_list])

    return {"results": [r for r in results if r]}


@app.get("/api/movie/{title_id}/providers")
async def movie_providers(title_id: str, region: str = "US"):
    """title_id is now an IMDb id (e.g. 'tt0068646') — MOTN's /shows/{id}
    endpoint accepts this directly. Kept the URL path name 'title_id' for
    frontend compatibility; the actual identifier type changed in stage 2
    of the Watchmode -> MOTN migration."""
    if not MOTN_API_KEY:
        raise HTTPException(status_code=500, detail="MOTN_API_KEY is not configured on the server.")

    region = region.upper() if region.upper() in SUPPORTED_REGIONS else DEFAULT_REGION

    _stats["total_provider_checks"] += 1

    cache_key = ("providers", title_id, region)
    cached = cache_get(cache_key)
    if cached is not None:
        _stats["cache_hits"] += 1
        return cached

    async with httpx.AsyncClient(timeout=15) as client:
        try:
            r = await client.get(
                f"{MOTN_BASE}/shows/{title_id}",
                params={"country": region.lower()},
                headers={"X-API-Key": MOTN_API_KEY},
            )
        except httpx.HTTPError as e:
            raise HTTPException(status_code=502, detail=f"MOTN request failed: {e}")
        if r.status_code != 200:
            raise HTTPException(status_code=502, detail=f"MOTN error {r.status_code}: {r.text}")

        _stats["total_motn_api_calls"] += 1
        show = r.json()

    options = show.get("streamingOptions", {}).get(region.lower(), [])

    def clean(s):
        service = s.get("service", {})
        images = service.get("imageSet", {})
        price = s.get("price") or {}
        return {
            "name": service.get("name"),
            "logo_url": images.get("darkThemeImage") or images.get("whiteImage"),
            "price": price.get("formatted"),  # a ready-to-display string like "3.99 USD" — already includes currency
            "format": s.get("quality"),
            "web_url": s.get("link"),  # a real deep link to the title's page on the service
        }

    subscription = [clean(s) for s in options if s.get("type") == "subscription"]
    rent = [clean(s) for s in options if s.get("type") == "rent"]
    buy = [clean(s) for s in options if s.get("type") == "buy"]
    free = [clean(s) for s in options if s.get("type") == "free"]

    # De-duplicate by name within each group — a service can appear multiple
    # times for different video qualities (SD/HD/4K); keep the first seen.
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
    # a fresh call on every single "final pick" view of the same title.
    cache_set(cache_key, response, ttl_seconds=3600)
    return response

