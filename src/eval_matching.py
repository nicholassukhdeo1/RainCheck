"""Offline evaluation of listing recognition: keyword matching versus the LLM classifier.

Without --llm this runs fully offline and scores only today's keyword matcher.
With --llm it first prints a worst-case cost and does nothing else until --confirm-spend
is given.  Saved predictions let every metric be recomputed later without spending again.

Titles are seller text from real listings, so the report never prints them unless
--show-errors is given; that keeps a report safe to paste into a pull request.
"""
import argparse
import hashlib
import json
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

import db
import listing_classifier
import llm_client
from matching import title_matches

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LABELS = REPO_ROOT / "eval" / "listing_match" / "labels.jsonl"
DEFAULT_ITEMS = REPO_ROOT / "eval" / "listing_match" / "items.json"
DEFAULT_RUNS_DIR = REPO_ROOT / "eval" / "runs"

# Higher rank means more confident.  A threshold keeps decisions at or above its rank.
CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}
ARMS = ("keyword", "llm", "hybrid", "union")
LABEL_KEYS = {"brand", "title", "expected", "tags", "notes"}
# Stage 1 of the request cost estimate: the API adds its own structured-output system prompt.
STRUCTURED_OUTPUT_OVERHEAD_TOKENS = 500
DEFAULT_MAX_USD = 3.00


class EvalError(Exception):
    """A runtime problem the user can fix; main() prints it as one `Error:` message, exit 1."""


@dataclass(frozen=True)
class EvalItem:
    id: int
    name: str
    brand: str
    keywords: tuple
    exclude_keywords: tuple


@dataclass(frozen=True)
class Label:
    line: int
    brand: str
    title: str
    expected: int | None  # item ID, or None for "none of my tracked items"
    tags: tuple


@dataclass(frozen=True)
class LlmResult:
    """What the model said about one title, or that its batch failed."""
    ok: bool
    failure: str | None = None
    item_id: int | None = None
    confidence: str | None = None
    evidence: str | None = None


# ---------------------------------------------------------------------------
# Loading and validating the inputs
# ---------------------------------------------------------------------------

def _read_bytes(path, what):
    try:
        return Path(path).read_bytes()
    except OSError as exc:
        raise EvalError(f"cannot read {what} {path}: {exc.strerror or exc}") from exc


def _decode(raw, what):
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EvalError(f"{what} is not valid UTF-8") from exc


def load_items(path):
    """Returns (items, sha256).  Uses the same validator as catalog.json, so messages match."""
    raw = _read_bytes(path, "items file")
    try:
        entries = json.loads(_decode(raw, "items file"))
    except ValueError as exc:
        raise EvalError(f"items file is not valid JSON: {exc}") from exc
    try:
        catalog = db.validate_catalog(entries)
    except db.CatalogError as exc:
        raise EvalError(str(exc)) from exc
    # Synthetic IDs in file order; that order is also the keyword arm's tie-break order.
    items = [
        EvalItem(number, entry.name, entry.brand, entry.keywords, entry.exclude_keywords)
        for number, entry in enumerate(catalog, start=1)
    ]
    return items, hashlib.sha256(raw).hexdigest()


