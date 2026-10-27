-- Schema version 3 fixture preserved for RC-14's version 3 -> 4 migration.
CREATE TABLE brands (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    yahoo_brand_id TEXT NOT NULL UNIQUE
);
CREATE TABLE items (
    id INTEGER PRIMARY KEY,
    brand_id INTEGER NOT NULL REFERENCES brands(id),
    name TEXT NOT NULL,
    market_price_usd REAL,
    search_query TEXT,
    manual_price_usd REAL CHECK (manual_price_usd IS NULL OR manual_price_usd > 0),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    UNIQUE (brand_id, name)
);
CREATE TABLE item_keywords (
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    keyword TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'include' CHECK (kind IN ('include', 'exclude')),
    PRIMARY KEY (item_id, keyword)
);
CREATE TABLE listings (
    id INTEGER PRIMARY KEY,
    brand_id INTEGER NOT NULL REFERENCES brands(id),
    url TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    price_yen INTEGER NOT NULL,
    image_url TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    ended_at TEXT,
    final_price_yen INTEGER,
    is_fixed_price INTEGER CHECK (is_fixed_price IN (0, 1)),
    buy_now_price_yen INTEGER CHECK (buy_now_price_yen IS NULL OR buy_now_price_yen > 0),
    scheduled_end_at TEXT
);
CREATE TABLE listing_matches (
    listing_id INTEGER NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    matched_at TEXT NOT NULL,
    PRIMARY KEY (listing_id, item_id)
);
CREATE INDEX idx_listings_brand_seen ON listings (brand_id, first_seen_at);
CREATE INDEX idx_matches_item ON listing_matches (item_id);
CREATE TABLE ebay_listing_prices (
    item_id INTEGER NOT NULL REFERENCES items(id),
    ebay_listing_id TEXT NOT NULL CHECK (length(ebay_listing_id) > 0),
    first_price_usd REAL NOT NULL CHECK (first_price_usd > 0),
    first_seen_at TEXT NOT NULL,
    PRIMARY KEY (item_id, ebay_listing_id)
);
CREATE INDEX idx_ebay_listing_prices_item_seen ON ebay_listing_prices (item_id, first_seen_at);
PRAGMA user_version = 3;
