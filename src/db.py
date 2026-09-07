from datetime import datetime, timezone

import sqlite3
import json
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "raincheck.db")
SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "schema.sql")
CATALOG_PATH = os.path.join(os.path.dirname(__file__), "catalog.json")


def get_connection():
    """
    Opens a connection to the SQLite database.
    row_factory lets you access columns by name (row['title'])
    instead of by index (row[1]), which is far more readable.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # SQLite ignores FOREIGN KEY constraints unless you turn them on,
    # and it's per-connection, so this has to run every single time.

    # basically a key refers to one of these tables that you make.
    # and there's restrictions that you'll set on these tables for
    # various reasons. a run is a row in a table. like row 3
    # could contain the row with all of the data about the
    # marathon you ran in shoe #7.
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    """Creates all tables if they don't already exist. Safe to re-run."""
    with open(SCHEMA_PATH, "r") as f:
        schema = f.read()

    conn = get_connection()
    conn.executescript(schema)
    conn.commit()
    conn.close()
    print("Database initialized.")


if __name__ == "__main__":
    init_db()


#helps us read the brands and items in catalog and store it into our db
def seed_from_catalog():
    with open(CATALOG_PATH, "r") as f:
        catalog = json.load(f)
    conn = get_connection()
    for entry in catalog:
        # the question marks are placeholder values for those columns.
        # ignore when u alr have that item in ur db, or 
        conn.execute(
            "INSERT OR IGNORE INTO brands (name, yahoo_brand_id) VALUES (?,?)",
            (entry["brand"],"103538"),
        )
        brand_id = conn.execute(
            "SELECT id FROM brands WHERE name = ?",
            (entry["brand"],),
        ).fetchone()["id"]
        # brand new SQL calls for each entry.
        # each execute() call is its own statement

        conn.execute("INSERT OR IGNORE INTO items (brand_id,name,market_price_usd,search_query) VALUES (?,?,?,?)",
                     (brand_id,entry["item name"],entry["market_price"],entry["search_query"]))

        item_id = conn.execute(
            "SELECT id FROM items WHERE brand_id = ? AND name = ?",
            (brand_id,entry["item name"]),
        ).fetchone()["id"]

        for keyword in entry["keywords"]:
            conn.execute(
                "INSERT OR IGNORE INTO item_keywords (item_id,keyword) VALUES (?,?)",
                (item_id,keyword),
            )

    conn.commit()
    conn.close()
    print("Catalog seeded.")

def insert_listing(brand_id,url,title,price_yen,image_url):
    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    # the execute func returns if smth was inserted or not. 
    ifInserted = conn.execute(
        "INSERT OR IGNORE INTO listings (brand_id, url, title, price_yen, image_url, first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?,?)",
        (brand_id,url,title,price_yen,image_url,now,now)
    )
    conn.commit()
    # how u will check if smth was inserted or not (if it was a new listing)
    is_new = (ifInserted.rowcount == 1)
    conn.close()
    return is_new


def update_market_price(item_id, price):
    conn = get_connection()
    conn.execute(
        "UPDATE items SET market_price_usd = ? WHERE id = ?",
        (price,item_id)
    )
    conn.commit()
    conn.close()
    print("Market price updated!")

def get_item_id(brand_name,item_name):
    conn = get_connection()
    brand_id = conn.execute(
        "SELECT id FROM brands WHERE name = ?",
        (brand_name,)
    ).fetchone()["id"]
    item_id = conn.execute(
        "SELECT id FROM items WHERE brand_id = ? and name = ?",
        (brand_id,item_name)
    ).fetchone()["id"]
    conn.close()
    # no conn commit bc we are accessing only, not changing.
    return item_id


