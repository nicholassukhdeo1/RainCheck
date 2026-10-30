# RC-19: Product images in daily and live-preview emails

**Status:** Approved
**Backlog item:** RC-19 is a new user request and is not yet listed in `docs/BACKLOG.md`.

## Summary

Embed a verified photo of each Yahoo listing inside both the regular daily digest and a one-time live preview email.
Listings without a retrievable listing-specific photo are withheld and remain eligible for a later run.

## Motivation

The daily digest currently links a 100 by 100 pixel remote Yahoo thumbnail (`src/digest.py:110-121`), but remote image display depends on the recipient's email client and Yahoo's continued hosting.
[Gmail Help](https://support.google.com/mail/answer/145919) says it can suppress external images.
The one-time live scrape sent on 2026-10-29 used the AWS SNS alert topic, which sends [plain-text notifications](https://docs.aws.amazon.com/sns/latest/dg/sns-user-notifications.html), so its recipient did not see product images.
The user wants a photo of the actual listing in both emails, without a generic product photo or a `No image` row.
The live job updates existing listings' `last_seen_at` and records emailed matches (`src/yahoo_scrape.py:430-485`), which makes it unsuitable as a repeatable preview against the production database.

## Scope

**In scope:**

- Download, validate, resize, and embed each shown listing's Yahoo photo as a CID image in the daily and preview HTML alternatives.
- For a missing or invalid search-result image, try a listing-specific photo from the corresponding Yahoo detail page when a verified parser can identify one.
- Defer a listing when its actual photo cannot be retrieved; do not email a generic substitute or mark the listing as emailed.
- Provide explicit preview-only and send-preview commands that reuse the same image-aware digest pipeline without writing listing or match history.
- Send a requested preview through RainCheck's SMTP sender as multipart HTML and text, not through the SNS alert topic.
- Clearly label previews and require an explicitly supplied nonproduction database path.

**Out of scope:**

- Changing matching rules, sizes, price calculations, the top-five limits, or live Yahoo page coverage.
- Changing the production daily schedule, AWS resources, alert topic, or database schema.
- Storing image bytes in SQLite or S3, retaining downloaded images between runs, or introducing a generic item-photo library.
- Computer-vision proof that a seller's photo depicts an authentic product.
- Guaranteed rendering in every email client, including clients that do not support inline CID images.
- Sending email automatically from a dry-run preview command.

## Design

### Files touched

| File | Change |
| --- | --- |
| `src/listing_images.py` (new) | Resolve listing-specific Yahoo photos, bound and validate downloads, reject placeholders, normalize thumbnails, and return in-memory image parts. |
| `src/yahoo_scrape.py` | Select only image-backed deals, add explicit preview command modes, and preserve normal send-then-record ordering while making previews read-only. |
| `src/digest.py` | Reference accepted images by CID, carry their MIME parts, and label previews while keeping text alternatives readable. |
| `src/sendemail.py` | Build a `multipart/alternative` message with a `multipart/related` HTML branch containing the inline photos. |
| `requirements.txt` | Add a pinned Pillow wheel for bounded image decoding and thumbnail encoding. |
| `Makefile` | Include the new image module in the explicit Lambda source copy. |
| `tests/test_listing_images.py` (new) | Cover URL, redirect, byte, time, MIME, decoding, image-source, and fallback rules with offline fixtures. |
| `tests/test_yahoo_scrape.py` | Cover daily selection, image failures, deferral, previews, and database state. |
| `tests/test_digest.py` | Cover CID image references, content IDs, preview labels, and text alternative. |
| `tests/test_sendemail.py` | Parse sent MIME to check CID related parts and failure handling. |

`src/lambda_handler.py` needs review because it invokes the daily job and runs inside the 512 MiB, 840-second Lambda limits, but no change to its production behavior is planned.
`src/db.py`, `src/schema.sql`, and `src/catalog.json` are read for this design but are not changed.

### Schema changes and migration

None.
Existing schema-v4 databases require no migration.
Neither preview mode opens, reads, writes, or migrates the default real database at `src/raincheck.db`.

### Interfaces

`yahoo_scrape.main()` remains the Lambda production entry point with its existing send-then-record behavior.
CLI parsing occurs only under `if __name__ == "__main__"`, so Lambda's no-argument call cannot interpret runtime arguments as preview flags.
`python3 src/yahoo_scrape.py --preview` fetches live Yahoo pages and the exchange rate, builds an image-backed digest, prints its subject, plain text, and HTML, and sends no email.
`python3 src/yahoo_scrape.py --send-preview` fetches the same live data and sends one image-bearing HTML and plain-text message through `sendemail.send_email`.
Both preview commands require `RAINCHECK_DB_PATH` to be set to an existing schema-v4 database outside `src/raincheck.db` before the first database or network operation.
An unset path, the default path (including a symlink resolving to it), or an invalid database produces a nonzero exit and a clear error without a send.
`--send-preview` fails before fetching or sending when `RAINCHECK_DRY_RUN` is enabled; a preview operator must explicitly remove dry-run mode to request a real email.
The recipient comes from the existing `RECEIVER_EMAIL` SMTP configuration, falling back to `SENDER_EMAIL` as today.
The command must never print sender credentials, email passwords, or secret values.

Extend the pure `build_digest(sections, run_date, *, inline_images, preview=False) -> Digest | None` so shown rows use a supplied mapping of listing URL to prepared image part.
The returned `Digest` carries the exact accepted in-memory image parts alongside subject, HTML, text, and shown deals.
No network or database access occurs in `digest.py`.
Existing direct callers without `inline_images` may keep the old remote-image rendering for compatibility, but both daily and preview job paths must pass prepared images and must not send a remote-image row.
With `preview=False`, subject and text content retain the normal deal semantics.
With `preview=True`, its subject starts with `RainCheck PREVIEW:` and its HTML and text contain a visible notice that this is a one-time live search, may include previously seen listings, and has not changed RainCheck listing history.
Both modes use the same CID image renderer for accepted rows.
Each HTML `<img>` has `src="cid:..."`, width and height of 100, escaped listing-title `alt`, and a link to the safe listing URL.
The HTML contains no external image source URL.

Extend `send_email(subject, html_body, text_body, *, inline_images=()) -> bool` without changing current three-argument callers.
For inline images, build `multipart/alternative` with `text/plain` first and `multipart/related` second; the related branch has the HTML root followed by one `Content-ID` image part for each displayed row.
Each part has its decoded `image/jpeg` or `image/png` MIME type, `Content-Disposition: inline`, and a unique CID referenced once in HTML.
The content IDs contain no listing URL, secret, or user address.
This MIME structure follows [RFC 2387](https://www.rfc-editor.org/rfc/rfc2387.html), [RFC 2392](https://www.rfc-editor.org/rfc/rfc2392.html), and the [Python email examples](https://docs.python.org/3/library/email.examples.html).
In dry-run mode, print only text and HTML markup, never raw image bytes or base64 data.

The preview path selects from all current matching Yahoo listings instead of filtering known URLs.
It does not call `update_last_seen_at`, `record_listing_matches`, or any other database writer, whether email succeeds, fails, or no deals exist.
It still reads active item configuration and effective prices from the caller's nonproduction database.
If there are no matching deals, it reports that fact and sends no email.
If any brand page or the exchange rate fails, it uses the job's established error or fallback policy and never sends a partial preview after a brand-page failure.

Yahoo `imageUrl` comes from the listing's results-page `__NEXT_DATA__` record (`src/yahoo_scrape.py:126-194`).
The captured 2026-10-26 Yahoo fixture has 100 result images, all on `auc-pctr.c.yimg.jp`, including flea-market listings.
Only an HTTPS URL with that exact host, no credentials, no unexpected port, no whitespace or control characters, and a listing-image path is accepted as a direct image source.
Remove Yahoo CDN `nf_*` fallback query parameters before download so the CDN cannot silently substitute its generic `na_170x170.png` image; reject known placeholder paths and generic Yahoo social-preview artwork.
Reject all redirects instead of following a response to an untrusted address.
Use TLS verification, a 3-second connect timeout, an 8-second total per-request deadline, streamed reads capped at 1 MiB per image, and an aggregate 300-second image phase deadline.
Give a run at most 60 image or detail-page requests; stop attempting more when the budget is exhausted and leave unselected listings eligible.
Accept only a successful HTTP response with JPEG, PNG, WebP, or GIF content type whose bytes Pillow can decode as one nonempty raster frame of at most 4 megapixels.
Reject HTML, SVG, zero-byte data, corrupt images, decompression bombs, and mismatched MIME signatures.
Resize without stretching into a 100 by 100 pixel box, normalize to JPEG or PNG, and require each encoded part to be at most 80 KiB and all included parts to be at most 8 MiB.
Close responses and decoded images on every path, and hold image bytes only in memory until send completion.

If the results-page image is missing, invalid, or unavailable, fetch only that listing's Yahoo detail URL, restricted to the exact expected auction or flea-market host and listing-ID path.
The detail response must be nonredirected HTTPS HTML, capped at 2 MiB and the same 8-second request deadline.
Accept its `og:image` only when a canonical or `og:url` value identifies the same listing URL and the image candidate passes the same listing-photo host, placeholder, and byte-validation rules.
Yahoo's [product-page help](https://support.yahoo-net.jp/PccAuctions/s/article/H000005375) confirms listing pages display product photos, but it does not promise `og:image` or another stable machine-readable field.
The verifier must capture and inspect current auction and flea-market detail fixtures before relying on this fallback; if their identity and photo fields cannot be verified, the parser returns no image and the listing is deferred.
The older Yahoo Auctions Web API is discontinued, so this design does not depend on an image API ([Yahoo developer notice](https://developer.yahoo.co.jp/changelog/auctions.html)).

Resolve photos before applying the separate per-item limits of five immediate deals and five auction watches, so an image failure can be replaced by the next ranked image-backed candidate.
Deduplicate image downloads for the same URL in one run.
Never substitute a photo from another item or a generic product image.
Only displayed, image-backed deals are passed to the post-send database writer.
If an image cannot be retrieved, skip that row, log its listing ID and a nonsecret reason, and leave it eligible for a later run; a failed image must not abort otherwise valid rows.
If no image-backed deals remain, send no email and record no matches.

## Acceptance criteria

1. A normal run with one matched listing and one valid Yahoo JPEG produces exactly one displayed row whose HTML has a 100 by 100 pixel linked `<img src="cid:...">`, and the MIME message contains exactly one matching inline `image/jpeg` part whose decoded bytes form the normalized thumbnail of that source photo.
2. The sent MIME root is `multipart/alternative`, with `text/plain` first and `multipart/related` second; the related HTML root is followed by its referenced image parts, with no orphan or duplicate CID.
3. The HTML contains no remote `<img src>` values, and CID values contain no listing URL, sender address, password, or other secret.
4. A result image URL with an unsafe scheme or host, credentials, unexpected port, whitespace, or a redirect is rejected without following the redirect or contacting the URL's destination.
5. A result image that returns HTTP 404 or 500, exceeds 1 MiB, exceeds eight seconds, has an unsupported MIME type, has corrupt bytes, or triggers a Pillow decompression warning is rejected without leaking a response or image handle.
6. A valid JPEG, PNG, WebP, or GIF is decoded, normalized into a raster thumbnail no larger than 100 by 100 pixels, and encoded as JPEG or PNG no larger than 80 KiB; a known Yahoo fallback or generic asset is rejected.
7. When a result image is missing or invalid, a detail page with a matching canonical or `og:url` listing identity and a valid listing-photo `og:image` supplies the photo; a mismatched identity, generic artwork, invalid photo, redirect, oversized response, or absent field does not.
8. A matched listing with no retrievable listing-specific photo is absent from both HTML and text email bodies, is absent from `record_listing_matches`, and remains eligible on the next normal run.
9. When the highest-ranked candidate lacks a photo but a lower-ranked candidate has one, the lower-ranked candidate fills the available per-item immediate-deal or auction-watch slot in the same ranking order.
10. If every candidate lacks a verified photo, the job reports no image-backed deals, sends no email, and records no listing matches.
11. After a successful daily email, exactly the displayed image-backed deals are recorded; an SMTP failure records none, preserving the existing send-then-record ordering.
12. Running `--preview` with a temp schema-v4 database and stubbed Yahoo and image responses prints a CID-based HTML digest and text alternative with a visible preview label and makes zero send calls.
13. Running `--send-preview` with the same temp database sends exactly one CID-based multipart message with a preview-labeled subject and a useful text alternative.
14. A listing already stored in the temp database can appear in either preview, while every database table remains unchanged after success, send failure, no deals, image failure, or Yahoo fetch failure.
15. Neither preview mode calls `get_known_listing_urls`, `update_last_seen_at`, `record_listing_matches`, or any other database writer.
16. An unset `RAINCHECK_DB_PATH`, a path resolving to `src/raincheck.db`, or an invalid schema-v4 path causes a nonzero exit before network access or email.
17. `--send-preview` with `RAINCHECK_DRY_RUN=1` causes a nonzero exit before network access or email; `--preview` never sends even when dry-run is unset.
18. A failed Yahoo brand fetch causes a nonzero exit and no partial email, even if earlier brands fetched successfully.
19. Normal scheduled execution without preview flags retains its existing known-listing filtering, effective-price selection, and per-item ordering.
20. Across 60 image and detail requests or 300 seconds of image processing, the run stops further image fetches, sends at most the already-prepared image-backed rows, and leaves unselected rows eligible; total encoded image parts remain at most 8 MiB.
21. No preview, dry-run output, error log, or MIME body contains a sender password, API token, or database backup's contents; normal `From` and `To` email headers remain present.
22. A SAM build includes the pinned Pillow wheel and `src/listing_images.py`, with no database, `.env`, fixture, or credential file in the artifact.

## Edge cases

- Empty results: report no deals and send nothing.
- `None` or missing values: try the listing detail page for a missing result image; if no verified photo is available, defer the listing.
- Network failure or bad HTTP status: preserve the existing whole-run Yahoo brand failure behavior and exchange-rate fallback; treat one photo fetch failure as one deferred listing.
- Malformed input: reject a changed `__NEXT_DATA__` layout, unsafe image URLs, deceptive detail-page identity, malicious HTML values, corrupt image bytes, and decompression bombs.
- Duplicates: deduplicate Yahoo URLs and image downloads within one run; a stored URL may reappear in a labeled preview without changing database rows.
- Email delivery failure: report failure and record no displayed matches; the preview database remains unchanged.
- CID image not displayed by an email client: the alt text, listing title, price, and plain-text alternative stay useful, but the product photo cannot be guaranteed in that client.
- Image budget exhausted: defer remaining candidates and log only counts and nonsecret error types.

## Test plan

Use the saved Yahoo results page in `tests/fixtures/yahoo/brand_page_live.html`, malformed and empty Yahoo fixtures, catalog seed data, and a throwaway schema-v4 database from `tests/conftest.py`.
Add small saved auction and flea-market detail-page fixtures with matching and mismatching listing identity, `og:image`, and generic Yahoo artwork.
Add byte fixtures for valid JPEG, PNG, WebP, and GIF images, a known placeholder, truncated image, HTML disguised as JPEG, and a decompression-bomb header.
Stub Yahoo, exchange-rate, image, detail-page, clock, and SMTP responses; tests never make a network call or send email.
Parse the captured MIME message to check both alternatives, related HTML, CID matching, image content type, decoded bytes, size limits, and no remote image sources.
Snapshot all database table rows before and after both preview modes, including a previously stored listing, to prove read-only behavior across success and failure paths.
Test the normal job's image filter, lower-ranked replacement, send-then-record transaction, and no-photo deferral.
The current RC-15 direct-renderer tests assert remote `<img src>` markup; amend them only where the image-aware job now intentionally emits CID references, while keeping direct-renderer compatibility covered.
For verification, capture a fresh Yahoo auction and flea-market detail page and validate that the fallback parser is tied to that listing's photo, then render a MIME message at desktop and 375-pixel phone widths to inspect image display and legibility.
Use an explicitly named throwaway database and a recipient controlled by the user for any authorized live send-preview check.

## Shared-file conflicts

No changes to `src/schema.sql`, `src/db.py`, or `src/catalog.json` are planned.
`src/yahoo_scrape.py` and `src/digest.py` are also used by daily production runs, so review their diff for unintended behavior changes before deployment.

## Open questions

None remain after the user approved the recommendations below.

## Resolved

On 2026-10-29, the user approved embedded listing photos in both daily and one-time preview emails.
The user rejected remote-only thumbnails and `No image` rows.
RainCheck will defer a listing without a verified listing-specific photo, including a time-sensitive auction, and leave it eligible for a later run.
The implementation will try a conservative detail-page fallback only when current Yahoo markup reliably ties the photo to that exact listing; otherwise it will defer the row.

## Amendments

On 2026-10-29, the resolver safety policy was clarified.
A missing result image, HTTP error, or corrupt image may trigger the verified detail-page fallback.
An unsafe result-image URL, redirect response, or transport timeout is terminal for that candidate and triggers no further request for that listing in the run.
This bounds exposure to untrusted destinations and avoids extra requests after a timeout.

## Rollback

Restore the prior remote-image renderer and SMTP construction from the previous release, remove the image resolver and preview command modes, and redeploy the previous code package.
The database needs no rollback because the schema is unchanged, and photos are never persisted.
Listings withheld for lack of a photo were not marked emailed and remain eligible after rollback.
