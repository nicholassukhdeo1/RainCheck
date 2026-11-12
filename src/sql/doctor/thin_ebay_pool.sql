-- doctor check 7 of 7: thin_ebay_pool (severity: warning)
--
-- Finds: active items with fewer than :min_observations stored eBay observations, including
-- items with none. Fewer than five raw observations guarantees no eBay price can be set;
-- five or more does not guarantee one, because outliers are removed first.
-- Healthy means zero rows.
--
-- Parameters (named, always bound)
--   :min_observations  integer, the smallest pool size that is not thin (db.MIN_POOL_PRICES)
--
-- Columns, in this order
--   item_id       INTEGER  items.id
--   brand         TEXT     brand name of the item
--   item          TEXT     items.name
--   observations  INTEGER  number of stored eBay observations; 0 for an item with none
--
-- Ordering: item_id ascending.
--
-- One statement only, read-only, named parameters only, exactly the columns above.
-- Write your query below.

SELECT
    items.id    AS item_id,
    brands.name AS brand,
    items.name  AS item,
    -- COUNT of a column from the LEFT JOINed table, not COUNT(*): an item with no observations
    -- still produces one all-NULL row, which COUNT(*) would count as 1 instead of the correct 0.
    COUNT(ebay_listing_prices.ebay_listing_id) AS observations
FROM items
-- LEFT JOIN so this check stands alone: an item whose brand is missing (a dangling brand_id,
-- which dangling_reference reports) is still listed here, with brand shown as NULL.
LEFT JOIN brands ON brands.id = items.brand_id
-- LEFT JOIN so items with no observations at all are kept (an inner join would hide the
-- thinnest pools of all). Only this one many-side table is joined, so rows are not multiplied.
LEFT JOIN ebay_listing_prices ON ebay_listing_prices.item_id = items.id
-- Row filter before grouping: inactive items are not priced, so they are not a problem.
WHERE items.active = 1
-- Every non-aggregate selected column is grouped, so no value is picked from a random row.
GROUP BY items.id, items.name, brands.name
-- The condition is on a count, which only exists after grouping, so it must be HAVING.
-- The parameter is the smallest pool that is NOT thin, hence strictly less than.
HAVING COUNT(ebay_listing_prices.ebay_listing_id) < :min_observations
ORDER BY items.id ASC
