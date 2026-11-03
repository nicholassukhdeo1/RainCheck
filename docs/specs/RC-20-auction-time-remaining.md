# RC-20: Auction time remaining in emails

**Status:** Approved
**Backlog item:** RC-20 in `docs/BACKLOG.md`

## Summary

Show a static days, hours, and minutes countdown beside the scheduled end of each auction in the daily digest and both one-time preview modes.
Calculate every countdown from one timestamp captured when the email is created.

## Motivation

`src/digest.py:108-116` formats an auction's scheduled end in Japan time, and `src/digest.py:140-154` and `src/digest.py:260-274` place it in HTML and text.
The recipient must calculate the remaining time, which is especially inconvenient for a soon-ending auction.
`src/yahoo_scrape.py:551-555` builds both normal and preview digests but currently passes a date without a shared creation timestamp.

Yahoo's [auction end-time help](https://support.yahoo-net.jp/PccAuctions/s/article/H000014249) describes the seller's scheduled end time.
Yahoo's [automatic-extension help](https://support.yahoo-net.jp/SccAuctions/s/article/H000008832) says a qualifying bid in the last five minutes can extend the end repeatedly and that the displayed remaining time may briefly lag an extension.
The email must therefore describe a snapshot of the scheduled end, not promise a closing deadline.
Yahoo does not publish a contract for the embedded `__NEXT_DATA__.endTime` field, so the existing saved-fixture parser and its conservative handling of invalid values remain authoritative for this feature.

## Scope

**In scope:**

- Show time remaining for `auction_watch` and `auction_buy_now` rows in the HTML and plain-text bodies of normal, printed preview, and sent preview digests.
- Keep the existing scheduled end line directly next to the new countdown line.
- Capture one timezone-aware UTC creation timestamp per digest and use it for every auction row and the digest's existing local run date.
- Label the countdown as measured at email creation and keep the scheduled-end warning that Yahoo may change it.
- Show clear unavailable and reached-or-passed states without claiming that an auction actually closed.
- Preserve sale classification, selection, ordering, image requirements, email delivery, and post-send recording.

**Out of scope:**

- A live countdown that changes after the email is sent.
- Refetching an auction to detect extensions, sending reminder emails, or verifying an auction's actual outcome.
- Adding a countdown to fixed-price or flea-market rows.
- Changing Yahoo parsing, eBay prices, matching, listing history, or SMTP behavior.
- Displaying seconds or promising an exact bidding deadline.

## Design

### Files touched

| File | Change |
| --- | --- |
| `src/digest.py` | Add a pure countdown formatter, accept a supplied creation timestamp for auction digests, and render the same countdown in HTML and text. |
| `src/yahoo_scrape.py` | Capture one UTC timestamp just before building the normal or preview digest, derive the existing local subject date from that timestamp, and pass the timestamp to the builder. |
| `tests/test_digest.py` | Cover arithmetic, formatting, missing and elapsed ends, mixed sections, HTML/text parity, and unchanged fixed-price rendering. |
| `tests/test_rc16_auctions.py` | Update direct auction digest callers to supply a fixed creation timestamp and cover buy-now and watch rows. |
| `tests/test_yahoo_scrape.py` | Cover one shared timestamp across multiple rows and both preview paths with a frozen clock and isolated fixtures. |
| `docs/specs/RC-20-auction-time-remaining.md` | Record the approved wording and any resolved product decision. |

### Schema changes and migration

None.
Existing `listings.scheduled_end_at` already stores Yahoo's parsed UTC time, and this feature computes display text without writing data.
No database migration or real-database backup is required for this feature.

### Interfaces

Extend `build_digest(sections, run_date, *, inline_images=None, preview=False, created_at=None)` with a keyword-only `created_at` timezone-aware `datetime`.
For any shown auction row, `created_at` must be supplied and timezone-aware; otherwise the builder raises `ValueError` rather than reading the clock or silently calculating from `run_date`.
For fixed-price-only digests, the optional argument may remain absent so existing direct callers keep working.
Normalize the supplied instant to UTC before subtraction; an offset-aware non-UTC instant represents the same instant and is accepted.
`build_digest` remains pure and returns the existing `Digest | None` without any new network or database access.

The countdown formatter takes `scheduled_end_at` and the supplied creation instant.
For a valid aware end strictly after creation, calculate the positive duration, truncate fractional seconds, and decompose whole minutes as `days = minutes // 1440`, `hours = (minutes % 1440) // 60`, and `minutes = minutes % 60`.
Render all three units in a compact, stable form, for example `Time remaining at email creation: 2d 3h 4m`.
For a positive interval under one minute, render `Time remaining at email creation: less than 1m` rather than `0d 0h 0m`.
For an end equal to or before creation, render `Scheduled end reached or passed at email creation; check listing` and retain the absolute scheduled end line.
For `None`, naive, malformed, or unrepresentable end values, render `Time remaining unavailable` and retain `Scheduled end unavailable`.
Do not infer actual completion from an elapsed scheduled time.

