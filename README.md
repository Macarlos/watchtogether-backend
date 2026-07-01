# Reel API — backend

Proxies Watchmode's movie + streaming-availability data so the frontend
never needs to hold the API key.

Uses **Watchmode**, not TMDB, because TMDB's free tier excludes commercial
(ad-supported) use and requires a $149/month plan. Watchmode's free tier
(2,500 requests/month, no card required) is built for this exact use case —
confirm their current terms before your traffic scales past the free tier.

## Get a Watchmode API key

1. Go to https://api.watchmode.com/requestApiKey
2. Sign up — no credit card required
3. Copy your API key

## Local run

```
pip install -r requirements.txt
set WATCHMODE_API_KEY=your_key_here      (Windows)
export WATCHMODE_API_KEY=your_key_here   (Mac/Linux)
uvicorn main:app --reload
```

Visit http://127.0.0.1:8000/api/discover?platforms=netflix,hbo&moods=comedy

## Deploy on Render (same pattern as InstaSummary)

1. Push this folder to a new GitHub repo (e.g. `reel-backend`).
2. Render → New → Web Service → connect the repo.
3. Start command: `uvicorn main:app --host 0.0.0.0 --port 10000`
4. Environment variable: `WATCHMODE_API_KEY` = your Watchmode key
5. Free tier works, but the same cold-start sleep issue from InstaSummary applies.

## Before launch — confirm the source ids

Watchmode source ids (which number = which platform) are hardcoded in
`main.py` under `PLATFORM_TO_WATCHMODE`. Confirm these against a live call to
`GET /list-sources/?apiKey=your_key` before relying on them — Watchmode
source ids are stable but worth a one-time check rather than assuming.

## Endpoints

`GET /api/discover?platforms=netflix,hbo&moods=comedy,romance&time_budget=120&region=PL`
Returns a list of movies matching the selected platforms/moods, with poster URLs and Watchmode ids.

`GET /api/movie/{id}/providers?region=PL`
Returns the exact subscription streaming providers for one title in a given region.

