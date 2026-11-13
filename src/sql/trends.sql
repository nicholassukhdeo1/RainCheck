-- trends: recent eBay asking prices per item
--
-- Question: for each item, what were the last N eBay asking prices RainCheck discovered,
-- how did each compare with the one discovered just before it, and what was the rolling average?
--
-- Honest definition
--   These rows are the first-seen asking prices of newly discovered eBay listings
--   (ebay_listing_prices), with no outlier filtering. They are NOT past values of
--   items.market_price_usd. change_usd compares two different listings.
--
-- Observation order (per item)
--   first_seen_at ascending, then ebay_listing_id ascending. Rows sharing first_seen_at came
--   from one pricing run, so first_seen_at alone does not order them.
--   "Most recent" is the exact reverse of this order.
--
-- Window semantics
--   Change and rolling average are computed over the item's COMPLETE history, and only then
--   are the latest N rows kept, so the oldest shown row still compares with its true predecessor.
--   The rolling average covers the row and up to W-1 rows immediately before it, counted as rows
--   (not by time). Early rows average over what exists; window_rows says how many.
--
-- Item selection
--   Active items only, unless :include_inactive is 1. :item_id, when not NULL, restricts the
--   output to that one item whether it is active or not. Items with no observations give no rows.
--
-- Parameters (named, always bound)
--   :latest            integer, how many most recent observations to keep per item (N)
--   :window            integer, rolling window size in rows (W)
--   :item_id           integer, or NULL when omitted
--   :include_inactive  0 or 1
--
-- Columns, in this order
--   brand            TEXT     brand name of the item
--   item_id          INTEGER  items.id
--   item             TEXT     items.name
--   recency_rank     INTEGER  1 for the item's most recent observation, 2 for the one before, ...
--   first_seen_at    TEXT     as stored
--   ebay_listing_id  TEXT     as stored
--   price_usd        REAL     first_price_usd rounded to 2 decimals
--   change_usd       REAL     this price minus the previous observation's price, from unrounded
--                             prices and then rounded to 2 decimals; NULL for the item's first
--                             observation ever (never 0)
--   rolling_avg_usd  REAL     mean of the unrounded prices in the window, rounded to 2 decimals
--   window_rows      INTEGER  observations in that window: the smaller of W and the row's
--                             position in the item's history
--   Only rows with recency_rank at most N are returned.
--
-- Ordering: brand ascending, item ascending, item_id ascending, then recency_rank descending
-- (each item reads oldest to newest, newest last).
--
-- One statement only, read-only, named parameters only, exactly the columns above.
-- Write your query below.

-- Step 1: compute every window function over each item's COMPLETE history. Filtering to the
-- latest N rows must wait until step 2, because window functions only see rows that survive
-- WHERE. Trimming here would make the oldest shown row look like the first observation ever
-- and would shrink its rolling window.
WITH observations AS (
    SELECT
        brands.name                              AS brand,
        items.id                                 AS item_id,
        items.name                               AS item,
        ebay_listing_prices.first_seen_at        AS first_seen_at,
        ebay_listing_prices.ebay_listing_id      AS ebay_listing_id,
        ebay_listing_prices.first_price_usd      AS first_price_usd,

        -- Recency is the EXACT reverse of the observation order: both columns reversed.
        -- If only first_seen_at were reversed, rows from one pricing run (same timestamp)
        -- could disagree between this rank and the LAG and average below about who is newest.
        ROW_NUMBER() OVER (
            PARTITION BY ebay_listing_prices.item_id
            ORDER BY ebay_listing_prices.first_seen_at DESC, ebay_listing_prices.ebay_listing_id DESC
        ) AS recency_rank,

        -- LAG looks one row back in observation order and is NULL on the first row, which is
        -- the intended change_usd for an item's first observation (it must not become 0).
        -- The subtraction uses unrounded prices, and rounding happens only in step 2.
        ebay_listing_prices.first_price_usd - LAG(ebay_listing_prices.first_price_usd) OVER item_order AS raw_change,

        -- The window is the current row plus (W - 1) rows before it, so W rows in total.
        -- The frame must be spelled out. With an ORDER BY and no frame, SQLite's default is
        -- RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW, which would give a running average
        -- from the start of the item's history instead of the last W rows. ROWS counts physical
        -- rows. (RANGE would also pull in peers, rows tied on the whole ORDER BY. Here the order
        -- ends with ebay_listing_id, unique per item, so no row has a peer. Peers would only
        -- matter when ordering by first_seen_at alone.)
        AVG(ebay_listing_prices.first_price_usd) OVER rolling AS raw_avg,

        -- COUNT(*) is fine here: it counts the rows inside the frame, and every row is real.
        -- Early rows have fewer than W predecessors, so this is the smaller of W and position.
        COUNT(*) OVER rolling AS window_rows
    FROM ebay_listing_prices
    JOIN items  ON items.id  = ebay_listing_prices.item_id
    JOIN brands ON brands.id = items.brand_id
    -- Item selection. With no --item, show active items (plus inactive ones on request).
    -- With --item, show exactly that item, active or not, so the first branch is switched off.
    -- Doing this before the window functions is safe because each item is its own partition,
    -- and it keeps other items' rows out of the window sorts. (The plan is still a scan of
    -- ebay_listing_prices, so this saves sorting work, not reading work.)
    WHERE (:item_id IS NULL AND (items.active = 1 OR :include_inactive = 1))
       OR items.id = :item_id
    -- Named windows so the observation order is written once and cannot drift between functions.
    -- The first window starts on the WINDOW line, with its comment inside the parentheses:
    -- SQLite 3.49 fails to parse a comment placed directly after the WINDOW keyword
    -- ("near WINDOW: syntax error"), while a comment after "name AS (" is fine.
    WINDOW item_order AS (
        -- One partition per item (otherwise LAG would compare across items). The order ends
        -- with ebay_listing_id because first_seen_at ties within a pricing run, and without a
        -- unique last key SQLite numbers tied rows in an arbitrary order. The id is unique per item.
        PARTITION BY ebay_listing_prices.item_id
        ORDER BY ebay_listing_prices.first_seen_at ASC, ebay_listing_prices.ebay_listing_id ASC
    ),
    -- Same order, plus an explicit ROWS frame sized by the parameter. W - 1 PRECEDING
    -- (not W) because the current row is already one of the W rows.
    rolling AS (item_order ROWS BETWEEN :window - 1 PRECEDING AND CURRENT ROW)
)
-- Step 2: keep the latest N rows and format. Rounding is last, after subtracting and averaging,
-- so no rounded number feeds another calculation and change_usd cannot drift.
SELECT
    observations.brand                          AS brand,
    observations.item_id                        AS item_id,
    observations.item                           AS item,
    observations.recency_rank                   AS recency_rank,
    observations.first_seen_at                  AS first_seen_at,
    observations.ebay_listing_id                AS ebay_listing_id,
    ROUND(observations.first_price_usd, 2)      AS price_usd,
    ROUND(observations.raw_change, 2)           AS change_usd,
    ROUND(observations.raw_avg, 2)              AS rolling_avg_usd,
    observations.window_rows                    AS window_rows
FROM observations
-- This filter is on the outer query, after the windows were computed in step 1.
WHERE observations.recency_rank <= :latest
-- Brand, then item name, then item_id. (brand, item) is already unique, so item_id is not
-- breaking real ties; it is there so the order is guaranteed total, as the contract requires.
-- Rank DESCENDING last so each item reads oldest to newest with the newest row last.
ORDER BY observations.brand ASC, observations.item ASC, observations.item_id ASC, observations.recency_rank DESC
