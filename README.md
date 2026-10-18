# TSMC Museum ticket watcher

Checks https://www.tsmcmoi.com/en/bookOnline_pickDate on a schedule, records every run, and sends a phone push (ntfy) when seats open on the target date.

- **Dashboard:** `docs/index.html` (Hebrew, phone-friendly). Each run copies it, along with `data/runs.json`, into the public repo named by the `DASHBOARD_REPO` Actions variable, which serves both on GitHub Pages. The workflow pushes there with the `DASHBOARD_DEPLOY_KEY` secret (a write-enabled deploy key on that repo).
- **Interval:** the `cron:` line in `.github/workflows/check.yml` (examples are in the comments there).
- **Target date / seats:** `config.json`.
- **Notifications:** repo secret `NTFY_TOPIC`. The phone subscribes to the same topic in the ntfy app.
- **Test notification:** Actions → "Check TSMC tickets" → Run workflow → tick "Only send a test notification".

## How the site works
The booking page embeds `calendarRecord` JSON. Every date listed there is blocked (`status 0` = closed, `1` = full), and any date in the window that is *not* listed is bookable. Per-slot seats come from `GET /res?type=booktime&id=YYYYMMDD` → `availableTime.items[].spaceLeft`.

The site is behind Cloudflare. Python's stdlib `urllib` gets through, but curl and `requests` get challenged. Keep the checker stdlib-only.

## Run locally
```
python3 checker.py --dry-run
```
