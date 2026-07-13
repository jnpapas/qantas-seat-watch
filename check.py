import json
import os
import smtplib
import sys
import urllib.parse
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

PASSENGERS = 2
CABINS = "Business,First"
DEST = ";EU"
ORIGIN = ";OC"

STATE_FILE = "state.json"
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
        "p": PASSENGERS,
        "o": ORIGIN,
        "pg": page,
    }
    return BASE_API + "?" + urllib.parse.urlencode(params, safe=";,")


def fetch_all_flights_for_window(page_obj, dr):
    """Fetch every page of results for one date-range window."""
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
    """Load the search page first (to establish cookies / pass any Cloudflare
    check the way a normal visit does), then call the API directly for each
    month window via an in-page fetch()."""
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


def extract_flight_keys(data):
    """Turns flights into one string per (date, route, cabin) where at
    least PASSENGERS seats are actually available -- this is the real
    'bookable for both of us' check, done here as a safety net even
    though the API's own p= filter should already handle it."""
    keys = set()
    if not data:
        return keys

    for flight in data.get("flights", []):
        origin = (flight.get("origin") or {}).get("code")
        dest = (flight.get("destination") or {}).get("code")
        departs = flight.get("departsAt")
        cabins = flight.get("cabins") or {}

        for cabin_name in ("Business", "First"):
            cabin = cabins.get(cabin_name)
            if cabin and cabin.get("seats", 0) >= PASSENGERS:
                points = cabin.get("points")
                seats = cabin.get("seats")
                keys.add(
                    f"{departs} | {origin}->{dest} | {cabin_name} | "
                    f"{points} pts | {seats} seats"
                )
    return keys


def load_previous_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return set(json.load(f))
    return set()


def save_state(keys):
    with open(STATE_FILE, "w") as f:
        json.dump(sorted(keys), f, indent=2)


def send_email(new_keys):
    body = "New Qantas reward seats found (2 pax, MEL/SYD-Europe, Business/First):\n\n"
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

    current_keys = extract_flight_keys(data) if data is not None else set()
    previous_keys = load_previous_state()

    if data is not None:
        print(f"extract_flight_keys() found {len(current_keys)} bookable "
              f"entries (>= {PASSENGERS} seats)")

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
