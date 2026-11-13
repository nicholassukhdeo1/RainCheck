-- doctor check 3 of 7: noncanonical_timestamp (severity: error)
--
-- Finds: values that are not canonical UTC text in these columns:
--   listings.first_seen_at, listings.last_seen_at,
--   listing_matches.matched_at, ebay_listing_prices.first_seen_at
-- Canonical means exactly 25 characters shaped YYYY-MM-DDTHH:MM:SS+00:00, or exactly 32 characters
-- shaped YYYY-MM-DDTHH:MM:SS.ffffff+00:00, with ASCII digits in every digit position.
-- Only the text shape is checked, not calendar correctness (month 13 is not a finding).
-- Healthy means zero rows.
--
-- Parameters: none.
--
-- Columns, in this order
--   table_name   TEXT  the table name
--   column_name  TEXT  the column name
--   row_key      TEXT  the row, in exactly these formats:
--                        listings             id=<n>
--                        listing_matches      listing_id=<n> item_id=<n>
--                        ebay_listing_prices  item_id=<n> ebay_listing_id=<id>
--   value        TEXT  the offending value, as stored
--
-- Ordering: table_name, column_name, row_key ascending.
--
-- One statement only, read-only, exactly the columns above.
-- Write your query below.

-- Step 1: stack the four checked columns into one list of (table, column, row, value), so the
-- shape test below is written once instead of four times. UNION ALL because the sources are
-- different tables and nothing should be merged.
WITH stamps AS (
    SELECT 'listings' AS table_name, 'first_seen_at' AS column_name,
           'id=' || listings.id AS row_key, listings.first_seen_at AS value
    FROM listings
    UNION ALL
    SELECT 'listings', 'last_seen_at', 'id=' || listings.id, listings.last_seen_at
    FROM listings
    UNION ALL
    SELECT 'listing_matches', 'matched_at',
           'listing_id=' || listing_matches.listing_id || ' item_id=' || listing_matches.item_id,
           listing_matches.matched_at
    FROM listing_matches
    UNION ALL
    SELECT 'ebay_listing_prices', 'first_seen_at',
           'item_id=' || ebay_listing_prices.item_id || ' ebay_listing_id=' || ebay_listing_prices.ebay_listing_id,
           ebay_listing_prices.first_seen_at
    FROM ebay_listing_prices
)
SELECT
    stamps.table_name  AS table_name,
    stamps.column_name AS column_name,
    stamps.row_key     AS row_key,
    stamps.value       AS value
FROM stamps
-- GLOB (not LIKE) because it is case-sensitive and has character classes. [0-9] matches only
-- ASCII digits, which LIKE cannot express, and in GLOB the characters . + : - are plain text.
-- A GLOB pattern must match the WHOLE string, so each pattern also pins the exact length
-- (25 characters without a fraction, 32 with exactly six fractional digits) and no separate
-- length() test is needed. The check is on shape only, so month 13 is not a finding.
-- A row is a finding when it matches NEITHER canonical shape.
WHERE NOT (
       stamps.value GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]+00:00'
    OR stamps.value GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]+00:00'
)
ORDER BY stamps.table_name ASC, stamps.column_name ASC, stamps.row_key ASC
