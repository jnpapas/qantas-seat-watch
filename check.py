import json
import os
import smtplib
import sys
from email.mime.text import MIMEText

from playwright.sync_api import sync_playwright

# --- Configuration ---
SEARCH_URL = "https://flightrewardfinder.qantas.com/?pg=1&d=;EU&dr=2027-06-01I2027-07-30&p=2&c=Business,First"
STATE_FILE = "state.json"
RAW_DUMP_FILE = "last_raw_response.json"  # written every run, for debugging the schema

GMAIL_USER = os.environ["GMAIL_USER"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
ALERT_TO = os.environ["ALERT_EMAIL_TO"]


def fetch_availability():
    """Drive a real headless browser to the search page and capture the
    /api/search response body — this handles Cloudflare's bot challenge the
    same way a normal browser visit does."""
    captured = {}

    def handle_response(response):
        if "/api/search" in response.url and response.status == 200:
            try:
                captured["data"] = response.json()
            except Exception:
                pass

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.on("response", handle_response)
        page.goto(SEARCH_URL, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(3000)  # let a late-firing API call resolve
        browser.close()

    return captured.get("data")


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

    if data is None:
        print("Could not capture the /api/search response — page structure "
              "or Cloudflare challenge may have changed. See workflow logs.")
        sys.exit(1)

    current_keys = extract_flight_keys(data)
    previous_keys = load_previous_state()
    new_keys = current_keys - previous_keys

    if new_keys:
        print(f"Found {len(new_keys)} new seat(s) — sending email.")
        send_email(new_keys)
    else:
        print("No new seats this run.")

    save_state(current_keys)


if __name__ == "__main__":
    main()
