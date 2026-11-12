-- doctor check 1 of 7: dangling_reference (severity: error)
--
-- Finds: rows whose foreign key points at a missing parent. One finding per missing reference,
-- so a pair missing both parents gives two findings.
--   listing_matches      -> listings (listing_id) and items (item_id)
--   item_keywords        -> items
--   ebay_listing_prices  -> items
--   listings             -> brands
--   items                -> brands
-- Healthy means zero rows. (PRAGMA foreign_key_check finds the same rows; a test compares them.)
--
-- Parameters: none.
--
-- Columns, in this order
--   child_table   TEXT     the table holding the bad reference
--   child_key     TEXT     the child row, in exactly these formats:
--                            listing_matches      listing_id=<n> item_id=<n>
--                            item_keywords        item_id=<n> keyword=<keyword>
--                            ebay_listing_prices  item_id=<n> ebay_listing_id=<id>
--                            listings and items   id=<n>
--   parent_table  TEXT     the table the reference should point into
--   missing_id    INTEGER  the id that does not exist there
--
-- Ordering: child_table, parent_table, missing_id, child_key, all ascending.
--
-- One statement only, read-only, exactly the columns above.
-- Write your query below.

-- Each branch below is an anti-join: LEFT JOIN the child row to its parent, then keep only the
-- rows where the parent side came back NULL, which means no parent exists. The test is on the
-- parent's primary key (it can never be NULL on a real row), so IS NULL can only mean "missing".
-- One branch per foreign key gives one finding per missing reference, and UNION ALL (not UNION)
-- keeps duplicates, so two different findings that look alike are never merged away.
-- Integers are joined to text with || for the readable child_key formats.
SELECT
    'listing_matches' AS child_table,
    'listing_id=' || listing_matches.listing_id || ' item_id=' || listing_matches.item_id AS child_key,
    'listings' AS parent_table,
    listing_matches.listing_id AS missing_id
FROM listing_matches
LEFT JOIN listings ON listings.id = listing_matches.listing_id
WHERE listings.id IS NULL

UNION ALL

-- A pair missing both parents appears in this branch AND the one above: two findings, as required.
SELECT
    'listing_matches',
    'listing_id=' || listing_matches.listing_id || ' item_id=' || listing_matches.item_id,
    'items',
    listing_matches.item_id
FROM listing_matches
LEFT JOIN items ON items.id = listing_matches.item_id
WHERE items.id IS NULL

UNION ALL

SELECT
    'item_keywords',
    'item_id=' || item_keywords.item_id || ' keyword=' || item_keywords.keyword,
    'items',
    item_keywords.item_id
FROM item_keywords
LEFT JOIN items ON items.id = item_keywords.item_id
WHERE items.id IS NULL

UNION ALL

SELECT
    'ebay_listing_prices',
    'item_id=' || ebay_listing_prices.item_id || ' ebay_listing_id=' || ebay_listing_prices.ebay_listing_id,
    'items',
    ebay_listing_prices.item_id
FROM ebay_listing_prices
LEFT JOIN items ON items.id = ebay_listing_prices.item_id
WHERE items.id IS NULL

UNION ALL

SELECT
    'listings',
    'id=' || listings.id,
    'brands',
    listings.brand_id
FROM listings
LEFT JOIN brands ON brands.id = listings.brand_id
WHERE brands.id IS NULL

UNION ALL

SELECT
    'items',
    'id=' || items.id,
    'brands',
    items.brand_id
FROM items
LEFT JOIN brands ON brands.id = items.brand_id
WHERE brands.id IS NULL

-- ORDER BY on a compound query can only use the column names of the FIRST branch, which is why
-- that branch has aliases and the others do not. Sorting follows the contract, ending with
-- child_key so the order is total even when one table has several findings.
ORDER BY child_table, parent_table, missing_id, child_key
