# Reel Project — Handoff Summary
*(Paste this whole file as your first message in a new chat to continue seamlessly)*

## What this is
**Reel** — a swipe-based "what should I watch" web app. Pick platforms + mood, swipe through real movies/series with real posters/pricing/trailers, save picks, share them with anyone via a link. Solo experience only (multiplayer was built then deliberately removed — see Decisions Made below).

Live site: **https://macarlos.github.io/watchtogether-web/**
Backend API: **https://watchtogether-backend-zxwy.onrender.com**

---

## Repos & folders (Windows machine)

**Frontend** — `C:\Users\karol\OneDrive\Documents\Revenue Engine\WatchTogether-web`
→ GitHub: `github.com/Macarlos/watchtogether-web` → deploys via GitHub Pages automatically on push to `main`.

Files in this repo:
- `index.html` — **the real app, always the file to update**. Currently **build v3.6**.
- `watchtogether.html` — legacy redirect stub only (forwards to index.html, preserves `?shared=` query string). Don't need to touch this again.
- `legal.html` — privacy/legal page, linked from index.html's footer.
- `robots.txt`, `sitemap.xml` — basic SEO files.

**Backend** — `C:\Users\karol\OneDrive\Documents\Revenue Engine\WatchTogether`
→ GitHub: `github.com/Macarlos/watchtogether-backend` → deploys via Render automatically on push to `main`.

Files in this repo:
- `main.py` — FastAPI backend, single file.
- `requirements.txt` — fastapi, uvicorn[standard], httpx (unpinned versions — pinned versions caused deploy failures early on).

---

## Deployment — known gotchas (read before troubleshooting)
1. **GitHub Pages sometimes fails with a generic "Deployment failed, try again later."** Not our code's fault. Fix: `git commit --allow-empty -m "retrigger deploy" && git push`, or Actions tab → failed run → "Re-run all jobs", or re-save the Source dropdown in repo Settings → Pages.
2. **The most common real bug: Karol downloads a file but doesn't actually overwrite it in the folder before committing.** Always verify by opening the file and searching for the new version number *before* pushing.
3. **Browser caching makes successful deploys look broken.** Always verify with a hard refresh or incognito window — or ask Claude to `web_fetch` the live URL directly to check server-side truth.
4. **Always bump the version marker** `<div class="wordmark">Reel — build vX.X</div>` in index.html on every change, and get explicit confirmation the new number is visible live before considering anything shipped. This has caught many otherwise-invisible failed deploys.
5. Karol is a genuine git/deployment beginner — give exact commands, exact file paths, one step at a time. Don't assume familiarity.

---

## Backend architecture (main.py)

**Data source: Watchmode API** (NOT TMDB). TMDB's free tier explicitly forbids commercial/ad-supported use ($149/mo required); Watchmode's free tier (2,500 req/month) fits this use case. `WATCHMODE_API_KEY` set as a Render env var.

