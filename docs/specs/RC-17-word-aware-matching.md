# RC-17: Word-aware keyword matching

**Status:** Draft
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
- Measure old and new decisions on every title in both saved live fixtures using the real catalog keywords.
- Update matcher tests that currently pin the old substring and unnormalized-keyword behavior.

**Out of scope:**

- Changing Yahoo or eBay search queries, API calls, parsing, prices, or deal thresholds.
- Editing `src/catalog.json`, reseeding the database, or automatically adding singular/plural keyword variants.
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

### Files touched

| File | Change |
| --- | --- |
| `src/matching.py` | Implement normalization and edge-aware literal matching in `title_matches`, with any private helper kept here. |
| `tests/test_matching.py` | Replace obsolete RC-03b expectations and add explicit Unicode, boundary, punctuation, empty-input, and live-fixture comparisons. |
| `docs/specs/RC-17-word-aware-matching.md` | Record the approved behavior and measured fixture impact. |

`src/yahoo_scrape.py`, `src/ebay_pricing.py`, `src/db.py`, `src/schema.sql`, and `src/catalog.json` are read for compatibility but need no edits.
Both jobs already call `title_matches` with the same three arguments.

### Schema changes and migration

None.
Existing `raincheck.db` stays at schema version 1 and requires no migration or reseed because the matcher normalizes keywords loaded from existing rows at use time.

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

## Edge cases

- Empty results: no Yahoo listings or an eBay response with no `itemSummaries` produces no match decisions; an empty include iterable returns `False`.
- `None` or missing values: `title_matches` expects strings; `None` title or keyword raises `TypeError`, and existing caller validation remains responsible for missing fields.
- Network failure or bad HTTP status: matcher has no network access; each job retains its present timeout and HTTP error behavior.
- Malformed input: Yahoo malformed HTML remains handled by `parse_listings`; malformed keyword strings are outside the matcher's type contract; a blank string keyword is ignored.
- Duplicates: repeated keywords do not change the boolean decision; Yahoo's existing duplicate auction ID handling remains unchanged.
- Unicode: NFKC is applied before lowercase, and Japanese adjacency is treated as a Latin boundary without transliterating Japanese text.

## Test plan

Use small synthetic titles in `tests/test_matching.py` for all boundary, Unicode, case, punctuation, multiword, empty, and exclude-precedence cases.
Reuse the saved `tests/fixtures/yahoo/brand_page_live.html` through `yahoo_scrape.parse_listings` and `tests/fixtures/ebay/search_live.json` through `json.loads`.
Use `src/catalog.json` only as read-only seed input for the fixture comparison, or its validated `CatalogEntry` values; do not open the real database.
These seed keywords are a reproducible proxy for the fixture measurement; both jobs actually read keywords from the database, which may differ from the current catalog.
For each fixture and tracked item, compare a local copy of the old substring predicate against `title_matches`, assert the old/new counts and exact changed IDs listed above, and fail if a fixture is empty.
Keep existing fake HTTP and throwaway database fixtures for any job-level assertion that needs them; no live network or email is needed.
The fixture comparison measures match selection only; it does not assert email delivery or write a market price.

## Shared-file conflicts

No edits to `src/schema.sql`, `src/db.py`, or `src/catalog.json` are planned, so RC-17 has no shared-file conflict under the project parallelism rule.
If the user decides to add `ramone` to the real catalog as part of this feature, that decision expands scope to `src/catalog.json` and must be coordinated with any other feature touching that file.

## Open questions

- Should the singular eBay title `Ramone High Top` be restored by adding `ramone` as an explicit real-catalog include keyword after the matcher change?
  Recommendation: accept its removal in RC-17 and consider a separate catalog adjustment after reviewing whether singular `Ramone` listings are relevant.
  Tradeoff: the strict rule removes one potentially valid candidate from the saved eBay fixture, while an explicit `ramone` keyword would preserve it but broaden the catalog's matches and add a shared-file edit.
  The effect on an eventual stored market price depends on the pricing estimator and the keywords and listings available when that job runs.

## Rollback

Revert the matcher and its test changes to restore substring behavior; no database restore or migration is needed.
Previously stored eBay prices are not recalculated automatically, so rerun the eBay pricing job with the user's normal credentials if the old matching rule must be reflected in stored market prices.