In `yahoo_scrape.main`, capture `created_at = datetime.now(timezone.utc)` once immediately before `build_digest`.
Pass `created_at.astimezone().date()` as `run_date` to preserve the current local-date subject convention and `created_at=created_at` for countdowns.
This applies equally to normal runs, `--preview`, and `--send-preview`.
An earlier timestamp used for `last_seen_at` or the exchange rate is not reused because image work can take time.

## Acceptance criteria

1. An auction ending exactly 2 days, 3 hours, 4 minutes, and 59 seconds after the supplied creation instant shows `2d 3h 4m` in both HTML and text, beside the unchanged JST scheduled end.
2. An auction ending 59 seconds after creation shows `less than 1m`, while one ending exactly 60 seconds after creation shows `0d 0h 1m`.
3. The minute calculation uses real instants across UTC/JST offsets and calendar boundaries, including 23 hours 59 minutes, 24 hours, and 25 hours.
4. A future `auction_watch` and a future `auction_buy_now` each show one countdown in the listing cell and one matching countdown in the text alternative; a `fixed_price` row shows none.
5. Two auction rows with the same end time in one digest show identical countdowns because the builder uses one supplied creation instant.
6. An end equal to or before creation shows the reached-or-passed wording without saying the auction has closed or been sold; the existing absolute scheduled end remains visible.
7. A missing, naive, invalid, or out-of-range end shows `Time remaining unavailable` and `Scheduled end unavailable` in both bodies without crashing or changing row eligibility.
8. Building a shown auction digest without a timezone-aware `created_at` raises `ValueError`; a fixed-price-only digest retains its current behavior without `created_at`.
9. One normal run and both preview modes pass exactly one captured UTC instant to `build_digest`; the subject date is the local date of that same instant, even at a local midnight boundary.
10. The normal run still sends at most one digest and records exactly its displayed listings only after successful send; the preview modes remain read-only and preserve their current send or print behavior.
11. The HTML still has three columns, escapes scraped text, preserves the image and price layout, and remains readable at 375px and desktop widths with a long auction title and the new line.

## Edge cases

- Empty results: `build_digest` returns `None`; no countdown or email is created.
- `None` or missing values: a missing end gives the explicit unavailable line; a missing creation instant for a shown auction raises `ValueError`.
- Network failure or bad HTTP status: retain the existing Yahoo and image-fetch failure behavior; formatting adds no network call.
- Malformed input: naive and unrepresentable end datetimes give the unavailable line; an invalid supplied creation timestamp fails before rendering a shown auction.
- Duplicates: existing Yahoo URL deduplication and one-row-per-listing behavior stay unchanged; identical ends use the same reference instant.
- Delayed delivery or Yahoo extension: the static countdown remains labeled as of email creation, and the existing `(may change)` scheduled-end warning remains.

## Test plan

Use constructed `Deal` and `ItemDeals` values with fixed UTC and offset-aware `datetime` values for exact pure formatter tests.
Use the existing saved Yahoo `brand_page_minimal.html` and `brand_page_live.html` fixtures to confirm parsed `endTime` reaches the digest; no new live Yahoo request or credentialed fixture capture is required.
Use the existing temporary seeded schema-v4 database, mocked HTTP responses, prepared-image stub, and fake email sender from `tests/test_yahoo_scrape.py` to verify normal and both preview paths without network, real database, or actual email.
Freeze the job clock near a local date boundary so tests can assert that the subject date and all row countdowns come from one capture.
Render a mixed fixed-price, buy-now, and watch digest at 375px and desktop width as required by `AGENTS.md` and inspect that the listing, photo, countdown, scheduled end, and price remain legible.

## Shared-file conflicts

This feature does not touch `src/schema.sql`, `src/db.py`, or `src/catalog.json`.
It does touch shared email and Yahoo job files, so implementation should not run in parallel with another change to `src/digest.py` or `src/yahoo_scrape.py`.

## Open questions

None remain after the user approved the recommendation below.

## Resolved

On 2026-10-31, the user approved a static time-remaining line for both auction watches and auctions with a buy-now option in daily and one-time preview emails.
If the scheduled end has already passed when the email is built, the listing remains visible with a warning rather than being hidden.
The countdown is explicitly measured at email creation because Yahoo may extend an auction and email delivery may be delayed.

## Rollback

Revert the digest formatter and its job call-site change in a code deployment.
No database restoration or migration rollback is needed.
