# ZIPAIR SFO ⇄ Tokyo Fare Tracker

Watches ZIPAIR's published fares for **San Francisco ⇄ Tokyo** (both directions)
and **emails you when a fare drops below your threshold** — one-way under **$400**,
round-trip under **$700** by default, focused on **November 2026** travel. Runs in
the cloud around the clock; your computer can be off.

Modeled on [matcha-alert](https://github.com/zijun-liu/matcha-alert): a small
Python checker driven by GitHub Actions, pinged by cron-job.org, emailing over
Gmail and committing its state back to the repo so you never get the same alert
twice.

## How it works

```
cron-job.org  ──every 30 min──▶  GitHub Actions workflow  ──runs──▶  zipair_monitor.py
(free pinger)                    (zipair-check.yml)                  checks ZIPAIR fares,
                                                                     emails on new deals,
                                                                     commits state.json
```

- `zipair_monitor.py` fetches ZIPAIR's own fare pages at `flights.zipair.net`
  (e.g. *flights-from-san-francisco-to-tokyo*), reads the fares embedded in each
  page, and keeps the cheapest per route/date/flight-type.
- A fare **alerts** when its USD total is below the threshold for its flight type
  **and** it departs (or returns) in your target month. Thresholds and month live
  in `config.json`.
- It remembers every alerted fare in `state.json` (committed back after each run)
  and only emails when something is **newly** below threshold, or dips **lower**
  than the last alert — no repeat spam.
- If a page looks like a bot-block/challenge (no fare data), that route is skipped
  and simply retried next run rather than mistaken for "no deals".

## Data source & limitations

ZIPAIR's fare-marketing pages embed their current lowest **published** fares in
the page HTML (a Next.js `__NEXT_DATA__` blob). No API key or login is required —
that's what makes this dependable to run from CI, and why it can't be silently
rate-limited behind an auth token.

Two honest caveats:

1. **These are ZIPAIR's published/cached fares** ("collected within the last
   48hrs"), not a live booking-engine quote. Treat an alert as "go check and
   book now," the same way matcha-alert says "go add to cart now."
2. **The cache is mostly round-trip.** ZIPAIR surfaces round-trip fares on these
   pages far more than one-way, so in practice the round-trip threshold is what
   usually fires. If ZIPAIR publishes a one-way fare, it's picked up
   automatically and checked against the `ONE_WAY` threshold. For guaranteed
   per-date one-way coverage you'd have to drive ZIPAIR's live booking calendar
   (a stateful session that can be blocked from cloud IPs) — deliberately avoided
   here to keep the tracker reliable.

## Setup

1. **Gmail App Password** — turn on
   [2-Step Verification](https://myaccount.google.com/security), then create an
   [app password](https://myaccount.google.com/apppasswords).
2. **Repo secrets** — in *Settings → Secrets and variables → Actions*, add:
   - `ZIPAIR_SMTP_USER` — your Gmail address
   - `ZIPAIR_SMTP_PASS` — the 16-character app password
   - `ZIPAIR_MAIL_TO` — where alerts go (comma-separated for multiple)
3. **Pinger** — create a
   [fine-grained token](https://github.com/settings/personal-access-tokens/new)
   scoped to this repo with **Actions: Read and write**, then on
   [cron-job.org](https://cron-job.org) create a job:
   - URL: `https://api.github.com/repos/<you>/zipair-tracker/actions/workflows/zipair-check.yml/dispatches`
   - Schedule: every 30 minutes · Method: `POST` · Body: `{"ref":"main"}`
   - Headers: `Authorization: Bearer <token>`, `Accept: application/vnd.github+json`,
     `Content-Type: application/json`
   - A test run returning **204** means it works.
4. **Verify** — the *Actions* tab should show runs on schedule. Use
   *Run workflow* to trigger one by hand, and `--test-email` (below) to confirm
   delivery.

## Configuration

`config.json` controls everything:

```json
{
  "routes": [
    { "slug": "flights-from-san-francisco-to-tokyo", "label": "SFO → Tokyo" },
    { "slug": "flights-from-tokyo-to-san-francisco", "label": "Tokyo → SFO" },
    { "slug": "flights-from-san-jose-to-tokyo",      "label": "SJC → Tokyo" },
    { "slug": "flights-from-tokyo-to-san-jose",      "label": "Tokyo → SJC" }
  ],
  "thresholds": { "ONE_WAY": 400, "ROUND_TRIP": 700 },
  "travel_month": "2026-11",
  "request_pause_seconds": 2
}
```

- **thresholds** — dollar ceiling per flight type. A fare alerts when it's below
  its type's number.
- **travel_month** — `"YYYY-MM"` to only alert on fares touching that month, or
  `null` to alert on any date.
- **routes** — add or remove ZIPAIR page slugs (any `flights-from-…-to-…` page).

## Running locally

```bash
python3 zipair_monitor.py            # normal run
python3 zipair_monitor.py --dry-run  # check + print, never email, never touch state
python3 zipair_monitor.py --force    # email current best fares regardless of state
python3 zipair_monitor.py --test-email   # send yourself a test email and exit
```

No third-party packages — standard library only (`requirements.txt` is empty).

## Files

| File | What it is |
|------|-----------|
| `zipair_monitor.py` | The checker/emailer. |
| `config.json` | Routes, thresholds, target month. Editable. |
| `state.json` | Last known fares + what's been alerted; auto-committed by the workflow. |
| `.github/workflows/zipair-check.yml` | The Actions workflow (dispatch-driven + 6-hour backup cron). |
