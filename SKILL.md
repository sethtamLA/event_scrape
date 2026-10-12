---
name: partiful-guest-list
description: "Use when pulling a Partiful event's guest list."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [partiful, guests, events, api, enrichment, contacts]
    category: research
    related_skills: [contact-list-enrichment, google-workspace, xlsx]
---

# Partiful Guest List

Pull a Partiful event's guest list as structured JSON instead of screenshotting
the guest-list UI. Feeds directly into `contact-list-enrichment` (LinkedIn
lookup + spreadsheet).

## When to Use

- "Here's the Partiful event — get the guest list."
- "Scrape the names from this Partiful event and enrich them."
- Any recurring "new event, same pipeline" job (cron-able).
- Not for: events on other platforms, or a guest list you already have as data.

## The API (this is the whole trick)

Partiful has no official API, but its web app calls Firebase Cloud Functions at
`https://api.partiful.com/`. Every endpoint is the same shape:

```
POST https://api.partiful.com/<functionName>
Content-Type: application/json
Origin: https://partiful.com
Referer: https://partiful.com/

{"data": {"params": {<endpoint-specific>}, "userId": "<firebase-uid>"}}
```

Response envelope: `{"result": {"data": ...}}`.

Guest list:

```bash
curl -s -X POST "https://api.partiful.com/getGuests" \
  -H "Content-Type: application/json" \
  -H "Origin: https://partiful.com" \
  -H "Referer: https://partiful.com/" \
  -d '{"data":{"params":{"eventId":"<EVENT_ID>"},"userId":""}}' \
  -o guests.json
```

The event ID is the last path segment of the event URL:
`https://partiful.com/e/<EVENT_ID>`.

Each guest record:

```json
{"id":"...","status":"GOING","count":1,"plusOneCount":0,
 "rsvpDate":"2026-10-10T19:18:40.119Z",
 "user":{"id":"...","name":"...","photo":null},
 "name":"...",
 "rsvpHistory":[{"status":"GOING","message":"...","date":"..."}]}
```

`getEventInfo` (`{"eventId":"..."}`) returns event metadata — title, dates,
location, `displaySettings.showGuestList`, `isPublic`, `visibility`.
Useful to confirm the event ID and whether the guest list is exposed at all.

## CRITICAL: names are masked without auth

**An unauthenticated `getGuests` returns HTTP 200 with the full record set but
MASKED names** — `"Txxx Rxxxxxxxxx"` (first letter kept, rest replaced with `x`).
Statuses, counts, RSVP dates and messages come back real; only `name` is
blanked. This looks like a successful scrape and is not.

Detect it: if the names look like a letter followed by `x`s, you are
unauthenticated. Do not build a sheet from masked names.

So authentication is required for real names, even on a `public` event with
`showGuestList: true`.

## Auth: the Firebase token

Partiful auth is Firebase (phone + SMS OTP). The tokens live in **IndexedDB**
(`firebaseLocalStorageDb` -> `firebaseLocalStorage`), NOT in cookies:

- `stsTokenManager.accessToken` — the JWT Bearer token (expires ~1 hour)
- `stsTokenManager.refreshToken` — long-lived; use it to mint new access tokens

Firebase project `getpartiful`; the web API key is public and embedded in the
app bundle (grep the public Next.js chunks for `AIza`).

Refresh (the `Referer` header is REQUIRED or Google blocks it):

```bash
curl -s -X POST "https://securetoken.googleapis.com/v1/token?key=<API_KEY>" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -H "Referer: https://partiful.com/" \
  -d "grant_type=refresh_token&refresh_token=<REFRESH_TOKEN>"
```

Returns `id_token` (new access token) and possibly a rotated `refresh_token`.

Then call the API with `Authorization: Bearer <accessToken>` and
`"userId": "<firebase-uid>"` in the envelope.

### Getting the token — read it out of Chrome's leveldb

The Hermes local browser runs a **copy** of the user's Chrome profile
(`browser.use_real_profile`). Do NOT rely on that copy for Partiful auth: the
Firebase token lives in **IndexedDB**, and in practice the copied leveldb comes
over EMPTY (Chrome recreates a fresh one on launch) — the re-sync only covers
Cookies / Login Data / Web Data / Preferences. Verified the hard way.

Read the token straight out of the real profile's leveldb instead.

