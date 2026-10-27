# RC-17: Word-aware keyword matching

**Status:** Approved
**Backlog item:** RC-17 in `docs/BACKLOG.md`

## Summary

Normalize listing titles and include and exclude keywords with Unicode NFKC followed by `str.lower()` in the shared matcher.
Match Latin words at ASCII letter and digit boundaries while retaining substring matching for Japanese keywords.

## Motivation

`src/matching.py:11-18` lowercases only the title and then uses substring checks, so `low` excludes `yellow` and full-width `ＲＡＭＯＮＥＳ` cannot match `ramones`.
Both the Yahoo deal path at `src/yahoo_scrape.py:179` and the eBay price path at `src/ebay_pricing.py:54` use this one function.
`src/db.py:333-350` lowercases catalog keywords before storing them, but the matcher must normalize its own keyword arguments because it is a public function and existing database rows are not NFKC-normalized.

## Scope

**In scope:**

- Apply NFKC, then `str.lower()`, to the title and each keyword at match time.
- Require a Latin keyword's ASCII letter or digit at either outer edge to have no adjacent ASCII letter or digit in the normalized title.
- Preserve literal substring matching for keywords whose outer edges are Japanese characters.
- Keep excludes decisive over includes, and keep one matching include sufficient.
- Add `pod shorts` as an explicit include keyword for the pod shorts catalog item, alongside `pod short` and `ポッドショーツ`.
- Measure old and new decisions on every title in both saved live fixtures using the real catalog keywords.
- Update matcher and real-catalog tests that pin the old substring, unnormalized-keyword, or exact pod shorts keyword behavior.

**Out of scope:**

- Changing Yahoo or eBay search queries, API calls, parsing, prices, or deal thresholds.
- Bulk reseeding the user's existing database or automatically generating singular/plural keyword variants beyond the explicit `pod shorts` addition.
- Japanese word segmentation, transliteration, accent removal, fuzzy matching, synonyms, or stemming.
- Rewriting historical `listings`, `listing_matches`, or stored market prices.

## Design

Normalize each string with `unicodedata.normalize("NFKC", value).lower()`.
Do not use `casefold()`: the backlog asks for lowercasing, and casefolding changes additional Unicode characters beyond the requested behavior.
For each literal occurrence of a normalized keyword, check its two outer edges independently.
If the keyword starts with an ASCII letter or digit, the preceding title character must be absent or outside `[a-z0-9]`.
If the keyword ends with an ASCII letter or digit, the following title character must be absent or outside `[a-z0-9]`.
An edge that is Japanese or punctuation has no boundary condition, so a Japanese-only keyword still matches inside Japanese text.
The rule applies equally to include and exclude keywords, including multiword and mixed-script keywords.
Match the keyword's internal characters literally after NFKC and lowercasing; do not collapse whitespace or equate punctuation with spaces.
Spaces, punctuation (including `_` and `-`), and Japanese characters may separate a Latin word from another Latin word.
Digits are part of a Latin word for boundary purposes, so `ramones42` does not match `ramones`.
For example, `low` does not match `yellow` or `follow`, but matches `LOW`, `low-top`, and `ローlowカット`.
`ramones` matches `ＲＡＭＯＮＥＳ`, `ラモーンズRamones`, and `Ramonesスニーカー`, but not `ramones42` or `superramones`.
`ポッドショーツ` continues to match inside `リックオウエンスポッドショーツ`.
The new `pod shorts` include matches the plural product name as a whole phrase, while the existing `pod short` still matches the singular phrase under the boundary rule.
`src/catalog.json` is seed input only; the jobs continue to read keyword rows from the database.

### Files touched

| File | Change |
| --- | --- |
| `src/matching.py` | Implement normalization and edge-aware literal matching in `title_matches`, with any private helper kept here. |
| `src/catalog.json` | Add `pod shorts` to the pod shorts item's include keywords without removing its two existing includes. |
| `tests/fixtures/catalog.json` | Add `pod shorts` beside the sample fixture's existing `pod short` include so `seeded_db` represents plural matching. |
| `tests/test_matching.py` | Replace obsolete RC-03b expectations and add explicit Unicode, boundary, punctuation, empty-input, and live-fixture comparisons. |
| `tests/test_seed_catalog.py` | Update real-catalog and sample-fixture keyword expectations; verify all three real includes. |
| `tests/test_db.py` | Update exact keyword sets, row counts, and tracked-item tuple for the changed sample fixture. |
| `tests/test_db_cli.py` | Update exact seeded keyword rows and count for the changed sample fixture. |
| `docs/specs/RC-17-word-aware-matching.md` | Record the approved behavior and measured fixture impact. |

