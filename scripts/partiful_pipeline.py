"""Partiful event -> LinkedIn-enriched workbook, in one command.

    python partiful_pipeline.py --event https://partiful.com/e/<ID> \
        --out "~/Desktop/<Event> Guest List - LinkedIn.xlsx"

Does the whole pipeline with no agent round-trips:

  1. refresh the stored Firebase token
  2. getEventInfo + getGuests (hard-fails on masked names)
  3. filter to real first+last names (drops single names, initial-surnames,
     couples, company names)
  4. Exa searches in parallel (default 8 workers), with a
     `site:linkedin.com/in` retry for names that surface no profile
  5. score candidates per profile slug and build the workbook
  6. audit: assert every URL cell is backed by a literal /in/ hit
  7. optionally create a standalone Google Sheet (--upload)
  8. optionally add a tab to the Oxen-Forecast master sheet (--append-master)

Auth comes from <hermes>/partiful-auth.json; the Exa key from <hermes>/.env.
Exit code is non-zero on any failure, so a cron job can alert on it.
"""
from __future__ import annotations

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

# openpyxl is imported lazily inside write_workbook()/upload_to_sheets() so that
# partiful_incremental.py can `from partiful_pipeline import classify` without
# needing openpyxl installed in the interpreter doing the scrape.

HERMES = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "hermes")
AUTH = os.path.join(HERMES, "partiful-auth.json")
ENV = os.path.join(HERMES, ".env")
API = "https://api.partiful.com/"
MASTER_ID = "1XNtycJwuDnl_OuPw_1KAASU19TRD2WstEiYH9JPFy7o"
MAX_TAB_NAME_ATTEMPTS = 50
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36")


def die(msg, code=1):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


# ---------------------------------------------------------------- Partiful

def post(url, data, headers):
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=45) as r:
        return r.read().decode()


def refresh_access(cfg):
    body = urllib.parse.urlencode({"grant_type": "refresh_token",
                                   "refresh_token": cfg["refresh_token"]}).encode()
    try:
        out = json.loads(post(
            f"https://securetoken.googleapis.com/v1/token?key={cfg['firebase_api_key']}",
            body, {"Content-Type": "application/x-www-form-urlencoded",
                   "Referer": "https://partiful.com/"}))
    except Exception as e:
        die(f"token refresh failed ({e}). The refresh token was likely revoked - "
            "log into Partiful in Chrome and re-extract it.")
    cfg["access_token"] = out["id_token"]
    cfg["user_id"] = out["user_id"]
    if out.get("refresh_token"):
        cfg["refresh_token"] = out["refresh_token"]
    json.dump(cfg, open(AUTH, "w", encoding="utf-8"))
    return cfg["access_token"], cfg["user_id"]


def api(fn, params, access, uid):
    payload = json.dumps({"data": {"params": params, "userId": uid}}).encode()
    body = post(API + fn, payload, {
        "Content-Type": "application/json", "Origin": "https://partiful.com",
        "Referer": "https://partiful.com/", "Authorization": "Bearer " + access,
        "User-Agent": UA})
    return json.loads(body)["result"]["data"]


MASKED = re.compile(r"^[A-Za-z]\s*x{2,}")
TOKCH = re.compile(r"^[A-Za-z\u00c0-\u024f'\u2019.\-]+$")
INITIAL = re.compile(r"^[A-Za-z]?\.?$")
COMPANY = re.compile(r"\b(inc|llc|ltd|corp|company|group|labs?|studio|ventures|capital"
                     r"|partners|naturals|team)\b", re.I)


def strip_handles(n):
    n = re.sub(r"@\S+", "", n)
    n = re.sub(r"https?://\S+", "", n)
    n = re.sub(r"[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F]", "", n)
    return re.sub(r"\s+", " ", n).strip(" ,;-|")


