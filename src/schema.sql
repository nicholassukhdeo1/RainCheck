CREATE TABLE IF NOT EXISTS brands (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,
    yahoo_brand_id  TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS items (
    id                   INTEGER PRIMARY KEY,
    brand_id             INTEGER NOT NULL REFERENCES brands(id),
    name                 TEXT NOT NULL,
    market_price_usd     REAL,
    market_price_source  TEXT DEFAULT 'manual',
    search_query         TEXT,
    UNIQUE (brand_id, name)
);

CREATE TABLE IF NOT EXISTS item_keywords (
    item_id  INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    keyword  TEXT NOT NULL,
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
    final_price_yen  INTEGER
);

CREATE TABLE IF NOT EXISTS listing_matches (
    listing_id  INTEGER NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
    item_id     INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    matched_at  TEXT NOT NULL,
    PRIMARY KEY (listing_id, item_id)
);

CREATE INDEX IF NOT EXISTS idx_listings_brand_seen ON listings (brand_id, first_seen_at);
CREATE INDEX IF NOT EXISTS idx_matches_item        ON listing_matches (item_id);