**Key verified facts (learned empirically, not guessed — don't re-guess these):**
- Watchmode uses **its own genre numbering**, different from TMDB's.
- Platform IDs: `netflix=203, hbo=387 (HBO Max/Max), disney=372, prime=26, apple=371, hulu=157`
- Genre IDs: `comedy=4, thriller=17, romance=14, horror=11, action=1, scifi=15, fantasy=9, animated=3, documentary=6, crime=5, mystery=13, family=8, history=10, war=18, western=19, musical=[12,32], feelgood=[4,8], mindbend=[13,15], nostalgic=7`
- `source_ids` param must be **comma-separated** (pipe `|` gives a 400 error) and comma behaves as OR (union), not AND.
- Region is hardcoded to `US` — Watchmode's free tier only supports accurate US availability data. (Karol is in Poland — this is an open/unresolved honesty item, see Next 10 #10.)

**Endpoints:**
- `GET /` — health check
- `POST /api/ping` — anonymous page-load counter, no identifier attached
- `GET /api/debug-discover?stats_only=true` — **view usage stats without burning API credits** (this is the main way to check stats). Without the param, runs a full diagnostic suite of live Watchmode test calls (built during development to verify API behavior; costs real credits, rarely needed now)
- `GET /api/discover?platforms=&moods=&region=US&limit=&page=&content_type=` — main browse/search endpoint. Two-stage: cheap `list-titles` call (3 retries, results shuffled for session variety) → parallel detail-fetch enrichment (semaphore-capped at 8 concurrent, 2 retries each — this concurrency cap was critical, an earlier uncapped version overloaded Render's 0.1 CPU instance and caused cascading failures)
- `GET /api/search?query=&content_type=` — text search (Watchmode's `/search/` endpoint returns mixed `title_results`/`people_results`; we filter to titles only, enrich top 15 candidates, filter by content_type)
- `GET /api/title/{id}` — fetch one title's full details by Watchmode ID
- `GET /api/titles?ids=1,2,3` — **batch** fetch multiple titles (built specifically because firing many simultaneous individual `/api/title/{id}` requests from the browser was overloading the free-tier instance and silently failing most of them — always use the batch endpoint for multiple IDs)
- `GET /api/movie/{id}/providers?region=US` — real subscription/rent/buy providers with logos (from `/sources/`) and prices, grouped by type
- Shared helper `build_result_from_details(d)` — maps raw Watchmode details into the consistent shape the frontend expects (title, year, end_year, overview, will_you_like_this, poster_url, backdrop_url, genres, runtime_minutes, rating, critic_score, content_rating, trailer_url, watchmode_id). Reused by discover, search, title, and titles endpoints — don't duplicate this mapping logic elsewhere.

**Rate limiting:** in-memory per-IP middleware, 30 requests/60 seconds, reads `X-Forwarded-For` (Render sits behind a proxy — `request.client.host` alone would just show the proxy's IP).

**Anonymous stats** (in-memory, resets on redeploy — same tradeoff as everything else on Render free tier): `total_page_loads`, `total_discover_calls`, `total_search_calls`, `total_provider_checks`, `platform_counts`, `genre_counts`, `content_type_counts`. Zero new frontend network calls needed for most of these — they're just counted from existing endpoint traffic. Only `/api/ping` is a dedicated new call (fired once per page load).

---

## Frontend architecture (index.html)

Single HTML file, vanilla JS, no framework. Screens are `<section class="screen">` elements toggled via `.active` class (no real routing/history — browser back/forward doesn't work, this is a known, explicitly deferred limitation).

**Flow:** Movies/Series choice (`screen-content-type`, the first screen) → Setup (platforms + mood chips, `screen-setup`) → Swipe deck (`screen-deck`) → Favorites grid / Search results / Shared picks grid → Final pick detail screen (`screen-final-pick`).

**Persistent UI (visible on every screen):**
- Global search bar (text search alongside swiping)
- "↺ New search" button, top-right fixed, hidden only on the very first screen

**Swipe deck:**
- Cards use `background-size: contain` (not `cover`) so posters never get cropped
- Title shown as text below the poster, not overlaid on it
- "Plot summary" and "▶ Trailer" buttons at the bottom of each card (both properly isolate pointer events so they don't trigger the drag/swipe system)
- "🏷 Adjust categories" button opens a dialog to change mood filters **mid-swipe without resetting** — changes apply to future fetches only, doesn't retroactively filter cards already queued (a known, accepted simplification)
- Each new deck session starts on a **random backend page** (1–5) plus backend-side shuffling, so repeated sessions with the same filters don't look identical

**Favorites:**
- Persist via `localStorage` (key: `reel_favorites_v1`) — survive tab close, new sessions, restarts
- Heart button (♡/♥) on every grid poster (favorites, search results, shared picks) — tap to save/unsave directly without opening the full detail screen
- "Clear all picks" button with a confirm dialog

**Sharing:**
- "Share my picks" opens a dialog showing the **actual visible link** in a text field (deliberately not relying solely on the unreliable Clipboard/Web Share APIs) with a Copy button that tries modern → falls back gracefully
- Link format: `https://macarlos.github.io/watchtogether-web/?shared=<comma-separated-watchmode-ids>` — **hardcoded to this canonical URL**, not derived from `location.href` (an earlier bug: if someone had a stale bookmark, the generated share link would inherit the broken path)
- Recipient's page detects `?shared=` on load, fetches all IDs via the batch endpoint, shows a "Their picks" grid

**Navigation correctness (fixed after real bugs):**
- Grids track where they were opened from (`favoritesReturnScreen`, `searchReturnScreen`, `finalPickReturnScreen`) so "Back" buttons actually return to wherever the user came from, with dynamic labels ("← Back to search results" vs "← Back to your picks")
- "Continue swiping →" buttons on search-results and final-pick screens resume the existing deck without resetting (or start a fresh one if none exists yet)

**Final pick screen shows:** description, Watchmode's "is it for you" AI blurb, ratings/critic score/content rating/genres, trailer link, and the **real** where-to-watch panel (subscription/rent/buy grouped, with logos and prices) — this replaced an earlier placeholder that just redirected to JustWatch.

**Series handling:** no single runtime (shows "Series" instead of "— min"), year ranges for ongoing shows (e.g. "2022–").

---

## Decisions made (with reasoning, so we don't redo this thinking)

- **Dropped simulated "watch with friends" multiplayer entirely.** It looked convincing but was 100% fake (no real backend session sync, no two real devices could actually swipe together). Rebuilding it for real would mean websockets + a database — a different scale of project. Replaced with async sharing (a link) instead, which solves "decide together" reasonably well for much less effort.
- **Watchmode over TMDB** for cost reasons (TMDB forbids commercial/ad-supported use on its free tier).
- **In-memory everything on the backend** (rate limits, stats) — acceptable because Render's free tier is a single instance; resets on redeploy are a known, accepted tradeoff, not a bug.
- **No cast/actor photos** — checked via the debug endpoint, Watchmode doesn't reliably provide this.
- **No og:image yet** — text-only social share meta tags are in place; an actual preview image is a nice-to-have, not done.
- Established habit: **before building on any assumption about Watchmode's API** (field names, ID mappings, parameter semantics), verify empirically via the debug endpoint first. This caught several real bugs already (wrong genre IDs carried over from TMDB assumptions, comma-vs-pipe semantics, response shapes). Keep doing this for anything new.
- Established habit: **after any JS edit**, validate brace balance, duplicate IDs, and dangling `getElementById` references, plus `node --check` on the extracted script, before shipping.

---

## "The Next 10" — prioritized backlog

1. ✅ **Done** — Remember picks across visits (localStorage)
2. ✅ **Done** — Text search alongside swiping (persistent bar, results grid, heart-to-save, same detail view)
3. ✅ **Done** — Basic per-IP rate limiting (30 req/60s)
4. ✅ **Done** — Anonymous aggregate usage stats (page loads, call counts, platform/genre breakdowns) — check via `/api/debug-discover?stats_only=true`
5. ⬜ **Not started** — Make it installable (PWA)
6. ⬜ **Not started** — A 3-second first-time explainer/onboarding hint
7. ⬜ **Not started** — Expand "ran out of results" recovery to also suggest adding another platform (currently only offers to widen genre)
8. ⬜ **Not started** — A "surprise me" mode (ignore all filters, show something random)
9. ⬜ **Not started** — Accessibility pass (screen-reader labels, keyboard nav, contrast)
10. ⬜ **Not started** — Be honest about the US-only data limitation for non-US users (Karol is in Poland — current app doesn't disclose this anywhere)

## Other open items (not on the Next 10 list, discussed but not done)
- Real browser back/forward support — explicitly deferred, would need `history.pushState`/`popstate` handling across all screen transitions
- Custom domain (~$10–15/year) — discussed as worthwhile before wider public posting, not purchased yet
- Upgrading Render to the paid Starter tier ($7/month, removes cold starts, 5x CPU) — Karol said "wait for now"
- og:image for richer social link previews
- Drafting the actual Reddit (r/InternetIsBeautiful, r/SideProject) and Product Hunt posts to get real users — paused before the Next 10 list started, not yet written

---

## Working style notes
- Karol values **honest pushback over cheerleading** — willing to hear "this might not be a good idea" (this led directly to dropping the fake multiplayer feature).
- Appreciates being asked a clarifying question or two before big builds, rather than guessing silently.
- Needs precise, step-by-step deployment instructions (exact commands, exact paths) — still learning git/GitHub/Render mechanics.
- Prefers iterating one Next 10 item at a time rather than batching.