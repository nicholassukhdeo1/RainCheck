-- doctor check 5 of 7: no_include_keywords (severity: error)
--
-- Finds: active items with no include keyword, including items with no keyword rows at all
-- and items with only exclude keywords. An item with no include keyword silently never matches
-- on Yahoo or eBay (matching.title_matches returns False for an empty include set).
-- Healthy means zero rows.
--
-- Parameters: none.
--
-- Columns, in this order
--   item_id  INTEGER  items.id
--   brand    TEXT     brand name of the item
--   item     TEXT     items.name
--
-- Ordering: item_id ascending.
--
-- One statement only, read-only, exactly the columns above.
-- Write your query below.

SELECT
    items.id    AS item_id,
    brands.name AS brand,
    items.name  AS item
FROM items
-- LEFT JOIN so this check stands alone: an item whose brand is missing (a dangling brand_id,
-- which dangling_reference reports) is still listed here, with brand shown as NULL.
LEFT JOIN brands ON brands.id = items.brand_id
-- Anti-join: LEFT JOIN to only the INCLUDE keywords, then keep items where nothing joined.
-- The kind test sits in the join condition (not in WHERE) on purpose. In WHERE it would remove
-- the NULL rows this query is looking for. In the join condition an item with only exclude
-- keywords, or no keyword rows at all, simply finds no match and comes back with NULLs.
-- An item with several include keywords matches several rows, but those rows are all discarded
-- by the IS NULL test, so there is no row multiplication in the result.
LEFT JOIN item_keywords
       ON item_keywords.item_id = items.id
      AND item_keywords.kind = 'include'
-- item_keywords.item_id is part of the primary key, so it is NULL only when no row joined.
-- Inactive items are ignored because they are not searched anyway.
WHERE items.active = 1
  AND item_keywords.item_id IS NULL
ORDER BY items.id ASC
