# Luma API reference

Condensed from `https://docs.luma.com/llms.txt` (append `.md` to any docs URL for
its markdown form). Only what is needed to pull a guest list.

## Auth

`x-luma-api-key: <key>` header. Keys are scoped to a Luma Calendar; every event
and API key is managed by one. Organization keys cover all calendars in the org.

## `GET /v1/calendars/events/list`

Lists the events the calendar manages. Returns `{entries, has_more, next_cursor}`.

| Param | Notes |
|---|---|
| `before` / `after` | ISO 8601 datetime, filter on start |
| `pagination_cursor` | value of `next_cursor` from the previous response |
| `pagination_limit` | server enforces a maximum |
| `platforms` | repeated param: `luma`, `external`. Defaults to `luma` |
| `sort_column` | `start_at` |
| `sort_direction` | `asc`, `desc`, `asc nulls last`, `desc nulls last` |
| `status` | calendar submission status: `approved` (default), `pending` |
| `access` | repeated param: `manage` (default), `view` |

Each entry carries an `access` field. Default is `manage` — only events the
calendar manages. Pass `access=manage&access=view` to also get events listed on
the calendar but managed elsewhere; those come back `access: "view"` with
location obfuscated to city level and host-only fields (`meeting_url`,
`registration_questions`, `feedback_email`) omitted. Private events the calendar
does not manage are never returned.

## `GET /v1/events/guests/list`

| Param | Required | Notes |
|---|---|---|
| `event_id` | yes | usually `evt-…` |
| `approval_status` | no | `approved`, `session`, `pending_approval`, `invited`, `declined`, `waitlist` |
| `pagination_cursor` | no | value of `next_cursor` from the previous response |
| `pagination_limit` | no | server enforces a maximum |
| `sort_column` | no | `name`, `email`, `created_at`, `registered_at`, `checked_in_at` |
| `sort_direction` | no | `asc`, `desc`, `asc nulls last`, `desc nulls last` |

Response: `{entries: [Guest], has_more: boolean, next_cursor: string}`.

Includes guest summaries and `event_tickets`, but **not** order-level detail
(`event_ticket_orders`). Use `GET /v1/events/guests/get` for one guest's order
data.

### Guest fields

| Field | Type | Notes |
|---|---|---|
| `id` | string | guest id |
| `user_id` | string | |
| `user_email` | string | |
| `user_name` | string \| null | full name |
| `user_first_name` | string \| null | |
| `user_last_name` | string \| null | |
| `approval_status` | enum | `approved`, `session`, `pending_approval`, `invited`, `declined`, `waitlist` |
| `check_in_qr_code` | string | |
| `eth_address` | string \| null | `^0x[0-9a-fA-F]{40}$` |
| `invited_at` | datetime \| null | null if they registered on their own |
| `joined_at` | datetime \| null | virtual join link click; online events only |
| `phone_number` | string \| null | |
| `registered_at` | datetime \| null | |
| `registration_answers` | array \| null | `{label, question_id, value, question_type}` |

**Prefer `user_name`, falling back to `user_first_name` + `user_last_name`.**
`user_name` is nullable, so a parser that reads only that field silently drops
guests who never completed a Luma profile.

`approval_status` is NOT a going/not-going boolean: `approved` means "Going",
`session` is an online-session attendee, and `invited` guests have not
responded. Decide which statuses count as attendees before filtering, and record
the status in the output rather than collapsing it.

## Rate limits

Per-minute, shared across GET and POST:

- Calendar API keys / OAuth tokens: **200/min per calendar**
- Organization API keys: **500/min per organization**

Exceeding it returns `429` and blocks you for **1 minute**. Headers on
authenticated, rate-limited responses:

| Header | Meaning |
|---|---|
| `X-RateLimit-Limit` | max requests per minute |
| `X-RateLimit-Remaining` | remaining in the current window |
| `X-RateLimit-Reset` | window reset, or block expiry on a 429 |
| `Retry-After` | 429 only — seconds to wait |

Pace off `X-RateLimit-Remaining` and honor `Retry-After`. Retrying immediately
on a 429 extends the block.

## Other endpoints worth knowing

- `GET /v1/events/get` — event detail. For events you manage (`access: "manage"`)
it includes full detail, host contact info and **guest counts broken down by
status** — useful to cross-check a guest pull. For public events you do not
manage it returns less.
- `GET /v1/entities/lookup` — resolve a slug or Luma URL (`event/evt-…`,
`e/evt-…`, `calendar/cal-…`) to an entity. Query params and fragments ignored.
- `GET /v1/users/get-self` — confirm which key/account you are using.
