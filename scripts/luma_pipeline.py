#!/usr/bin/env python3
"""luma_pipeline.py — Luma event -> LinkedIn-enriched workbook + master sheet tab.

Usage:
    python luma_pipeline.py --url https://luma.com/<slug>?tk=<token>
    python luma_pipeline.py --url https://luma.com/<slug>?tk=<token> --append-master
    python luma_pipeline.py --url <url> --no-enrich --out guests.xlsx

The share token (?tk=...) authenticates the get-guest-list endpoint. No session
cookie needed. Luma provides linkedin_handle directly for most guests; missing
ones are enriched via Exa.

Auth: Exa key from <hermes>/.env; Google token from <hermes>/google_token.json.
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERMES = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "hermes")
ENV = os.path.join(HERMES, ".env")
MASTER_ID = "1XNtycJwuDnl_OuPw_1KAASU19TRD2WstEiYH9JPFy7o"
MAX_TAB_NAME_ATTEMPTS = 50

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36"


def die(msg, code=1):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


# ---------------------------------------------------------------- Luma

def parse_luma_url(url):
    """Extract event_api_id, title, and share token from a Luma URL."""
    print(f"Fetching event page: {url}")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        html = r.read().decode()

    m = re.search(r'"api_id":"(evt-[A-Za-z0-9]+)"', html)
    if not m:
        die("Could not find event_api_id in page HTML")
    event_id = m.group(1)

    title_m = re.search(r'<meta property="og:title" content="([^"]+)"', html)
    title = title_m.group(1).replace(" · Luma", "") if title_m else event_id

    parsed = urllib.parse.urlparse(url)
    params = urllib.parse.parse_qs(parsed.query)
    ticket_key = params.get("tk", [None])[0]

    print(f"  event_id: {event_id}")
    print(f"  title: {title}")
    print(f"  ticket_key: {'present' if ticket_key else 'NOT FOUND'}")

    if not ticket_key:
        die("No share token (?tk=...) in URL. The get-guest-list endpoint requires it.")

    return event_id, title, ticket_key


def fetch_guests(event_id, ticket_key):
    """Fetch guest list from Luma's internal endpoint."""
    params = urllib.parse.urlencode({
        "event_api_id": event_id,
        "ticket_key": ticket_key,
        "pagination_limit": 200,
    })
    url = f"https://api.luma.com/event/get-guest-list?{params}"

    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read().decode())

    entries = data.get("entries", [])
    has_more = data.get("has_more", False)

    if has_more:
        print(f"  WARNING: has_more=true, got {len(entries)} entries (may be truncated)")

    return entries


def classify(entries):
    """Split entries into (passing, dropped) — same filter as Partiful pipeline."""
    passing, dropped, seen = [], [], set()

    for e in entries:
        u = e.get("user", {})
        name = u.get("name", "") or ""
        first = u.get("first_name", "") or ""
        last = u.get("last_name", "") or ""
        linkedin = u.get("linkedin_handle", "") or ""

        if name and len(name.split()) >= 2:
            full_name = name
        elif first and last:
            full_name = f"{first} {last}"
        elif first:
            full_name = first
        else:
            full_name = name or ""

        full_name = re.sub(r"\s+", " ", full_name).strip()
        key = full_name.lower()

        if key in seen:
            continue
        seen.add(key)

        if len(full_name.split()) < 2:
            dropped.append([full_name or "(blank)", "Name incomplete (first+last required)"])
            continue

        if re.search(r"\b(inc|llc|ltd|corp|company|group|labs?|studio|ventures|capital|partners)\b", full_name, re.I):
            dropped.append([full_name, "company/brand-like"])
            continue

        if re.search(r"@\S+|https?://|[^\x00-\x7F\u00c0-\u024f\s'\-\.]", full_name):
            dropped.append([full_name, "contains handle/URL/emoji"])
            continue

        li_url = f"https://www.linkedin.com{linkedin}" if linkedin else None
        passing.append({
            "name": full_name,
            "linkedin": li_url,
            "timezone": u.get("timezone", ""),
        })

    return passing, dropped


# ---------------------------------------------------------------- Exa

def load_exa_key():
    if not os.path.isfile(ENV):
        die(f"no .env at {ENV}")
    for line in open(ENV, encoding="utf-8", errors="ignore"):
        if line.startswith("EXA_API_KEY="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    die("EXA_API_KEY not found in .env")


def exa_search(key, query, n=5, tries=3):
    payload = json.dumps({"query": query, "numResults": n,
                          "contents": {"text": {"maxCharacters": 1500}}}).encode()
    for attempt in range(tries):
        try:
            req = urllib.request.Request(
                "https://api.exa.ai/search", data=payload, method="POST",
                headers={"Content-Type": "application/json", "x-api-key": key})
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode()).get("results", [])
        except Exception:
            time.sleep(1.5 * (attempt + 1))
    return []


