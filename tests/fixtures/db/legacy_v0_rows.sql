-- Rows mirroring the shape of the last committed pre-RC-03 database (git blob d632e3c).
-- Loaded into a database created from schema_v0.sql; user_version stays 0.
INSERT INTO brands (id, name, yahoo_brand_id) VALUES (1, 'Rick Owens', '103538');

INSERT INTO items (id, brand_id, name, market_price_usd, market_price_source, search_query)
VALUES (1, 1, 'rick owens ramones', 420.0, 'manual', 'rick+owens+ramones');
INSERT INTO items (id, brand_id, name, market_price_usd, market_price_source, search_query)
VALUES (2, 1, 'pod shorts', 300.0, 'manual', 'pod+shorts');

INSERT INTO item_keywords (item_id, keyword) VALUES (1, 'ramones');
INSERT INTO item_keywords (item_id, keyword) VALUES (1, 'ramon');
INSERT INTO item_keywords (item_id, keyword) VALUES (1, 'high top');
INSERT INTO item_keywords (item_id, keyword) VALUES (2, 'ramones');
INSERT INTO item_keywords (item_id, keyword) VALUES (2, 'ramon');
INSERT INTO item_keywords (item_id, keyword) VALUES (2, 'high top');

INSERT INTO listings (id, brand_id, url, title, price_yen, image_url, first_seen_at, last_seen_at, ended_at, final_price_yen)
VALUES (1, 1, 'https://auctions.yahoo.co.jp/jp/auction/u1243433255', 'Synthetic legacy listing one', 88935,
        'https://auc-pctr.c.yimg.jp/i/legacy-one.jpg', '2026-09-06T00:13:01.331953+00:00', '2026-09-06T00:13:01.331953+00:00', NULL, NULL);
INSERT INTO listings (id, brand_id, url, title, price_yen, image_url, first_seen_at, last_seen_at, ended_at, final_price_yen)
VALUES (2, 1, 'https://auctions.yahoo.co.jp/jp/auction/p1243375576', 'Synthetic legacy listing two', 70339,
        'https://auc-pctr.c.yimg.jp/i/legacy-two.jpg', '2026-09-06T00:13:01.333810+00:00', '2026-09-06T00:13:01.333810+00:00', NULL, NULL);

INSERT INTO listing_matches (listing_id, item_id, matched_at) VALUES (1, 1, '2026-09-06T00:13:01.331953+00:00');
