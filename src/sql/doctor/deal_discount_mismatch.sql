-- doctor check 4 of 7: deal_discount_mismatch (severity: error)
--
-- Finds: pairs with deal-time values whose stored deal_discount_pct differs by more than the
-- tolerance from the discount recomputed from the stored inputs: 100 times
-- (deal_market_price_usd minus deal_price_yen times deal_yen_to_usd), divided by deal_market_price_usd.
-- Pairs recorded before schema version 5 have NULL deal-time columns and are never findings.
-- Healthy means zero rows.
--
-- Parameters (named, always bound)
--   :tolerance_pct  REAL, the allowed absolute difference in percentage points (0.001)
--
-- Columns, in this order
--   listing_id    INTEGER  listing_matches.listing_id
--   item_id       INTEGER  listing_matches.item_id
--   stored_pct    REAL     deal_discount_pct as stored, unrounded
--   computed_pct  REAL     the recomputed discount, unrounded
--
-- Ordering: listing_id, item_id ascending.
--
-- One statement only, read-only, named parameters only, exactly the columns above.
-- Write your query below.

-- Step 1: recompute the discount from the stored inputs once, so the same formula feeds both
-- the comparison and the output column instead of being typed twice.
WITH recomputed AS (
    SELECT
        listing_matches.listing_id         AS listing_id,
        listing_matches.item_id            AS item_id,
        listing_matches.deal_discount_pct  AS stored_pct,
        -- 100.0 (not 100) so the whole expression is REAL. Dividing integers in SQLite
        -- truncates, and deal_price_yen is an INTEGER column, so the safe habit is to start
        -- from a REAL operand even though the other operands are already REAL here.
        100.0 * (listing_matches.deal_market_price_usd
                 - listing_matches.deal_price_yen * listing_matches.deal_yen_to_usd)
              / listing_matches.deal_market_price_usd AS computed_pct
    FROM listing_matches
    -- Pairs recorded before schema version 5 have all six deal-time columns NULL (a table CHECK
    -- guarantees all-or-none), so testing the discount alone is enough to skip them. Arithmetic
    -- on NULL gives NULL, which would also fail the comparison below, but saying so explicitly
    -- keeps the rule from depending on that subtle behavior.
    WHERE listing_matches.deal_discount_pct IS NOT NULL
)
SELECT
    recomputed.listing_id   AS listing_id,
    recomputed.item_id      AS item_id,
    -- Both values are returned unrounded so the finding shows the real difference.
    recomputed.stored_pct   AS stored_pct,
    recomputed.computed_pct AS computed_pct
FROM recomputed
-- ABS because the stored value may be off in either direction, and the tolerance is an
-- absolute number of percentage points that absorbs float noise (25.0005 vs 25.0 is fine).
WHERE ABS(recomputed.stored_pct - recomputed.computed_pct) > :tolerance_pct
ORDER BY recomputed.listing_id ASC, recomputed.item_id ASC