def classify(guests):
    passing, dropped, seen = [], [], set()
    for g in guests:
        raw = (g.get("user") or {}).get("name") or g.get("name")
        if not raw:
            continue
        key = raw.strip().lower()
        if key in seen:
            continue
        seen.add(key)
        n = strip_handles(raw)
        if not n:
            dropped.append([raw, "no name after cleanup"]); continue
        if COMPANY.search(n):
            dropped.append([raw, "company/brand-like"]); continue
        if re.search(r"\s*(?:&|\band\b)\s*", n):
            dropped.append([raw, "multiple people (couple/duo)"]); continue
        toks = n.split()
        # only a *surname* initial disqualifies; a middle initial is fine
        real = [t for t in toks if not INITIAL.match(t)]
        if len(real) < 2:
            dropped.append([raw, "initial instead of a surname" if len(toks) > 1
                            else "first name only"]); continue
        if INITIAL.match(toks[-1]):
            dropped.append([raw, "initial instead of a surname"]); continue
        if len(toks) >= 2 and all(TOKCH.match(t) for t in toks):
            passing.append({"name": n, "status": g.get("status"),
                            "plusOneCount": g.get("plusOneCount", 0),
                            "rsvpDate": g.get("rsvpDate")})
        elif len(toks) == 1:
            dropped.append([raw, "first name only"])
        else:
            dropped.append([raw, "not a two-part name"])
    return passing, dropped


# --------------------------------------------------------------------- Exa

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
    last = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(
                "https://api.exa.ai/search", data=payload, method="POST",
                headers={"Content-Type": "application/json", "x-api-key": key})
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode()).get("results", [])
        except Exception as e:
            last = e
            time.sleep(1.5 * (attempt + 1))
    return [{"error": str(last)[:200]}]


def norm_hits(results):
    out = []
    for h in results:
        out.append({"url": h.get("url"), "title": h.get("title") or "",
                    "desc": (h.get("text") or h.get("summary") or "")[:1500]})
    return out


def enrich(key, names, workers, city):
    store, t0 = {}, time.time()

    def one(n):
        hits = norm_hits(exa_search(key, f'"{n}" LinkedIn {city}'))
        if not any(re.search(r"linkedin\.com/in/", h.get("url") or "") for h in hits):
            hits += norm_hits(exa_search(key, f'"{n}" site:linkedin.com/in'))
        return n, hits

    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for n, hits in ex.map(one, names):
            store[n] = hits
            done += 1
            if done % 25 == 0:
                print(f"    searched {done}/{len(names)}  ({time.time()-t0:.0f}s)")
    return store, time.time() - t0


# ---------------------------------------------------------------- scoring

METRO_RE = re.compile(r"([A-Z][A-Za-z.'\- ]+Metropolitan Area)\s*\(US\)")
CITY_RE = re.compile(
    r"([A-Z][A-Za-z.'\-]+(?: [A-Z][A-Za-z.'\-]+)*,\s*"
    r"(?:California|CA|New York|NY|Texas|TX|Washington|WA|Illinois|IL|Massachusetts|MA|"
    r"Florida|FL|Georgia|GA|Colorado|CO|Arizona|AZ|Nevada|NV|Oregon|OR|Utah|UT|Michigan|MI|"
    r"Ohio|OH|Pennsylvania|PA|North Carolina|NC|Virginia|VA|Tennessee|TN|Minnesota|MN|"
    r"Missouri|MO|Maryland|MD|Indiana|IN|Wisconsin|WI|Connecticut|CT|New Jersey|NJ|"
    r"[A-Z][a-z]+(?: [A-Z][a-z]+)?),\s*United States)")

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
 "pac oima","arleta","panorama city","northridge","granada hills","porter ranch","north hills",
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


def norm(s):
    return re.sub(r"\s+", " ", re.sub(r"[.\u2019']", "", (s or "").lower())).strip()


def name_forms(n):
    f = norm(n)
    forms = {f}
    parts = f.split()
    if len(parts) > 2:
        forms.add(parts[0] + " " + parts[-1])
    return forms