`tests/conftest.py` maps `seeded_db` to `tests/fixtures/catalog.json` and needs no edit.
`src/yahoo_scrape.py`, `src/ebay_pricing.py`, `src/db.py`, and `src/schema.sql` are read for compatibility but need no edits.
Both jobs already call `title_matches` with the same three arguments.

### Schema changes and migration

None.
Existing `raincheck.db` stays at schema version 1 and requires no schema migration.
The matcher normalizes keywords loaded from existing rows at use time, but adding `pod shorts` to `src/catalog.json` does not update the user's existing database by itself.
Apply the targeted backup-first data update below after merge to add that one keyword to the real database.
Do not run a full `--seed` for this amendment because it replaces each item's complete keyword set and could erase database-only edits.

### Existing database keyword update after merge

Stop the scheduled jobs and run the following from the repository root on the user's machine after the merged code is in place.
It explicitly opens `src/raincheck.db`, makes a unique SQLite backup in `~/RainCheck-backups`, requires schema version 1 and exactly one `Rick Owens` / `pod shorts` item, and inserts only one include row with bound SQL parameters.
It refuses to overwrite an existing exclude keyword or proceed if keyword rows change between the backup and transaction.
It checks the stored keyword sets and confirms that the new keyword matches a plural title before committing, so a conflicting exclude rolls back the insert.

```sh
python3 - <<'PY'
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import os
import sqlite3
import sys

sys.path.insert(0, str(Path("src").resolve()))
from matching import title_matches

db_path = Path("src/raincheck.db").resolve(strict=True)
backup_dir = Path.home() / "RainCheck-backups"
backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
backup_path = backup_dir / f"raincheck-before-rc17-{stamp}.db"
keyword = "pod shorts"
all_keywords = "SELECT item_id, keyword, kind FROM item_keywords"
find_item = (
    "SELECT items.id FROM items JOIN brands ON brands.id = items.brand_id "
    "WHERE brands.name = ? AND items.name = ?"
)

with closing(sqlite3.connect(db_path.as_uri() + "?mode=rw", uri=True,
                             isolation_level=None)) as conn:
    conn.execute("PRAGMA foreign_keys = ON")
    if conn.execute("PRAGMA user_version").fetchone()[0] != 1:
        raise RuntimeError("Expected schema version 1; database left unchanged")
    matches = conn.execute(find_item, ("Rick Owens", "pod shorts")).fetchall()
    if len(matches) != 1:
        raise RuntimeError("Expected exactly one Rick Owens / pod shorts item")
    item_id = matches[0][0]
    before = set(conn.execute(all_keywords).fetchall())
    if (item_id, keyword, "exclude") in before:
        raise RuntimeError("pod shorts already exists as an exclude keyword")

    fd = os.open(backup_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    with closing(sqlite3.connect(backup_path)) as backup:
        conn.backup(backup)
        if backup.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("Backup integrity check failed; database left unchanged")
        if set(backup.execute(all_keywords).fetchall()) != before:
            raise RuntimeError("Backup differs from source; database left unchanged")

    conn.execute("BEGIN IMMEDIATE")
    try:
        if conn.execute("PRAGMA user_version").fetchone()[0] != 1:
            raise RuntimeError("Schema changed after backup")
        if conn.execute(find_item, ("Rick Owens", "pod shorts")).fetchall() != matches:
            raise RuntimeError("Item changed after backup")
        if set(conn.execute(all_keywords).fetchall()) != before:
            raise RuntimeError("Keywords changed after backup")
        conn.execute(
            "INSERT INTO item_keywords (item_id, keyword, kind) VALUES (?, ?, ?) "
            "ON CONFLICT(item_id, keyword) DO NOTHING",
            (item_id, keyword, "include"),
        )
        after = set(conn.execute(all_keywords).fetchall())
        if after != before | {(item_id, keyword, "include")}:
            raise RuntimeError("Unexpected keyword change")
        rows = conn.execute(
            "SELECT keyword, kind FROM item_keywords WHERE item_id = ? ORDER BY keyword",
            (item_id,),
        ).fetchall()
        includes = tuple(word for word, kind in rows if kind == "include")
        excludes = tuple(word for word, kind in rows if kind == "exclude")
        if keyword not in includes or not title_matches(
            "Rick Owens Pod Shorts", (keyword,), excludes
        ):
            raise RuntimeError("Plural keyword is blocked by the stored excludes")
        if not title_matches("Rick Owens Pod Shorts", includes, excludes):
            raise RuntimeError("Plural title did not match stored keywords")
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise

print(f"Backup: {backup_path}")
print(f"Verified pod shorts includes: {includes}")
PY
```

