#!/usr/bin/env python3
"""
ZIPAIR SFO <-> Tokyo fare watcher.

Checks ZIPAIR's published fares for the San Francisco <-> Tokyo route (both
directions) and emails you when a fare drops below your threshold. Modeled on
the matcha-alert project: runs in GitHub Actions on a schedule, remembers what
it has already alerted on in state.json, and only emails on *new* deals so you
don't get spammed.

Data source
-----------
ZIPAIR's own fare-marketing pages at flights.zipair.net embed their current
lowest published fares in the page (a Next.js `__NEXT_DATA__` JSON blob). No API
key or login is required, which is what makes this reliable to run from CI. The
published cache is mostly ROUND_TRIP fares; if ZIPAIR ever exposes one-way fares
on these pages they are picked up automatically and matched against the ONE_WAY
threshold. See README.md ("Data source & limitations") for the full story.

Usage
-----
    python3 zipair_monitor.py            # normal run (used by the workflow)
    python3 zipair_monitor.py --force    # email the current best fares regardless of state
    python3 zipair_monitor.py --dry-run  # check + print, never email, never write state
    python3 zipair_monitor.py --test-email   # send a test email and exit
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import smtplib
import sys
import time
from email.mime.text import MIMEText
from email.utils import formatdate
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
STATE_PATH = ROOT / "state.json"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)
BASE = "https://flights.zipair.net/en-us/"
NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.DOTALL
)


# --------------------------------------------------------------------------- #
# Config / state
# --------------------------------------------------------------------------- #
def load_json(path: Path, default):
    try:
        return json.loads(path.read_text("utf-8"))
    except FileNotFoundError:
        return default
    except json.JSONDecodeError:
        print(f"! {path.name} was corrupt; starting fresh")
        return default


def save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", "utf-8")


DEFAULT_CONFIG = {
    "routes": [
        {"slug": "flights-from-san-francisco-to-tokyo", "label": "SFO → Tokyo"},
        {"slug": "flights-from-tokyo-to-san-francisco", "label": "Tokyo → SFO"},
        {"slug": "flights-from-san-jose-to-tokyo", "label": "SJC → Tokyo"},
        {"slug": "flights-from-tokyo-to-san-jose", "label": "Tokyo → SJC"},
    ],
    # A fare triggers an alert when its USD total is below the threshold for its
    # flight type. One-way keeps the original $400 target; round-trip defaults to
    # a level below ZIPAIR's usual SFO<->Tokyo low so real dips fire. Edit freely.
    "thresholds": {"ONE_WAY": 400, "ROUND_TRIP": 700},
    # If set (e.g. "2026-11"), a fare must also depart in that month to alert.
    # Set to null to alert on any date.
    "travel_month": "2026-11",
    "request_pause_seconds": 2,
}


# --------------------------------------------------------------------------- #
# Fetch + parse
# --------------------------------------------------------------------------- #
class BlockedError(RuntimeError):
    """Raised when the response looks like a bot-block / challenge page."""


def fetch_page(slug: str, retries: int = 3) -> str:
    url = BASE + slug
    last = None
    for attempt in range(1, retries + 1):
        req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html"})
        try:
            with urlopen(req, timeout=30) as resp:
                html = resp.read().decode("utf-8", "replace")
            if "__NEXT_DATA__" not in html:
                raise BlockedError("no __NEXT_DATA__ in response")
            return html
        except (HTTPError, URLError, BlockedError) as exc:
            last = exc
            print(f"  attempt {attempt}/{retries} failed for {slug}: {exc}")
            time.sleep(2 * attempt)
    raise BlockedError(f"could not fetch {slug}: {last}")


def extract_fares(html: str) -> list[dict]:
    m = NEXT_DATA_RE.search(html)
    if not m:
        raise BlockedError("could not locate __NEXT_DATA__")
    data = json.loads(m.group(1))
    apollo = data.get("props", {}).get("pageProps", {}).get("apolloState", {}).get("data", {})
    fares: list[dict] = []
    for node in apollo.values():
        if isinstance(node, dict) and isinstance(node.get("fares"), list):
            for f in node["fares"]:
                if not isinstance(f, dict):
                    continue
                usd = f.get("usdTotalPrice")
                if usd is None:
                    continue
                fares.append(
                    {
                        "origin": f.get("originAirportCode"),
                        "destination": f.get("destinationAirportCode"),
                        "flight_type": f.get("flightType", "ROUND_TRIP"),
                        "usd": round(float(usd), 2),
                        "departure_date": f.get("departureDate"),
                        "return_date": f.get("returnDate"),
                        "formatted_price": f.get("formattedTotalPrice"),
                    }
                )
    return fares


def deal_key(f: dict) -> str:
    return f"{f['origin']}-{f['destination']}-{f['flight_type']}-{f['departure_date']}-{f['return_date']}"


def in_travel_month(f: dict, travel_month: str | None) -> bool:
    if not travel_month:
        return True
    dep = f.get("departure_date") or ""
    ret = f.get("return_date") or ""
    # match if either leg touches the target month (covers one-way outbound and
    # round-trips that span into November)
    return dep.startswith(travel_month) or ret.startswith(travel_month)


# --------------------------------------------------------------------------- #
# Email
# --------------------------------------------------------------------------- #
def send_email(subject: str, body: str) -> None:
    user = os.environ.get("ZIPAIR_SMTP_USER")
    password = os.environ.get("ZIPAIR_SMTP_PASS")
    mail_to = os.environ.get("ZIPAIR_MAIL_TO", user or "")
    if not user or not password:
        print("! ZIPAIR_SMTP_USER / ZIPAIR_SMTP_PASS not set; cannot send email")
        print("---- would have sent ----")
        print("Subject:", subject)
        print(body)
        return
    recipients = [r.strip() for r in mail_to.split(",") if r.strip()]
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = ", ".join(recipients)
    msg["Date"] = formatdate(localtime=True)
    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(user, password)
        server.sendmail(user, recipients, msg.as_string())
    print(f"  emailed {', '.join(recipients)}")


def fare_line(route_label: str, f: dict) -> str:
    dates = f["departure_date"] or "?"
    if f["flight_type"] == "ROUND_TRIP" and f.get("return_date"):
        dates = f"{f['departure_date']} → {f['return_date']}"
    ftype = f["flight_type"].replace("_", "-").title()
    return f"  ${f['usd']:.0f}  {route_label}  {ftype}  {dates}"


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def run(force: bool, dry_run: bool) -> int:
    config = load_json(CONFIG_PATH, DEFAULT_CONFIG)
    state = load_json(STATE_PATH, {})
    alerted = state.get("alerted", {})  # deal_key -> lowest usd we've already emailed
    thresholds = config.get("thresholds", DEFAULT_CONFIG["thresholds"])
    travel_month = config.get("travel_month")
    pause = config.get("request_pause_seconds", 2)

    all_fares: list[tuple[str, dict]] = []
    errors: list[str] = []
    for route in config["routes"]:
        slug, label = route["slug"], route.get("label", route["slug"])
        try:
            html = fetch_page(slug)
            fares = extract_fares(html)
            print(f"  {label}: {len(fares)} fares")
            for f in fares:
                all_fares.append((label, f))
        except BlockedError as exc:
            print(f"! {label}: {exc}")
            errors.append(f"{label}: {exc}")
        time.sleep(pause)

    if not all_fares and errors:
        print("No fares retrieved; all routes errored. Will retry next run.")
        return 1

    # Find deals: under threshold, and (if set) touching the target month.
    new_deals: list[tuple[str, dict]] = []
    current_alerted = dict(alerted)
    for label, f in all_fares:
        thr = thresholds.get(f["flight_type"])
        if thr is None or f["usd"] >= thr:
            continue
        if not in_travel_month(f, travel_month):
            continue
        k = deal_key(f)
        prev = alerted.get(k)
        # alert if brand-new, or the price dropped further than last time
        if force or prev is None or f["usd"] < prev - 0.01:
            new_deals.append((label, f))
        current_alerted[k] = min(f["usd"], prev) if prev is not None else f["usd"]

    # Cheapest overall (for the run summary / state).
    cheapest = min(all_fares, key=lambda t: t[1]["usd"]) if all_fares else None
    if cheapest:
        cl, cf = cheapest
        print(f"  cheapest right now: ${cf['usd']:.0f} {cl} ({cf['flight_type']}, dep {cf['departure_date']})")

    if new_deals:
        new_deals.sort(key=lambda t: t[1]["usd"])
        window = f" departing {travel_month}" if travel_month else ""
        lines = [fare_line(lbl, f) for lbl, f in new_deals]
        body = (
            f"ZIPAIR fare alert — {len(new_deals)} fare(s){window} under your threshold:\n\n"
            + "\n".join(lines)
            + "\n\nThresholds: "
            + ", ".join(f"{k.replace('_','-').title()} < ${v}" for k, v in thresholds.items())
            + "\n\nBook: https://www.zipair.net/en\n"
            + f"Checked: {dt.datetime.now().strftime('%Y-%m-%d %H:%M %Z')}\n"
        )
        cheapest_deal = new_deals[0][1]["usd"]
        subject = f"✈️ ZIPAIR SFO–Tokyo deal: ${cheapest_deal:.0f}"
        print(f"  {len(new_deals)} new deal(s) → alerting")
        if dry_run:
            print("---- dry run, not emailing ----\n" + body)
        else:
            send_email(subject, body)
    else:
        print("  no new deals under threshold")

    if not dry_run:
        state["alerted"] = current_alerted
        state["last_run"] = dt.datetime.now(dt.timezone.utc).isoformat()
        state["last_cheapest"] = (
            {"usd": cheapest[1]["usd"], "route": cheapest[0], **cheapest[1]} if cheapest else None
        )
        save_json(STATE_PATH, state)

    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="ZIPAIR SFO<->Tokyo fare watcher")
    ap.add_argument("--force", action="store_true", help="email current best fares regardless of state")
    ap.add_argument("--dry-run", action="store_true", help="check and print, never email or write state")
    ap.add_argument("--test-email", action="store_true", help="send a test email and exit")
    args = ap.parse_args()

    if args.test_email:
        send_email(
            "✈️ ZIPAIR tracker test email",
            "This is a test from zipair_monitor.py. If you got this, email is wired up correctly.\n",
        )
        return 0

    return run(force=args.force, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
