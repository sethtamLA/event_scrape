---
name: luma-guest-list
description: "Use when pulling a Luma event's guest list."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [luma, guests, events, api, enrichment, contacts]
    category: research
    related_skills: [partiful-guest-list, contact-list-enrichment, google-workspace]
---

# Luma Guest List

Pull a Luma (`luma.com`) event's guest list as structured data. Two paths, and
they are not interchangeable:

- **Official API** — documented, stable, paginated. Only works for events on a
  calendar you manage. **Prefer this.**
- **Internal endpoint** — undocumented, session-authenticated. The only option
  for a public event you do not host. See the honesty note below before relying
  on it.

Feeds into `contact-list-enrichment` for the LinkedIn pass and workbook build,
exactly like `partiful-guest-list`.

## When to Use

- "Get the guest list from this Luma event."
- "Build me a tool that scrapes Luma attendees."
- Any recurring "new event, same pipeline" job.
- Not for: events on other platforms, or a roster you already have as data.

## Path 1: the official API

Base URL `https://public-api.luma.com`, auth via an `x-luma-api-key` header.
Generate a key at `https://luma.com/settings/api` (or the calendar's settings).
Load it from `.env` as `LUMA_API_KEY` — never inline it.

```bash
# 1. list the events this calendar manages
curl -s -H "x-luma-api-key: $LUMA_API_KEY" \
  "https://public-api.luma.com/v1/calendars/events/list?pagination_limit=50"

# 2. page the guests for one event
curl -s -H "x-luma-api-key: $LUMA_API_KEY" \
  "https://public-api.luma.com/v1/events/guests/list?event_id=evt-XXXX&pagination_limit=100"
```

Both return `{entries, has_more, next_cursor}`. Loop on `pagination_cursor`
(feed it the previous response's `next_cursor`) until `has_more` is false.

Full parameter and field detail: `references/api-reference.md`.

**Pagination is cursor-based, not offset-based.** There is no `skip`/`offset`.
Stop on `has_more == false`, not on an empty page — and never assume a single
call returned everything. A silent truncation here looks exactly like a
complete list.

### Rate limits

200 requests/minute per calendar key, 500/minute per organization key. On
`429` you are blocked for a full minute — honor the `Retry-After` header rather
than retrying immediately, and pace off `X-RateLimit-Remaining`.

## Path 2: the internal endpoint (public events you don't host)

```
GET https://api.luma.com/event/get-guest-list
      ?event_api_id=<evt-…>&ticket_key=&pagination_cursor=&pagination_limit=100
```

**Verified end-to-end.** The endpoint, its parameters, and its response shape
have all been confirmed against live events:

```
GET https://api.luma.com/event/get-guest-list
      ?event_api_id=<evt-…>&ticket_key=<tk-value>&pagination_limit=100
```

Response: `{"has_more": bool, "next_cursor": str, "entries": [...]}`.

```json
{"api_id":"usr-…", "num_tickets_registered":1, "section_label":null,
 "user":{"api_id":"usr-…", "name":"Seth Tam", "first_name":"Seth",
         "last_name":"Tam", "username":"sethtam",
         "linkedin_handle":"/in/seth-tamrowski-7a9471", "bio_short":"…",
         "timezone":"America/Los_Angeles", "instagram_handle":null,
         "twitter_handle":null, "tiktok_handle":null, "website":null,
         "youtube_handle":"", "avatar_url":"…", "is_verified":false}}
```

**There is no `user_email` and no `approval_status` in this response.** Do not
build CSV columns that assume them; leave them blank and say so.
`linkedin_handle` IS present (~80% of entries), which is free enrichment input —
prefix it with `https://www.linkedin.com`.

### Auth: the share token works

**A `?tk=` share token DOES authenticate this endpoint.** Confirmed: `GET
/event/get-guest-list?event_api_id=evt-…&ticket_key=<tk>` returned 200 with
full names and LinkedIn handles for 92 guests. The token comes from the event
URL: `https://luma.com/<slug>?tk=<value>`. No session cookie needed.

This means you can scrape a Luma event with nothing but the share link — no
login, no API key, no browser. The `ticket_key` parameter is the `tk` query
param from the URL.

**Fallback order:** try `ticket_key` first (from the share URL). If the URL
has no `?tk=`, then fall back to a session cookie or API key. A 401 means none
of the auth methods worked.

### Extracting the event API ID

The event's `api_id` (`evt-…`) is in the page HTML — fetch the page and grep:

```bash
curl -s -A "$UA" "https://luma.com/<slug>" | grep -oE '"api_id":"evt-[A-Za-z0-9]+"' | sort -u
```

No browser needed. The event name is in `og:title`; strip a trailing ` · Luma`.

### Enrichment: Luma gives you LinkedIn handles for free

**Use `linkedin_handle` from the Luma response directly.** ~80% of guests have
one. For the remainder, enrich via Exa. This is a major efficiency win over
Partiful where every name needs an Exa lookup.

Workflow:
1. Parse `user.linkedin_handle` — if present, prefix with
   `https://www.linkedin.com` and use it directly (no Exa needed).
2. Collect names without a `linkedin_handle` into a separate list.
3. Enrich only that remainder list via Exa (same `enrich()` function from
   `partiful_pipeline.py`).
4. Merge results and build the workbook.

### Location: timezone field uses underscores, not spaces

`user.timezone` returns values like `America/Los_Angeles`, `Asia/Shanghai`,
`Europe/Helsinki`. Split on `/`, take the second part, and replace `_` with
spaces before feeding to `classify_loc`. A raw timezone string won't match any
city keyword because of the underscores.
- **A host who hides the list returns `403 {"message":"You don’t have access to
  see the guest list."}`**, with `guest_count: 0` and `show_guest_list: false`
  on the page. Handle it as "not available", not as an empty list, and still
  write a header-only CSV so the run does not look like a crash.
- **The event page is not a substitute.** The HTML embeds a `featured_guests`
  array of roughly ten people — a teaser, not the list — plus `guest_count` and
  `show_guest_list`. `show_guest_list: true` does not mean the full list is
  reachable without a session.
- **The event's `api_id` (`evt-…`) is in the page HTML**, so you can get it with
  a plain fetch — no browser needed for that part.
- **Pagination is cursor-based**: loop on `next_cursor` until `has_more` is
  false. A 93-guest event returned all 93 in one page at `pagination_limit=100`,
  matching the page's `guest_count` exactly — use that as the cross-check.

### Reading the event name off the page

`<title>` carries attributes (`<title data-next-head="">`), so a bare
`<title>(.*?)</title>` pattern silently matches nothing and you get an empty
event name. Prefer `og:title`, and strip a trailing ` · Luma`.

Use a Playwright persistent profile so the user logs into
`https://luma.com/signin` once and the session is reused on later runs. Add
polite delays and stay at or under one request per second.

### Finding an undocumented endpoint (the technique that worked)

Do not guess endpoint names. Luma is a Next.js app, so its client bundle names
every endpoint it calls:

```bash
# 1. fetch the event page and pull the api_id
curl -s -A "$UA" "https://luma.com/<slug>" -o event.html
grep -oE '"api_id":"evt-[A-Za-z0-9]+"' event.html | sort -u

# 2. download the JS chunks and grep for quoted API paths + the API host
grep -oE 'src="/_next/static/chunks/[^"]*\.js"' event.html | sed 's/src="//;s/"//' | sort -u > chunks.txt
while read -r c; do curl -s -A "$UA" "https://luma.com$c" -o "$(basename "$c")"; done < chunks.txt
grep -ohE '"/[a-zA-Z0-9/_.-]*guest[a-zA-Z0-9/_.-]*"' *.js | sort -u
grep -ohE 'https://[a-z0-9.-]+\.(com|io|dev|net)' *.js | sort | uniq -c | sort -rn
```

That is what surfaced `/event/get-guest-list` and the `api.luma.com` host, plus
the call site (`args: {event_api_id, ticket_key}`, `limit: 100`) and the
pagination parameter names. **Verify the endpoint with a live call before
trusting it** — a path string in a bundle proves the app calls it, not that it
still exists or what it returns.

This generalises: for any JS-rendered site, the bundle is the API documentation.
Grep it for quoted paths and hostnames rather than guessing routes.

## Pitfalls

- **Chrome locks its cookie database while running.** Reading the user's Luma
  session out of their Chrome profile fails while Chrome is open: `shutil.copy2`
  raises `PermissionError` and `esentutl /y` (the Windows locked-file copier)
  fails with `JET_errFileAccessDenied`. This also blocks launching a browser
  against the real profile.
- **Prefer a dedicated Playwright profile over closing the user's Chrome.** Run
  `login.py` once: it opens a headed Chromium on a separate persistent profile
  (`<project>/.browser-profile`), the user signs in, and the session is reused
  headlessly on every later run. This costs the user one login instead of
  closing their browser and losing their tabs, and it keeps the scraper out of
  their daily profile. Only fall back to "quit Chrome" if you specifically need
  cookies from their real profile.
- **Playwright's bundled Chromium may not be downloaded.** Check
  `%LOCALAPPDATA%\ms-playwright\chromium-*/chrome-win64/chrome.exe` and pass the
  newest one as `executable_path` rather than forcing a fresh
  `playwright install chromium` download.
- **Do not present the internal path as equivalent to the API.** It is
  undocumented, can break without notice, and expires with the session. Say
  which path a deliverable depends on.
- **A `show_guest_list: true` public event is still not scrapable anonymously.**
  Visibility in the UI is not the same as access to the endpoint.
- **Do not screenshot or scrape the rendered guest-list modal** when an
  endpoint exists. The API returns the same data complete, ordered and typed.
- **Rate-limit the fallback yourself.** The internal endpoint publishes no
  limits; one request per second with backoff on 429 is the safe default.

## Delivering the result

Write a CSV with `name`, `email` (when available), `approval_status`,
`event_name`, `source` (`api` | `web`). Deduplicate on normalized name, handle a
zero-guest event without crashing, and print a summary count.

When the user asks for a **tool** rather than a one-off pull, they want a
self-contained project folder — `scraper.py`, `requirements.txt`,
`.env.example`, `README.md` — with a CLI taking either an event id or a URL,
plus step-by-step setup and copy-paste commands at the end of the reply. This
user is on Windows 11 and does not consider himself a strong coder: give
PowerShell commands, not bash, and never assume he will hand-edit a script.
**Research the endpoint first and say what you verified** — he asks for
"research first, then build" and will notice if you guessed.

## Verification

- Assert the guest count is plausible against `guest_count` from the event page.
- Assert `has_more` was false on the last API page — a truncated pull is the
  failure mode that looks like success.
- Report which path produced the data and whether the session path was actually
  exercised end-to-end.
