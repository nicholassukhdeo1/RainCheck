# RC-04: Record which item each listing matched

**Status:** Approved
**Backlog item:** RC-04 in `docs/BACKLOG.md`

## Summary

Use the existing `listing_matches` table to associate a Yahoo listing with the tracked item it matched.
The user must decide whether to record every keyword match or only matches for deals that are emailed, because recording all matches changes when a later price drop can generate an alert.

## Motivation

`listing_matches` was created in schema version 1 but is not currently written by the Yahoo job.
`src/yahoo_scrape.py:170-192` associates matching listings with `TrackedItem` while constructing deals, and `src/yahoo_scrape.py:253` currently stores only the deals in the successfully sent digest.
This means the database cannot answer which tracked item a stored listing matched.
The existing deal-only storage also allows a listing first seen above market to be stored later if its price drops below market.

## Scope

**In scope:**

- Record the item association for each listing selected for a successfully sent digest.
- Keep the database write after successful email delivery so failed sends remain eligible for retry.
- Make listing and match persistence safe to retry when the URL or listing-item pair already exists.
- Preserve the current one-email and deal-selection behavior unless the user resolves the open question differently.

**Out of scope:**

- Tracking listing lifecycle fields such as `last_seen_at`, `ended_at`, or final price.
- Recording listings that do not match any active item's keywords.
- Changing how keyword matching, deal thresholds, digest ordering, or the per-item limit work.
- Backfilling item associations for existing listing rows, because current stored rows do not reliably identify all matching items.

## Design

### Files touched

| File | Change |
| --- | --- |
| `src/db.py` | Add a database operation that inserts or finds a listing and idempotently records its item association. |
| `src/yahoo_scrape.py` | Call the operation for each deal included in a successfully sent digest, using `Deal.item_id`. |

### Schema changes and migration

None under the recommended approach.
`src/schema.sql` already defines `listing_matches` with primary key `(listing_id, item_id)`, timestamp `matched_at`, and foreign keys to `listings` and `items`.
Existing databases already have this table at schema version 1, so no migration or backup-first database upgrade is required.
Existing listing rows remain unchanged, and no historical match rows are inferred.

### Interfaces

Add `record_listing_match(brand_id, url, title, price_yen, image_url, item_id) -> bool` in `db.py`.
The function must create the listing if absent, find its `listings.id` by unique URL, and insert the `(listing_id, item_id, matched_at)` association idempotently.
It returns `True` only when this call created a new listing row, and `False` when the URL already existed, regardless of whether the match association was new.
On URL conflict, the existing listing's brand, title, price, image, and timestamps remain unchanged, consistent with `insert_listing` using `INSERT OR IGNORE`.
The operation must commit listing and match changes together and close its connection on success and error.
An invalid brand or item foreign key must raise the existing SQLite integrity error and leave neither a partial listing nor a partial match.
The Yahoo job must use the new operation only after `send_email` reports success.

### Alert behavior and recommendation

The recommended initial behavior is to associate listings only when they are deals included in a successfully sent digest.
This preserves the current behavior where an above-market listing is not marked known and can alert after its price drops.
The tradeoff is that `listing_matches` represents item associations for emailed deals, not every listing that passed keyword matching.
If the user wants every keyword match recorded, the design must separately define how a later deal remains eligible after its listing and match have already been persisted.

## Acceptance criteria

1. After a successful digest send, each displayed deal has exactly one `listing_matches` row associating its listing URL with that deal's `item_id`.
2. The `record_listing_match` database API can associate the same listing URL with two different item IDs, creating two match rows and only one listing row.
   This criterion exercises database capability only and does not change `build_sections`, which currently suppresses a URL after its first item.
3. Repeating the persistence operation for the same listing and item leaves exactly one listing row and one match row for that pair.
4. When a listing URL already exists with different metadata, calling `record_listing_match` leaves its existing metadata unchanged while recording the requested item association.
5. If email delivery fails, the Yahoo job writes neither the listing nor its match row, and the next run can offer the deal again.
6. A listing that does not qualify as a deal is not stored under the recommended approach, and if it later becomes a deal it can be included in a digest.
7. If either referenced brand or item does not exist, the database operation raises an integrity error and rolls back all writes from that operation.
8. An existing schema version 1 database can use the feature without changing `PRAGMA user_version` or losing existing listing and match rows.

## Edge cases

- Empty results: No database writes occur when there are no deals and no digest is sent.
- `None` or missing values: `image_url` may be `None`; required URL, title, price, brand, and item values retain database validation behavior.
- Network failure or bad HTTP status: Yahoo fetch failures happen before any listing or match writes; email failure happens before persistence.
- Malformed input: The Yahoo parser continues to reject malformed records before persistence.
- Duplicates: Duplicate URLs reuse the existing listing row, and the composite primary key prevents duplicate item associations.
- Existing data: Existing listings without match rows remain intact and are not assigned guessed item associations.

## Test plan

- Use the existing temporary SQLite database fixture and seeded item rows from `tests/conftest.py`.
- Add database tests for new listing plus match insertion, a second item association for the same URL, idempotent repeats, unchanged metadata on URL conflict, `None` image URLs, required-field failure rollback, foreign-key failure rollback, and connection closure on errors.
- Extend Yahoo job tests with mocked email delivery and database functions to assert persistence occurs only after a successful send and uses each `Deal.item_id`, including a digest with deals for two different items.
- Intercept `yahoo_scrape.record_listing_match` by its public name in post-send persistence tests.
- Test the above-market then below-market sequence with the same fixture listing to verify the recommended alert behavior remains intact.
- No saved HTML or recorded API JSON fixture changes are needed because Yahoo parsing and eBay behavior are unchanged.

## Shared-file conflicts

This feature touches `src/db.py`, a shared file.
It must not run in parallel with another feature that touches `src/db.py`, `src/schema.sql`, or `src/catalog.json`.
The recommended design does not touch `src/schema.sql` or `src/catalog.json`.

## Open questions

1. Should RC-04 record only listings included in successfully sent deal digests, or every listing that matches an active item's keywords?
   Recommendation: record only digest deals for this change, preserving the existing behavior that lets a listing alert after a later price drop.
   Recording every keyword match is a more complete history, but it requires a separate definition and mechanism for keeping a later price drop eligible for alerting.

## Resolved

- 2026-10-26: The user approved recording only deals included in a successfully sent digest.
  This preserves the existing later-price-drop alert behavior.

## Amendments

- 2026-10-26: Expanded the test plan after independent review to cover NULL item-ID rollback, distinct item IDs in one Yahoo digest, and the correctly named `record_listing_match` job hook.

## Rollback

Deploying the recommended approach requires no schema migration.
To roll back the code, restore the previous `src/db.py` and `src/yahoo_scrape.py`; previously added `listing_matches` rows may remain because the table already supports them and they do not affect current deal selection.
