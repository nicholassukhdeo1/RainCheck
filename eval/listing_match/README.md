# Listing recognition evaluation data

This folder holds the inputs for `src/eval_matching.py`.
You create two files here: `items.json` and `labels.jsonl`.
Neither is created by agents, because the labels carry your judgment about what counts as a match.

## items.json

The item snapshot uses the same format as `src/catalog.json`, and it is checked with the same validator.
Copy the entries you want to evaluate from the catalog.
Items get IDs 1, 2, 3, and so on in file order, and that order is also the keyword arm's tie-break order, like the daily job.
A snapshot file keeps every evaluation reproducible without a database.

## labels.jsonl

One JSON object per line.
Blank lines are ignored.

| Key | Required | Rule |
| --- | --- | --- |
| `brand` | yes | A brand name from `items.json`. |
| `title` | yes | The title exactly as seen on Yahoo. |
| `expected` | yes | An item name of that brand, or `null` when the title is none of your tracked items. |
| `tags` | no | A list of non-blank strings, compared in lowercase, used for per-tag slices. |
| `notes` | no | A string for you only. |

Any other key is an error.
The `expected` key must always be present, so "not labeled yet" can never be confused with `null`.
Two lines with the same brand and the same stripped title are an error, because a duplicate would count twice.
Every problem is reported at once with its line number.

Example lines:

```json
{"brand": "Rick Owens", "title": "Rick Owens ラモーンズ ブラック 42", "expected": "rick owens ramones", "tags": ["katakana"]}
{"brand": "Rick Owens", "title": "Rick Owens Ramones laces only", "expected": null, "tags": ["accessory"]}
```

## Tag vocabulary

| Tag | Use it for |
| --- | --- |
| `katakana` | The item is named in katakana or another Japanese spelling. |
| `misspelling` | The item name is misspelled. |
| `model_code` | The item is only identified by a model or style code. |
| `accessory` | A part, box, or lace that mentions the item but is not the item. |
| `lookalike` | A different product, or a "style" imitation, that resembles the item. |
| `bundle` | The title mentions more than one tracked item. |
| `english` | A plain English title, as a baseline. |
| `injection` | A title that tries to instruct the model; add a few so robustness is measured. |

Bundle rule: when a title mentions two tracked items, label the item you would want to be alerted about, and tag it `bundle`.

## How to run

Run from the repository root.
The keyword arm is free and offline:

```bash
python3 src/eval_matching.py
python3 src/eval_matching.py --show-errors
```

`--show-errors` also prints the titles some arm got wrong.
Without it, no title text appears in the output, so a report is safe to share.

The LLM arms call the paid API.
`--llm` alone only prints the request count and an upper-bound cost, then stops:

```bash
python3 src/eval_matching.py --llm
python3 src/eval_matching.py --llm --confirm-spend
python3 src/eval_matching.py --llm --confirm-spend --model claude-haiku-5-5 --effort low --prompt listing_match_v2
```

`--confirm-spend` is required to call the API, and a run is refused when the upper bound is over `--max-usd` (default 3.00).
`ANTHROPIC_API_KEY` is read from the environment or `.env`.

Each paid run saves its predictions under `eval/runs/` (ignored by git), or at `--save-predictions PATH`.
Recompute every metric from a saved file, offline and for free:

```bash
python3 src/eval_matching.py --from-predictions eval/runs/<file>.json
```

That fails if the labels or items changed since the run.
Create a new prompt version (`listing_match_v2.md`) instead of editing one that already produced predictions.
