"""Pull a Partiful event's guest list and filter it to first+last names.

Usage:
    python partiful_guests.py --check
    python partiful_guests.py --event https://partiful.com/e/<ID> --out guests.json
    python partiful_guests.py --event <ID> --out guests.json --statuses GOING,MAYBE

Reads auth from partiful-auth.json (see the skill's Auth section). Exits non-zero
on any failure so a cron job can alert on it.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36")
API = "https://api.partiful.com/"
DEFAULT_AUTH = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
                            "hermes", "partiful-auth.json")


def die(msg: str, code: int = 1):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def post(url: str, data: bytes, headers: dict) -> str:
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=45) as r:
        return r.read().decode()


def load_auth(path: str) -> dict:
    if not os.path.isfile(path):
        die(f"auth file not found: {path}\n"
            "Re-extract the Firebase token from Chrome's leveldb (see skill Auth section).")
    return json.load(open(path, encoding="utf-8"))


def refresh_access(cfg: dict, auth_path: str) -> str:
    """Mint a fresh access token from the long-lived refresh token."""
    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": cfg["refresh_token"],
    }).encode()
    try:
        out = json.loads(post(
            f"https://securetoken.googleapis.com/v1/token?key={cfg['firebase_api_key']}",
            body,
            {"Content-Type": "application/x-www-form-urlencoded",
             "Referer": "https://partiful.com/"},
        ))
    except Exception as e:
        die(f"token refresh failed ({e}). The refresh token was likely revoked - "
            "log into Partiful in Chrome and re-extract.")
    cfg["access_token"] = out["id_token"]
    cfg["user_id"] = out["user_id"]
    if out.get("refresh_token"):
        cfg["refresh_token"] = out["refresh_token"]
    json.dump(cfg, open(auth_path, "w", encoding="utf-8"))
    return cfg["access_token"]


def api(fn: str, params: dict, access: str, uid: str):
    payload = json.dumps({"data": {"params": params, "userId": uid}}).encode()
    body = post(API + fn, payload, {
        "Content-Type": "application/json",
        "Origin": "https://partiful.com",
        "Referer": "https://partiful.com/",
        "Authorization": "Bearer " + access,
        "User-Agent": UA,
    })
    return json.loads(body)["result"]["data"]


def event_id(s: str) -> str:
    m = re.search(r"/e/([A-Za-z0-9]+)", s)
    return m.group(1) if m else s.strip()


MASKED = re.compile(r"^[A-Za-z]\s*x{2,}")


def is_masked(n: str) -> bool:
    return bool(n) and bool(MASKED.match(n)) or bool(n and re.search(r"\bx{3,}\b", n))


TOKCH = re.compile(r"^[A-Za-z\u00c0-\u024f'\u2019.\-]+$")
# A single letter (with or without a trailing period) is an initial, not a
# surname: "Manuel U.", "Ari D", "Lisa G". TOKCH alone accepts these because it
# permits '.', so they must be tested for separately.
INITIAL = re.compile(r"^[A-Za-z]?\.?$")
COMPANY = re.compile(r"\b(inc|llc|ltd|corp|company|group|labs?|studio|ventures|capital|partners|naturals|team)\b", re.I)


def strip_handles(n: str) -> str:
    n = re.sub(r"@\S+", "", n)
    n = re.sub(r"https?://\S+", "", n)
    n = re.sub(r"[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F]", "", n)
    return re.sub(r"\s+", " ", n).strip(" ,;-|")


def classify(guests: list) -> tuple[list, list]:
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
        # A single letter is an initial. Only a *surname* initial disqualifies the
        # name ("Manuel U.", "Ari D", "Lisa G"); a middle initial is fine
        # ("Aaron C. Morgan", "Martin M Gaiss"). Require two non-initial tokens.
        real = [t for t in toks if not INITIAL.match(t)]
        if len(real) < 2:
            dropped.append([raw, "initial instead of a surname" if len(toks) > 1
                            else "first name only"]); continue
        if INITIAL.match(toks[-1]):
            dropped.append([raw, "initial instead of a surname"]); continue
        if len(toks) >= 2 and all(TOKCH.match(t) for t in toks):
            passing.append({"name": n, "status": g.get("status"),
                            "plusOneCount": g.get("plusOneCount", 0)})
        elif len(toks) == 1:
            dropped.append([raw, "first name only"])
        else:
            dropped.append([raw, "not a two-part name"])
    return passing, dropped


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--event", help="Partiful event URL or ID")
    ap.add_argument("--out", default="partiful-guests.json")
    ap.add_argument("--auth", default=DEFAULT_AUTH)
    ap.add_argument("--check", action="store_true", help="validate auth only")
    ap.add_argument("--statuses", default="",
                    help="comma list to keep, e.g. GOING,MAYBE (default: all)")
    a = ap.parse_args()

    cfg = load_auth(a.auth)
    access = refresh_access(cfg, a.auth)
    uid = cfg["user_id"]

    if a.check:
        print(f"AUTH OK  uid={uid}  access_len={len(access)}")
        return

    if not a.event:
        die("--event is required (or use --check)")

    eid = event_id(a.event)
    try:
        info = api("getEventInfo", {"eventId": eid}, access, uid)
    except Exception as e:
        die(f"getEventInfo failed for {eid}: {e}")
    ev = info.get("event", {})

    try:
        guests = api("getGuests", {"eventId": eid}, access, uid)
    except Exception as e:
        die(f"getGuests failed for {eid}: {e}")

    names = [(g.get("user") or {}).get("name") or g.get("name") for g in guests]
    masked = sum(1 for n in names if is_masked(n))
    if masked:
        die(f"{masked}/{len(names)} names are MASKED - auth is not effective. "
            "Re-extract the token (see skill Auth section).")

    if a.statuses:
        keep = {s.strip().upper() for s in a.statuses.split(",")}
        guests = [g for g in guests if (g.get("status") or "").upper() in keep]

    passing, dropped = classify(guests)
    result = {
        "event_id": eid,
        "title": ev.get("title"),
        "start_date": ev.get("startDate"),
        "location": ev.get("location"),
        "url": f"https://partiful.com/e/{eid}",
        "guests_returned": len(guests),
        "possible_cap": len(guests) == 500,
        "passed": len(passing),
        "dropped": len(dropped),
        "passing": passing,
        "dropped_list": dropped,
    }
    json.dump(result, open(a.out, "w", encoding="utf-8"), indent=1)
    print(f"{ev.get('title')}")
    print(f"  event:    {eid}")
    print(f"  returned: {len(guests)}" + ("  <-- exactly 500, likely a CAP" if len(guests) == 500 else ""))
    print(f"  passed:   {len(passing)}")
    print(f"  dropped:  {len(dropped)}")
    print(f"  wrote:    {a.out}")


if __name__ == "__main__":
    main()