def enrich_missing(passing, key, workers, city):
    """Enrich names that lack LinkedIn via Exa."""
    missing = [p for p in passing if not p["linkedin"]]
    if not missing:
        return

    names = [p["name"] for p in missing]
    print(f"  enriching {len(names)} names via Exa ({workers} workers)...")
    t0 = time.time()

    def one(n):
        hits = exa_search(key, f'"{n}" LinkedIn {city}')
        urls = [h["url"] for h in hits if "linkedin.com/in/" in h.get("url", "")]
        if not urls:
            hits2 = exa_search(key, f'"{n}" site:linkedin.com/in')
            urls = [h["url"] for h in hits2 if "linkedin.com/in/" in h.get("url", "")]
        return n, urls[0] if urls else None

    results = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for n, url in ex.map(one, names):
            results[n] = url

    enriched = 0
    for p in missing:
        if p["name"] in results and results[p["name"]]:
            p["linkedin"] = results[p["name"]]
            enriched += 1

    print(f"  enriched {enriched}/{len(names)} in {time.time()-t0:.0f}s")


# ---------------------------------------------------------------- scoring

LA_CORE = [
    "los angeles","santa monica","culver city","venice","west hollywood","beverly hills","pasadena",
    "burbank","glendale","long beach","manhattan beach","torrance","el segundo","sherman oaks",
    "monterey park","santa clarita","inglewood","redondo beach","hermosa beach","playa vista",
    "woodland hills","encino","north hollywood","studio city","van nuys","chatsworth","canoga park",
    "reseda","tarzana","calabasas","agoura hills","malibu","pacific palisades","brentwood","westwood",
    "century city","hollywood","echo park","silver lake","los feliz","koreatown","marina del rey",
    "westchester","gardena","hawthorne","lawndale","carson","compton","pomona","claremont","whittier",
    "norwalk","downey","cerritos","lakewood","alhambra","arcadia","monrovia","azusa","covina",
    "el monte","rosemead","san gabriel","temple city","south pasadena","san marino","la canada",
    "la crescenta","montrose","eagle rock","highland park","atwater village","boyle heights",
    "lincoln heights","el sereno","west adams","leimert park","crenshaw","baldwin hills",
    "huntington park","south gate","lynwood","paramount","bell gardens","maywood","glendora",
    "san dimas","la verne","diamond bar","walnut","rowland heights","hacienda heights","west covina",
    "baldwin park","duarte","altadena","sierra madre","san pedro","wilmington","rancho palos verdes",
    "palos verdes","lomita","signal hill","bellflower","artesia","la mirada","pico rivera",
    "santa fe springs","universal city","toluca lake","tujunga","sunland","sylmar","san fernando",
    "pacoima","arleta","panorama city","northridge","granada hills","porter ranch","north hills",
    "winnetka","sherman village","topanga","westlake","greater los angeles","los angeles county",
]
LA_ADJACENT = [
    "thousand oaks","newbury park","westlake village","simi valley","oxnard","ventura","camarillo",
    "moorpark","riverside","san bernardino","ontario, ca","rancho cucamonga","fontana","moreno valley",
    "corona, ca","chino","chino hills","redlands","temecula","murrieta","lancaster","palmdale",
]
NON_LA = [
    "irvine","anaheim","tustin","orange, ca","orange county","santa ana","newport beach","costa mesa",
    "huntington beach","fullerton","garden grove","mission viejo","laguna","yorba linda","brea",
    "buena park","fountain valley","westminster","seal beach","placentia","la habra","san clemente",
    "dana point","aliso viejo","lake forest","rancho santa margarita","san francisco","san jose",
    "sunnyvale","palo alto","mountain view","santa clara","cupertino","fremont","oakland","berkeley",
    "pleasanton","san mateo","redwood city","menlo park","san bruno","milpitas","hayward","union city",
    "los altos","campbell","saratoga","walnut creek","concord, ca","san diego","santa barbara",
    "sacramento","new york","nyc","brooklyn","manhattan","austin","seattle","chicago","boston","miami",
    "atlanta","denver","phoenix","scottsdale","portland","salt lake","detroit","columbus",
    "philadelphia","pittsburgh","raleigh","nashville","minneapolis","kansas city","baltimore",
    "indianapolis","milwaukee","hartford","newark, nj","jersey city","london","toronto","vancouver",
    "dubai","singapore","bangalore","bengaluru","tel aviv","paris","berlin","houston","dallas",
    "san antonio","orlando","tampa","charlotte","st. louis","st louis","cincinnati","cleveland",
    "buffalo","providence","richmond","new orleans","las vegas","honolulu","anchorage","stockholm",
    "sweden","united kingdom","germany","france","canada","india","israel","australia","netherlands",
    "spain","italy","japan","china","brazil","mexico","ireland","switzerland","denmark","norway",
    "finland","poland","portugal","austria","belgium","czech","romania","ukraine","turkey",
]
ORDER = {"Yes": 0, "Adjacent": 1, "Unknown": 2, "No": 3}