**1. Find the right profile.** Chrome may have several. The one with Partiful
data has `https_partiful.com_0.indexeddb.leveldb` under its `IndexedDB` dir:

```bash
REAL="$LOCALAPPDATA/Google/Chrome/User Data"
for d in "$REAL"/Default "$REAL"/Profile*; do
  echo "$(basename "$d"): $(ls "$d/IndexedDB" 2>/dev/null | grep -i partiful)"
done
```

Note `browser.real_profile_pin` exists to force which profile Hermes copies,
but for token extraction you read the profile directly and skip that entirely.

**2. Extract the token.** Chrome's IndexedDB leveldb stores strings as
`"` + length-byte + key + `"` + length-byte + value — there is NO JSON colon,
so a `"key":"value"` regex finds nothing. Anchor on `key"` and take the first
run of token characters after it:

```python
import re, os, glob, json
base = os.path.join(os.environ['LOCALAPPDATA'], 'Google','Chrome','User Data','Profile 71',
                    'IndexedDB','https_partiful.com_0.indexeddb.leveldb')
s = b''.join(open(f,'rb').read() for f in glob.glob(os.path.join(base,'*')) if os.path.isfile(f)).decode('utf-8','ignore')
TOK = set('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-.')
def first_run_after(key):
    i = s.find(key + '"')
    if i < 0: return None
    j = i + len(key) + 1
    while j < len(s) and s[j] not in TOK: j += 1
    k = j
    while k < len(s) and s[k] in TOK: k += 1
    return s[j:k]
refresh = first_run_after('refreshToken')   # ~183 chars, starts 'AMf-v'
access  = first_run_after('accessToken')    # JWT, exactly 2 dots
uid     = first_run_after('uid')            # ~28 chars
apikey  = re.search(r'(AIza[A-Za-z0-9_\-]{30,})', s).group(1)
```

Validate before trusting: refresh ~183 chars, access is a JWT with 2 dots,
uid 20-40 chars. If a run comes back ~900 chars you grabbed the JWT instead —
anchor and window are wrong.

**3. Store and reuse.** Save to a 0600 file (e.g.
`<hermes-home>/partiful-auth.json`) and refresh the access token on each run.
The refresh token is long-lived, so after this one-time extraction future runs
need no browser at all.

## Procedure

### Updating an event that is already in a Sheet (the common case)

Once an event has a Sheet, later runs should add only the new arrivals — never
rebuild it. `scripts/partiful_incremental.py` does that:

```bash
"$VENV" <skill>/scripts/partiful_incremental.py \
    --event https://partiful.com/e/<ID> --sheet-id <SPREADSHEET_ID> [--dry-run]
```

It scrapes, reads the target sheet, appends only genuinely-new names, and never
removes or reorders existing rows. Always `--dry-run` first.

A naive name-diff gets this wrong in three ways, all handled:

1. **Spelling drift re-adds people.** `daniela nevarez` vs `Daniela Nevaez`,
   `Johnny Tran` vs `Jonny Tran` are one person each. Whole-string similarity is
   checked *before* any first-name gate, because first names drift too — a
   strict first-name rule rejects `Johnny`/`Jonny`. The first-name gate still
   runs afterwards to keep `Julia Roga` and `Julia Price` apart.
2. **Prior exclusions get forgotten.** A name the earlier run rejected as a
   company or handle (`Momentum Impact`, `GODMorphea Darklight`) still passes the
   raw token filter, so it returns as a "new guest". Read the existing
   `Not Included` tab back and treat it as a carry-forward denylist.
3. **Display names change.** `Julia Price` became `Julia Price @ Millbrook` and
   is dropped by the filter; `Helen Quon` became `Helen Q`. Neither is a
   departure, so nothing may be deleted on the strength of a missing name.

Two ordering rules that are easy to get backwards:

- **Match against the sheet's existing rows BEFORE the exclusion list.** Prior
  exclusions are often bare first names (`Winnie`, `Kat`), and a prefix rule
  will match `Winnie` against the real row `Winnie Kemp` and misreport her as
  excluded.
- **Only multi-token exclusions may block a name.** A one-token `Not Included`
  entry is a first-name-only drop and must not veto a full name.

Re-running is idempotent: a second pass reports 0 new and writes nothing.