This is a post-merge user data step, not a schema migration or part of the test suite.
No planner, test-writer, implementer, or verifier should run it against the user's real database.

### Interfaces

Keep `title_matches(title: str, keywords: Iterable[str], exclude_keywords: Iterable[str]) -> bool` and its boolean return contract.
The function remains pure and accepts lists, tuples, and other one-pass iterables without mutating them.
An empty title or no usable include keywords returns `False`.
An empty or whitespace-only keyword after normalization is ignored, so it cannot match every title.
`None` or non-string title or keyword values remain invalid inputs and raise `TypeError`; callers already supply validated strings.
The function does not catch or alter network, database, or parser exceptions from callers.

## Acceptance criteria

1. `title_matches("ＲＡＭＯＮＥＳ", ("ramones",), ())` and `title_matches("Ramones", ("ＲＡＭＯＮＥＳ",), ())` both return `True`.
2. Uppercase keyword arguments work without reseeding: `title_matches("rick owens ramones", ("RAMONES",), ())` returns `True`.
3. `title_matches("Ramones Yellow", ("ramones",), ("low",))` returns `True`, while titles containing a separate `low`, `LOW-top`, or `low_top` return `False` with that exclude.
4. A Latin include `ramones` matches at the start or end of a title and next to whitespace, punctuation, or Japanese text, but does not match `superramones`, `ramones42`, or `ramonesx`.
5. A Latin exclude follows the same boundary rule: `low` rejects `Low Ramones` and `Ramones-low`, but not `Yellow Ramones` or `Follow Ramones`.
6. Japanese `ポッドショーツ` matches inside `リックオウエンスポッドショーツ`; `ローカット` excludes a title containing that Japanese substring without requiring spaces.
7. Multiword `pod short` matches `POD SHORT`, `pod short-42`, and `ポッドpod shortショーツ`, but not `pod shorts` or `pod  short`; NFKC-equivalent full-width spaces normalize according to Unicode NFKC.
8. With an empty include iterable, an empty title, or only empty or whitespace-only include keywords, the result is `False`; empty excludes do not reject an otherwise matching title.
9. If a title matches an include and an exclude, the result is `False`, regardless of iterable order or duplicate keywords.
10. The Yahoo and eBay jobs continue to import and use the same `matching.title_matches` function with no new parameters and no network requests made by the matcher.
11. On `tests/fixtures/yahoo/brand_page_live.html`, the 100 parsed titles with the real `src/catalog.json` keywords have old/new matched counts of 1/1 for Ramones and 0/0 for pod shorts, with no changed title IDs.
12. On `tests/fixtures/ebay/search_live.json`, the 50 titles with the real catalog keywords have old/new matched counts of 9/8 for Ramones and 0/0 for pod shorts; the sole removed match is item `v1|178432099849|0`, titled `Size 41.5EU/8.5US - Rick Owens Textured Leather Ramone High Top`.
13. The validated real catalog's pod shorts item has include keywords `("pod short", "ポッドショーツ", "pod shorts")` in catalog order, and its `pants` exclude and search query remain unchanged.
14. With that catalog keyword tuple, `title_matches("Rick Owens Pod Shorts", includes, ("pants",))` returns `True`, while `title_matches("Rick Owens Pod Shorts", ("pod short",), ())` returns `False`.
15. On a throwaway database seeded from the real catalog, the pod shorts item has all three include rows and the existing exclude row; the suite's `seeded_db` fixture instead has the sample catalog's `pod short` and `pod shorts` includes and `pants` exclude.
16. The sample fixture adds only `pod shorts` to its prior pod shorts keyword set; its Ramones keywords, pod shorts exclude, prices, and queries remain unchanged.

## Edge cases

- Empty results: no Yahoo listings or an eBay response with no `itemSummaries` produces no match decisions; an empty include iterable returns `False`.
- `None` or missing values: `title_matches` expects strings; `None` title or keyword raises `TypeError`, and existing caller validation remains responsible for missing fields.
- Network failure or bad HTTP status: matcher has no network access; each job retains its present timeout and HTTP error behavior.
- Malformed input: Yahoo malformed HTML remains handled by `parse_listings`; malformed keyword strings are outside the matcher's type contract; a blank string keyword is ignored.
- Duplicates: repeated keywords do not change the boolean decision; Yahoo's existing duplicate auction ID handling remains unchanged.
- Unicode: NFKC is applied before lowercase, and Japanese adjacency is treated as a Latin boundary without transliterating Japanese text.

