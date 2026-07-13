import json
import os
import smtplib
import sys
import urllib.parse
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from playwright.sync_api import sync_playwright

# --- Configuration ---
BASE_API = "https://flightrewardfinder.qantas.com/api/search"

SEARCH_WINDOWS = [
    ("2027-06-01", "2027-06-30"),
    ("2027-07-01", "2027-07-31"),
]

ALERT_PASSENGERS = 2       # trigger an email only when a fare bucket has >= this many seats
FETCH_PASSENGERS = 1       # query broadly so 1-seat results aren't filtered out server-side
CABINS = "Business,First"
DEST = ";EU"
ORIGIN = ";OC"

def build_search_page_url(dr):
    return (
        f"https://flightrewardfinder.qantas.com/?pg=1&d={DEST}&dr={dr}"
        f"&p={ALERT_PASSENGERS}&c={CABINS}"
    )

STATE_FILE = "state.json"
RESULTS_FILE = "results.json"
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
            page.goto(build_search_page_url(full_range),
                      wait_until="networkidle", timeout=60000)
        except Exception as e:
            print(f"goto() raised: {e}")
        page.wait_for_timeout(3000)

        print(f"Page title after load: {page.title()!r}")

        all_entries = []
        for start, end in SEARCH_WINDOWS:
            dr = f"{start}I{end}"
            print(f"Fetching {dr} ...")
            flights = fetch_all_flights_for_window(page, dr)
            print(f"  -> {len(flights)} flight(s) returned")
            window_url = build_search_page_url(dr)
            all_entries.extend(flatten_entries(flights, window_url))

        try:
            page.screenshot(path=DEBUG_SCREENSHOT, full_page=True)
        except Exception as e:
            print(f"screenshot failed: {e}")

        browser.close()

    return all_entries


def flatten_entries(flights, search_url):
    """One dict per (flight, cabin) pair where that cabin has an offer.
    Includes full airport names, every leg with operating carrier, and the
    exact search URL (matching this flight's date window) so each result
    can link straight to a pre-filled search."""
    entries = []

    for flight in flights:
        origin = flight.get("origin") or {}
        dest = flight.get("destination") or {}
        legs = []
        for leg in flight.get("legs", []):
            leg_origin = leg.get("origin") or {}
            leg_dest = leg.get("destination") or {}
            legs.append({
                "departsAt": leg.get("departsAt"),
                "arrivesAt": leg.get("arrivesAt"),
                "duration": leg.get("duration"),
                "layoverDuration": leg.get("layoverDuration"),
                "flightNumber": leg.get("flightNumber"),
                "operatedBy": leg.get("operatedBy"),
                "equipment": leg.get("equipment"),
                "originCode": leg_origin.get("code"),
                "originName": leg_origin.get("city") or leg_origin.get("name"),
                "destCode": leg_dest.get("code"),
                "destName": leg_dest.get("city") or leg_dest.get("name"),
            })

        cabins = flight.get("cabins") or {}
        for cabin_name in ("Business", "First"):
            cabin = cabins.get(cabin_name)
            if not cabin:
                continue
            entries.append({
                "departsAt": flight.get("departsAt"),
                "arrivesAt": flight.get("arrivesAt"),
                "originCode": origin.get("code"),
                "originName": origin.get("city") or origin.get("name"),
                "destCode": dest.get("code"),
                "destName": dest.get("city") or dest.get("name"),
                "cabin": cabin_name,
                "points": cabin.get("points"),
                "tax": cabin.get("tax"),
                "currency": cabin.get("currency"),
                "seats": cabin.get("seats"),
                "stopovers": flight.get("stopovers"),
                "duration": flight.get("duration"),
                "legs": legs,
                "searchUrl": search_url,
            })
    return entries