def locs_in(text):
    out = [m.group(1).strip() for m in METRO_RE.finditer(text)]
    out += [m.group(1).strip() for m in CITY_RE.finditer(text)]
    return out


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


def slug_from(url):
    """ONLY a literal /in/ URL proves a profile slug - a post URL's slug is the
    author's, and for a company post it is the company. Never construct one."""
    m = re.search(r"linkedin\.com/in/([A-Za-z0-9\-_%.]+)", url or "")
    return (m.group(1), True) if m else (None, False)


def build_rows(store):
    rows = []
    for name in sorted(store, key=str.lower):
        forms = name_forms(name)
        cands = {}
        for h in store[name]:
            slug, is_profile = slug_from(h.get("url"))
            if not slug:
                continue
            blob = (h.get("title") or "") + " \n " + (h.get("desc") or "")
            name_hit = any(f in norm(blob) for f in forms)
            c = cands.setdefault(slug, {"slug": slug, "profile": False, "name_hit": False,
                                        "locs": [], "flags": []})
            c["profile"] = c["profile"] or is_profile
            c["name_hit"] = c["name_hit"] or name_hit
            for l in locs_in(blob):
                if l not in c["locs"]:
                    c["locs"].append(l)
                    c["flags"].append(classify_loc(l))
        ranked = []
        for c in cands.values():
            if not c["profile"]:
                continue
            flag = sorted(c["flags"], key=lambda f: ORDER[f])[0] if c["flags"] else "Unknown"
            loc = next((l for l, f in zip(c["locs"], c["flags"]) if f == flag), None)
            score = (5 if c["name_hit"] else 0) + 3 + (2 if c["locs"] else 0)
            ranked.append({**c, "flag": flag, "loc": loc, "score": score})
        ranked.sort(key=lambda c: (-c["score"], ORDER[c["flag"]]))
        n = len(ranked)
        if not ranked:
            rows.append({"name": name, "url": None, "loc": None, "flag": "Unknown",
                         "conf": "Low", "note": "No LinkedIn profile surfaced", "n": 0})
            continue
        best = ranked[0]
        if best["flag"] == "Yes":
            conf = "High" if n == 1 else "Medium"
        elif best["flag"] == "Adjacent":
            conf = "Medium" if n == 1 else "Low"
        else:
            conf = "Low"
        note = f"{n} candidate{'s' if n != 1 else ''}"
        if best["loc"]:
            note += f"; {best['loc']}"
        rows.append({"name": name, "url": f"https://linkedin.com/in/{best['slug']}",
                     "loc": best["loc"], "flag": best["flag"], "conf": conf,
                     "note": note, "n": n})
    return rows


def audit(rows, store):
    bad = []
    for r in rows:
        if not r["url"]:
            continue
        slug = r["url"].rsplit("/", 1)[-1]
        if not any(re.search(r"linkedin\.com/in/" + re.escape(slug), h.get("url") or "")
                   for h in store.get(r["name"], [])):
            bad.append((r["name"], slug))
    return bad


# -------------------------------------------------------------- workbook

def write_workbook(rows, dropped, ev, eid, out, extra_meta=()):
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
    conf = Counter(r["conf"] for r in rows)
    meta = [
        ("Event", ev.get("title")),
        ("Partiful URL", f"https://partiful.com/e/{eid}"),
        ("Start (UTC)", ev.get("startDate")),
        ("Venue", ev.get("location")),
        ("Names in this build", len(rows)),
        ("With LinkedIn URL", sum(1 for r in rows if r["url"])),
        ("LA Area? = Yes", sum(1 for r in rows if r["flag"] == "Yes")),
        ("Confidence High", conf["High"]),
        ("Confidence Medium", conf["Medium"]),
        ("Confidence Low", conf["Low"]),
        ("Source", "Partiful getGuests API (authenticated) + Exa search API"),
    ]
    for k, v in list(meta) + list(extra_meta):
        ws3.append([k, v])
    for row in ws3.iter_rows(min_row=1, max_row=ws3.max_row, min_col=1, max_col=1):
        row[0].font = Font(bold=True)
    ws3.column_dimensions["A"].width = 26
    ws3.column_dimensions["B"].width = 95

    wb.save(out)