def tz_to_loc(tz):
    parts = (tz or "").split("/")
    return parts[1].replace("_", " ") if len(parts) == 2 else tz


def classify_loc(loc):
    L = (loc or "").lower()
    for k in LA_CORE:
        if k in L:
            return "Yes"
    for k in LA_ADJACENT:
        if k in L:
            return "Adjacent"
    for k in NON_LA:
        if k in L:
            return "No"
    if "california" in L or ", ca" in L:
        return "Unknown"
    return "No"


def build_rows(passing):
    rows = []
    for p in passing:
        loc = tz_to_loc(p.get("timezone", ""))
        flag = classify_loc(loc)
        rows.append({
            "name": p["name"],
            "url": p.get("linkedin"),
            "loc": loc,
            "flag": flag,
            "conf": "Medium",
            "note": "Luma profile" if p.get("linkedin") else "Exa enriched",
        })
    return rows


# ---------------------------------------------------------------- workbook

def write_workbook(rows, dropped, title, url, out):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    hf = Font(bold=True, color="FFFFFF")
    hfill = PatternFill("solid", fgColor="1F3864")

    ws = wb.active
    ws.title = "LA-Area"
    cols = ["Name", "LinkedIn Profile", "Location", "LA Area?", "Confidence", "Notes"]
    ws.append(cols)
    ordered = sorted(rows, key=lambda r: (ORDER[r["flag"]], r["name"].lower()))
    for r in ordered:
        ws.append([r["name"], r["url"], r["loc"], r["flag"], r["conf"], r["note"]])
    for i, r in enumerate(ordered, start=2):
        if r["url"]:
            c = ws.cell(i, 2)
            c.hyperlink = r["url"]
            c.font = Font(color="0563C1", underline="single")
    for c in range(1, len(cols) + 1):
        cell = ws.cell(1, c)
        cell.font = hf
        cell.fill = hfill
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(cols))}{ws.max_row}"
    for col, w in zip("ABCDEF", (30, 46, 42, 11, 12, 54)):
        ws.column_dimensions[col].width = w

    ws2 = wb.create_sheet("Not Included")
    ws2.append(["Name", "Reason"])
    for name, reason in dropped:
        ws2.append([name, reason])
    for c in (1, 2):
        cell = ws2.cell(1, c)
        cell.font = hf
        cell.fill = hfill
    ws2.freeze_panes = "A2"
    ws2.column_dimensions["A"].width = 46
    ws2.column_dimensions["B"].width = 34

    ws3 = wb.create_sheet("Event")
    meta = [
        ("Event", title),
        ("Luma URL", url),
        ("Names in this build", len(rows)),
        ("With LinkedIn URL", sum(1 for r in rows if r["url"])),
        ("LA Area? = Yes", sum(1 for r in rows if r["flag"] == "Yes")),
        ("Source", "Luma get-guest-list endpoint (share token) + Exa search API"),
    ]
    for k, v in meta:
        ws3.append([k, v])
    for row in ws3.iter_rows(min_row=1, max_row=ws3.max_row, min_col=1, max_col=1):
        row[0].font = Font(bold=True)
    ws3.column_dimensions["A"].width = 26
    ws3.column_dimensions["B"].width = 95

    wb.save(out)


# ---------------------------------------------------------------- Google Sheets

