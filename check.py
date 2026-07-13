import json
import os
import smtplib
import sys
import urllib.parse
from datetime import datetime, timezone
from email.mime.text import MIMEText

from playwright.sync_api import sync_playwright

# --- Configuration ---
BASE_API = "https://flightrewardfinder.qantas.com/api/search"
BASE_SEARCH_PAGE = "https://flightrewardfinder.qantas.com/?pg=1&d=;EU&dr={dr}&p=2&c=Business,First"

# Searched separately per month, since live examples were single-month windows
SEARCH_WINDOWS = [
    ("2027-06-01", "2027-06-30"),
    ("2027-07-01", "2027-07-31"),
]

ALERT_PASSENGERS = 2       # trigger an email only when a fare bucket has >= this many seats
FETCH_PASSENGERS = 1       # query broadly so 1-seat results aren't filtered out server-side
CABINS = "Business,First"
DEST = ";EU"
ORIGIN = ";OC"

STATE_FILE = "state.json"          # tracks only >=2-seat entries, for email diffing
RESULTS_FILE = "results.json"      # ALL entries found, for the dashboard
RAW_DUMP_FILE = "last_raw_response.json"
DEBUG_SCREENSHOT = "debug_screenshot.png"

GMAIL_USER = os.environ["GMAIL_USER"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
ALERT_TO = os.environ["ALERT_EMAIL_TO"]


def build_api_url(dr, page=1):
    params = {
        "d": DEST,
        "dr": dr,
        "c": CABINS,
        "p": FETCH_PASSENGERS,
        "o": ORIGIN,
        "pg": page,
    }
    return BASE_API + "?" + urllib.parse.urlencode(params, safe=";,")


def fetch_all_flights_for_window(page_obj, dr):
    all_flights = []
    current_page = 1
    max_known_page = 1

    while current_page <= max_known_page:
        api_url = build_api_url(dr, current_page)
        result = page_obj.evaluate(
            """
            async (apiUrl) => {
                const res = await fetch(apiUrl, { credentials: "include" });
                const text = await res.text();
                return { status: res.status, body: text };
            }
            """,
            api_url,
        )
        if result["status"] != 200:
            print(f"[{dr} page {current_page}] API status {result['status']}: "
                  f"{result['body'][:300]}")
            break
        try:
            data = json.loads(result["body"])
        except json.JSONDecodeError as e:
            print(f"[{dr} page {current_page}] Could not parse JSON: {e}")
            break

        flights = data.get("flights", [])
        all_flights.extend(flights)

        pagination = data.get("pagination", {})
        max_known_page = pagination.get("maxKnownPage", current_page)
        current_page += 1

    return all_flights


def fetch_availability():
    all_flights = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            args=["--disable-blink-features=AutomationControlled"]
        )
        page = browser.new_page(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1366, "height": 900},
            locale="en-AU",
        )

        full_range = f"{SEARCH_WINDOWS[0][0]}I{SEARCH_WINDOWS[-1][1]}"
        try:
            page.goto(BASE_SEARCH_PAGE.format(dr=full_range),
                      wait_until="networkidle", timeout=60000)
        except Exception as e:
            print(f"goto() raised: {e}")
        page.wait_for_timeout(3000)

        print(f"Page title after load: {page.title()!r}")

        for start, end in SEARCH_WINDOWS:
            dr = f"{start}I{end}"
            print(f"Fetching {dr} ...")
            flights = fetch_all_flights_for_window(page, dr)
            print(f"  -> {len(flights)} flight(s) returned")
            all_flights.extend(flights)

        try:
            page.screenshot(path=DEBUG_SCREENSHOT, full_page=True)
        except Exception as e:
            print(f"screenshot failed: {e}")

        browser.close()

    return {"flights": all_flights}


def flatten_entries(data):
    """One dict per (flight, cabin) pair where that cabin actually has an
    offer -- this is the full picture, used for both the dashboard and the
    alert-worthy subset."""
    entries = []
    if not data:
        return entries

    for flight in data.get("flights", []):
        origin = (flight.get("origin") or {}).get("code")
        dest = (flight.get("destination") or {}).get("code")
        departs = flight.get("departsAt")
        stopovers = flight.get("stopovers")
        duration = flight.get("duration")
        cabins = flight.get("cabins") or {}

        for cabin_name in ("Business", "First"):
            cabin = cabins.get(cabin_name)
            if not cabin:
                continue
            entries.append({
                "departsAt": departs,
                "origin": origin,
                "destination": dest,
                "cabin": cabin_name,
                "points": cabin.get("points"),
                "tax": cabin.get("tax"),
                "currency": cabin.get("currency"),
                "seats": cabin.get("seats"),
                "stopovers": stopovers,
                "duration": duration,
            })
    return entries


def entry_key(e):
    return (f"{e['departsAt']} | {e['origin']}->{e['destination']} | "
            f"{e['cabin']} | {e['points']} pts | {e['seats']} seats")


def load_previous_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return set(json.load(f))
    return set()


def save_state(keys):
    with open(STATE_FILE, "w") as f:
        json.dump(sorted(keys), f, indent=2)


def save_results(entries):
    entries_sorted = sorted(entries, key=lambda e: (e["departsAt"] or "", e["cabin"]))
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "search_windows": SEARCH_WINDOWS,
        "alert_passengers": ALERT_PASSENGERS,
        "flights": entries_sorted,
    }
    with open(RESULTS_FILE, "w") as f:
        json.dump(payload, f, indent=2)


def send_email(new_keys):
    body = f"New Qantas reward seats found ({ALERT_PASSENGERS} pax, MEL/SYD-Europe, Business/First):\n\n"
    body += "\n".join(sorted(new_keys))
    body += "\n\nCheck live: https://flightrewardfinder.qantas.com/"

    msg = MIMEText(body)
    msg["Subject"] = f"New Qantas reward seat(s) found ({len(new_keys)})"
    msg["From"] = GMAIL_USER
    msg["To"] = ALERT_TO

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_USER, [ALERT_TO], msg.as_string())


def main():
    data = fetch_availability()

    with open(RAW_DUMP_FILE, "w") as f:
        json.dump(data, f, indent=2)

    all_entries = flatten_entries(data) if data is not None else []
    save_results(all_entries)

    bookable_entries = [e for e in all_entries if (e.get("seats") or 0) >= ALERT_PASSENGERS]
    current_keys = {entry_key(e) for e in bookable_entries}
    previous_keys = load_previous_state()

    if data is not None:
        print(f"{len(all_entries)} total fare entries found, "
              f"{len(current_keys)} bookable for {ALERT_PASSENGERS} pax")

    save_state(current_keys if data is not None else previous_keys)

    if data is None:
        print("Could not get a usable API response -- see debug_screenshot.png "
              "and last_raw_response.json.")
        sys.exit(1)

    new_keys = current_keys - previous_keys
    if new_keys:
        print(f"Found {len(new_keys)} new seat(s) -- sending email.")
        send_email(new_keys)
    else:
        print("No new seats this run.")


if __name__ == "__main__":
    main()
