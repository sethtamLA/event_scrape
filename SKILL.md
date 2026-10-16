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

Detect it: if the names look like a letter followed by `x`s, you are not an
authorized viewer. Do not build a sheet from masked names.

**Masking is per-event authorization, not just auth state.** A valid token is
necessary but not sufficient: Partiful unmasks names only to guests and hosts
of *that* event. Observed on one token in the same minute — a GenJam event
returned real names while `AI, FILM & THE NEW ECONOMICS of CREATIVITY` returned
`Pxxxxxx Hxxxxxx` for all 89 records, because the user had merely browsed that
page and never RSVP'd. `getEventInfo` still reports `visibility: public` and
`showGuestList: true`, so those fields do not predict it.

Consequence: when the masked guard fires, the fix is for the user to RSVP to
that event (or be invited) — not to re-harvest the token. Check that first
before spending a cycle on Chrome's leveldb.

**RSVP is necessary but not sufficient on approval-gated events.** An RSVP
with status `PENDING_APPROVAL` keeps names masked until the host approves —
observed with 321/335 names masked while the user's own record sat
`PENDING_APPROVAL` beside 334 `APPROVED` guests. Diagnose by matching the
token uid against the guest records (`getGuests` includes your own row): if
your status is PENDING_APPROVAL, the fix is host approval or an invitation,
not a token re-harvest and not another re-run.

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
    --out "C:/Users/<user>/Desktop/<Event> Guest List - LinkedIn.xlsx" \
    --raw-out "C:/Users/<user>/raw_<ID>.json" \
    --append-master --strict-identity