def _label_problems(number, row, items_by_brand, seen):
    """Problems with one parsed label line, plus the Label when it is clean."""
    problems = []
    if not isinstance(row, dict):
        return ["must be a JSON object"], None

    for key in row:
        if key not in LABEL_KEYS:
            problems.append(f"unknown key {key!r}")
    for key in ("brand", "title", "expected"):
        if key not in row:
            problems.append(f"missing required key {key!r}")

    brand, title, expected = row.get("brand"), row.get("title"), row.get("expected")
    brand_items = None
    if "brand" in row:
        if not isinstance(brand, str) or not brand.strip():
            problems.append("brand must be a non-blank string")
        elif brand not in items_by_brand:
            problems.append(f"unknown brand {brand!r} (not in the item snapshot)")
        else:
            brand_items = items_by_brand[brand]
    title_ok = isinstance(title, str) and bool(title.strip())
    if "title" in row and not title_ok:
        problems.append("title must be a non-blank string")

    expected_id = None
    if "expected" in row and expected is not None:
        if not isinstance(expected, str):
            problems.append("expected must be an item name or null")
        elif brand_items is not None:
            matches = [item.id for item in brand_items if item.name == expected]
            if matches:
                expected_id = matches[0]
            else:
                problems.append(f"expected {expected!r} is not an item of brand {brand!r}")

    tags = row.get("tags", [])
    if "tags" in row and not (
        isinstance(tags, list) and all(isinstance(t, str) and t.strip() for t in tags)
    ):
        problems.append("tags must be a list of non-blank strings")
        tags = []
    if "notes" in row and not isinstance(row["notes"], str):
        problems.append("notes must be a string")

    # A duplicate would count twice in every metric.  The stripped title is the identity.
    if brand_items is not None and title_ok:
        key = (brand, title.strip())
        if key in seen:
            problems.append("duplicate of an earlier label (same brand and title)")
        seen.add(key)

    if problems:
        return problems, None
    return [], Label(number, brand, title, expected_id, tuple(sorted({t.strip().lower() for t in tags})))


