-- doctor check 6 of 7: no_usable_price (severity: warning)
--
-- Finds: active items where neither market_price_usd nor manual_price_usd is a usable price.
-- Usable means not NULL and greater than 0. market_price_usd has no CHECK, so 0 or negative
-- legacy values are possible and count as unusable. The Yahoo job skips such items.
-- Healthy means zero rows.
--
-- Parameters: none.
--
-- Columns, in this order
--   item_id           INTEGER  items.id
--   brand             TEXT     brand name of the item
--   item              TEXT     items.name
--   market_price_usd  REAL     as stored
--   manual_price_usd  REAL     as stored
--
-- Ordering: item_id ascending.
--
-- One statement only, read-only, exactly the columns above.
-- Write your query below.

SELECT
    items.id               AS item_id,
    brands.name            AS brand,
    items.name             AS item,
    -- Returned as stored (NULL, 0.0, or negative) so the finding shows what is actually wrong.
    items.market_price_usd AS market_price_usd,
    items.manual_price_usd AS manual_price_usd
FROM items
-- LEFT JOIN so this check stands alone: an item whose brand is missing (a dangling brand_id,
-- which dangling_reference reports) is still listed here, with brand shown as NULL.
LEFT JOIN brands ON brands.id = items.brand_id
WHERE items.active = 1
  -- "Unusable" is the opposite of "not NULL and greater than 0". Each side is spelled out as
  -- IS NULL OR <= 0 because NOT (price > 0) is NULL, not true, for a NULL price, and
  -- WHERE would silently drop exactly the items with no price at all.
  AND (items.market_price_usd IS NULL OR items.market_price_usd <= 0)
  AND (items.manual_price_usd IS NULL OR items.manual_price_usd <= 0)
ORDER BY items.id ASC
