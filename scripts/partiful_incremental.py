"""Incremental Partiful -> Google Sheets update.

Scrapes an event, works out which guests are genuinely NEW relative to an
existing sheet, enriches only those, and appends them. Existing rows are never
removed or reordered.

    python partiful_incremental.py --event <URL> --sheet-id <ID> [--dry-run]

Three things a naive name-diff gets wrong, all handled here:

1. Spelling drift. "daniela nevarez" vs "Daniela Nevaez", "Johnny Tran" vs
   "Jonny Tran" are the same person. Exact set-difference reports them as new
   and appends duplicates. Matching is normalised + fuzzy, with first names
   required to agree so "Julia Roga" and "Julia Price" stay distinct.

2. Prior exclusions are forgotten. A name the earlier run rejected as a company
   or a handle ("Momentum Impact", "GODMorphea Darklight") still passes the raw
   token filter, so it gets re-added as a guest. The existing "Not Included" tab
   is read back and treated as a carry-forward denylist.

3. Display names change. "Julia Price" became "Julia Price @ Millbrook" and is
   dropped by the filter, "Helen Quon" became "Helen Q". Neither is a departure,
   so nothing is removed on the strength of a missing name.
"""
from __future__ import annotations

import argparse
import difflib
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

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

HERMES = Path(os.environ.get("LOCALAPPDATA", os.path.expanduser("~"))) / "hermes"
AUTH = HERMES / "partiful-auth.json"
ENV = HERMES / ".env"
TOKEN = HERMES / "google_token.json"
API = "https://api.partiful.com/"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36")

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


# --------------------------------------------------------------- helpers

def die(msg, code=1):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def norm(s):
    return re.sub(r"\s+", " ", re.sub(r"[.\u2019'-]", " ", (s or "").lower())).strip()


def strip_company_suffix(n):
    """'Julia Price @ Millbrook' -> 'Julia Price'"""
    return re.split(r"\s+[@|]\s+|\s+-\s+", n)[0].strip()


def same_person(a, b):
    """Is `a` the same person as `b`?

    Whole-string similarity is checked FIRST, because first names drift too:
    "Johnny Tran" vs "Jonny Tran" is one person, and a strict first-name gate
    rejects it. Only after that does the first-name rule apply, which is what
    keeps "Julia Roga" and "Julia Price" apart.
    """
    na, nb = norm(strip_company_suffix(a)), norm(strip_company_suffix(b))
    if not na or not nb:
        return False
    if na == nb:
        return True
    # one name is the other plus extra trailing words (a surname added or
    # dropped: "Ana Maria Lopez" vs "Ana Maria Lopez Ruiz")
    if na.startswith(nb + " ") or nb.startswith(na + " "):
        return True
    if difflib.SequenceMatcher(None, na, nb).ratio() >= 0.88:
        return True
    ta, tb = na.split(), nb.split()
    if ta[0] != tb[0]:
        return False
    la, lb = ta[-1], tb[-1]
    if la == lb:
        return True
    if len(la) == 1 and lb.startswith(la):
        return True
    if len(lb) == 1 and la.startswith(lb):
        return True
    return False


def find_match(name, existing):
    for e in existing:
        if same_person(name, e):
            return e
    return None


# -------------------------------------------------------------- Partiful

def post(url, data, headers):
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=45) as r:
        return r.read().decode()


def partiful_guests(event):
    cfg = json.loads(AUTH.read_text(encoding="utf-8"))
    body = urllib.parse.urlencode({"grant_type": "refresh_token",
                                   "refresh_token": cfg["refresh_token"]}).encode()
    try:
        out = json.loads(post(
            f"https://securetoken.googleapis.com/v1/token?key={cfg['firebase_api_key']}",
            body, {"Content-Type": "application/x-www-form-urlencoded",
                   "Referer": "https://partiful.com/"}))
    except Exception as e:
        die(f"token refresh failed ({e}). Re-extract the Partiful token.")
    cfg["access_token"] = out["id_token"]
    cfg["user_id"] = out["user_id"]
    if out.get("refresh_token"):
        cfg["refresh_token"] = out["refresh_token"]
    AUTH.write_text(json.dumps(cfg), encoding="utf-8")

    m = re.search(r"/e/([A-Za-z0-9]+)", event)
    eid = m.group(1) if m else event.strip()
    payload = json.dumps({"data": {"params": {"eventId": eid},
                                   "userId": cfg["user_id"]}}).encode()
    raw = post(API + "getGuests", payload, {
        "Content-Type": "application/json", "Origin": "https://partiful.com",
        "Referer": "https://partiful.com/", "Authorization": "Bearer " + cfg["access_token"],
        "User-Agent": UA})
    guests = json.loads(raw)["result"]["data"]
    names = [(g.get("user") or {}).get("name") or g.get("name") for g in guests]
    masked = sum(1 for n in names if n and (re.match(r"^[A-Za-z]\s*x{2,}", n)
                                            or re.search(r"\bx{3,}\b", n)))
    if masked:
        die(f"{masked}/{len(names)} names are MASKED - auth is not effective.")
    return eid, guests


