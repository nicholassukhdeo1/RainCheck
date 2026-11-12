# RC-22 index decision: EXPLAIN QUERY PLAN evidence

Decision: **no index is added.**
Three candidates were tried, each failed the acceptance rule's 25 percent improvement test for its target query, and migration 5 keeps exactly its six `ALTER TABLE` statements.
The numbers and plans below are copied from the benchmark output and are not rounded beyond what is printed.

## Method

- SQLite version: `3.54.1` (`sqlite3.sqlite_version` of the project's `.venv`, Python 3.15.2).
- Machine: Apple M4, 10 cores, 16 GB RAM, macOS (Darwin 24.6.0), local SSD.
  The machine was in normal interactive use, so timings carry noise, which is why each measurement is repeated and interleaved as described below.
- Database: `python3 tests/tools/build_bench_db.py <path>` built into a scratch directory outside the repository (never in the repo, never `src/raincheck.db`).
  It has 5 brands, 200 items (180 active), 500,000 `ebay_listing_prices` rows, 50,000 `listings`, and 55,000 `listing_matches` rows.
  `ANALYZE` was never run, so plans reflect production statistics (none).
  File size 78196736 bytes.
- Query timing: for each query file in `src/sql/` with the parameters listed in the tables, one read-only connection (`file:...?mode=ro`), 3 untimed warm-up executions, then 15 timed executions.
  The timer is `time.perf_counter()` around `conn.execute(sql, params).fetchall()`, so it includes preparing the statement, running it, and fetching every row.
  The measurement for that query is the median of the 15 timed runs.
- Repetition: the whole benchmark was run in 3 interleaved rounds (base, A1, A2, A3, then again, then again), so drift in machine load hits every variant equally.
  The tables show the three per-round medians and, in bold, the median of those three.
  The acceptance rule uses that median of three round medians.
- Write batches: one `db.record_ebay_listing_prices` call inserting 50 new rows for item 1 (item 1 already has 2,500 rows, so the call also reads and sorts the full pool), and one `db.record_listing_matches` call with 10 new deals, each timed with `time.perf_counter()` around the single call on a fresh copy of the database file (the copy is outside the timer).
  21 repetitions per round, median per round, then the median of the three rounds.
  Write timings include SQLite's commit and fsync, which vary much more between runs than read timings do.
- Candidate databases: a byte copy of the base file plus one `CREATE INDEX`, with no `ANALYZE`.
- Plans are the `detail` column of `EXPLAIN QUERY PLAN`, indented by `parent`, exactly as SQLite printed them.
  SQLite does not promise the plan text stays stable between releases, and no test asserts it.
- One benchmark artifact: `deal_discount_mismatch` returns 44,000 rows on this database because the builder stores random discounts that do not match their prices.
  That is the heaviest realistic output for that query, so its timing is still a fair worst case.

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
| report (defaults) | 5 | 59.39 | 57.04 | 55.61 | **57.04** |
| report --since 2026-06-01 | 5 | 26.08 | 24.50 | 24.40 | **24.50** |
| trends (defaults) | 1800 | 1069.89 | 1047.86 | 1014.10 | **1047.86** |
| trends --item 1 | 10 | 25.28 | 21.87 | 20.89 | **21.87** |
| doctor dangling_reference | 0 | 18.59 | 17.34 | 19.27 | **18.59** |
| doctor listing_brand_mismatch | 0 | 7.23 | 7.13 | 15.57 | **7.23** |
| doctor noncanonical_timestamp | 0 | 119.48 | 144.38 | 106.70 | **119.48** |
| doctor deal_discount_mismatch | 44000 | 23.08 | 24.09 | 21.71 | **23.08** |
| doctor no_include_keywords | 0 | 0.08 | 0.08 | 0.08 | **0.08** |
| doctor no_usable_price | 0 | 0.01 | 0.01 | 0.01 | **0.01** |
| doctor thin_ebay_pool | 0 | 26.12 | 26.53 | 25.26 | **26.12** |

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
SEARCH brands USING INTEGER PRIMARY KEY (rowid=?)
SEARCH item_keywords USING INDEX sqlite_autoindex_item_keywords_1 (item_id=?) LEFT-JOIN
```

### doctor no_usable_price

```text
SCAN items
SEARCH brands USING INTEGER PRIMARY KEY (rowid=?)
```

### doctor thin_ebay_pool

```text
SCAN items
SEARCH brands USING INTEGER PRIMARY KEY (rowid=?)
SEARCH ebay_listing_prices USING COVERING INDEX sqlite_autoindex_ebay_listing_prices_1 (item_id=?) LEFT-JOIN
USE TEMP B-TREE FOR ORDER BY
```

## Targets

The large tables are `ebay_listing_prices` (500,000 rows) and `listing_matches` (55,000 rows).
Plan steps that touch them and match the spec's target definition:

- `trends (defaults)`, the slowest query by far, reads all 500,000 `ebay_listing_prices` rows (every active item's whole history, because the window functions need it) and shows `USE TEMP B-TREE FOR LAST TERM OF ORDER BY` in the innermost subquery, then two more `USE TEMP B-TREE FOR ORDER BY` steps for the other window orderings and the final sort.
  `trends --item 1` shows the same plan shape, and its `SCAN ... USING INDEX` reads the whole index because the optional-parameter `WHERE` (an `OR` between the all-items branch and `items.id = :item_id`) cannot be turned into an index search on `item_id`.
- `report (defaults)` and `report --since` show `SCAN listing_matches` with no index, inside the materialized `brand_pairs` step, and `USE TEMP B-TREE FOR ORDER BY` over the five brand rows (negligible).
- `doctor noncanonical_timestamp` shows `SCAN` of `listings` (twice), `listing_matches`, and `ebay_listing_prices`, which is inherent: the check must read every timestamp, and no index can skip rows that must all be inspected.
- The other doctor checks take between 0.01 and about 26 ms and already use covering or primary-key index access.

## Attempt 1: `idx_ebay_listing_prices_item_seen_id`

```sql
CREATE INDEX idx_ebay_listing_prices_item_seen_id ON ebay_listing_prices (item_id, first_seen_at, ebay_listing_id)
```

- Aimed at: `trends (defaults)` (the slowest target query), specifically its `USE TEMP B-TREE FOR LAST TERM OF ORDER BY`.
- Predicted plan change: the `LAST TERM OF ORDER BY` temporary b-tree under `SCAN ebay_listing_prices` disappears, because the index already delivers `(item_id, first_seen_at, ebay_listing_id)` order.
- Existing index relationship: the existing `idx_ebay_listing_prices_item_seen (item_id, first_seen_at)` becomes a strict prefix of this index and therefore redundant.  It is kept (the spec's default), which is why the file grows by the full size of the new index.
- File size: 78196736 bytes before, 101814272 bytes after (+23617536).

### Timings (median ms; the three per-round medians in brackets; change against the base median of three)

| Query | Base | With index | Change |
| --- | ---: | ---: | ---: |
| report (defaults) | **57.04** [59.39, 57.04, 55.61] | **62.61** [73.77, 62.61, 55.84] | +9.8% |
| report --since 2026-06-01 | **24.50** [26.08, 24.50, 24.40] | **25.22** [25.45, 25.22, 24.26] | +2.9% |
| trends (defaults) | **1047.86** [1069.89, 1047.86, 1014.10] | **964.30** [1009.54, 964.30, 930.32] | -8.0% |
| trends --item 1 | **21.87** [25.28, 21.87, 20.89] | **19.72** [20.11, 19.72, 19.45] | -9.9% |
| doctor dangling_reference | **18.59** [18.59, 17.34, 19.27] | **18.00** [18.68, 18.00, 16.60] | -3.2% |
| doctor listing_brand_mismatch | **7.23** [7.23, 7.13, 15.57] | **6.78** [7.34, 6.78, 6.74] | -6.3% |
| doctor noncanonical_timestamp | **119.48** [119.48, 144.38, 106.70] | **106.78** [109.98, 104.84, 106.78] | -10.6% |
| doctor deal_discount_mismatch | **23.08** [23.08, 24.09, 21.71] | **23.03** [24.85, 21.88, 23.03] | -0.2% |
| doctor no_include_keywords | **0.08** [0.08, 0.08, 0.08] | **0.08** [0.08, 0.08, 0.08] | +2.0% |
| doctor no_usable_price | **0.01** [0.01, 0.01, 0.01] | **0.01** [0.01, 0.01, 0.01] | -0.4% |
| doctor thin_ebay_pool | **26.12** [26.12, 26.53, 25.26] | **25.83** [27.09, 25.59, 25.83] | -1.1% |
| write: 50-row `record_ebay_listing_prices` batch | **3.74** [3.74, 3.94, 3.09] | **3.40** [4.59, 3.40, 2.89] | -9.0% |
| write: 10-deal `record_listing_matches` batch | **1.97** [2.04, 1.97, 1.73] | **1.50** [1.90, 1.50, 1.37] | -24.1% |

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
- File size: 78196736 bytes before, 106332160 bytes after (+28135424).

### Timings (median ms; the three per-round medians in brackets; change against the base median of three)

| Query | Base | With index | Change |
| --- | ---: | ---: | ---: |
| report (defaults) | **57.04** [59.39, 57.04, 55.61] | **60.67** [60.67, 60.51, 69.31] | +6.4% |
| report --since 2026-06-01 | **24.50** [26.08, 24.50, 24.40] | **24.83** [25.79, 24.52, 24.83] | +1.3% |
| trends (defaults) | **1047.86** [1069.89, 1047.86, 1014.10] | **935.93** [945.21, 906.72, 935.93] | -10.7% |
| trends --item 1 | **21.87** [25.28, 21.87, 20.89] | **16.51** [16.52, 16.51, 16.41] | -24.5% |
| doctor dangling_reference | **18.59** [18.59, 17.34, 19.27] | **16.94** [16.91, 17.40, 16.94] | -8.9% |
| doctor listing_brand_mismatch | **7.23** [7.23, 7.13, 15.57] | **6.61** [7.00, 6.61, 6.51] | -8.6% |
| doctor noncanonical_timestamp | **119.48** [119.48, 144.38, 106.70] | **108.60** [110.67, 108.60, 106.75] | -9.1% |
| doctor deal_discount_mismatch | **23.08** [23.08, 24.09, 21.71] | **21.95** [22.35, 21.88, 21.95] | -4.9% |
| doctor no_include_keywords | **0.08** [0.08, 0.08, 0.08] | **0.08** [0.08, 0.08, 0.09] | +3.0% |
| doctor no_usable_price | **0.01** [0.01, 0.01, 0.01] | **0.01** [0.01, 0.01, 0.01] | +1.3% |
| doctor thin_ebay_pool | **26.12** [26.12, 26.53, 25.26] | **25.36** [24.92, 25.81, 25.36] | -2.9% |
| write: 50-row `record_ebay_listing_prices` batch | **3.74** [3.74, 3.94, 3.09] | **3.40** [3.40, 3.13, 3.86] | -9.1% |
| write: 10-deal `record_listing_matches` batch | **1.97** [2.04, 1.97, 1.73] | **1.40** [1.40, 1.46, 1.38] | -29.1% |

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
- File size: 78196736 bytes before, 81231872 bytes after (+3035136).

### Timings (median ms; the three per-round medians in brackets; change against the base median of three)

| Query | Base | With index | Change |
| --- | ---: | ---: | ---: |
| report (defaults) | **57.04** [59.39, 57.04, 55.61] | **47.78** [48.02, 47.78, 46.40] | -16.2% |
| report --since 2026-06-01 | **24.50** [26.08, 24.50, 24.40] | **20.57** [20.57, 20.25, 21.03] | -16.1% |
| trends (defaults) | **1047.86** [1069.89, 1047.86, 1014.10] | **1064.36** [1064.36, 1087.54, 1010.78] | +1.6% |
| trends --item 1 | **21.87** [25.28, 21.87, 20.89] | **20.80** [20.80, 20.27, 21.22] | -4.9% |
| doctor dangling_reference | **18.59** [18.59, 17.34, 19.27] | **17.19** [19.57, 16.55, 17.19] | -7.5% |
| doctor listing_brand_mismatch | **7.23** [7.23, 7.13, 15.57] | **6.90** [6.90, 6.66, 6.97] | -4.7% |
| doctor noncanonical_timestamp | **119.48** [119.48, 144.38, 106.70] | **109.24** [109.96, 109.24, 108.11] | -8.6% |
| doctor deal_discount_mismatch | **23.08** [23.08, 24.09, 21.71] | **22.04** [22.04, 22.14, 21.91] | -4.5% |
| doctor no_include_keywords | **0.08** [0.08, 0.08, 0.08] | **0.08** [0.08, 0.08, 0.08] | +0.5% |
| doctor no_usable_price | **0.01** [0.01, 0.01, 0.01] | **0.01** [0.01, 0.01, 0.01] | -1.7% |
| doctor thin_ebay_pool | **26.12** [26.12, 26.53, 25.26] | **25.77** [25.84, 24.97, 25.77] | -1.3% |
| write: 50-row `record_ebay_listing_prices` batch | **3.74** [3.74, 3.94, 3.09] | **2.82** [2.76, 3.16, 2.82] | -24.5% |
| write: 10-deal `record_listing_matches` batch | **1.97** [2.04, 1.97, 1.73] | **1.71** [1.60, 1.96, 1.71] | -13.5% |

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

| Attempt | Target query | Plan changed as predicted | Target change in median | At least 25 percent faster | Slowest other change |
| --- | --- | --- | ---: | --- | ---: |
| 1 | trends (defaults) | yes | -8.0% | **no** | report (defaults): +9.8% |
| 2 | trends (defaults) | yes | -10.7% | **no** | report (defaults): +6.4% |
| 3 | report (defaults) | yes | -16.2% | **no** | trends (defaults): +1.6% |

Every attempt changed the plan the way it predicted, but none came near the 25 percent threshold on its target, so the other two parts of the rule were never the deciding factor.
The write-batch rows in the tables above are not evidence about index cost either way: they vary by tens of percent between rounds (fsync dominated), and the indexed variants even appear faster, which an extra index cannot cause.

## Decision

No index.
`MIGRATIONS[5]` in `src/db.py` keeps exactly six `ALTER TABLE listing_matches ADD COLUMN` statements, and `src/schema.sql` gains no index, so fresh and migrated version 5 databases still have identical index names.

What the evidence shows, without guessing beyond it:

- With attempts 1 and 2 the plan for `trends (defaults)` still contains two `USE TEMP B-TREE FOR ORDER BY` steps (one inside the window-function subqueries and one for the final sort), and the query still pushes 450,000 rows (180 active items times 2,500 observations) through three nested window subqueries.
  Removing the innermost `LAST TERM OF ORDER BY` sort, and in attempt 2 also the table lookups, bought 8.0 and 10.7 percent, well short of 25.
  The remaining sorts are probably needed because the reverse-order `ROW_NUMBER` uses a different order from `LAG` and the rolling average, but this was not tested separately.
- With attempt 3 the `report` plan changed to a covering-index scan, as predicted, but `report --since` stays a scan, because `:since IS NULL OR matched_at >= :since` is an `OR` that SQLite cannot turn into a range search.
  The gain on both report queries was about 16 percent, and the absolute saving is about 9 ms on a rarely run command.
- The write-batch rows are too noisy to measure a few percent of index maintenance, but they are not what failed the candidates.

A different kind of fix, for example rewriting `trends` so every window shares one ordering or so only the latest rows per item are windowed, would change a query rather than the schema.
That is outside this spec's index process and could be proposed as a separate backlog item.
