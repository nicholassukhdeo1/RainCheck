-- Schema version 3 (PRAGMA user_version = 3).
-- This file always describes the LATEST version, and it is only used to create
-- a brand new database. An existing database is upgraded by MIGRATIONS in
-- db.py instead, because CREATE TABLE IF NOT EXISTS cannot add a column to a
-- table that already exists. Change both together.

CREATE TABLE IF NOT EXISTS brands (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,
    yahoo_brand_id  TEXT NOT NULL UNIQUE
);

-- items: one row per tracked item.
-- Two prices on purpose, each with exactly one writer:
--   manual_price_usd  = the fallback price typed in catalog.json (written only by seeding)
--   market_price_usd  = the eBay-derived price (written by the listing pool or compatibility setter)
-- so re-seeding can never overwrite an eBay price.
-- active = 0 means "removed from catalog.json"; we keep the row (instead of
-- deleting it) because other tables point at items.id.
CREATE TABLE IF NOT EXISTS items (
    id                INTEGER PRIMARY KEY,
    brand_id          INTEGER NOT NULL REFERENCES brands(id),
    name              TEXT NOT NULL,
    market_price_usd  REAL,
    search_query      TEXT,
    manual_price_usd  REAL CHECK (manual_price_usd IS NULL OR manual_price_usd > 0),
    active            INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    UNIQUE (brand_id, name)
);

-- item_keywords: the primary key (item_id, keyword) means a word can be an
-- include or an exclude for an item, never both (that item could never match).
CREATE TABLE IF NOT EXISTS item_keywords (
    item_id  INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    keyword  TEXT NOT NULL,
    kind     TEXT NOT NULL DEFAULT 'include' CHECK (kind IN ('include', 'exclude')),
    PRIMARY KEY (item_id, keyword)
);

CREATE TABLE IF NOT EXISTS listings (
    id               INTEGER PRIMARY KEY,
    brand_id         INTEGER NOT NULL REFERENCES brands(id),
    url              TEXT NOT NULL UNIQUE,
    title            TEXT NOT NULL,
    price_yen        INTEGER NOT NULL,
    image_url        TEXT,
    first_seen_at    TEXT NOT NULL,
    last_seen_at     TEXT NOT NULL,
    ended_at         TEXT,
    final_price_yen  INTEGER,
    is_fixed_price   INTEGER CHECK (is_fixed_price IN (0, 1)),
    buy_now_price_yen INTEGER CHECK (buy_now_price_yen IS NULL OR buy_now_price_yen > 0),
    scheduled_end_at TEXT
);

CREATE TABLE IF NOT EXISTS listing_matches (
    listing_id  INTEGER NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
    item_id     INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    matched_at  TEXT NOT NULL,
    PRIMARY KEY (listing_id, item_id)
);

CREATE INDEX IF NOT EXISTS idx_listings_brand_seen ON listings (brand_id, first_seen_at);
CREATE INDEX IF NOT EXISTS idx_matches_item        ON listing_matches (item_id);

-- One immutable first-seen asking price per eBay listing and tracked item.
CREATE TABLE IF NOT EXISTS ebay_listing_prices (
    item_id         INTEGER NOT NULL REFERENCES items(id),
    ebay_listing_id TEXT NOT NULL CHECK (length(ebay_listing_id) > 0),
    first_price_usd REAL NOT NULL CHECK (first_price_usd > 0),
    first_seen_at   TEXT NOT NULL,
    PRIMARY KEY (item_id, ebay_listing_id)
);

CREATE INDEX IF NOT EXISTS idx_ebay_listing_prices_item_seen
    ON ebay_listing_prices (item_id, first_seen_at);
