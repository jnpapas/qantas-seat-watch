import json
import os
import smtplib
import sys
from email.mime.text import MIMEText

from playwright.sync_api import sync_playwright

# --- Configuration ---
SEARCH_URL = "https://flightrewardfinder.qantas.com/?pg=1&d=;EU&dr=2027-06-01I2027-07-30&p=2&c=Business,First"
API_URL = (
    "https://flightrewardfinder.qantas.com/api/search"
    "?d=%3BEU&dr=2027-06-01I2027-07-30&c=Business%2CFirst&p=2&o=%3BOC"
)
STATE_FILE = "state.json"
RAW_DUMP_FILE = "last_raw_response.json"  # written every run, for debugging the schema
DEBUG_SCREENSHOT = "debug_screenshot.png"  # written every run, for debugging

GMAIL_USER = os.environ["GMAIL_USER"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
ALERT_TO = os.environ["ALERT_EMAIL_TO"]


def fetch_availability():
    """Load the search page first (to establish cookies / pass any Cloudflare
    check the way a normal visit does), then call the known API endpoint
    directly via an in-page fetch() so it carries the right session/cookies
    as if the page itself had made the call."""
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

        try:
            page.goto(SEARCH_URL, wait_until="networkidle", timeout=60000)
        except Exception as e:
            print(f"goto() raised: {e}")

        page.wait_for_timeout(3000)

        print(f"Page title after load: {page.title()!r}")
        print(f"Page URL after load: {page.url}")

        result = page.evaluate(
            """
            async (apiUrl) => {
                const res = await fetch(apiUrl, { credentials: "include" });
                const text = await res.text();
                return { status: res.status, body: text };
            }
            """,
            API_URL,
        )

        print(f"API call status: {result['status']}")

        try:
            page.screenshot(path=DEBUG_SCREENSHOT, full_page=True)
        except Exception as e:
            print(f"screenshot failed: {e}")

        browser.close()

    if result["status"] != 200:
        print(f"API response body (first 800 chars): {result['body'][:800]}")
        return None

    try:
        return json.loads(result["body"])
    except json.JSONDecodeError as e:
        print(f"Could not parse API response as JSON: {e}")
        print(f"Body (first 800 chars): {result['body'][:800]}")
        return None


def extract_flight_keys(data):
    """
    Turns the raw API JSON into a set of short strings, one per distinct
    (date, flight, cabin) combo, so we can diff runs against each other.

    IMPORTANT: the field names below (results/flights/cabin/etc.) are a
    best guess. Check last_raw_response.json after your first run and
    adjust this function to match the real shape of the JSON.
    """
    keys = set()
    if not data:
        return keys

    for item in data.get("results", []):
        date = item.get("date")
        for flight in item.get("flights", []):
            keys.add(
                "|".join([
                    str(date),
                    str(flight.get("origin")),
                    str(flight.get("destination")),
                    str(flight.get("flightNumber")),
                    str(flight.get("cabin")),
                    str(flight.get("points")),
                ])
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
    body = "New Qantas reward seats found (MEL-Europe, Business/First):\n\n"
    body += "\n".join(sorted(new_keys))
    body += f"\n\nCheck live: {SEARCH_URL}"

    msg = MIMEText(body)
    msg["Subject"] = f"New Qantas reward seat(s) found ({len(new_keys)})"
    msg["From"] = GMAIL_USER
    msg["To"] = ALERT_TO

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_USER, [ALERT_TO], msg.as_string())


def main():
    data = fetch_availability()

    # Always dump the raw response so you can inspect/fix extract_flight_keys()
    with open(RAW_DUMP_FILE, "w") as f:
        json.dump(data, f, indent=2)

    current_keys = extract_flight_keys(data) if data is not None else set()
    previous_keys = load_previous_state()

    if data is not None:
        if isinstance(data, dict):
            print(f"Top-level JSON keys: {list(data.keys())}")
        else:
            print(f"Top-level JSON type: {type(data)}")
        preview = json.dumps(data)
        print(f"Response preview (first 3000 chars):\n{preview[:3000]}")
        print(f"extract_flight_keys() found {len(current_keys)} entries")

    # Always write state.json, even on failure, so the commit step has
    # something to add (an empty/unchanged state just means no new alert).
    save_state(current_keys if data is not None else previous_keys)

    if data is None:
        print("Could not get a usable API response — see debug_screenshot.png "
              "and last_raw_response.json for what the browser actually saw.")
        sys.exit(1)

    new_keys = current_keys - previous_keys
    if new_keys:
        print(f"Found {len(new_keys)} new seat(s) — sending email.")
        send_email(new_keys)
    else:
        print("No new seats this run.")


if __name__ == "__main__":
    main()
