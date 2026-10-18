#!/usr/bin/env python3
"""Check TSMC Museum of Innovation availability and record every run.

Uses only the standard library on purpose: the site is behind Cloudflare,
and urllib's TLS fingerprint gets through where curl/requests get challenged.

Usage:
  python3 checker.py                 # normal run (writes docs/data/runs.json, alerts if found)
  python3 checker.py --test-notify   # send a test push notification only
  python3 checker.py --dry-run       # check and print, don't write or notify

Env:
  NTFY_TOPIC   secret ntfy topic name (no notifications if unset)
  NTFY_SERVER  defaults to https://ntfy.sh
  RUNS_FILE    where run history is kept (default docs/data/runs.json)
"""
import json
import os
import re
import sys
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG = json.loads((ROOT / "config.json").read_text())
# In GitHub Actions this points into the public dashboard repo checkout.
RUNS_FILE = Path(os.environ.get("RUNS_FILE", ROOT / "docs" / "data" / "runs.json"))

BASE = "https://www.tsmcmoi.com"
PAGE_URL = BASE + "/en/bookOnline_pickDate"
SLOTS_URL = BASE + "/res?type=booktime&id={}"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}
GUIDES_HE = {1: "ללא הדרכה", 2: "סיור בסינית", 3: "סיור באנגלית", 4: "אישי", 5: "מדריך קולי"}


class Blocked(Exception):
    pass


def fetch(url):
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        if "Just a moment" in body or "cf_chl" in body:
            raise Blocked(f"Cloudflare blocked the request (HTTP {e.code})")
        raise
    if "Just a moment" in body:
        raise Blocked("Cloudflare challenge page")
    return body


def parse_calendar(html):
    m = re.search(r"\$\.parseJSON\('(\{\"calendarRecord\".*?)'\);", html, re.S)
    if not m:
        raise ValueError("calendar data not found in page (site layout changed?)")
    rec = json.loads(m.group(1))["calendarRecord"]
    last = re.search(r"lastDayBooking\s*=\s*new Date\('(\d{4}/\d{2}/\d{2})'\)", html)
    to_date = lambda s: datetime.strptime(s, "%Y/%m/%d").date()
    site_today = to_date(rec["today"])
    last_day = to_date(last.group(1)) if last else max(to_date(i["date"]) for i in rec["item"])
    # Dates listed in the calendar are blocked: status 0 = closed, 1 = fully booked.
    # Any date in the bookable window that is NOT listed is available.
    blocked = {to_date(i["date"]): ("closed" if str(i["status"]) == "0" else "full") for i in rec["item"]}
    days = {}
    d = site_today + timedelta(days=1)
    while d <= last_day:
        days[d] = blocked.get(d, "available")
        d += timedelta(days=1)
    return site_today, last_day, days


def get_slots(d):
    data = json.loads(fetch(SLOTS_URL.format(d.strftime("%Y%m%d"))))
    return [
        {"time": s.get("time"), "guide": int(s.get("guide") or 0),
         "guide_he": GUIDES_HE.get(int(s.get("guide") or 0), ""), "space": int(s.get("spaceLeft") or 0)}
        for s in data["availableTime"]["items"]
    ]


def check():
    target = date.fromisoformat(CONFIG["target_date"])
    site_today, last_day, days = parse_calendar(fetch(PAGE_URL))

    available = []
    for d, status in sorted(days.items()):
        if status == "available":
            time.sleep(1)  # be polite
            slots = [s for s in get_slots(d) if s["space"] > 0]
            if slots:
                available.append({"date": d.isoformat(), "slots": slots})

    target_status = days.get(target, "outside_window")
    if target_status == "available":
        target_slots = next((a["slots"] for a in available if a["date"] == target.isoformat()), [])
    elif target_status == "outside_window":
        target_slots = []
    else:
        # Check the slots anyway; costs one request and catches a calendar that lags behind.
        time.sleep(1)
        target_slots = [s for s in get_slots(target) if s["space"] > 0]
        if target_slots:
            target_status = "available"
            available.append({"date": target.isoformat(), "slots": target_slots})
            available.sort(key=lambda a: a["date"])

    max_space = max((s["space"] for s in target_slots), default=0)
    counts = {k: sum(1 for v in days.values() if v == k) for k in ("available", "full", "closed")}
    return {
        "ok": True,
        "site_today": site_today.isoformat(),
        "window_end": last_day.isoformat(),
        "target": {"date": target.isoformat(), "status": target_status,
                   "slots": target_slots, "max_space": max_space},
        "found": max_space >= CONFIG["min_seats_to_alert"],
        "available_days": available,
        "counts": counts,
        "closed_days": [d.isoformat() for d, v in sorted(days.items()) if v == "closed"],
    }


def notify(title, message, priority=5, tags=("tada",)):
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        print("NTFY_TOPIC not set; skipping notification:", title)
        return
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh")
    payload = {
        "topic": topic, "title": title, "message": message, "priority": priority,
        "tags": list(tags), "click": CONFIG["booking_url"],
        "actions": [{"action": "view", "label": "להזמנה באתר", "url": CONFIG["booking_url"]}],
    }
    req = urllib.request.Request(server, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        print("notification sent:", r.status)


def found_message(run):
    t = run["target"]
    seats = lambda n: "מקום אחד" if n == 1 else f"{n} מקומות"
    lines = [f"{s['time']} · {s['guide_he']} · {seats(s['space'])}" for s in t["slots"]]
    if t["max_space"] >= CONFIG["seats_needed"]:
        head = f"יש מקום ל-{CONFIG['seats_needed']} אנשים! להזמין מהר:"
    else:
        head = f"נמצא רק מקום אחד (צריך {CONFIG['seats_needed']}). אולי שווה להזמין/להתקשר:"
    return head + "\n" + "\n".join(lines)


def load_runs():
    try:
        return json.loads(RUNS_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def main():
    args = set(sys.argv[1:])
    if "--test-notify" in args:
        notify("בדיקה: התראות TSMC עובדות ✅",
               "זו הודעת בדיקה. כשיתפנו מקומות ל-9 בנובמבר תגיע הודעה כזו.", priority=3, tags=("white_check_mark",))
        return

    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    try:
        run = check()
    except Exception as e:  # record failures too, so the dashboard shows them
        run = {"ok": False, "error": f"{type(e).__name__}: {e}", "blocked": isinstance(e, Blocked)}
    run = {"time": now, **run}
    print(json.dumps(run, ensure_ascii=False, indent=1))

    if "--dry-run" in args:
        return

    runs = load_runs()
    runs.append(run)
    runs = runs[-CONFIG["max_runs_kept"]:]
    RUNS_FILE.parent.mkdir(parents=True, exist_ok=True)
    RUNS_FILE.write_text(json.dumps(runs, ensure_ascii=False, separators=(",", ":")))

    if run.get("found"):
        notify("🎉 התפנו כרטיסים ל-TSMC ב-9 בנובמבר!", found_message(run))
    elif not run["ok"]:
        n = CONFIG["alert_after_consecutive_failures"]
        tail = runs[-(n + 1):]
        # Alert exactly once, on the n-th failure in a row.
        if len(tail) > n and all(not r["ok"] for r in tail[1:]) and tail[0]["ok"]:
            notify("⚠️ בודק הכרטיסים לא מצליח לגשת לאתר",
                   f"{n} בדיקות אחרונות נכשלו. אלון יבדוק את זה.\n{run['error']}", priority=3, tags=("warning",))


if __name__ == "__main__":
    main()
