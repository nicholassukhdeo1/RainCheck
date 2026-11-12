# RC-22 index decision: EXPLAIN QUERY PLAN evidence

Decision: **no index is added.**
Three candidates were tried on the project interpreter (Python 3.13.5, SQLite 3.49.1).
Two failed the acceptance rule's 25 percent improvement test for their target query.
The third (attempt 3) passed the 25 percent test for its target, but a write batch measured more than 10 percent slower, so by the rule as written it is rejected; see "Decision" for why that single reading is doubtful and what to do about it.
Migration 5 keeps exactly its six `ALTER TABLE` statements.
The numbers and plans below are generated from the benchmark output and are not rounded beyond two decimals.

An earlier version of this file was measured on the wrong interpreter (Python 3.15 with SQLite 3.54.1) and called it the project's `.venv`; that was wrong and the whole process was rerun.

## Method

- Interpreter: the project's `.venv`, `/Users/nicholassukhdeo/Desktop/RainCheck-main/.venv/bin/python`: Python `3.13.5`, SQLite `3.49.1` (`sqlite3.sqlite_version`).
  An earlier run of this process used a different interpreter (Python 3.15, SQLite 3.54.1) by mistake; it hid a SQL parse failure and its numbers are superseded by this file.
- Machine: Apple M4, 10 cores, 16 GB RAM, macOS (Darwin 24.6.0), local SSD.
  The machine was in normal interactive use, so timings carry noise, which is why each measurement is repeated and interleaved as described below.
- Database: `python tests/tools/build_bench_db.py <path>` built into a scratch directory outside the repository (never in the repo, never `src/raincheck.db`).
  It has 5 brands, 200 items (180 active), 500,000 `ebay_listing_prices` rows, 50,000 `listings`, and 55,000 `listing_matches` rows.
  `ANALYZE` was never run, so plans reflect production statistics (none).
  File size 78184448 bytes.
- Query timing: for each query file in `src/sql/` with the parameters listed in the tables, one read-only connection (`file:...?mode=ro`), 3 untimed warm-up executions, then 15 timed executions.
  The timer is `time.perf_counter()` around `conn.execute(sql, params).fetchall()`, so it includes preparing the statement, running it, and fetching every row.
  The measurement for that query in one round is the median of the 15 timed runs.
- Repetition: the whole benchmark was run in 3 interleaved rounds (base, A1, A2, A3, then again, then again), so drift in machine load hits every variant equally.
  The tables show the three per-round medians in brackets and, in bold, the median of those three.
  The acceptance rule uses that median of three round medians.
- Write batches: one `db.record_ebay_listing_prices` call inserting 50 new rows for item 1 (item 1 already has 2,500 rows, so the call also reads and sorts the full pool), and one `db.record_listing_matches` call with 10 new deals, each timed with `time.perf_counter()` around the single call on a fresh copy of the database file (the copy is outside the timer).
  21 repetitions per round, median per round, then the median of the three rounds.
  Write timings include SQLite's commit and fsync, which vary much more between runs than read timings do.
- Candidate databases: a byte copy of the base file plus one `CREATE INDEX`, with no `ANALYZE`.
- Plans are the `detail` column of `EXPLAIN QUERY PLAN`, indented by `parent`, exactly as SQLite printed them.
  SQLite does not promise the plan text stays stable between releases, and no test asserts it.
- The measurements were taken on the queries as committed in `src/sql/` after the review fixes (including the `LEFT JOIN brands` in three doctor checks).
- Benchmark artifact: `deal_discount_mismatch` returns 44,000 rows on this database because the builder stores random discounts that do not match their prices, so every pair that carries deal-time values (80 percent of the 55,000) is flagged.
  Real data would return few rows or none.
  That row count is an artificial worst case for output size, not a realistic workload; the timing of that check is mostly the cost of producing and fetching that many rows and says little about a real run.

### Parameters per query