# ------------------------------------------------------------------ Exa

def load_exa_key():
    for line in ENV.read_text(encoding="utf-8", errors="ignore").splitlines():
        if line.startswith("EXA_API_KEY="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    die("EXA_API_KEY not found in .env")


def exa(key, query, n=5, tries=3):
    payload = json.dumps({"query": query, "numResults": n,
                          "contents": {"text": {"maxCharacters": 1500}}}).encode()
    last = None
    for a in range(tries):
        try:
            req = urllib.request.Request(
                "https://api.exa.ai/search", data=payload, method="POST",
                headers={"Content-Type": "application/json", "x-api-key": key})
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode()).get("results", [])
        except Exception as e:
            last = e
            time.sleep(1.5 * (a + 1))
    return [{"error": str(last)[:200]}]


def hits_of(results):
    return [{"url": h.get("url"), "title": h.get("title") or "",
             "desc": (h.get("text") or h.get("summary") or "")[:1500]} for h in results]


def enrich(key, names, workers, city):
    store = {}

    def one(n):
        hits = hits_of(exa(key, f'"{n}" LinkedIn {city}'))
        if not any(re.search(r"linkedin\.com/in/", h.get("url") or "") for h in hits):
            hits += hits_of(exa(key, f'"{n}" site:linkedin.com/in'))
        return n, hits

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for n, hits in ex.map(one, names):
            store[n] = hits
    return store


# -------------------------------------------------------------- scoring

def locs_in(text):
    return ([m.group(1).strip() for m in METRO_RE.finditer(text)]
            + [m.group(1).strip() for m in CITY_RE.finditer(text)])


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
    m = re.search(r"linkedin\.com/in/([A-Za-z0-9\-_%.]+)", url or "")
    return m.group(1) if m else None


def build_rows(store):
    rows = []
    for name in sorted(store, key=str.lower):
        forms = {norm(name)}
        parts = norm(name).split()
        if len(parts) > 2:
            forms.add(parts[0] + " " + parts[-1])
        cands = {}
        for h in store[name]:
            slug = slug_from(h.get("url"))
            if not slug:
                continue
            blob = (h.get("title") or "") + " \n " + (h.get("desc") or "")
            c = cands.setdefault(slug, {"slug": slug, "name_hit": False, "locs": [], "flags": []})
            c["name_hit"] = c["name_hit"] or any(f in norm(blob) for f in forms)
            for l in locs_in(blob):
                if l not in c["locs"]:
                    c["locs"].append(l)
                    c["flags"].append(classify_loc(l))
        ranked = []
        for c in cands.values():
            flag = sorted(c["flags"], key=lambda f: ORDER[f])[0] if c["flags"] else "Unknown"
            loc = next((l for l, f in zip(c["locs"], c["flags"]) if f == flag), None)
            ranked.append({**c, "flag": flag, "loc": loc,
                           "score": (5 if c["name_hit"] else 0) + 3 + (2 if c["locs"] else 0)})
        ranked.sort(key=lambda c: (-c["score"], ORDER[c["flag"]]))
        if not ranked:
            rows.append({"name": name, "url": None, "loc": None, "flag": "Unknown",
                         "conf": "Low", "note": "No LinkedIn profile surfaced", "n": 0})
            continue
        best, n = ranked[0], len(ranked)
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


# ------------------------------------------------------------ sheet I/O