def append_to_master(rows, tab_name):
    """Add a new tab to the Oxen-Forecast master sheet with enriched rows."""
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError

    token = os.path.join(HERMES, "google_token.json")
    if not os.path.isfile(token):
        die(f"not authenticated with Google (no {token}).")
    creds = Credentials.from_authorized_user_file(token)
    sh = build("sheets", "v4", credentials=creds)

    base = (tab_name or "Event")[:80].strip() or "Event"
    tab_name = base
    attempt_n = 1
    new_sheet_id = None
    while True:
        try:
            resp = sh.spreadsheets().batchUpdate(
                spreadsheetId=MASTER_ID,
                body={"requests": [{"addSheet": {"properties": {"title": tab_name}}}]}
            ).execute()
            new_sheet_id = resp["replies"][0]["addSheet"]["properties"]["sheetId"]
            break
        except HttpError as e:
            if "already exists" not in str(e):
                raise
            attempt_n += 1
            if attempt_n > MAX_TAB_NAME_ATTEMPTS:
                die(f"Could not find a free tab name for {base!r} in master sheet.")
            tab_name = f"{base[:75]} ({attempt_n})"

    SHEET_COLS = ["Name", "LinkedIn Profile", "Location", "LA Area?", "Confidence", "Notes"]
    ordered = sorted(rows, key=lambda r: (ORDER[r["flag"]], r["name"].lower()))
    sheet_rows = [SHEET_COLS] + [
        [r["name"], r["url"] or "", r["loc"], r["flag"], r["conf"], r["note"]]
        for r in ordered
    ]

    if sheet_rows:
        sh.spreadsheets().values().update(
            spreadsheetId=MASTER_ID,
            range=f"'{tab_name}'!A1",
            valueInputOption="RAW",
            body={"values": sheet_rows}).execute()

    ncols = max((len(r) for r in sheet_rows), default=6)
    nrows = len(sheet_rows)
    style_reqs = [
        {"updateSheetProperties": {
            "properties": {"sheetId": new_sheet_id, "gridProperties": {"frozenRowCount": 1}},
            "fields": "gridProperties.frozenRowCount"}},
        {"repeatCell": {
            "range": {"sheetId": new_sheet_id, "startRowIndex": 0, "endRowIndex": 1,
                      "startColumnIndex": 0, "endColumnIndex": ncols},
            "cell": {"userEnteredFormat": {
                "textFormat": {"bold": True, "foregroundColor": {"red": 1.0, "green": 1.0, "blue": 1.0}},
                "backgroundColor": {"red": 0.122, "green": 0.220, "blue": 0.392}}},
            "fields": "userEnteredFormat(textFormat,backgroundColor)"}},
    ]
    if nrows > 1:
        style_reqs.append({"setBasicFilter": {"filter": {
            "range": {"sheetId": new_sheet_id, "startRowIndex": 0, "endRowIndex": nrows,
                      "startColumnIndex": 0, "endColumnIndex": ncols}}}})
    sh.spreadsheets().batchUpdate(spreadsheetId=MASTER_ID, body={"requests": style_reqs}).execute()
    return f"https://docs.google.com/spreadsheets/d/{MASTER_ID}#gid={new_sheet_id}"


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="Scrape a Luma event guest list, enrich with LinkedIn, and export.")
    ap.add_argument("--url", required=True, help="Luma event URL with ?tk=<share_token>")
    ap.add_argument("--out", default=None, help="Output .xlsx path (default: Desktop/<Event>.xlsx)")
    ap.add_argument("--city", default="Los Angeles", help="City used in Exa search queries")
    ap.add_argument("--workers", type=int, default=8, help="Parallel Exa searches")
    ap.add_argument("--no-enrich", action="store_true", help="Names only, skip LinkedIn enrichment")
    ap.add_argument("--append-master", action="store_true",
                    help="Add a tab to the Oxen-Forecast master Google Sheet")
    a = ap.parse_args()

    event_id, title, ticket_key = parse_luma_url(a.url)
    print(f"\nFetching guests...")
    entries = fetch_guests(event_id, ticket_key)
    print(f"  returned: {len(entries)}")

    passing, dropped = classify(entries)
    print(f"  passed:   {len(passing)}   dropped: {len(dropped)}")

    luma_li = sum(1 for p in passing if p["linkedin"])
    print(f"  LinkedIn from Luma: {luma_li}/{len(passing)}")

    if not a.no_enrich:
        key = load_exa_key()
        enrich_missing(passing, key, a.workers, a.city)

    rows = build_rows(passing)
    print(f"  LA=Yes:   {sum(1 for r in rows if r['flag'] == 'Yes')}")
    print(f"  with URL: {sum(1 for r in rows if r['url'])}")

    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", title)[:50]
    desktop = os.path.join(os.environ.get("USERPROFILE", os.path.expanduser("~")), "Desktop")
    out_path = a.out or os.path.join(desktop, f"{safe} Guest List - LinkedIn.xlsx")

    write_workbook(rows, dropped, title, a.url, out_path)
    print(f"wrote: {out_path}")

    if a.append_master:
        tab = re.sub(r"[^A-Za-z0-9 _-]+", "", title)[:80].strip() or "Event"
        url = append_to_master(rows, tab)
        print(f"master: {url}")


if __name__ == "__main__":
    main()