| Query | Parameters |
| --- | --- |
| report (defaults) | `min_deals=1`, `since=NULL` |
| report --since 2026-06-01 | `min_deals=1`, `since='2026-06-01'` |
| trends (defaults) | `latest=10`, `window=5`, `item_id=NULL`, `include_inactive=0` |
| trends --item 1 | `latest=10`, `window=5`, `item_id=1`, `include_inactive=0` |
| doctor deal_discount_mismatch | `tolerance_pct=0.001` |
| doctor thin_ebay_pool | `min_observations=5` |
| other doctor checks | none |

## Before: plans and timings with no new index

| Query | Rows returned | Round 1 ms | Round 2 ms | Round 3 ms | Median ms |
| --- | ---: | ---: | ---: | ---: | ---: |
| report (defaults) | 5 | 56.13 | 57.86 | 56.79 | **56.79** |
| report --since 2026-06-01 | 5 | 24.15 | 23.74 | 23.84 | **23.84** |
| trends (defaults) | 1800 | 1033.89 | 1122.64 | 1109.47 | **1109.47** |
| trends --item 1 | 10 | 19.94 | 23.39 | 21.28 | **21.28** |
| doctor dangling_reference | 0 | 17.25 | 17.76 | 19.23 | **17.76** |
| doctor listing_brand_mismatch | 0 | 6.64 | 7.95 | 8.49 | **7.95** |
| doctor noncanonical_timestamp | 0 | 106.74 | 114.92 | 108.68 | **108.68** |
| doctor deal_discount_mismatch | 44000 | 20.37 | 23.08 | 21.15 | **21.15** |
| doctor no_include_keywords | 0 | 0.08 | 0.08 | 0.08 | **0.08** |
| doctor no_usable_price | 0 | 0.01 | 0.01 | 0.01 | **0.01** |
| doctor thin_ebay_pool | 0 | 26.55 | 29.15 | 26.87 | **26.87** |

### report (defaults)

```text
MATERIALIZE brand_pairs
  SCAN listing_matches
  SEARCH items USING INTEGER PRIMARY KEY (rowid=?)
SCAN brands USING COVERING INDEX sqlite_autoindex_brands_1
BLOOM FILTER ON brand_pairs (brand_id=?)
SEARCH brand_pairs USING AUTOMATIC COVERING INDEX (brand_id=?) LEFT-JOIN
USE TEMP B-TREE FOR ORDER BY
```

### report --since 2026-06-01

```text
MATERIALIZE brand_pairs
  SCAN listing_matches
  SEARCH items USING INTEGER PRIMARY KEY (rowid=?)
SCAN brands USING COVERING INDEX sqlite_autoindex_brands_1
BLOOM FILTER ON brand_pairs (brand_id=?)
SEARCH brand_pairs USING AUTOMATIC COVERING INDEX (brand_id=?) LEFT-JOIN
USE TEMP B-TREE FOR ORDER BY
```

### trends (defaults)

```text
CO-ROUTINE observations
  CO-ROUTINE (subquery-3)
    CO-ROUTINE (subquery-4)
      CO-ROUTINE (subquery-5)
        SCAN ebay_listing_prices USING INDEX idx_ebay_listing_prices_item_seen
        SEARCH items USING INTEGER PRIMARY KEY (rowid=?)
        SEARCH brands USING INTEGER PRIMARY KEY (rowid=?)
        USE TEMP B-TREE FOR LAST TERM OF ORDER BY
      SCAN (subquery-5)
    SCAN (subquery-4)
    USE TEMP B-TREE FOR ORDER BY
  SCAN (subquery-3)
SCAN observations
USE TEMP B-TREE FOR ORDER BY
```

### trends --item 1