def upload_to_sheets(xlsx_path, title=None):
    """Create a Google Sheet from the workbook, mirroring every tab.

    Imports are lazy so the pipeline still runs on an interpreter without the
    Google libraries when --upload is not requested.
    """
    import openpyxl
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    token = os.path.join(HERMES, "google_token.json")
    if not os.path.isfile(token):
        die(f"not authenticated with Google (no {token}). "
            "Run the google-workspace setup first.")
    creds = Credentials.from_authorized_user_file(token)
    sh = build("sheets", "v4", credentials=creds)

    wb = openpyxl.load_workbook(xlsx_path)
    tabs = []
    for ws in wb.worksheets:
        rows = [[("" if c is None else c) for c in r]
                for r in ws.iter_rows(values_only=True)]
        while rows and all(c == "" for c in rows[-1]):
            rows.pop()
        tabs.append({"name": ws.title, "rows": rows})

    created = sh.spreadsheets().create(body={
        "properties": {"title": title or os.path.splitext(os.path.basename(xlsx_path))[0]},
        "sheets": [{"properties": {"title": t["name"]}} for t in tabs],
    }).execute()
    sid = created["spreadsheetId"]
    # sheetId is assigned by the API, not 0..n - read it back from the response
    for t, s in zip(tabs, created["sheets"]):
        t["sheet_id"] = s["properties"]["sheetId"]

    reqs = []
    for t in tabs:
        if t["rows"]:
            sh.spreadsheets().values().update(
                spreadsheetId=sid, range=f"'{t['name']}'!A1",
                valueInputOption="RAW", body={"values": t["rows"]}).execute()
        ncols = max((len(r) for r in t["rows"]), default=1)
        reqs.append({"updateSheetProperties": {
            "properties": {"sheetId": t["sheet_id"],
                           "gridProperties": {"frozenRowCount": 1}},
            "fields": "gridProperties.frozenRowCount"}})
        if len(t["rows"]) > 1:
            reqs.append({"setBasicFilter": {"filter": {
                "range": {"sheetId": t["sheet_id"], "startRowIndex": 0,
                          "endRowIndex": len(t["rows"]),
                          "startColumnIndex": 0, "endColumnIndex": ncols}}}})
        reqs.append({"repeatCell": {
            "range": {"sheetId": t["sheet_id"], "startRowIndex": 0, "endRowIndex": 1,
                      "startColumnIndex": 0, "endColumnIndex": ncols},
            "cell": {"userEnteredFormat": {
                "textFormat": {"bold": True,
                               "foregroundColor": {"red": 1.0, "green": 1.0, "blue": 1.0}},
                "backgroundColor": {"red": 0.122, "green": 0.220, "blue": 0.392}}},
            "fields": "userEnteredFormat(textFormat,backgroundColor)"}})
    sh.spreadsheets().batchUpdate(spreadsheetId=sid, body={"requests": reqs}).execute()
    return f"https://docs.google.com/spreadsheets/d/{sid}"