```

`--append-master` adds a tab to the Oxen-Forecast Google Sheet (the user's
standard deliverable). `--strict-identity` blanks URLs where the LinkedIn
profile title has no surname evidence — safer than shipping mismatches.
Always use both for new events going into the master sheet.

On Windows, use `C:/Users/...` forward-slash paths — MSYS `$HOME` expands to
`/c/Users/...` which native Windows programs (openpyxl's ZipFile, Python's
`open()`) cannot open. The same applies to `--raw-out` and `--out`.

It refreshes the token, fetches `getEventInfo` + `getGuests`, hard-fails on
masked names, filters to first+last, enriches via Exa (default 8 workers), builds
the workbook, and runs **two audits before writing**:

- **provenance** — every URL cell is backed by a literal `/in/` hit (hard fail)
- **identity** — every matched profile's own *title* carries the person's name
  tokens; failures print and are downgraded to Low with a `NAME CHECK:` note

Provenance alone is not enough. On one 63-name event provenance passed 62/62
while identity flagged 9, of which 5 were a different human entirely
(`Adem Betta` -> `/in/allen-adem-5851708` "ALLEN ADEM", `alex vicars` ->
`/in/alan-prile-40787199` "Alan Prile", `Grace Tabib` -> `/in/joypereira`,
`Momentum Impact` -> `/in/chikos`, plus a couple the name filter missed). The
other flags were genuine variants (`Jen Essa` -> "Jennifer Essa", `Tatiana
Arefyeva` -> "Tatiana Arefieva") — surface those, do not drop them. Measured: ~9s for 82 names, ~41s for 190, ~102s for 459 —
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
The Hermes venv does not ship openpyxl; install it before the first pipeline
run: `uv pip install --python "$VENV_PY" openpyxl`.

**Embedding API (GUI / cron / tests):** the script is importable —
`partiful_pipeline.run_pipeline(args_namespace)` runs one event end to end,
raises `PipelineError` (never `sys.exit`) on failure, and returns a dict
{rows, dropped, ev, eid, store, raw_path, blanked, flagged, sheet_url,
master_url, target_url, out}. Live output goes to stdout AND the module-global
`LOG_FN`; enrichment ticks go to `PROGRESS(done, total)` — set both before
calling. `--append-sheet <sheet_id_or_url>` appends rows into a
user-specified Google Sheet as ONE consolidated tab (schema:
Event Name | Event URL | Guest Name | LinkedIn Profile | Company/Headline |
Timestamp; tab created on first use, created via `--append-tab`, default the
event title) — distinct from `--append-master` (Oxen-Forecast tab per event) —
and returns its `#gid` URL as `target_url`. **It dedupes**: rows already in the
tab for that event (keyed on event URL + normalized guest name) are skipped,
so re-runs are idempotent (like `partiful_incremental.py`); the same person at
a *different* event is still added. If reading the existing rows fails, it
appends everything rather than risk losing data.  `--append-master` is unchanged;
`--append-master-sheet <url-or-id>` makes `--append-master` target any sheet
instead of the Oxen-Forecast master (the GUI's per-event tab path).
The `enrich()` thread-pool bug (only the first name was enriched, early return
inside the loop) is fixed.

### The Streamlit batch GUI (where the embedding API is consumed)

The user drives batches from a local Streamlit app at
`C:\Users\Seth\partiful-gui` (run `run.bat`, or `streamlit run app.py`; its
venv already has streamlit + google libs + openpyxl — don't reinstall into the
system python). It pastes up to 10 event URLs, a target sheet (URL, ID, or
`PARTIFUL_SHEET_ID` env), hits Start, and the worker thread calls
`run_pipeline()` per event with `append_master=True, append_master_sheet=<id>`
→ **one tab per event** (Oxen-Forecast style: Name | LinkedIn Profile |
Location | LA Area? | Confidence | Notes). The user chose per-event tabs over
the consolidated single-tab schema — do not flip it back. Architecture rules:

- **`st.session_state` is thread-bound — never read it in a worker thread.**
  A background thread gets a DIFFERENT session proxy than the main script
  thread, so `st.session_state["job"]` inside the thread raises KeyError and
  the worker dies silently (the exception lands on thread stderr, the UI just
  never updates). Pass the mutable state object (a `JobState` dataclass) into
  the thread  as an argument; the main thread alone touches session_state.
- **A background thread mutating a plain object does NOT re-render Streamlit.**
  The log box stays frozen at the last script run, so a run that failed 5s in
  looks like a silent hang for the whole session — the recurring "I started
  it and nothing happened" report. Wrap the live view in `@st.fragment(run_every=2)`
  (Streamlit ≥1.32) so progress lines and `FAIL ...` lines actually appear;
  without it you cannot distinguish a dead worker from an enriching one.
- **Verify the app headlessly with `streamlit.testing.v1.AppTest`** — no
  browser needed: `at = AppTest.from_file("app.py")`, `at.run()`,
  `at.text_area[0].set_value(...)`, `at.button[0].click().run()`, then read
  `at.exception`, `at.markdown`, or `at.session_state`. This is the fastest
  way to reproduce what a user's click did and to prove the widget tree
  renders. It bypasses browser-profile locks entirely.
- **Normalize pasted URLs tolerantly.** A strict anchor regex rejects real
  pasted links — trailing spaces, share-link `?utm_...`/`#` fragments, missing
  `https://` — and the user sees only "fix these inputs". Trim, strip `<>`,
  cut at `#`/`?`, allow a bare `partiful.com/e/<id>`, and canonicalize to
  `https://partiful.com/e/<id>` before validating.
- **Throttle progress→log.** Wiring `PROGRESS` straight to the log floods it;
  log every ~5 ticks (`searched 5/92 names...`) so the console visibly moves
  without spam, and keep the progress bar ticking per name.
- **Monitoring a run by file mtimes: keep a seen-set.** A fixed `since`
  timestamp re-reports the same file every poll tick (used to spam 15
  identical NEW PULL/NEW XLSX lines for one run). Track reported paths in a
  set and only report paths not yet seen; report sheet growth once.
- **`partiful-auth.json` mtime is a false start signal.** Token refresh
  rewrites it as the FIRST pipeline step, so a fresh mtime proves only that the
  worker began — not that scraping happened. The raw pull
  `partiful-raw-<id>.json` is the first artifact that proves `getGuests` ran;
  a refreshed token with no fresh raw pull means the pipeline died between
  refresh and the API call, or the masked-name guard is about to fire. Check
  the raw pull before concluding a run is progressing.
- **Cleanup drill rows from a sheet by content match, not row numbers.** Row
  indices shift after appends, so fixed-index clears hit the wrong cells.
  Read the tab back, match the exact offending values, and `deleteDimension`
  (delete bottom-up so earlier indices stay valid).

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
- **The couple/duo filter must be case-insensitive.** `re.search(r"\s*(?:&|\band\b)\s*", n)`
  without `re.I` misses `Kenz And Chris` (capital A) and ships a two-person row
  as one lead. Fixed in `partiful_pipeline.py`; check any re-implementation.
- **The company denylist does not catch every brand.** `Momentum Impact` passes
  the keyword list (`inc|llc|labs|studio|ventures|...`) and matched a real
  person's profile. The identity audit is what surfaces it — another reason that
  check is mandatory rather than optional.
- **Surname evidence is the identity rule; the given name is not.** A title
  containing only the given name is a different person (`Adem Betta` vs `ALLEN
  ADEM`). Require the surname — exact, accent-folded, a hyphenated part, or a
  near-variant on tokens of 6+ characters — or the surname inside the profile
  slug. Two rules that look right and are not: fuzzy-matching short surnames
  re-admits `Seth Tam` -> `Seth Tams` (ratio 0.857), and title-only evidence
  wrongly blanks real matches whose profile title is abbreviated or accented
  (`Nour H.` for `/in/nourhassoun`, `radim vysoký` for `Radim Vysoky`).
  `--strict-identity` implements this; run it, and expect a handful of
  `NAME CHECK:` rows to survive for review (nicknames, hyphenated surnames).
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
  **Same-session re-pulls return 0 new names.** Two pulls within minutes share
  the exact same 500-record window. Wait hours (or overnight) before trying again
  so the sample rotates; don't re-run immediately and waste an enrichment cycle.
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
- **On Windows, file-path arguments must use `C:/Users/...` format, not MSYS
  Unix paths.** `$HOME/Desktop/file.xlsx` expands to `/c/Users/...` in MSYS
  bash, which native Windows programs (openpyxl, Python's stdlib) cannot open.
  `--out` and `--raw-out` both hit this — `FileNotFoundError` from `ZipFile`
  or `open()`. Use `C:/Users/<user>/Desktop/...` instead.

## Adding LinkedIn Bio Snippets (Post-Enrichment)

After a sheet is built or updated, the user may ask for a "Bio" column with a
snippet from each person's LinkedIn About section. Fetch these via Exa's
`/contents` endpoint — do NOT drive `web_extract` one URL at a time (165 URLs
× 5 per call = ~33 round-trips and minutes of overhead).

**0. Find the exact tab name.** The pipeline prints a gid URL but not the tab
name. The tab name can have subtle variations (double spaces, truncated
characters) that break `values().get()` with a 400. Use `spreadsheets().get()`
to list all tabs and grep for the event name before any post-enrichment work:

```python
sheet = service.spreadsheets().get(spreadsheetId=SHEET_ID).execute()
for s in sheet['sheets']:
    p = s['properties']
    if '<keyword from event title>' in p['title']:
        print(repr(p['title']))  # use repr() — reveals double spaces
```

**1. Count rows and read URLs.** Read column A to get the row count, then
columns A:F for the URL mapping:

```python
result = service.spreadsheets().values().get(
    spreadsheetId=SHEET_ID, range=f"'{TAB}'!A:A").execute()
total_data_rows = len(result.get('values', [])) - 1  # minus header

result = service.spreadsheets().values().get(
    spreadsheetId=SHEET_ID, range=f"'{TAB}'!A:F").execute()
rows = result.get('values', [])
url_map = {}
for i, row in enumerate(rows[1:], 0):
    if len(row) > 1 and row[1].startswith('https://linkedin.com/in/'):
        url_map[i] = row[1]
```

**2. Call Exa `/contents` in batches of 20.** Auth is `x-api-key` only (no
`Authorization` header — using both returns 402).

```python
body = json.dumps({"urls": batch, "text": {"max_characters": 800}}).encode()
req = urllib.request.Request("https://api.exa.ai/contents", data=body,
    headers={"Content-Type": "application/json", "x-api-key": exa_key})
```

**3. Extract the About section** from each result's text:

```python
about_match = re.search(r'## About\s*\n+(.*?)(?=\n##|\Z)', text, re.DOTALL)
if about_match:
    about = about_match.group(1).strip()
    if len(about) > 200:
        about = about[:197] + '...'
    bios[url] = about
```

**4. Write all bios in one `values.update` call.** Add a "Bio" header at row 1,
then write the full column from row 2 down in a single range — avoid individual
cell writes or batch HTTP requests; one call finishes it. Use the row count from
step 1 to build the full column array:

```python
service.spreadsheets().values().update(
    spreadsheetId=SHEET_ID, range=f"'{TAB}'!G1",
    valueInputOption="RAW", body={"values": [["Bio"]]}).execute()

bio_values = []
for data_row_idx in range(total_data_rows):
    url = url_map.get(data_row_idx)
    bio_values.append([bios.get(url, "") if url else ""])

service.spreadsheets().values().update(
    spreadsheetId=SHEET_ID, range=f"'{TAB}'!G2",
    valueInputOption="RAW", body={"values": bio_values}).execute()
```

Expect ~95% coverage (158/165 in practice); the rest are profiles Exa couldn't
reach or rows without a LinkedIn URL.

**5. Clean the bio text.** Exa returns full profile text including a trailing
"Total Experience: X years" line — strip it and any trailing newlines:

```python
bio = re.sub(r'\n+Total Experience:.*$', '', bio, flags=re.DOTALL).strip()
```

Also clean the Notes column — strip the "N candidates; " prefix the pipeline
leaves as provenance metadata:

```python
note = re.sub(r'^\d+ candidates?;\s*', '', note)
```

Write both cleaned columns back with `values.update` before formatting.

## Location Column Shortening (Post-Enrichment)

The pipeline outputs verbose location strings like "Los Angeles, California,
United States" and "Santa Monica, California, United States". Shorten them
before final review — the user wants "Los Angeles, CA", not the full form.

```python
def shorten_loc(loc):
    if not loc:
        return loc
    loc = re.sub(r',?\s*United States\s*$', '', loc)
    loc = re.sub(r',\s*California', ', CA', loc)
    loc = re.sub(r',\s*,', ',', loc)
    return loc.strip().rstrip(',')
```

Read the full sheet back, apply to column C, and `values.update` the whole
range back in one call. Run this after bios are added and cleaned, before
formatting.

## Sheet Formatting (Post-Enrichment)

After all data is in and cleaned, apply professional formatting to every event tab:

- **Header row:** dark blue (#1F385B) background, white bold 10pt text, frozen
- **Data rows:** 10pt font, top-aligned, wrap only on Notes and Bio columns
- **Column widths:** Name 160px, LinkedIn 280px, Location 200px, LA Area 70px,
  Confidence 85px, Notes 180px, Bio 350px
- **Borders:** subtle grey horizontal lines between rows (no vertical borders)
- **Filters:** on every column
- **No alternating row colors** — the user prefers clean, tight, minimal styling

Apply via `batchUpdate` with `repeatCell`, `updateDimensionProperties`,
`setBasicFilter`, and `updateBorders` requests. Use `spreadsheets().get()` to
enumerate all tabs and skip non-event tabs (Leads, Vendors, Communities, and
any tab whose first column header is not "Name").

Before formatting, delete any stale duplicate tabs (tabs ending in " (2)" etc.)
with `deleteSheet`.

## Verification

- Assert no returned name matches the masked pattern.
- Assert the record count is plausible against `getEventInfo` guest counts.
- After building the sheet, re-open it and check row counts and that every
  URL cell contains `linkedin.com/in/`.