```text
CO-ROUTINE observations
  CO-ROUTINE (subquery-3)
    CO-ROUTINE (subquery-4)
      CO-ROUTINE (subquery-5)
        SCAN ebay_listing_prices USING INDEX idx_ebay_listing_prices_item_seen
        SEARCH items USING INTEGER PRIMARY KEY (rowid=?)
        SEARCH brands USING INTEGER PRIMARY KEY (rowid=?)
        USE TEMP B-TREE FOR LAST TERM OF ORDER BY
      SCAN (subquery-5)
    SCAN (subquery-4)
    USE TEMP B-TREE FOR ORDER BY
  SCAN (subquery-3)
SCAN observations
USE TEMP B-TREE FOR ORDER BY
```

### doctor dangling_reference

```text
MERGE (UNION ALL)
  LEFT
    MERGE (UNION ALL)
      LEFT
        MERGE (UNION ALL)
          LEFT
            SCAN listing_matches USING COVERING INDEX sqlite_autoindex_listing_matches_1
            SEARCH listings USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN
            USE TEMP B-TREE FOR ORDER BY
          RIGHT
            SCAN listing_matches USING INDEX idx_matches_item
            SEARCH items USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN
            USE TEMP B-TREE FOR ORDER BY
      RIGHT
        SCAN item_keywords USING COVERING INDEX sqlite_autoindex_item_keywords_1
        SEARCH items USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN
        USE TEMP B-TREE FOR ORDER BY
  RIGHT
    MERGE (UNION ALL)
      LEFT
        MERGE (UNION ALL)
          LEFT
            SCAN ebay_listing_prices USING COVERING INDEX sqlite_autoindex_ebay_listing_prices_1
            SEARCH items USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN
            USE TEMP B-TREE FOR ORDER BY
          RIGHT
            SCAN listings USING COVERING INDEX idx_listings_brand_seen
            SEARCH brands USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN
            USE TEMP B-TREE FOR ORDER BY
      RIGHT
        SCAN items USING COVERING INDEX sqlite_autoindex_items_1
        SEARCH brands USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN
        USE TEMP B-TREE FOR ORDER BY
```

### doctor listing_brand_mismatch

```text
SCAN listing_matches USING COVERING INDEX sqlite_autoindex_listing_matches_1
SEARCH listings USING INTEGER PRIMARY KEY (rowid=?)
SEARCH items USING INTEGER PRIMARY KEY (rowid=?)
SEARCH listing_brands USING INTEGER PRIMARY KEY (rowid=?)
SEARCH item_brands USING INTEGER PRIMARY KEY (rowid=?)
```

### doctor noncanonical_timestamp

```text
MERGE (UNION ALL)
  LEFT
    MERGE (UNION ALL)
      LEFT
        SCAN listings USING COVERING INDEX idx_listings_brand_seen
        USE TEMP B-TREE FOR ORDER BY
      RIGHT
        SCAN listings
        USE TEMP B-TREE FOR ORDER BY
  RIGHT
    MERGE (UNION ALL)
      LEFT
        SCAN listing_matches
        USE TEMP B-TREE FOR ORDER BY
      RIGHT
        SCAN ebay_listing_prices
        USE TEMP B-TREE FOR ORDER BY
```

### doctor deal_discount_mismatch

```text
SCAN listing_matches USING INDEX sqlite_autoindex_listing_matches_1
```

### doctor no_include_keywords

```text
SCAN items
SEARCH brands USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN
SEARCH item_keywords USING INDEX sqlite_autoindex_item_keywords_1 (item_id=?) LEFT-JOIN
```

### doctor no_usable_price

```text
SCAN items
SEARCH brands USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN
```

### doctor thin_ebay_pool

```text
SCAN items
SEARCH brands USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN
SEARCH ebay_listing_prices USING COVERING INDEX sqlite_autoindex_ebay_listing_prices_1 (item_id=?) LEFT-JOIN
USE TEMP B-TREE FOR ORDER BY
```

## Targets

The large tables are `ebay_listing_prices` (500,000 rows) and `listing_matches` (55,000 rows).
Plan steps that touch them and match the spec's target definition:

- `trends (defaults)`, the slowest query by far, reads the whole history of every active item (180 items times 2,500 observations, so 450,000 of the 500,000 `ebay_listing_prices` rows, because the window functions need all of it) and shows `USE TEMP B-TREE FOR LAST TERM OF ORDER BY` in the innermost subquery, then two more `USE TEMP B-TREE FOR ORDER BY` steps for the other window orderings and the final sort.
  `trends --item 1` shows the same plan shape, and its `SCAN ... USING INDEX` reads the whole index because the optional-parameter `WHERE` (an `OR` between the all-items branch and `items.id = :item_id`) cannot be turned into an index search on `item_id`.
- `report (defaults)` and `report --since` show `SCAN listing_matches` with no index, inside the materialized `brand_pairs` step, and `USE TEMP B-TREE FOR ORDER BY` over the five brand rows (negligible).
- `doctor noncanonical_timestamp` shows `SCAN` of `listings` (twice), `listing_matches`, and `ebay_listing_prices`, which is inherent: the check must read every timestamp, and no index can skip rows that must all be inspected.
- The other doctor checks take between 0.01 and about 27 ms and already use covering or primary-key index access.
  `deal_discount_mismatch` is dominated by the 44,000 artificial rows described under Method.

## Attempt 1: `idx_ebay_listing_prices_item_seen_id`

```sql
CREATE INDEX idx_ebay_listing_prices_item_seen_id ON ebay_listing_prices (item_id, first_seen_at, ebay_listing_id)
```

