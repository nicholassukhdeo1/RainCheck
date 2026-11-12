-- doctor check 2 of 7: listing_brand_mismatch (severity: error)
--
-- Finds: recorded pairs whose listing's brand_id differs from the item's brand_id, which would
-- make brand reporting ambiguous. Pairs with a missing parent are left to check 1.
-- Healthy means zero rows.
--
-- Parameters: none.
--
-- Columns, in this order
--   listing_id     INTEGER  listing_matches.listing_id
--   item_id        INTEGER  listing_matches.item_id
--   listing_brand  TEXT     name of the listing's brand
--   item_brand     TEXT     name of the item's brand
--
-- Ordering: listing_id, item_id ascending.
--
-- One statement only, read-only, exactly the columns above.
-- Write your query below.

SELECT
    listing_matches.listing_id AS listing_id,
    listing_matches.item_id    AS item_id,
    listing_brands.name        AS listing_brand,
    item_brands.name           AS item_brand
FROM listing_matches
-- Plain inner joins on purpose: a pair whose listing, item, or brand is missing cannot be
-- compared, and those rows belong to check 1 (dangling_reference), so dropping them here
-- avoids reporting the same damage twice.
JOIN listings ON listings.id = listing_matches.listing_id
JOIN items    ON items.id    = listing_matches.item_id
-- The brands table is joined twice under two aliases, once per side, to get both names.
-- Without aliases the two joins would be ambiguous.
JOIN brands AS listing_brands ON listing_brands.id = listings.brand_id
JOIN brands AS item_brands    ON item_brands.id    = items.brand_id
-- Compare the ids (cheap and exact) rather than the names. Both sides are NOT NULL columns,
-- so <> cannot silently drop rows through a NULL comparison.
WHERE listings.brand_id <> items.brand_id
ORDER BY listing_matches.listing_id ASC, listing_matches.item_id ASC