**Master workbooks have no `Not Included` tab.** When the target is an
aggregation sheet (one tab per event) rather than a per-event sheet, the
denylist is not there — and with an empty denylist the prior company/handle
exclusions come straight back as "new guests". Observed: `Momentum Impact`
matched to `linkedin.com/in/chikos` and `GODMorphea Darklight` to
`linkedin.com/in/morphea`, both wrong. Point the run at the sibling sheet that
does hold the denylist:

```bash
"$VENV" <skill>/scripts/partiful_incremental.py --event <URL> \
    --sheet-id <MASTER_ID> --tab "<Event Tab>" \
    --exclusions-sheet <PER_EVENT_SHEET_ID>
```

**A near-miss match is not caught by the URL audit.** The audit proves a URL
came from a real `/in/` hit; it cannot tell that the profile is a *different
person*. `Seth Tam` matched `linkedin.com/in/seth-tams` — one character off, a
real profile, and the wrong human. When a name has no profile in the earlier
run, prefer carrying the earlier row across verbatim over re-enriching it, and
never treat a one-character surname difference as confirmation.

### New event, one command

**Preferred: one command, end to end.** `scripts/partiful_pipeline.py` does the
whole job with no agent round-trips — it calls the Exa API directly in parallel
instead of driving `web_search` one name at a time:

```bash
S="$LOCALAPPDATA/hermes/skills/research/partiful-guest-list/scripts/partiful_pipeline.py"
python "$S" --event https://partiful.com/e/<ID> \
    --out "$HOME/Desktop/<Event> Guest List - LinkedIn.xlsx" \
    --raw-out "$HOME/raw_<ID>.json"
```

It refreshes the token, fetches `getEventInfo` + `getGuests`, hard-fails on
masked names, filters to first+last, enriches via Exa (default 8 workers), builds
the workbook, and **audits that every URL cell is backed by a literal `/in/`
hit** before writing. Measured: ~9s for 82 names, ~41s for 190, ~102s for 459 —
where the manual path took ~20 minutes at 459.

Flags: `--union-raw <prev-raw.json>` (merge an earlier pull to get past the 500
cap), `--statuses GOING,MAYBE`, `--workers N`, `--city "<City>"`, `--no-enrich`,
`--upload` (create a standalone Google Sheet and print its URL; add
`--sheet-title` to name it), and `--append-master` (add a tab to the
Oxen-Forecast master sheet with header styling, frozen row, and filters).
`--upload` and `--append-master` can be used together.

Two guards worth knowing:

- **The raw pull is always saved** (`<hermes>/partiful-raw-<eventId>.json`, or
  `--raw-out`). It is the only stable record of guest ids, and a union across
  pulls is impossible without it — so there is no way to end up with none.
- **`--statuses` aborts rather than emptying the list.** Filtering to a status
  the event does not use (`--statuses GOING` on an approval-gated event where
  every guest is `APPROVED`) used to write an empty workbook and look like a
  quiet success. It now fails and prints the statuses that are actually present.

openpyxl is imported lazily inside the workbook functions, so the scrape half
can be imported by another script on an interpreter without openpyxl.

**Manual path**, if the pipeline breaks — `partiful_guests.py` for the scrape,
then `contact-list-enrichment`'s `build_guest_workbook.py` for the workbook:

```bash
S="$LOCALAPPDATA/hermes/skills/research/partiful-guest-list/scripts/partiful_guests.py"
python "$S" --check                                        # is auth still good?
python "$S" --event https://partiful.com/e/<ID> --out guests.json
```

It refreshes the token, calls `getEventInfo` + `getGuests`, hard-fails on
masked names, filters to first+last, and writes `{title, start_date, location,
passed, dropped, passing[], dropped_list[]}`. It prints a CAP warning when the
count is exactly 500. Exit code is non-zero on any failure, so a cron job can
alert on it.

**Keep the raw `getGuests` response, not just the script's output.** The script
writes only the filtered names, so a later union across pulls is impossible
without the raw records — save `{"result": {"data": [...]}}` alongside it.
The union is keyed on the guest `id`, which is the only stable identifier.

**When the script reports exactly 500 records, pull again later and union.** A
single pull is a shifting sample of a larger list (see Pitfalls), so the
deliverable should be the union of every pull you have, deduped by guest `id`
and then by normalized name. Report the union size and the per-pull counts
rather than presenting one pull as "the guest list".

Then:

5. Hand `passing[]` to `contact-list-enrichment` for the LinkedIn pass and
   the workbook build (its first+last filter, metro filter, confidence tiers,
   and workbook shape all apply unchanged). On the manual path the enrichment is
   the long pole — a 500-guest event yields ~400 names and ~15-20 minutes of
   lookups, so state that estimate before starting. **`partiful_pipeline.py`
   does the same work in ~100 seconds**, so prefer it and don't quote the
   20-minute figure for it.
6. Deliver the workbook; offer the Google Sheet conversion. If the user then
   asks for a write-up of the work, use `status-reports`.

Manual fallback (if the script breaks): `getEventInfo` to confirm the event,
`getGuests` with auth, check names are NOT masked, then flatten preferring
`user.name` over `name`.

## Pitfalls

- **Masked names are the #1 trap.** A 200 with 500 records looks like success.
  Always assert the names are not `x`-masked before proceeding.
- **`classify_loc` must check LA_CORE before NON_LA.** A LinkedIn profile that
  mentions both "Los Angeles" and "Irvine" must return Yes (LA-area), not No.
  Checking NON_LA first inverts the result for any profile spanning both regions.
  The correct order: LA_CORE → Yes, LA_ADJACENT → Adjacent, NON_LA → No, then
  fallback to No (with a `california` substring escape to Unknown).
- **`norm()` must preserve hyphens, not split them.** Replacing `-` with a space
  turns `Jean-Luc` into `jean luc` — two tokens that won't match
  `jean-luc` in a LinkedIn profile. Hyphens are part of the name, not word
  separators. The function removes `.` `'` `'` and collapses whitespace, but
  hyphens stay.
- **`classify_names` must reject more than single-token names.** A bare
  `len(name.split()) >= 2` check lets through company names (`Momentum Impact`,
  `Acme Labs`), couples (`Julia & Mark`), handles (`@seth_tam`), and
  initial-surnames (`Manuel U.`). The real filter strips handles/URLs/emoji,
  rejects company keywords (`Inc`, `LLC`, `Studio`, `Ventures`, etc.), rejects
  `&`/`and` patterns, and requires at least two non-initial tokens with the
  surname token not being an initial.
- **Do not screenshot the guest list.** The user's manual workflow (Windows
  Snipping Tool, ~10-13 JPEGs per event, manually numbered
  `EventName_Date_N.jpg`) exists only because the UI was the only route. The
  API returns the same data as JSON, complete and in order. Screenshots are
  strictly worse: OCR errors, overlapping captures, manual naming.
- **The `/guests` URL path 404s.** Don't guess UI routes; use the API.
- **500-record cap — confirmed, and the 500 are NOT stable.** `getGuests` returns
exactly 500 records on a large event and ignores every pagination parameter:
`limit`, `skip`, `offset`, `page`, `startAfter`, `lastId`, `cursor`, `count`,
`pageSize` all returned the identical 500, and `getGuestList` / `getEventGuests`
/ `getAllGuests` are 404. Within one session the 500 are stable; **between pulls
they are not** — two pulls of the same event a day apart shared 421 records and
differed by 79 in each direction, for a union of 579. So the 500 is a shifting
sample of a larger list, not a prefix of it. Consequence: a single pull always
under-counts, and **unioning pulls taken at different times is the only way to
recover more of the list**. Do that when the count is exactly 500, and report
the union size rather than presenting one pull as the guest list.
- **The profile copy does NOT carry the Firebase token.** IndexedDB comes over
  empty; don't waste a cycle on `browser.use_real_profile` for Partiful. Read
  the leveldb directly (see Auth).
- **Chrome may have multiple profiles.** The one with Partiful data is not
  necessarily `Default` — check each profile's `IndexedDB` dir for
  `https_partiful.com_0.indexeddb.leveldb` before extracting.
- **`getGuests` includes declined/waitlist guests.** Filter by `status` if the
  user wants attendees only.
- **Statuses are not always GOING.** Approval-gated events return every guest
  as `APPROVED`, so a `--statuses GOING,MAYBE` filter silently drops the entire
  list. Read the raw status values before filtering on them, and record the
  status column in the deliverable rather than assuming it is uniform.
- **Refresh tokens can be revoked** (user logs out everywhere). When calls
  start 401ing, re-harvest the token.

## Verification

- Assert no returned name matches the masked pattern.
- Assert the record count is plausible against `getEventInfo` guest counts.
- After building the sheet, re-open it and check row counts and that every
  URL cell contains `linkedin.com/in/`.