def entry_key(e):
    return (f"{e['departsAt']} | {e['originCode']}->{e['destCode']} | "
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


def fmt_dt(iso):
    if not iso:
        return "—"
    try:
        d = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    return d.strftime("%a, %-d %b %Y %H:%M")


def format_entry_for_email(e):
    lines = []
    lines.append(
        f"\u2708 {e['originName']} ({e['originCode']}) \u2192 "
        f"{e['destName']} ({e['destCode']})"
    )
    points = f"{e['points']:,}" if e.get("points") is not None else "—"
    tax = f"{e['currency']}{e['tax']}" if e.get("tax") is not None else ""
    lines.append(
        f"   {e['cabin']} \u00b7 {points} pts + {tax} tax \u00b7 "
        f"{e['seats']} seat(s) available"
    )
    lines.append(
        f"   Depart {fmt_dt(e['departsAt'])} \u2192 Arrive {fmt_dt(e['arrivesAt'])} "
        f"({e['duration']}, {e['stopovers']} stop(s))"
    )
    for i, leg in enumerate(e.get("legs", []), start=1):
        lines.append(
            f"   Leg {i}: {leg['originCode']} \u2192 {leg['destCode']} "
            f"({leg['originName']} \u2192 {leg['destName']}) \u00b7 "
            f"{leg['flightNumber']} operated by {leg['operatedBy']} \u00b7 "
            f"{leg['equipment']} \u00b7 {leg['duration']}"
        )
        if leg.get("layoverDuration") and leg["layoverDuration"] not in ("00h 00m", "0h 00m"):
            lines.append(
                f"      Layover: {leg['layoverDuration']} in "
                f"{leg['destName']} ({leg['destCode']})"
            )
    lines.append(f"   Check live: {e.get('searchUrl', 'https://flightrewardfinder.qantas.com/')}")
    return "\n".join(lines)


def fmt_points(points):
    return f"{points:,}" if points is not None else "—"


def build_html_email(entries):
    ink = "#1A1D23"
    dim = "#6B7280"
    line = "#E5E7EB"
    gold = "#A9791A"
    gold_bg = "#FBF4E2"
    green = "#1E8A5F"
    green_bg = "#E9F7F0"

    cards = []
    for e in entries:
        legs_html = ""
        for i, leg in enumerate(e.get("legs", []), start=1):
            layover = ""
            if leg.get("layoverDuration") and leg["layoverDuration"] not in ("00h 00m", "0h 00m"):
                layover = (
                    f'<div style="font-size:12px;color:{dim};margin:2px 0 8px 20px;">'
                    f'Layover {leg["layoverDuration"]} \u00b7 {leg["destName"]} ({leg["destCode"]})'
                    f"</div>"
                )
            legs_html += f"""
            <div style="font-size:13px;color:{ink};padding:8px 0 0 20px;border-left:2px solid {line};margin-left:6px;">
              <strong>{leg['originCode']} \u2192 {leg['destCode']}</strong>
              <span style="color:{dim};"> \u00b7 {leg['originName']} \u2192 {leg['destName']}</span><br>
              <span style="color:{dim};">{leg['flightNumber']} operated by {leg['operatedBy']} \u00b7 {leg['equipment']} \u00b7 {leg['duration']}</span>
            </div>
            {layover}
            """

        cabin_color = gold if e["cabin"] == "Business" else ink
        cabin_bg = gold_bg if e["cabin"] == "Business" else "#F3F4F6"

        cards.append(f"""
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
               style="background:#ffffff;border:1px solid {line};border-radius:8px;margin-bottom:16px;">
          <tr>
            <td style="padding:20px;">
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
                <tr>
                  <td style="font-size:17px;font-weight:600;color:{ink};">
                    {e['originName']} ({e['originCode']}) &rarr; {e['destName']} ({e['destCode']})
                  </td>
                  <td align="right">
                    <span style="background:{green_bg};color:{green};font-size:12px;font-weight:600;
                                 padding:4px 10px;border-radius:999px;white-space:nowrap;">
                      {e['seats']} seat(s) free
                    </span>
                  </td>
                </tr>
              </table>

              <table role="presentation" cellpadding="0" cellspacing="0" style="margin:10px 0 4px;">
                <tr>
                  <td style="background:{cabin_bg};color:{cabin_color};font-size:12px;font-weight:600;
                             padding:3px 9px;border-radius:4px;">{e['cabin']}</td>
                  <td style="width:10px;"></td>
                  <td style="font-size:14px;color:{gold};font-weight:700;">{fmt_points(e['points'])} pts</td>
                  <td style="font-size:13px;color:{dim};padding-left:6px;">+ {e['currency']}{e['tax']} tax</td>
                </tr>
              </table>

              <div style="font-size:13px;color:{dim};margin:10px 0 14px;">
                Depart <strong style="color:{ink};">{fmt_dt(e['departsAt'])}</strong>
                &rarr; Arrive <strong style="color:{ink};">{fmt_dt(e['arrivesAt'])}</strong>
                &nbsp;\u00b7&nbsp; {e['duration']}, {e['stopovers']} stop(s)
              </div>

              {legs_html}

              <div style="margin-top:16px;">
                <a href="{e.get('searchUrl', 'https://flightrewardfinder.qantas.com/')}"
                   style="display:inline-block;background:{ink};color:#ffffff;font-size:13px;
                          font-weight:600;text-decoration:none;padding:10px 18px;border-radius:6px;">
                  Check live availability &rarr;
                </a>
              </div>
            </td>
          </tr>
        </table>
        """)

    body = "".join(cards)

    return f"""<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background:#F3F4F6;font-family:-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#F3F4F6;padding:24px 0;">
    <tr>
      <td align="center">
        <table role="presentation" width="100%" style="max-width:600px;" cellpadding="0" cellspacing="0">
          <tr>
            <td style="padding:0 0 20px;">
              <div style="font-size:20px;font-weight:700;color:{ink};">
                New reward seat(s) found
              </div>
              <div style="font-size:13px;color:{dim};margin-top:4px;">
                {len(entries)} fare(s) with {ALERT_PASSENGERS}+ seats \u00b7 MEL/SYD &rarr; Europe \u00b7 Business/First
              </div>
            </td>
          </tr>
          <tr><td>{body}</td></tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""


def send_email(new_entries):
    plain_body = (
        f"New Qantas reward seats found ({ALERT_PASSENGERS} pax, "
        f"MEL/SYD-Europe, Business/First):\n\n"
    )
    plain_body += "\n\n".join(format_entry_for_email(e) for e in new_entries)
    plain_body += "\n\nCheck live: https://flightrewardfinder.qantas.com/"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"New Qantas reward seat(s) found ({len(new_entries)})"
    msg["From"] = GMAIL_USER
    msg["To"] = ALERT_TO

    msg.attach(MIMEText(plain_body, "plain"))
    msg.attach(MIMEText(build_html_email(new_entries), "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_USER, [ALERT_TO], msg.as_string())


def main():
    try:
        all_entries = fetch_availability()
    except Exception as e:
        print(f"fetch_availability() raised: {e}")
        with open(RAW_DUMP_FILE, "w") as f:
            json.dump({"error": str(e)}, f, indent=2)
        sys.exit(1)

    with open(RAW_DUMP_FILE, "w") as f:
        json.dump(all_entries, f, indent=2)

    save_results(all_entries)

    bookable_entries = [e for e in all_entries if (e.get("seats") or 0) >= ALERT_PASSENGERS]
    entries_by_key = {entry_key(e): e for e in bookable_entries}
    current_keys = set(entries_by_key.keys())
    previous_keys = load_previous_state()

    print(f"{len(all_entries)} total fare entries found, "
          f"{len(current_keys)} bookable for {ALERT_PASSENGERS} pax")

    save_state(current_keys)

    new_keys = current_keys - previous_keys
    if new_keys:
        new_entries = [entries_by_key[k] for k in new_keys]
        print(f"Found {len(new_entries)} new seat(s) -- sending email.")
        send_email(new_entries)
    else:
        print("No new seats this run.")


if __name__ == "__main__":
    main()
