-- report: deals per brand
--
-- Question: for each brand, how many deals did RainCheck record, how good were they,
-- and when was the latest one?
--
-- Definitions
--   recorded pair   one listing_matches row (one listing shown for one item in a digest).
--   deal            a recorded pair whose deal_kind is fixed_price, auction_buy_now, or NULL.
--                   NULL means the pair was recorded before schema version 5.
--   watch           a recorded pair whose deal_kind is auction_watch. Watches are counted
--                   separately and never feed the discount statistics or last_deal_date,
--                   because their percentage is only a potential discount.
--   priced deal     a deal whose deal_discount_pct is not NULL.
--   Pairs are counted, not distinct listings: one listing recorded for two items is two deals.
--   A pair belongs to the brand of its ITEM (items.brand_id), not listings.brand_id.
--   Inactive items' pairs still count.
--
-- Parameters (named, always bound)
--   :min_deals  integer, 0 or more. Keeps only brands whose deals value is at least this
--               (a filter on groups, applied after counting). With 0, every brand appears,
--               including brands with no items or no pairs.
--   :since      text YYYY-MM-DD, or NULL when omitted. Keeps only pairs whose matched_at is
--               on or after 00:00:00 UTC on that date (a filter on single pairs, applied
--               before grouping). It must not drop zero-deal brands.
--
-- Columns, in this order
--   brand              TEXT     brands.name
--   deals              INTEGER  count of deals for the brand; 0 when none
--   watches            INTEGER  count of watches for the brand; 0 when none
--   priced_deals       INTEGER  count of priced deals; 0 when none
--   avg_discount_pct   REAL     mean deal_discount_pct over priced deals, rounded to 1 decimal
--                               AFTER averaging; NULL when priced_deals is 0
--   best_discount_pct  REAL     largest deal_discount_pct over priced deals, rounded to 1 decimal;
--                               NULL when priced_deals is 0
--   last_deal_date     TEXT     UTC calendar date YYYY-MM-DD of the latest matched_at among the
--                               brand's deals (watches excluded); NULL when deals is 0
--
-- Ordering: deals descending, then brand ascending.
--
-- One statement only, read-only, named parameters only, exactly the columns above.
-- Write your query below.

-- Step 1: build one row per recorded pair, already attached to its item's brand and already
-- limited by --since. The date filter lives HERE, inside the inner side of the join, and not in
-- the outer WHERE. If it sat in an outer WHERE after the LEFT JOIN below, every brand with no
-- pairs in range would carry NULLs, fail the comparison, and vanish, so --min-deals 0 could no
-- longer show zero-deal brands. Filtering first and joining afterwards keeps those brands.
WITH brand_pairs AS (
    SELECT
        -- brand of the ITEM, because the discount is relative to the item (not listings.brand_id).
        -- This is also why listings is not joined at all: nothing here needs it.
        items.brand_id                     AS brand_id,
        listing_matches.matched_at         AS matched_at,
        listing_matches.deal_discount_pct  AS deal_discount_pct,
        -- 1 = deal, 0 = watch. A NULL deal_kind (a pair recorded before schema version 5)
        -- must count as a deal, but "deal_kind <> 'auction_watch'" is NULL, not true, for NULL.
        -- CASE with an ELSE sends NULL to the deal side on purpose.
        CASE WHEN listing_matches.deal_kind = 'auction_watch' THEN 0 ELSE 1 END AS is_deal
    FROM listing_matches
    -- An inner JOIN is right here: a pair always has an item, and we want only pairs.
    JOIN items ON items.id = listing_matches.item_id
    -- ISO-8601 text compares correctly as text because every stored value has the same layout
    -- and the same +00:00 offset. The bare date '2026-10-20' sorts before every timestamp on
    -- that day, so >= means "on or after midnight UTC". Fractional seconds sort correctly too.
    -- Nothing wraps matched_at in a function, so a plain "matched_at >= :since" could use an
    -- index on it. The NULL test on the parameter makes the filter optional without changing
    -- the SQL text, but that OR forces SQLite to scan the table (it cannot do a range search),
    -- so the optional form does not use such an index.
    WHERE :since IS NULL OR listing_matches.matched_at >= :since
)
SELECT
    brands.name AS brand,

    -- Every aggregate below counts a column or a CASE result, never COUNT(*). A brand with no
    -- pairs still produces one row after the LEFT JOIN, all NULLs, and COUNT(*) would count
    -- that phantom row as 1. COUNT(expr) skips NULLs, so such a brand correctly shows 0.
    -- is_deal is NULL on that phantom row, so it matches neither the 1 nor the 0 test.
    COUNT(CASE WHEN brand_pairs.is_deal = 1 THEN 1 END) AS deals,
    COUNT(CASE WHEN brand_pairs.is_deal = 0 THEN 1 END) AS watches,

    -- A priced deal is a deal with a stored discount. The CASE hides watches (their percentage
    -- is only potential) and COUNT then skips the NULL discounts of pre-version-5 deals.
    COUNT(CASE WHEN brand_pairs.is_deal = 1 THEN brand_pairs.deal_discount_pct END) AS priced_deals,

    -- AVG and MAX ignore NULLs, which is right for unpriced legacy deals, and return NULL (not 0)
    -- when nothing is left, which is the contract for a brand with no priced deals.
    -- ROUND wraps the aggregate so rounding happens once, after averaging, not on each input.
    ROUND(AVG(CASE WHEN brand_pairs.is_deal = 1 THEN brand_pairs.deal_discount_pct END), 1) AS avg_discount_pct,
    ROUND(MAX(CASE WHEN brand_pairs.is_deal = 1 THEN brand_pairs.deal_discount_pct END), 1) AS best_discount_pct,

    -- Latest timestamp among deals only (watches are hidden by the CASE), then keep the first
    -- 10 characters, which are the UTC date YYYY-MM-DD in both the whole-second and the
    -- fractional-second forms. SUBSTR of NULL is NULL, so a brand with no deals shows NULL.
    SUBSTR(MAX(CASE WHEN brand_pairs.is_deal = 1 THEN brand_pairs.matched_at END), 1, 10) AS last_deal_date
FROM brands
-- LEFT JOIN from brands (not to it) so a brand with no items or no pairs survives as a row.
LEFT JOIN brand_pairs ON brand_pairs.brand_id = brands.id
-- Every selected column that is not an aggregate must be grouped. SQLite would silently accept
-- a missing one and pick a random row's value, so both are listed even though name is unique.
GROUP BY brands.id, brands.name
-- HAVING filters groups after counting, which WHERE cannot do because deals does not exist yet.
-- (SQLite and MySQL let HAVING use the alias deals defined above. Standard SQL and PostgreSQL
-- do not: there the aggregate expression would have to be repeated in HAVING.)
HAVING deals >= :min_deals
-- Highest deal count first, then brand name, which is unique, so the order is total and the
-- tests (and users) always see the same row order.
ORDER BY deals DESC, brands.name ASC