def load_labels(path, items):
    """Returns (labels, sha256).  Reports every problem at once, with line numbers."""
    raw = _read_bytes(path, "labels file")
    text = _decode(raw, "labels file")
    items_by_brand = {}
    for item in items:
        items_by_brand.setdefault(item.brand, []).append(item)

    labels, problems, seen = [], [], set()
    # split("\n"), not splitlines(): U+2028 and friends may legally appear inside a title.
    for number, line in enumerate(text.split("\n"), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            problems.append(f"- line {number}: invalid JSON")
            continue
        found, label = _label_problems(number, row, items_by_brand, seen)
        problems.extend(f"- line {number}: {message}" for message in found)
        if label is not None:
            labels.append(label)

    if problems:
        raise EvalError("labels file is invalid:\n" + "\n".join(problems))
    if not labels:
        raise EvalError("no labels found")
    return labels, hashlib.sha256(raw).hexdigest()


# ---------------------------------------------------------------------------
# Predictions per arm
# ---------------------------------------------------------------------------

def keyword_predictions(labels, items):
    """First matching item of the label's brand, in snapshot order (like the daily job)."""
    predictions, multi = [], 0
    for label in labels:
        matched = [
            item.id for item in items
            if item.brand == label.brand
            and title_matches(label.title, item.keywords, item.exclude_keywords)
        ]
        multi += len(matched) > 1
        predictions.append(matched[0] if matched else None)
    return predictions, multi


def _at_threshold(result, level):
    """An LLM decision below the threshold counts as none (it does not fall back)."""
    if CONFIDENCE_RANK[result.confidence] < CONFIDENCE_RANK[level]:
        return None
    return result.item_id


def arm_predictions(arm, keyword, llm, level="low"):
    if arm == "keyword":
        return list(keyword)
    predictions = []
    for kw, result in zip(keyword, llm):
        decided = _at_threshold(result, level) if result.ok else None
        if arm == "llm":
            predictions.append(decided)
        elif arm == "hybrid":
            # Only a FAILED batch falls back to keywords; a low-confidence answer is "none".
            predictions.append(decided if result.ok else kw)
        else:  # union: the model may add matches but never veto a keyword match
            predictions.append(kw if kw is not None else decided)
    return predictions


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def count(expected, predicted, indices):
    """Micro-averaged (tp, fp, fn); a wrong-item prediction is both an fp and an fn."""
    tp = fp = fn = 0
    for i in indices:
        e, p = expected[i], predicted[i]
        if p is not None:
            if p == e:
                tp += 1
            else:
                fp += 1
        if e is not None and p != e:
            fn += 1
    return tp, fp, fn


def scores(tp, fp, fn):
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    if precision is None or recall is None:
        f1 = None  # n/a, not 0.000: there is nothing to average
    elif precision + recall == 0:
        f1 = 0.0
    else:
        f1 = 2 * precision * recall / (precision + recall)
    return precision, recall, f1


def _fmt(value):
    return "n/a" if value is None else f"{value:.3f}"


_PRF_WIDTH = 9


def _prf(tp, fp, fn):
    return " ".join(f"{_fmt(v):>{_PRF_WIDTH}}" for v in scores(tp, fp, fn))


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def _names(items):
    names = {item.id: item.name for item in items}
    names[None] = "none"
    return names


def print_report(labels, items, keyword, multi, llm, facts, level_arms, show_errors,
                 failed_batches=None, usage=None):
    """Prints every section.  `llm` is None for a keyword-only run; `usage` is (in, out, cost)."""
    names = _names(items)
    expected = [label.expected for label in labels]
    everything = range(len(labels))
    arms = ARMS if llm is not None else ("keyword",)
    predicted = {arm: arm_predictions(arm, keyword, llm) for arm in arms}

    print("== Run facts ==")
    for line in facts:
        print(line)

    print()
    print("== Metrics (micro-averaged over titles) ==")
    print("A wrong-item prediction counts as both a false positive and a false negative.")
    print(f"{'arm':<10} {'tp':>4} {'fp':>4} {'fn':>4} {'precision':>9} {'recall':>9} {'f1':>9}")
    for arm in arms:
        tp, fp, fn = count(expected, predicted[arm], everything)
        print(f"{arm:<10} {tp:>4} {fp:>4} {fn:>4} {_prf(tp, fp, fn)}")

    if llm is not None:
        print()
        print("== Confidence thresholds (a decision below the threshold counts as none) ==")
        print(f"{'arm':<8} {'threshold':<9} {'tp':>4} {'fp':>4} {'fn':>4} {'precision':>9} {'recall':>9} {'f1':>9}")
        for arm in level_arms:
            for level in CONFIDENCE_RANK:
                tp, fp, fn = count(expected, arm_predictions(arm, keyword, llm, level), everything)
                print(f"{arm:<8} {level:<9} {tp:>4} {fp:>4} {fn:>4} {_prf(tp, fp, fn)}")

    for arm in arms:
        print()
        print(f"== Confusion ({arm}) ==")
        cells = Counter((names[e], names[p]) for e, p in zip(expected, predicted[arm]))
        # Count descending, then expected name, then predicted name.
        for (exp_name, pred_name), n in sorted(cells.items(), key=lambda c: (-c[1], c[0][0], c[0][1])):
            print(f"  {exp_name} -> {pred_name}: {n}")

    tags = sorted({tag for label in labels for tag in label.tags})
    if tags:
        print()
        print("== Per-tag slices (precision recall f1 per arm) ==")
        # Derive the span from _prf itself: three right-aligned cells joined by single spaces.
        cell = _PRF_WIDTH
        span = 3 * cell + 2
        lead = f"{'':<14} {'':>6}  "
        print((lead + "  ".join(f"{arm:^{span}}" for arm in arms)).rstrip())
        print((f"{'tag':<14} {'titles':>6}  "
               + "  ".join(" ".join(f"{name:>{cell}}" for name in ("P", "R", "F1")) for _ in arms)).rstrip())
        for tag in tags:
            indices = [i for i, label in enumerate(labels) if tag in label.tags]
            cells = "  ".join(_prf(*count(expected, predicted[arm], indices)) for arm in arms)
            print(f"{tag:<14} {len(indices):>6}  {cells}")

    print()
    print("== Diagnostics ==")
    print(f"Keyword multi-match titles: {multi}")
    if llm is not None:
        failures = Counter(result.failure for result in llm if not result.ok)
        if failed_batches is not None:
            print(f"Failed batches: {failed_batches}")
        if failures:
            print("Failed titles by reason:")
            for reason, n in sorted(failures.items()):
                print(f"  {reason}: {n} titles")
        else:
            print("Failed titles: 0")
        input_tokens, output_tokens, cost = usage
        print(f"Input tokens: {input_tokens:,}")
        print(f"Output tokens: {output_tokens:,}")
        print(f"Actual cost: ${cost:.4f}")
        print("This counts only usage the API returned; timed-out attempts that were billed are not included.")

    if show_errors:
        print()
        print("== Misclassified titles ==")
        for i, label in enumerate(labels):
            wrong = [arm for arm in arms if predicted[arm][i] != expected[i]]
            if wrong:
                guesses = "; ".join(f"{arm}={names[predicted[arm][i]]}" for arm in arms)
                print(f"line {label.line}: {label.title} | expected {names[expected[i]]} | {guesses}")


# ---------------------------------------------------------------------------
# LLM arm
# ---------------------------------------------------------------------------

def plan_requests(labels, items, prompt, model, effort):
    """Titles grouped by brand in label order, at most MAX_TITLES_PER_REQUEST per request."""
    by_brand = {}
    for index, label in enumerate(labels):
        by_brand.setdefault(label.brand, []).append(index)
    size = listing_classifier.MAX_TITLES_PER_REQUEST
    plan = []
    for brand, indices in by_brand.items():
        brand_items = [item for item in items if item.brand == brand]  # only its own items
        for start in range(0, len(indices), size):
            chunk = indices[start:start + size]
            request = listing_classifier.build_request(
                brand, [labels[i].title for i in chunk], brand_items, prompt, model=model, effort=effort)
            plan.append((brand, chunk, brand_items, request))
    return plan


def upper_bound_usd(plan, model):
    """Worst-case cost: 1 token per character of input, max_tokens of output, retries included.

    One token per character is a cautious guess, not a guarantee: emoji and rare kanji can
    use more than one token per character.  The (1 + MAX_RETRIES) factor covers a request
    that timed out on our side but was still billed, and is then sent again by the SDK.
    """
    total = 0.0
    for _, _, _, request in plan:
        estimated_input = (len(request["system"]) + len(request["messages"][0]["content"])
                           + STRUCTURED_OUTPUT_OVERHEAD_TOKENS)
        total += llm_client.cost_usd(model, estimated_input, listing_classifier.MAX_TOKENS)
    return total * (1 + llm_client.MAX_RETRIES)


def run_llm(client, labels, plan, prompt, model, effort):
    """Runs the requests one after another.  Returns (results, input_tokens, output_tokens, failed_batches)."""
    results = [None] * len(labels)
    input_tokens = output_tokens = failed_batches = 0
    for brand, chunk, brand_items, _ in plan:
        batch = listing_classifier.classify_titles(
            client, brand, [labels[i].title for i in chunk], brand_items, prompt, model=model, effort=effort)
        input_tokens += batch.input_tokens
        output_tokens += batch.output_tokens
        if batch.failure is not None:
            failed_batches += 1
            for i in chunk:
                results[i] = LlmResult(ok=False, failure=batch.failure)
            continue
        for i, decision in zip(chunk, batch.decisions):
            results[i] = LlmResult(True, None, decision.item_id, decision.confidence, decision.evidence)
    return results, input_tokens, output_tokens, failed_batches


# ---------------------------------------------------------------------------
# Predictions file
# ---------------------------------------------------------------------------

def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def write_predictions(handle, labels, items, results, run):
    names = _names(items)
    rows = []
    for label, result in zip(labels, results):
        rows.append({
            "line": label.line,
            "title_sha256": _sha(label.title),  # titles themselves are never stored
            "status": "ok" if result.ok else "failed",
            "failure": result.failure,
            "item": names[result.item_id] if result.item_id is not None else None,
            "confidence": result.confidence,
            "evidence": result.evidence,
        })
    try:
        handle.write(json.dumps({"run": run, "predictions": rows}, indent=2, ensure_ascii=False) + "\n")
        # Flush here so a disk-full error on the final bytes surfaces as this EvalError,
        # not as an OSError from close() after the run is already marked saved.
        handle.flush()
    except OSError as exc:
        raise EvalError(f"cannot write predictions to {handle.name}: {exc.strerror or exc}") from exc


def _malformed(reason):
    return EvalError(f"predictions file is malformed: {reason}")


def _is_count(value):
    # bool is an int subclass, so True must not pass as a token count.
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def read_predictions(path, labels, items, labels_sha, items_sha):
    """Returns (run, results).  Refuses a file made from different labels or items.

    Every value is type-checked, because a hand-edited or truncated file must give one
    clear error instead of a traceback or a silently wrong report.
    """
    try:
        document = json.loads(_decode(_read_bytes(path, "predictions file"), "predictions file"))
    except ValueError as exc:
        raise EvalError(f"predictions file is not valid JSON: {exc}") from exc

    if not isinstance(document, dict) or not isinstance(document.get("run"), dict) \
            or not isinstance(document.get("predictions"), list):
        raise _malformed("expected an object with run and predictions")
    run, rows = document["run"], document["predictions"]

    required = ("model", "effort", "prompt_version", "prompt_sha256", "labels_sha256", "items_sha256",
                "input_tokens", "output_tokens", "cost_usd")
    missing = [key for key in required if key not in run]
    if missing:
        raise _malformed("run is missing " + ", ".join(missing))
    for key in ("model", "effort", "prompt_version", "prompt_sha256", "labels_sha256", "items_sha256"):
        if not isinstance(run[key], str):
            raise _malformed(f"run.{key} must be a string")
    if not _is_count(run["input_tokens"]) or not _is_count(run["output_tokens"]):
        raise _malformed("token counts must be non-negative integers")
    cost = run["cost_usd"]
    if isinstance(cost, bool) or not isinstance(cost, (int, float)) or not 0 <= cost < float("inf"):
        raise _malformed("cost_usd must be a non-negative number")

    if run["labels_sha256"] != labels_sha or run["items_sha256"] != items_sha:
        raise EvalError("predictions file was made from different labels or items; re-run with --llm")
    if len(rows) != len(labels):
        raise EvalError("predictions file does not match the labels")

    results = []
    for label, row in zip(labels, rows):
        if not isinstance(row, dict):
            raise _malformed("every prediction must be an object")
        missing = [key for key in ("line", "title_sha256", "status", "failure", "item", "confidence", "evidence")
                   if key not in row]
        if missing:
            raise _malformed(f"line {label.line} is missing " + ", ".join(missing))
        if row["line"] != label.line or row["title_sha256"] != _sha(label.title):
            raise EvalError("predictions file does not match the labels")
        if row["status"] == "ok":
            brand_ids = {item.name: item.id for item in items if item.brand == label.brand}
            # Type first: a list or object is unhashable, so `in` on a dict would raise TypeError.
            if not isinstance(row["confidence"], str) or row["confidence"] not in CONFIDENCE_RANK:
                raise _malformed(f"line {label.line} has an unknown confidence")
            if row["item"] is not None and (not isinstance(row["item"], str) or row["item"] not in brand_ids):
                raise _malformed(f"line {label.line} names an item outside its brand")
            if not isinstance(row["evidence"], str):
                raise _malformed(f"line {label.line} evidence must be a string")
            item_id = None if row["item"] is None else brand_ids[row["item"]]
            results.append(LlmResult(True, None, item_id, row["confidence"], row["evidence"]))
        elif row["status"] == "failed":
            if not isinstance(row["failure"], str):
                raise _malformed(f"line {label.line} failure must be a string")
            results.append(LlmResult(False, row["failure"]))
        else:
            raise _malformed(f"line {label.line} status must be ok or failed")
    return run, results


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

def _max_usd(value):
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if not number > 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return number


def _parser():
    parser = argparse.ArgumentParser(
        description="Score keyword matching (and optionally the LLM classifier) on hand-labeled titles.")
    parser.add_argument("--labels", default=str(DEFAULT_LABELS), help="labels JSONL file")
    parser.add_argument("--items", default=str(DEFAULT_ITEMS), help="item snapshot (catalog.json format)")
    parser.add_argument("--show-errors", action="store_true",
                        help="also print the titles some arm got wrong (otherwise no title text is printed)")
    parser.add_argument("--llm", action="store_true",
                        help="also score the LLM arms; prints a cost estimate and stops unless --confirm-spend")
    parser.add_argument("--model", choices=sorted(llm_client.PRICES_USD_PER_MTOK), help="default: %s" % llm_client.DEFAULT_MODEL)
    parser.add_argument("--effort", choices=("low", "medium", "high"), help="default: %s" % llm_client.DEFAULT_EFFORT)
    parser.add_argument("--prompt", help="prompt version, default: %s" % listing_classifier.DEFAULT_PROMPT_VERSION)
    parser.add_argument("--confirm-spend", action="store_true", help="actually call the API")
    parser.add_argument("--max-usd", type=_max_usd, help="refuse to run if the upper-bound cost is higher (default %.2f)" % DEFAULT_MAX_USD)
    parser.add_argument("--save-predictions", help="where to write predictions (default eval/runs/...)")
    parser.add_argument("--from-predictions", help="recompute every arm from a saved predictions file, offline")
    return parser


def _check_usage(parser, args):
    if args.llm and args.from_predictions:
        parser.error("--llm and --from-predictions cannot be combined")
    # These only make sense for a paid run; failing early beats silently ignoring them.
    llm_only = [getattr(args, flag) is not None for flag in
                ("model", "effort", "prompt", "max_usd", "save_predictions")] + [args.confirm_spend]
    if not args.llm and any(llm_only):
        parser.error("--model, --effort, --prompt, --max-usd, --save-predictions and "
                     "--confirm-spend need --llm")


def _base_facts(labels, items, labels_sha, items_sha):
    return [
        f"Labels: {len(labels)}",
        f"Brands: {len({label.brand for label in labels})}",
        f"Items: {len(items)}",
        f"Labels SHA-256: {labels_sha[:12]}",
        f"Items SHA-256: {items_sha[:12]}",
    ]


def _llm_facts(run):
    return [
        f"Model: {run['model']}",
        f"Effort: {run['effort']}",
        f"Prompt: {run['prompt_version']}",
        f"Prompt SHA-256: {run['prompt_sha256'][:12]}",
    ]


def _run(args, client_factory):
    items, items_sha = load_items(args.items)
    labels, labels_sha = load_labels(args.labels, items)
    facts = _base_facts(labels, items, labels_sha, items_sha)
    keyword, multi = keyword_predictions(labels, items)
    report = dict(labels=labels, items=items, keyword=keyword, multi=multi, show_errors=args.show_errors,
                  level_arms=("llm", "hybrid"))

    if args.from_predictions:
        run, results = read_predictions(args.from_predictions, labels, items, labels_sha, items_sha)
        usage = (run["input_tokens"], run["output_tokens"], run["cost_usd"])
        print_report(llm=results, facts=facts + _llm_facts(run), usage=usage, **report)
        return 0

    if not args.llm:
        print_report(llm=None, facts=facts, **report)
        return 0

    model = args.model or llm_client.DEFAULT_MODEL
    effort = args.effort or llm_client.DEFAULT_EFFORT
    max_usd = args.max_usd if args.max_usd is not None else DEFAULT_MAX_USD
    try:
        prompt = listing_classifier.load_prompt(args.prompt or listing_classifier.DEFAULT_PROMPT_VERSION)
    except FileNotFoundError as exc:
        raise EvalError(f"prompt file not found: {exc.filename}") from exc
    except ValueError as exc:  # bad version, or PromptNotReadyError
        raise EvalError(str(exc)) from exc
    except OSError as exc:  # a directory in its place, a permission error, ...
        raise EvalError(f"cannot read prompt file: {exc.strerror or exc}") from exc

    plan = plan_requests(labels, items, prompt, model, effort)
    bound = upper_bound_usd(plan, model)
    print(f"Requests: {len(plan)}")
    print(f"Titles to classify: {len(labels)}")
    print(f"Upper-bound cost estimate (includes one retry per request): ${bound:.4f} ({model})")
    # Checked before the dry-run exit too, so an over-cap estimate is reported immediately.
    if bound > max_usd:
        raise EvalError(f"upper-bound cost ${bound:.4f} is over the --max-usd cap of ${max_usd:.4f}; "
                        "raise --max-usd to run")
    if not args.confirm_spend:
        print("Re-run with --confirm-spend to call the API.")
        return 0

    started_at = datetime.now(timezone.utc)
    save_path = Path(args.save_predictions or (
        DEFAULT_RUNS_DIR / f"{started_at.strftime('%Y%m%dT%H%M%SZ')}-{model}-{prompt.version}.json"))
    api_key = llm_client.api_key_from_env()
    if api_key is None:
        raise EvalError("ANTHROPIC_API_KEY is not set.")

    # Claim the output file before any spend.  Exclusive create ("x") fails if the file exists
    # and cannot race with another run, so an earlier run's predictions are never overwritten.
    # Remember which directories do not exist yet, so a failed run removes only what it made.
    new_dirs = []
    probe = save_path.absolute().parent
    while not probe.exists() and probe != probe.parent:
        new_dirs.append(probe)  # deepest first
        probe = probe.parent
    try:
        save_path.parent.mkdir(parents=True, exist_ok=True)
        handle = save_path.open("x", encoding="utf-8")
    except FileExistsError as exc:
        _remove_empty_dirs(new_dirs)
        raise EvalError(f"predictions file already exists: {save_path}; choose another --save-predictions path") from exc
    except OSError as exc:
        _remove_empty_dirs(new_dirs)
        raise EvalError(f"cannot create predictions file {save_path}: {exc.strerror or exc}") from exc

    saved = False
    try:
        try:
            client = client_factory(api_key)
        except ImportError as exc:  # the SDK is only a local-command dependency
            raise EvalError("the anthropic package is not installed; run pip install -r requirements-cli.txt.") from exc
        results, input_tokens, output_tokens, failed_batches = run_llm(client, labels, plan, prompt, model, effort)
        cost = llm_client.cost_usd(model, input_tokens, output_tokens)
        run = {
            "model": model,
            "effort": effort,
            "prompt_version": prompt.version,
            "prompt_sha256": prompt.sha256,
            "labels_sha256": labels_sha,
            "items_sha256": items_sha,
            "max_titles_per_request": listing_classifier.MAX_TITLES_PER_REQUEST,
            "started_at": started_at.isoformat(),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_usd": cost,
        }
        print()
        print_report(llm=results, facts=facts + _llm_facts(run), failed_batches=failed_batches,
                     usage=(input_tokens, output_tokens, cost), **report)
        write_predictions(handle, labels, items, results, run)
        saved = True
    finally:
        handle.close()
        if not saved:
            # Do not leave an empty claimed file behind when the run failed before writing.
            save_path.unlink(missing_ok=True)
            _remove_empty_dirs(new_dirs)
    print()
    print(f"Predictions saved to {save_path}")
    return 0


def _remove_empty_dirs(directories):
    """rmdir each directory (deepest first); rmdir refuses a non-empty one, which is the safety."""
    for directory in directories:
        try:
            directory.rmdir()
        except OSError:
            break


def main(argv=None, *, client_factory=llm_client.make_client):
    parser = _parser()
    args = parser.parse_args(argv)
    _check_usage(parser, args)

    # Help and usage errors finish before any environment or file access.
    load_dotenv()
    try:
        return _run(args, client_factory)
    except EvalError as exc:
        # Flush first so earlier output (counts, estimates) precedes the error even when piped.
        sys.stdout.flush()
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