## Test plan

Use small synthetic titles in `tests/test_matching.py` for all boundary, Unicode, case, punctuation, multiword, empty, and exclude-precedence cases.
Add a `pod shorts` title assertion with the validated real-catalog include tuple, and update the existing exact tuple expectation in `tests/test_matching.py`.
Update `tests/test_seed_catalog.py` to assert the three real-catalog includes, unchanged exclude, and unchanged search query.
Sync the explicit `pod shorts` include into `tests/fixtures/catalog.json` because `tests/conftest.py` seeds `seeded_db` from that sample file rather than `src/catalog.json`.
Keep the sample fixture's other values as they are; it intentionally does not mirror every real-catalog keyword.
Update exact seeded keyword sets, row counts, and tracked-item expectations in `tests/test_seed_catalog.py`, `tests/test_db.py`, and `tests/test_db_cli.py` to reflect the sample's one added keyword.
Use only throwaway database fixtures for seed assertions.
Reuse the saved `tests/fixtures/yahoo/brand_page_live.html` through `yahoo_scrape.parse_listings` and `tests/fixtures/ebay/search_live.json` through `json.loads`.
Use `src/catalog.json` only as read-only seed input for the fixture comparison, or its validated `CatalogEntry` values; do not open the real database.
These seed keywords are a reproducible proxy for the fixture measurement; both jobs actually read keywords from the database, which may differ from the current catalog.
Remeasuring the saved fixtures with the amended catalog still gives the counts in criteria 11 and 12; neither fixture contains a newly matched pod shorts title.
For each fixture and tracked item, compare a local copy of the old substring predicate against `title_matches`, assert the old/new counts and exact changed IDs listed above, and fail if a fixture is empty.
Keep existing fake HTTP and throwaway database fixtures for any job-level assertion that needs them; no live network or email is needed.
The fixture comparison measures match selection only; it does not assert email delivery or write a market price.

## Shared-file conflicts

RC-17 now edits `src/catalog.json`, so it must not run in parallel with another feature editing that file.
No edits to `src/schema.sql` or `src/db.py` are planned.
The `ramone` keyword remains outside RC-17 despite the catalog file already being in scope for the approved `pod shorts` addition.

## Open questions

- Should the singular eBay title `Ramone High Top` be restored by adding `ramone` as an explicit real-catalog include keyword after the matcher change?
  Recommendation: accept its removal in RC-17 and consider a separate catalog adjustment after reviewing whether singular `Ramone` listings are relevant.
  Tradeoff: the strict rule removes one potentially valid candidate from the saved eBay fixture, while an explicit `ramone` keyword would preserve it but broaden the catalog's matches and add a shared-file edit.
  The effect on an eventual stored market price depends on the pricing estimator and the keywords and listings available when that job runs.

## Resolved

- 2026-10-27: The user approved RC-17 with the recommended boundary rule and accepted the one removed fixture match.
  Adding an explicit `ramone` catalog keyword remains outside this feature.
- 2026-10-27: The user approved adding `pod shorts` as an explicit include keyword alongside `pod short` and `ポッドショーツ`.
  The change updates catalog seed input but requires the separate backup-first data update above for the user's existing database.

## Amendments

- 2026-10-27: Added the approved plural pod shorts keyword, real-catalog and seeded-data assertions, shared catalog conflict, and targeted existing-database update procedure.
  The live fixture counts remain unchanged under the amended seed keywords.
- 2026-10-27: Added `tests/fixtures/catalog.json` to the planned files because `seeded_db` loads this separate sample catalog through `tests/conftest.py`.
  Syncing only the approved plural keyword keeps database-backed matcher tests representative and requires updating their exact sample keyword counts and tuples.
- 2026-10-27: Moved the post-merge plural-title match check inside the database transaction before `COMMIT`.
  If an existing exclude blocks the new keyword after normalization, the targeted insert rolls back while the backup remains available.
- 2026-10-27: Validate every supplied include and exclude value before matching, so short-circuiting cannot hide a later non-string keyword.
  Added one-pass iterable regression cases for this contract.

## Rollback

Revert the matcher, catalog, and test changes to restore the prior code and seed input; no schema restore or migration is needed.
If the targeted existing-database update was applied, use the saved backup for recovery or remove only the verified `pod shorts` include row for the `Rick Owens` / `pod shorts` item with a new backup and a parameterized `DELETE` bounded by `item_id`, `keyword`, and `kind`.
Previously stored eBay prices are not recalculated automatically, so rerun the eBay pricing job with the user's normal credentials if the old matching rule must be reflected in stored market prices.