- Aimed at: `trends (defaults)` (the slowest target query), specifically its `USE TEMP B-TREE FOR LAST TERM OF ORDER BY`.
- Predicted plan change: the `LAST TERM OF ORDER BY` temporary b-tree under `SCAN ebay_listing_prices` disappears, because the index already delivers `(item_id, first_seen_at, ebay_listing_id)` order.
- Existing index relationship: the existing `idx_ebay_listing_prices_item_seen (item_id, first_seen_at)` becomes a strict prefix of this index and therefore redundant.
  It is kept (the spec's default), which is why the file grows by the full size of the new index.
- File size: 78184448 bytes before, 101801984 bytes after (+23617536).

### Timings (median ms; the three per-round medians in brackets; change against the base median of three)

| Query | Base | With index | Change |
| --- | ---: | ---: | ---: |
| report (defaults) | **56.79** [56.13, 57.86, 56.79] | **56.41** [78.13, 56.41, 54.06] | -0.7% |
| report --since 2026-06-01 | **23.84** [24.15, 23.74, 23.84] | **26.20** [38.51, 26.20, 24.47] | +9.9% |
| trends (defaults) | **1109.47** [1033.89, 1122.64, 1109.47] | **979.72** [964.49, 979.72, 992.22] | -11.7% |
| trends --item 1 | **21.28** [19.94, 23.39, 21.28] | **18.93** [18.85, 18.93, 20.11] | -11.0% |
| doctor dangling_reference | **17.76** [17.25, 17.76, 19.23] | **17.21** [16.55, 17.21, 17.28] | -3.1% |
| doctor listing_brand_mismatch | **7.95** [6.64, 7.95, 8.49] | **7.05** [6.59, 7.05, 7.41] | -11.3% |
| doctor noncanonical_timestamp | **108.68** [106.74, 114.92, 108.68] | **104.60** [100.48, 104.60, 121.84] | -3.8% |
| doctor deal_discount_mismatch | **21.15** [20.37, 23.08, 21.15] | **20.30** [20.30, 20.07, 20.67] | -4.0% |
| doctor no_include_keywords | **0.08** [0.08, 0.08, 0.08] | **0.08** [0.08, 0.08, 0.08] | -4.0% |
| doctor no_usable_price | **0.01** [0.01, 0.01, 0.01] | **0.01** [0.01, 0.01, 0.01] | -4.6% |
| doctor thin_ebay_pool | **26.87** [26.55, 29.15, 26.87] | **26.85** [25.31, 26.85, 29.02] | -0.1% |
| write: 50-row `record_ebay_listing_prices` batch | **2.57** [2.53, 3.34, 2.57] | **2.92** [2.32, 3.12, 2.92] | +13.7% |
| write: 10-deal `record_listing_matches` batch | **1.90** [1.91, 1.90, 1.63] | **1.42** [1.37, 1.76, 1.42] | -25.5% |

### Plans that changed (all others are verbatim identical to the before plans)

#### trends (defaults)

```text
CO-ROUTINE observations
  CO-ROUTINE (subquery-3)
    CO-ROUTINE (subquery-4)
      CO-ROUTINE (subquery-5)
        SCAN ebay_listing_prices USING INDEX idx_ebay_listing_prices_item_seen_id
        SEARCH items USING INTEGER PRIMARY KEY (rowid=?)
        SEARCH brands USING INTEGER PRIMARY KEY (rowid=?)
      SCAN (subquery-5)
    SCAN (subquery-4)
    USE TEMP B-TREE FOR ORDER BY
  SCAN (subquery-3)
SCAN observations
USE TEMP B-TREE FOR ORDER BY
```

#### trends --item 1

```text
CO-ROUTINE observations
  CO-ROUTINE (subquery-3)
    CO-ROUTINE (subquery-4)
      CO-ROUTINE (subquery-5)
        SCAN ebay_listing_prices USING INDEX idx_ebay_listing_prices_item_seen_id
        SEARCH items USING INTEGER PRIMARY KEY (rowid=?)
        SEARCH brands USING INTEGER PRIMARY KEY (rowid=?)
      SCAN (subquery-5)
    SCAN (subquery-4)
    USE TEMP B-TREE FOR ORDER BY
  SCAN (subquery-3)
SCAN observations
USE TEMP B-TREE FOR ORDER BY
```

#### doctor noncanonical_timestamp

```text
MERGE (UNION ALL)
  LEFT
    MERGE (UNION ALL)
      LEFT
        SCAN listings USING COVERING INDEX idx_listings_brand_seen
        USE TEMP B-TREE FOR ORDER BY
      RIGHT
        SCAN listings
        USE TEMP B-TREE FOR ORDER BY
  RIGHT
    MERGE (UNION ALL)
      LEFT
        SCAN listing_matches
        USE TEMP B-TREE FOR ORDER BY
      RIGHT
        SCAN ebay_listing_prices USING COVERING INDEX idx_ebay_listing_prices_item_seen_id
        USE TEMP B-TREE FOR ORDER BY
```

Identical to before: report (defaults), report --since 2026-06-01, doctor dangling_reference, doctor listing_brand_mismatch, doctor deal_discount_mismatch, doctor no_include_keywords, doctor no_usable_price, doctor thin_ebay_pool.

## Attempt 2: `idx_ebay_listing_prices_item_seen_id_price`

```sql
CREATE INDEX idx_ebay_listing_prices_item_seen_id_price ON ebay_listing_prices (item_id, first_seen_at, ebay_listing_id, first_price_usd)
```

- Aimed at: `trends (defaults)` again: the same temporary b-tree, plus the table lookups for `first_price_usd`.
- Predicted plan change: the same temporary b-tree disappears and the scan becomes `SCAN ... USING COVERING INDEX`, because `first_price_usd` is stored in the index too.
- Existing index relationship: the existing `idx_ebay_listing_prices_item_seen` is also a strict prefix of this index (redundant, kept).
- File size: 78184448 bytes before, 106311680 bytes after (+28127232).

### Timings (median ms; the three per-round medians in brackets; change against the base median of three)

| Query | Base | With index | Change |
| --- | ---: | ---: | ---: |
| report (defaults) | **56.79** [56.13, 57.86, 56.79] | **56.03** [55.83, 56.03, 56.83] | -1.3% |
| report --since 2026-06-01 | **23.84** [24.15, 23.74, 23.84] | **23.91** [23.85, 23.91, 25.65] | +0.3% |
| trends (defaults) | **1109.47** [1033.89, 1122.64, 1109.47] | **980.25** [1006.10, 952.11, 980.25] | -11.6% |
| trends --item 1 | **21.28** [19.94, 23.39, 21.28] | **16.87** [18.08, 16.65, 16.87] | -20.7% |
| doctor dangling_reference | **17.76** [17.25, 17.76, 19.23] | **18.69** [26.21, 18.69, 16.86] | +5.2% |
| doctor listing_brand_mismatch | **7.95** [6.64, 7.95, 8.49] | **6.65** [7.91, 6.65, 6.58] | -16.3% |
| doctor noncanonical_timestamp | **108.68** [106.74, 114.92, 108.68] | **108.62** [113.82, 108.62, 104.59] | -0.1% |
| doctor deal_discount_mismatch | **21.15** [20.37, 23.08, 21.15] | **20.62** [20.62, 20.94, 20.12] | -2.5% |
| doctor no_include_keywords | **0.08** [0.08, 0.08, 0.08] | **0.08** [0.08, 0.08, 0.08] | -3.6% |
| doctor no_usable_price | **0.01** [0.01, 0.01, 0.01] | **0.01** [0.01, 0.01, 0.01] | -1.7% |
| doctor thin_ebay_pool | **26.87** [26.55, 29.15, 26.87] | **26.12** [26.12, 26.48, 26.03] | -2.8% |
| write: 50-row `record_ebay_listing_prices` batch | **2.57** [2.53, 3.34, 2.57] | **2.61** [2.61, 2.99, 2.47] | +1.7% |
| write: 10-deal `record_listing_matches` batch | **1.90** [1.91, 1.90, 1.63] | **1.36** [1.36, 1.36, 1.33] | -28.5% |

### Plans that changed (all others are verbatim identical to the before plans)

#### trends (defaults)

```text
CO-ROUTINE observations
  CO-ROUTINE (subquery-3)
    CO-ROUTINE (subquery-4)
      CO-ROUTINE (subquery-5)
        SCAN ebay_listing_prices USING COVERING INDEX idx_ebay_listing_prices_item_seen_id_price
        SEARCH items USING INTEGER PRIMARY KEY (rowid=?)
        SEARCH brands USING INTEGER PRIMARY KEY (rowid=?)
      SCAN (subquery-5)
    SCAN (subquery-4)
    USE TEMP B-TREE FOR ORDER BY
  SCAN (subquery-3)
SCAN observations
USE TEMP B-TREE FOR ORDER BY
```

#### trends --item 1

```text
CO-ROUTINE observations
  CO-ROUTINE (subquery-3)
    CO-ROUTINE (subquery-4)
      CO-ROUTINE (subquery-5)
        SCAN ebay_listing_prices USING COVERING INDEX idx_ebay_listing_prices_item_seen_id_price
        SEARCH items USING INTEGER PRIMARY KEY (rowid=?)
        SEARCH brands USING INTEGER PRIMARY KEY (rowid=?)
      SCAN (subquery-5)
    SCAN (subquery-4)
    USE TEMP B-TREE FOR ORDER BY
  SCAN (subquery-3)
SCAN observations
USE TEMP B-TREE FOR ORDER BY
```

Identical to before: report (defaults), report --since 2026-06-01, doctor dangling_reference, doctor listing_brand_mismatch, doctor noncanonical_timestamp, doctor deal_discount_mismatch, doctor no_include_keywords, doctor no_usable_price, doctor thin_ebay_pool.

## Attempt 3: `idx_matches_matched_item_deal`

```sql
CREATE INDEX idx_matches_matched_item_deal ON listing_matches (matched_at, item_id, deal_kind, deal_discount_pct)
```

- Aimed at: `report (defaults)`, the next-slowest target after two failed attempts at `trends`: the `SCAN listing_matches` inside `brand_pairs`.
- Predicted plan change: `SCAN listing_matches` becomes a scan of a covering index, because the index holds every `listing_matches` column the query reads (`matched_at`, `item_id`, `deal_kind`, `deal_discount_pct`).
- Existing index relationship: no existing `listing_matches` index becomes a prefix: `idx_matches_item (item_id)` and the primary key `(listing_id, item_id)` both start with different columns.
- File size: 78184448 bytes before, 81219584 bytes after (+3035136).

### Timings (median ms; the three per-round medians in brackets; change against the base median of three)

| Query | Base | With index | Change |
| --- | ---: | ---: | ---: |
| report (defaults) | **56.79** [56.13, 57.86, 56.79] | **41.25** [41.07, 41.25, 42.51] | -27.4% |
| report --since 2026-06-01 | **23.84** [24.15, 23.74, 23.84] | **19.63** [19.59, 19.63, 19.66] | -17.6% |
| trends (defaults) | **1109.47** [1033.89, 1122.64, 1109.47] | **1086.19** [1154.12, 1045.11, 1086.19] | -2.1% |
| trends --item 1 | **21.28** [19.94, 23.39, 21.28] | **21.95** [21.95, 20.45, 32.50] | +3.1% |
| doctor dangling_reference | **17.76** [17.25, 17.76, 19.23] | **17.03** [17.26, 17.03, 17.01] | -4.1% |
| doctor listing_brand_mismatch | **7.95** [6.64, 7.95, 8.49] | **6.65** [6.70, 6.57, 6.65] | -16.3% |
| doctor noncanonical_timestamp | **108.68** [106.74, 114.92, 108.68] | **113.96** [113.96, 118.24, 107.29] | +4.9% |
| doctor deal_discount_mismatch | **21.15** [20.37, 23.08, 21.15] | **21.38** [21.25, 22.61, 21.38] | +1.1% |
| doctor no_include_keywords | **0.08** [0.08, 0.08, 0.08] | **0.08** [0.08, 0.09, 0.08] | -2.1% |
| doctor no_usable_price | **0.01** [0.01, 0.01, 0.01] | **0.01** [0.01, 0.01, 0.01] | +3.4% |
| doctor thin_ebay_pool | **26.87** [26.55, 29.15, 26.87] | **26.74** [34.76, 26.74, 25.70] | -0.5% |
| write: 50-row `record_ebay_listing_prices` batch | **2.57** [2.53, 3.34, 2.57] | **2.99** [3.58, 2.57, 2.99] | +16.4% |
| write: 10-deal `record_listing_matches` batch | **1.90** [1.91, 1.90, 1.63] | **1.81** [2.04, 1.81, 1.66] | -4.9% |

### Plans that changed (all others are verbatim identical to the before plans)

#### report (defaults)

```text
MATERIALIZE brand_pairs
  SCAN listing_matches USING COVERING INDEX idx_matches_matched_item_deal
  SEARCH items USING INTEGER PRIMARY KEY (rowid=?)
SCAN brands USING COVERING INDEX sqlite_autoindex_brands_1
BLOOM FILTER ON brand_pairs (brand_id=?)
SEARCH brand_pairs USING AUTOMATIC COVERING INDEX (brand_id=?) LEFT-JOIN
USE TEMP B-TREE FOR ORDER BY
```

#### report --since 2026-06-01

```text
MATERIALIZE brand_pairs
  SCAN listing_matches USING COVERING INDEX idx_matches_matched_item_deal
  SEARCH items USING INTEGER PRIMARY KEY (rowid=?)
SCAN brands USING COVERING INDEX sqlite_autoindex_brands_1
BLOOM FILTER ON brand_pairs (brand_id=?)
SEARCH brand_pairs USING AUTOMATIC COVERING INDEX (brand_id=?) LEFT-JOIN
USE TEMP B-TREE FOR ORDER BY
```

Identical to before: trends (defaults), trends --item 1, doctor dangling_reference, doctor listing_brand_mismatch, doctor noncanonical_timestamp, doctor deal_discount_mismatch, doctor no_include_keywords, doctor no_usable_price, doctor thin_ebay_pool.

## Acceptance rule (spec step 6) applied

The rule has three parts: the target query's plan changes as predicted, its median improves by at least 25 percent, and no other query or write batch gets more than 10 percent slower.

| Attempt | Target query | Plan changed as predicted | Target change in median | At least 25 percent faster | Slowest other change | No other more than 10 percent slower | Accepted |
| --- | --- | --- | ---: | --- | ---: | --- | --- |
| 1 | trends (defaults) | yes | -11.7% | **no** | write: 50-row `record_ebay_listing_prices` batch: +13.7% | no | **no** |
| 2 | trends (defaults) | yes | -11.6% | **no** | doctor dangling_reference: +5.2% | yes | **no** |
| 3 | report (defaults) | yes | -27.4% | **yes** | write: 50-row `record_ebay_listing_prices` batch: +16.4% | no | **no** |

The write-batch rows are the noisiest measurements here: each includes a commit and fsync, and they are single calls on a fresh file copy.

### Noise check on the write batches

To see how large that noise is, the write measurement was repeated afterwards with the same method (3 interleaved rounds of 21 repetitions, median of round medians) on the base file and three byte-identical copies of it, `N1` to `N3`.
No index differs between them, so any difference is noise.

| Write batch | base | N1 | N2 | N3 |
| --- | ---: | ---: | ---: | ---: |
| 50-row `record_ebay_listing_prices` | 2.61 ms | 2.73 ms (+4.5%) | 2.37 ms (-9.3%) | 2.55 ms (-2.5%) |
| 10-deal `record_listing_matches` | 1.62 ms | 1.59 ms (-1.7%) | 1.57 ms (-2.8%) | 1.72 ms (+6.0%) |

Identical files differed by up to about 9 percent in this check, so a 10 percent slowdown threshold on a single write reading is close to the noise floor.

## Decision

No index.
`MIGRATIONS[5]` in `src/db.py` keeps exactly six `ALTER TABLE listing_matches ADD COLUMN` statements, and `src/schema.sql` gains no index, so fresh and migrated version 5 databases still have identical index names.

What the evidence shows, without guessing beyond it:

- Attempts 1 and 2 changed the `trends (defaults)` plan as predicted (the innermost `LAST TERM OF ORDER BY` sort disappeared, and attempt 2 also removed the table lookups), but the query still contains two `USE TEMP B-TREE FOR ORDER BY` steps and still pushes 450,000 rows through three nested window subqueries.
  The gain was about 12 percent either way, well short of 25.
  The remaining sorts are probably needed because the reverse-order `ROW_NUMBER` uses a different order from `LAG` and the rolling average, but this was not tested separately.
- Attempt 3 changed the `report` plan to a covering-index scan as predicted and improved `report (defaults)` by more than 25 percent.
  `report --since` improved by a smaller amount, and its plan still scans: `:since IS NULL OR matched_at >= :since` is an `OR` that SQLite cannot turn into a range search, so the index is read in full rather than searched.
  The other target criteria all hold, and the only failing criterion is that the 50-row `record_ebay_listing_prices` batch read more than 10 percent slower with this index.
  That reading is hard to believe as an effect of the index: the index is on `listing_matches`, which that call never writes.
  The noise check above shows identical files differ by several percent, and attempt 2, which adds a larger index to the table that call does write, read only a few percent slower.
  Still, the rule as written fails on the recorded measurement, and it was applied as written, not adjusted afterwards to fit.
- The absolute gain for attempt 3 is about 15 ms on a command that is run by hand, at a cost of about 3 MB of file size and one more index to keep in step in two files.

Attempt 3 is the one candidate that is close.
If the user wants it, the honest path is a separate, pre-agreed change to the process (for example 100 or more write repetitions per variant, or a noise-based threshold), decided before looking at the result, and then a rerun, not a reinterpretation of this table.
That would be a new decision by the user, not something this file approves.

A different kind of fix, for example rewriting `trends` so every window shares one ordering or so only the latest rows per item are windowed, would change a query rather than the schema.
That is outside this spec's index process and could be proposed as a separate backlog item.