def append_to_master(rows, tab_name):
    """Add a new tab to the Oxen-Forecast master sheet with enriched rows.

    If the tab name already exists, retries with " (2)", " (3)", ... suffixes.
    Returns the URL to the new tab.
    """
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError

    token = os.path.join(HERMES, "google_token.json")
    if not os.path.isfile(token):
        die(f"not authenticated with Google (no {token}). "
            "Run the google-workspace setup first.")
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
                body={"requests": [{"addSheet": {
                    "properties": {"title": tab_name}}}]}
            ).execute()
            new_sheet_id = resp["replies"][0]["addSheet"]["properties"]["sheetId"]
            break
        except HttpError as e:
            if "already exists" not in str(e):
                raise
            attempt_n += 1
            if attempt_n > MAX_TAB_NAME_ATTEMPTS:
                die(f"Could not find a free tab name for {base!r} in master "
                    f"sheet after {MAX_TAB_NAME_ATTEMPTS} attempts.")
            tab_name = f"{base[:75]} ({attempt_n})"

    SHEET_COLS = ["Name", "LinkedIn Profile", "Location",
                  "LA Area?", "Confidence", "Notes"]
    sheet_rows = [SHEET_COLS] + [
        [r["name"], r["url"] or "", r["loc"] or "",
         r["flag"], r["conf"], r["note"]]
        for r in sorted(rows, key=lambda x: (ORDER[x["flag"]], x["name"].lower()))
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
            "properties": {"sheetId": new_sheet_id,
                           "gridProperties": {"frozenRowCount": 1}},
            "fields": "gridProperties.frozenRowCount"}},
        {"repeatCell": {
            "range": {"sheetId": new_sheet_id,
                      "startRowIndex": 0, "endRowIndex": 1,
                      "startColumnIndex": 0, "endColumnIndex": ncols},
            "cell": {"userEnteredFormat": {
                "textFormat": {"bold": True,
                               "foregroundColor": {"red": 1.0, "green": 1.0, "blue": 1.0}},
                "backgroundColor": {"red": 0.122, "green": 0.220, "blue": 0.392}}},
            "fields": "userEnteredFormat(textFormat,backgroundColor)"}},
    ]
    if nrows > 1:
        style_reqs.append({"setBasicFilter": {"filter": {
            "range": {"sheetId": new_sheet_id, "startRowIndex": 0,
                      "endRowIndex": nrows,
                      "startColumnIndex": 0, "endColumnIndex": ncols}}}})
    sh.spreadsheets().batchUpdate(
        spreadsheetId=MASTER_ID, body={"requests": style_reqs}).execute()
    return (f"https://docs.google.com/spreadsheets/d/{MASTER_ID}"
            f"#gid={new_sheet_id}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--event", required=True, help="Partiful event URL or ID")
    ap.add_argument("--out", required=True, help="output .xlsx path")
    ap.add_argument("--city", default="Los Angeles", help="city used in the search query")
    ap.add_argument("--workers", type=int, default=8, help="parallel Exa searches")
    ap.add_argument("--statuses", default="", help="comma list to keep, e.g. GOING,MAYBE")
    ap.add_argument("--raw-out", default=None, help="save the raw guest pull here")
    ap.add_argument("--upload", action="store_true",
                    help="also create a Google Sheet from the workbook and print its URL")
    ap.add_argument("--sheet-title", default=None,
                    help="title for the created Google Sheet (default: the workbook name)")
    ap.add_argument("--union-raw", default=None,
                    help="merge a previous raw pull (recovers past the 500 cap)")
    ap.add_argument("--no-enrich", action="store_true", help="names only, skip LinkedIn")
    ap.add_argument("--append-master", action="store_true",
                    help="add a tab to the Oxen-Forecast master Google Sheet")
    a = ap.parse_args()

    if not os.path.isfile(AUTH):
        die(f"auth file not found: {AUTH}")
    cfg = json.load(open(AUTH, encoding="utf-8"))
    access, uid = refresh_access(cfg)

    m = re.search(r"/e/([A-Za-z0-9]+)", a.event)
    eid = m.group(1) if m else a.event.strip()

    info = api("getEventInfo", {"eventId": eid}, access, uid)
    ev = info.get("event", {})
    guests = api("getGuests", {"eventId": eid}, access, uid)
    print(f"{ev.get('title')}\n  event: {eid}")

    names = [(g.get("user") or {}).get("name") or g.get("name") for g in guests]
    masked = sum(1 for n in names if n and (MASKED.match(n) or re.search(r"\bx{3,}\b", n)))
    if masked:
        die(f"{masked}/{len(names)} names are MASKED - auth is not effective.")

    # Always persist the raw pull: it is the only stable record of guest ids, and
    # a later union across pulls is impossible without it. Callers can override
    # the location, but there is no way to end up with none.
    raw_path = a.raw_out or os.path.join(HERMES, f"partiful-raw-{eid}.json")
    json.dump({"result": {"data": guests}}, open(raw_path, "w", encoding="utf-8"))
    print(f"  raw pull: {raw_path}")
    if a.union_raw and os.path.isfile(a.union_raw):
        prev = json.load(open(a.union_raw, encoding="utf-8"))["result"]["data"]
        seen = {g["id"] for g in guests}
        added = [g for g in prev if g["id"] not in seen]
        guests = guests + added
        print(f"  union: +{len(added)} records from {os.path.basename(a.union_raw)}")

    if a.statuses:
        keep = {s.strip().upper() for s in a.statuses.split(",")}
        present = Counter((g.get("status") or "").upper() for g in guests)
        kept = [g for g in guests if (g.get("status") or "").upper() in keep]
        if not kept:
            # Filtering to a status the event does not use (e.g. --statuses GOING
            # on an approval-gated event where everyone is APPROVED) would
            # otherwise write an empty workbook and look like a quiet success.
            die(f"--statuses {sorted(keep)} matched 0 of {len(guests)} guests. "
                f"Statuses present: {dict(present)}. Drop --statuses or use one of "
                f"those values.")
        if len(kept) < len(guests):
            print(f"  status filter {sorted(keep)}: {len(guests)} -> {len(kept)} "
                  f"(present: {dict(present)})")
        guests = kept

    passing, dropped = classify(guests)
    print(f"  returned: {len(guests)}"
          + ("  <-- exactly 500, likely a CAP" if len(guests) == 500 else ""))
    print(f"  passed:   {len(passing)}   dropped: {len(dropped)}")

    extra = []
    if len(guests) == 500:
        extra.append(("Caveat", "getGuests returned exactly 500 and ignores all pagination "
                                "params; this is a shifting sample, not the full list."))

    if a.no_enrich:
        rows = [{"name": p["name"], "url": None, "loc": None, "flag": "Unknown",
                 "conf": "Low", "note": "", "n": 0} for p in passing]
        write_workbook(rows, dropped, ev, eid, a.out, extra)
        print("wrote:", a.out)
        if a.upload:
            print("sheet:", upload_to_sheets(a.out, a.sheet_title))
        if a.append_master:
            tab = re.sub(r"[^A-Za-z0-9 _-]+", "", ev.get("title") or eid)[:80].strip() or "Event"
            print("master:", append_to_master(rows, tab))
        return

    key = load_exa_key()
    nm = [p["name"] for p in passing]
    print(f"  enriching {len(nm)} names via Exa ({a.workers} workers)...")
    store, secs = enrich(key, nm, a.workers, a.city)
    print(f"  enrichment done in {secs:.0f}s")

    rows = build_rows(store)
    bad = audit(rows, store)
    if bad:
        die(f"AUDIT FAILED: {len(bad)} URL(s) not backed by a literal /in/ hit: {bad[:5]}")
    print(f"  audit: 0 fabricated URLs across {sum(1 for r in rows if r['url'])} URL cells")

    write_workbook(rows, dropped, ev, eid, a.out, extra)
    conf = Counter(r["conf"] for r in rows)
    print(f"  LA=Yes:   {sum(1 for r in rows if r['flag'] == 'Yes')}")
    print(f"  with URL: {sum(1 for r in rows if r['url'])}")
    print(f"  conf:     {conf['High']} High / {conf['Medium']} Medium / {conf['Low']} Low")
    print("wrote:", a.out)
    if a.upload:
        print("sheet:", upload_to_sheets(a.out, a.sheet_title))
    if a.append_master:
        tab = re.sub(r"[^A-Za-z0-9 _-]+", "", ev.get("title") or eid)[:80].strip() or "Event"
        print("master:", append_to_master(rows, tab))


if __name__ == "__main__":
    main()