def read_tab(sh, sid, tab):
    """Read a tab, returning [] if it does not exist.

    Master workbooks often have no 'Not Included' tab — the exclusions live in a
    separate sheet. A missing tab must not abort the run; it just means there is
    no carry-forward denylist to apply.
    """
    try:
        return sh.spreadsheets().values().get(
            spreadsheetId=sid, range=f"'{tab}'!A1:F").execute().get("values", [])
    except Exception as e:
        if "Unable to parse range" in str(e) or "not found" in str(e).lower():
            print(f"  note: no '{tab}' tab in this spreadsheet - skipping")
            return []
        raise


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--event", required=True)
    ap.add_argument("--sheet-id", required=True)
    ap.add_argument("--tab", default="LA-Area")
    ap.add_argument("--dropped-tab", default="Not Included")
    ap.add_argument("--exclusions-sheet", default=None,
                    help="spreadsheet id to read the denylist from, when the target "
                         "sheet has no 'Not Included' tab (master workbooks often "
                         "don't). Without this, prior company/handle exclusions are "
                         "forgotten and come back as 'new guests'.")
    ap.add_argument("--city", default="Los Angeles")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    creds = Credentials.from_authorized_user_file(str(TOKEN))
    sh = build("sheets", "v4", credentials=creds)

    eid, guests = partiful_guests(a.event)
    print(f"event {eid}: {len(guests)} records returned")

    # ---- classify, reusing the same rules as the pipeline
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from partiful_pipeline import classify  # noqa: E402
    passing, dropped = classify(guests)
    print(f"  passing {len(passing)} | dropped {len(dropped)}")

    # ---- what the sheet already knows
    existing_rows = read_tab(sh, a.sheet_id, a.tab)
    existing_names = [r[0] for r in existing_rows[1:] if r and r[0]]
    dropped_rows = read_tab(sh, a.sheet_id, a.dropped_tab)
    if a.exclusions_sheet:
        extra = read_tab(sh, a.exclusions_sheet, a.dropped_tab)
        if extra:
            print(f"  borrowed {max(len(extra)-1,0)} exclusions from "
                  f"{a.exclusions_sheet[:12]}...")
            dropped_rows = dropped_rows + extra[1:]
    prior_exclusions = [r[0] for r in dropped_rows[1:] if r and r[0]]
    print(f"  sheet has {len(existing_names)} rows in '{a.tab}', "
          f"{len(prior_exclusions)} prior exclusions")

    # ---- decide what is genuinely new
    # Order matters: check the sheet's existing rows FIRST. A prior exclusion is
    # often a bare first name ("Winnie", "Kat"), and the prefix rule would
    # otherwise match it against a real full-name row ("Winnie Kemp") and
    # misreport that person as excluded.
    new_names, already, excluded = [], [], []
    for p in passing:
        n = p["name"]
        m = find_match(n, existing_names)
        if m:
            already.append((n, m))
            continue
        # Only multi-token prior exclusions can block a name. A one-token entry
        # in Not Included is a first-name-only drop and must not veto a full name.
        blockers = [e for e in prior_exclusions if len(norm(e).split()) >= 2]
        if find_match(n, blockers):
            excluded.append(n)
            continue
        new_names.append(n)

    print(f"\n  already in the sheet : {len(already)}")
    for n, m in already:
        if norm(n) != norm(m):
            print(f"      matched '{n}' -> existing row '{m}'")
    print(f"  re-excluded (prior Not Included): {len(excluded)} {excluded}")
    print(f"  GENUINELY NEW        : {len(new_names)} {new_names}")

    # names in the sheet that this scrape did not surface (informational only)
    seen = existing_names + [n for n, _ in already]
    missing = [e for e in existing_names if not any(same_person(e, n["name"]) for n in passing)]
    if missing:
        print(f"\n  in the sheet but not in this scrape ({len(missing)}) - NOT removed:")
        for m in missing:
            why = next((d[1] for d in dropped if same_person(m, d[0])), None)
            print(f"      {m}" + (f"  (now dropped: {why})" if why else ""))

    if not new_names:
        print("\nNothing new to add.")
        return 0

    key = load_exa_key()
    print(f"\n  enriching {len(new_names)} new names...")
    t0 = time.time()
    store = enrich(key, new_names, a.workers, a.city)
    print(f"  done in {time.time()-t0:.0f}s")

    rows = build_rows(store)
    bad = [r["name"] for r in rows if r["url"] and
           not any(slug_from(h.get("url")) == r["url"].rsplit("/", 1)[-1]
                   for h in store.get(r["name"], []))]
    if bad:
        die(f"AUDIT FAILED: fabricated URL for {bad}")

    for r in rows:
        print(f"      + {r['name']:26} {str(r['url'])[:46]:46} {r['flag']:7} {r['conf']}")

    if a.dry_run:
        print("\nDRY RUN - nothing written.")
        return 0

    values = [[r["name"], r["url"] or "", r["loc"] or "", r["flag"], r["conf"], r["note"]]
              for r in sorted(rows, key=lambda r: (ORDER[r["flag"]], r["name"].lower()))]
    sh.spreadsheets().values().append(
        spreadsheetId=a.sheet_id, range=f"'{a.tab}'!A1",
        valueInputOption="RAW", insertDataOption="INSERT_ROWS",
        body={"values": values}).execute()
    print(f"\n  appended {len(values)} rows to '{a.tab}'")

    # record anything newly dropped that is not already listed
    new_drops = [[d[0], d[1]] for d in dropped
                 if not find_match(d[0], prior_exclusions)]
    if new_drops:
        try:
            sh.spreadsheets().values().append(
                spreadsheetId=a.sheet_id, range=f"'{a.dropped_tab}'!A1",
                valueInputOption="RAW", insertDataOption="INSERT_ROWS",
                body={"values": new_drops}).execute()
            print(f"  appended {len(new_drops)} rows to '{a.dropped_tab}'")
        except Exception as e:
            if "Unable to parse range" in str(e) or "not found" in str(e).lower():
                print(f"  note: no '{a.dropped_tab}' tab here - "
                      f"{len(new_drops)} drops not recorded")
            else:
                raise

    print(f"\n  https://docs.google.com/spreadsheets/d/{a.sheet_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
