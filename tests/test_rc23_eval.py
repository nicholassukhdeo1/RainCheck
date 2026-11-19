"""RC-23a tests for src/eval_matching.py (the offline evaluation command).

Covers acceptance criteria 18-34 of docs/specs/RC-23-llm-listing-recognition.md.

Structure: src modules are imported inside functions, so while the feature is missing
each test fails individually with ModuleNotFoundError instead of one collection error.

Every test uses a scripted fake client behind `main(argv, client_factory=...)`.  No real
client is ever constructed, so results do not depend on whether `anthropic` is installed.
Predictions files are always written under tmp_path, never under eval/runs/.

The expected numbers below were worked out by hand from tests/fixtures/rc23/*.  The
fixture has 15 labels (line = 1-based file line).  Item IDs follow snapshot order:
1 = rick owens ramones, 2 = pod shorts (Rick Owens), 3 = tabi boots (Maison Margiela).

 line  expected  keyword prediction   notes
   1   ramones   ramones              TP
   2   ramones   ramones              TP (katakana keyword)
   3   ramones   none                 FN (misspelling "ramons")
   4   none      ramones              FP (laces)
   5   none      none                 excluded by drkshdw
   6   ramones   ramones              TP, also matches pod shorts (multi-match 1)
   7   pod       pod                  TP
   8   none      none
   9   none      none                 excluded by "fake"
  10   pod       ramones              wrong item: FP and FN, multi-match 2
  11   tabi      tabi                 TP
  12   none      tabi                 FP (injection title)
  13   tabi      none                 FN (katakana)
  14   none      none
  15   none      ramones              FP (box only)

Keyword arm: TP 5, FP 4, FN 3 -> P 0.556, R 0.625, F1 0.588.
"""
import hashlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from rc23_fakes import (
    ClientFactory, FakeClient, ForbiddenFactory, make_response, ok_response,
    payload_of, responder, schema_item_ids, text_block,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "rc23"
ITEMS = FIXTURES / "items_small.json"
LABELS = FIXTURES / "labels_small.jsonl"
INVALID_LABELS = FIXTURES / "labels_invalid.jsonl"

ROWS = [json.loads(line) for line in LABELS.read_text(encoding="utf-8").splitlines()]
T = [row["title"] for row in ROWS]  # T[0] is label line 1
BASE = ["--labels", str(LABELS), "--items", str(ITEMS)]

NUM = re.compile(r"n/a|\d+\.\d{3}|\d+")
CONFUSION = re.compile(r"^\s*(.+?) -> (.+?):\s*(\d+)\s*$")
ARMS = ("keyword", "llm", "hybrid", "union")
LEVELS = ("low", "medium", "high")
TAG_NAMES = {"accessory", "bundle", "english", "injection", "katakana", "misspelling"}

# Scripted model answers for scenario A (title -> (item_id, confidence)).
TABLE_A = {
    T[0]: (1, "high"), T[1]: (1, "high"), T[2]: (1, "medium"), T[3]: (None, "high"),
    T[4]: (None, "high"), T[5]: (1, "high"), T[6]: (2, "high"), T[7]: (None, "high"),
    T[8]: (None, "high"), T[9]: (2, "low"), T[14]: (None, "medium"),
}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def run(capsys, argv, factory=None):
    """Call main() like the console does; returns (exit_code, stdout, stderr, factory)."""
    import eval_matching

    factory = factory if factory is not None else ForbiddenFactory()
    try:
        code = eval_matching.main(list(argv), client_factory=factory)
    except SystemExit as exc:  # argparse usage errors and --help
        code = exc.code
    captured = capsys.readouterr()
    return code, captured.out, captured.err, factory


@pytest.fixture
def prompts(tmp_path, monkeypatch):
    """A temporary PROMPT_DIR holding a ready v1 and v2 prompt."""
    import listing_classifier

    directory = tmp_path / "prompts"
    directory.mkdir()
    (directory / "listing_match_v1.md").write_text(
        "<!-- contract header -->\n\nClassify each title.\n", encoding="utf-8")
    (directory / "listing_match_v2.md").write_text(
        "<!-- contract header -->\n\nClassify each title, version two.\n", encoding="utf-8")
    monkeypatch.setattr(listing_classifier, "PROMPT_DIR", directory)
    return directory


def refusal(input_tokens=200, output_tokens=50):
    return make_response([text_block("no")], stop_reason="refusal",
                         input_tokens=input_tokens, output_tokens=output_tokens)


def run_llm(capsys, tmp_path, monkeypatch, *, script=None, labels=LABELS, items=ITEMS,
            extra=(), key="sk-test-key", predictions="pred.json"):
    """Run the paid arm against a scripted client (scenario A by default).

    Scenario A: the Rick Owens batch succeeds with TABLE_A (usage 1000 in / 500 out) and
    the Maison Margiela batch is a refusal (usage 200 in / 50 out).
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", key)
    if script is None:
        script = [responder(TABLE_A, input_tokens=1000, output_tokens=500), refusal()]
    client = FakeClient(script)
    factory = ClientFactory(client)
    saved = tmp_path / predictions
    argv = ["--labels", str(labels), "--items", str(items), "--llm", "--confirm-spend",
            "--save-predictions", str(saved), *extra]
    code, out, err, _ = run(capsys, argv, factory)
    return SimpleNamespace(code=code, out=out, err=err, client=client, factory=factory, saved=saved)


def parse_report(out):
    """Pull the numeric rows out of a report without depending on exact column layout."""
    report = {"metrics": {}, "thresholds": {}, "confusion": [], "tags": {}}
    for line in out.splitlines():
        match = CONFUSION.match(line)
        if match:
            report["confusion"].append((match.group(1).strip(), match.group(2).strip(), int(match.group(3))))
            continue
        tokens = line.replace("|", " ").split()
        if not tokens:
            continue
        numbers = NUM.findall(line)
        lowered = [t.lower().strip(":") for t in tokens]
        arm_tokens = [t for t in lowered if t in ARMS]
        level_tokens = [t for t in lowered if t in LEVELS]
        if arm_tokens and level_tokens and len(numbers) == 6:
            report["thresholds"][(arm_tokens[0], level_tokens[0])] = numbers
        elif lowered[0] in ARMS and not level_tokens and len(numbers) == 6:
            report["metrics"][lowered[0]] = numbers
        elif lowered[0] in TAG_NAMES and numbers:
            report["tags"][lowered[0]] = numbers
    return report


def titles_in(text, titles=T):
    return [title for title in titles if title in text]


def prints_number(out, value, places):
    tolerance = 0.5 * 10 ** -places
    return any(abs(float(x) - value) < tolerance for x in re.findall(r"\d+\.\d+", out))


def write_labels(path, rows):
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    return path


def error_lines(err):
    return [line for line in err.splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# fixture sanity (guards the hand-computed numbers in this file)
# ---------------------------------------------------------------------------

def test_fixture_labels_match_the_hand_computed_table():
    assert len(ROWS) == 15
    assert [r["expected"] for r in ROWS] == [
        "rick owens ramones", "rick owens ramones", "rick owens ramones", None, None,
        "rick owens ramones", "pod shorts", None, None, "pod shorts",
        "tabi boots", None, "tabi boots", None, None,
    ]
    assert "laces" in T[3] and "DRKSHDW" in T[4] and "cargo" in T[7] and "sweater" in T[13]


# ---------------------------------------------------------------------------
# Evaluation inputs (criteria 18-20)
# ---------------------------------------------------------------------------

def test_c18_fixture_snapshot_loads_and_items_get_ids_in_file_order(capsys, tmp_path, monkeypatch, prompts):
    import db

    entries = db.validate_catalog(json.loads(ITEMS.read_text(encoding="utf-8")))
    assert [e.name for e in entries] == ["rick owens ramones", "pod shorts", "tabi boots"]

    result = run_llm(capsys, tmp_path, monkeypatch)
    assert result.code == 0, result.err
    # Rick Owens (items 1 and 2) is classified first, then Maison Margiela (item 3).
    assert [schema_item_ids(call) for call in result.client.calls] == [[1, 2, None], [3, None]]
    payloads = [payload_of(call) for call in result.client.calls]
    assert [{i["item_id"]: i["name"] for i in p["items"]} for p in payloads] == [
        {1: "rick owens ramones", 2: "pod shorts"}, {3: "tabi boots"}]


@pytest.mark.parametrize(
    "content",
    [
        "[]",
        '[{"brand": "Rick Owens", "yahoo_brand_id": "103538", "item name": "x", "keywords": []}]',
        '[{"brand": "Rick Owens"}]',
        "this is not json",
    ],
    ids=["empty_list", "empty_keywords", "missing_fields", "not_json"],
)
def test_c18_invalid_snapshot_exits_1_with_catalog_message(capsys, tmp_path, content):
    items = tmp_path / "items.json"
    items.write_text(content, encoding="utf-8")
    code, out, err, factory = run(capsys, ["--labels", str(LABELS), "--items", str(items)])
    assert code == 1
    assert err.startswith("Error:")
    if content != "this is not json":
        assert "Catalog is invalid" in err
    assert factory.calls == []


def test_c19_invalid_labels_report_every_problem_with_its_line_number(capsys):
    code, out, err, factory = run(capsys, ["--labels", str(INVALID_LABELS), "--items", str(ITEMS)])
    assert code == 1
    assert err.lstrip().startswith("Error:")
    assert factory.calls == []

    # Split the message into segments that each start at a "line N" reference.
    marks = list(re.finditer(r"\bline (\d+)\b", err, flags=re.IGNORECASE))
    segments = {}
    for position, mark in enumerate(marks):
        end = marks[position + 1].start() if position + 1 < len(marks) else len(err)
        segments.setdefault(int(mark.group(1)), "")
        segments[int(mark.group(1))] += err[mark.start():end].lower()

    # line -> text the message about that line must mention
    expected = {
        1: "json", 2: "object", 3: "extra", 4: "brand", 5: "title", 6: "expected",
        7: "title", 8: "nope brand", 9: "tabi boots", 11: "duplicate",
        12: "tags", 13: "tags", 14: "tags", 15: "notes",
    }
    for line_number, needle in expected.items():
        assert line_number in segments, f"no message mentions line {line_number}:\n{err}"
        assert needle in segments[line_number], f"line {line_number} message lacks {needle!r}:\n{err}"


@pytest.mark.parametrize("content", ["", "\n\n   \n\t\n"], ids=["empty_file", "only_blank_lines"])
def test_c20_no_labels_exits_1(capsys, tmp_path, content):
    labels = tmp_path / "labels.jsonl"
    labels.write_text(content, encoding="utf-8")
    code, out, err, factory = run(capsys, ["--labels", str(labels), "--items", str(ITEMS)])
    assert code == 1
    assert "Error: no labels found" in err
    assert factory.calls == []


def test_c20_blank_lines_between_labels_are_ignored(capsys, tmp_path):
    labels = tmp_path / "labels.jsonl"
    labels.write_text(
        "\n" + json.dumps({"brand": "Rick Owens", "title": "Rick Owens Ramones", "expected": "rick owens ramones"})
        + "\n\n   \n", encoding="utf-8")
    code, out, err, _ = run(capsys, ["--labels", str(labels), "--items", str(ITEMS)])
    assert code == 0, err
    assert parse_report(out)["metrics"]["keyword"][:3] == ["1", "0", "0"]


def test_edge_non_utf8_labels_exit_1_with_one_error_line(capsys, tmp_path):
    labels = tmp_path / "labels.jsonl"
    labels.write_bytes(b'{"brand": "Rick Owens", "title": "\xff\xfe", "expected": null}\n')
    code, out, err, factory = run(capsys, ["--labels", str(labels), "--items", str(ITEMS)])
    assert code == 1
    assert len(error_lines(err)) == 1 and err.startswith("Error:")
    assert factory.calls == []


# ---------------------------------------------------------------------------
# Keyword arm and metrics (criteria 21-26)
# ---------------------------------------------------------------------------

def test_c21_keyword_arm_takes_the_first_matching_item_in_snapshot_order(capsys, tmp_path):
    both = "Rick Owens Ramones pod shorts"  # matches item 1 and item 2
    first = write_labels(tmp_path / "first.jsonl", [
        {"brand": "Rick Owens", "title": both, "expected": "rick owens ramones"}])
    second = write_labels(tmp_path / "second.jsonl", [
        {"brand": "Rick Owens", "title": both, "expected": "pod shorts"}])

    _, out_first, _, _ = run(capsys, ["--labels", str(first), "--items", str(ITEMS)])
    _, out_second, _, _ = run(capsys, ["--labels", str(second), "--items", str(ITEMS)])
    # Item 1 claims the title: right when expected, a wrong item (FP and FN) otherwise.
    assert parse_report(out_first)["metrics"]["keyword"][:3] == ["1", "0", "0"]
    assert parse_report(out_second)["metrics"]["keyword"][:3] == ["0", "1", "1"]


def test_c21_keyword_arm_uses_title_matches_rules_normalization_and_boundaries(capsys, tmp_path):
    labels = write_labels(tmp_path / "labels.jsonl", [
        {"brand": "Rick Owens", "title": "ＲＡＭＯＮＥＳ black", "expected": "rick owens ramones"},
        {"brand": "Rick Owens", "title": "Ramonesque sneakers", "expected": None},
        {"brand": "Rick Owens", "title": "Ramones drkshdw", "expected": None},
    ])
    code, out, err, _ = run(capsys, ["--labels", str(labels), "--items", str(ITEMS)])
    assert code == 0, err
    # Full-width letters match after NFKC, "ramonesque" has no word boundary, drkshdw excludes.
    assert parse_report(out)["metrics"]["keyword"][:3] == ["1", "0", "0"]


def test_c21_each_brand_is_only_matched_against_its_own_items(capsys, tmp_path):
    labels = write_labels(tmp_path / "labels.jsonl", [
        {"brand": "Maison Margiela", "title": "Rick Owens Ramones", "expected": None},
        {"brand": "Rick Owens", "title": "Tabi boots", "expected": None},
    ])
    code, out, err, _ = run(capsys, ["--labels", str(labels), "--items", str(ITEMS)])
    assert code == 0, err
    assert parse_report(out)["metrics"]["keyword"][:3] == ["0", "0", "0"]


def test_c21_reports_the_multi_match_count(capsys):
    code, out, err, _ = run(capsys, BASE)
    assert code == 0, err
    lines = [line for line in out.splitlines() if re.search(r"multi", line, re.IGNORECASE)]
    assert lines, "no multi-match diagnostic line"
    assert any(re.search(r"\b2\b", line) for line in lines)


def test_c22_keyword_row_has_hand_computed_counts_and_scores(capsys):
    code, out, err, _ = run(capsys, BASE)
    assert code == 0, err
    # TP 5, FP 4 (laces, wrong item, injection, box), FN 3 (misspelling, wrong item, katakana)
    assert parse_report(out)["metrics"]["keyword"] == ["5", "4", "3", "0.556", "0.625", "0.588"]


def test_c22_keyword_only_run_has_no_other_arm_rows(capsys):
    code, out, err, _ = run(capsys, BASE)
    assert code == 0, err
    report = parse_report(out)
    assert set(report["metrics"]) == {"keyword"}
    assert report["thresholds"] == {}


def test_c22_run_facts_show_counts_and_hash_prefixes(capsys):
    code, out, err, _ = run(capsys, BASE)
    assert code == 0, err
    labels_sha = hashlib.sha256(LABELS.read_bytes()).hexdigest()
    items_sha = hashlib.sha256(ITEMS.read_bytes()).hexdigest()
    assert labels_sha[:12] in out
    assert items_sha[:12] in out
    assert re.search(r"\b15\b", out)  # labels
    assert any(re.search(r"\b3\b", line) and "item" in line.lower() for line in out.splitlines())
    assert any(re.search(r"\b2\b", line) and "brand" in line.lower() for line in out.splitlines())


def test_c23_slice_without_predictions_or_expectations_prints_na(capsys, tmp_path):
    labels = write_labels(tmp_path / "labels.jsonl", [
        {"brand": "Rick Owens", "title": "Rick Owens cargo pants", "expected": None}])
    code, out, err, _ = run(capsys, ["--labels", str(labels), "--items", str(ITEMS)])
    assert code == 0, err
    # No predicted items and no expected items: precision, recall and F1 are all n/a.
    assert parse_report(out)["metrics"]["keyword"] == ["0", "0", "0", "n/a", "n/a", "n/a"]


def test_c23_per_tag_slices_print_na_where_a_denominator_is_zero(capsys):
    code, out, err, _ = run(capsys, BASE)
    assert code == 0, err
    tags = parse_report(out)["tags"]
    # misspelling: one expected item, nothing predicted -> precision n/a, recall 0.000
    assert tags["misspelling"][:3] == ["1", "n/a", "0.000"]
    # accessory: two wrong predictions, nothing expected -> precision 0.000, recall n/a
    assert tags["accessory"][:3] == ["2", "0.000", "n/a"]
    # injection: same shape, one title
    assert tags["injection"][:3] == ["1", "0.000", "n/a"]


def test_c24_confusion_lists_exactly_the_non_zero_cells_in_sort_order(capsys):
    code, out, err, _ = run(capsys, BASE)
    assert code == 0, err
    ramones = "rick owens ramones"
    assert parse_report(out)["confusion"] == [
        ("none", "none", 4),
        (ramones, ramones, 3),
        ("none", ramones, 2),
        ("none", "tabi boots", 1),
        ("pod shorts", "pod shorts", 1),
        ("pod shorts", ramones, 1),  # the wrong-item cell
        (ramones, "none", 1),
        ("tabi boots", "none", 1),
        ("tabi boots", "tabi boots", 1),
    ]


def test_c25_per_tag_table_is_sorted_and_matches_hand_computed_values(capsys):
    code, out, err, _ = run(capsys, BASE)
    assert code == 0, err
    tags = parse_report(out)["tags"]
    assert sorted(tags) == ["accessory", "bundle", "english", "injection", "katakana", "misspelling"]
    # katakana (lines 2 and 13, the second tagged "Katakana"): TP 1, FN 1
    assert tags["katakana"] == ["2", "1.000", "0.500", "0.667"]
    # bundle (lines 6 and 10): TP 1, one wrong item counted as FP and FN
    assert tags["bundle"] == ["2", "0.500", "0.500", "0.500"]
    # english (line 11): a single hit
    assert tags["english"] == ["1", "1.000", "1.000", "1.000"]

    positions = [out.index(line) for line in out.splitlines()
                 if line.split() and line.split()[0].lower() in tags]
    assert positions == sorted(positions)
    order = [line.split()[0].lower() for line in out.splitlines()
             if line.split() and line.split()[0].lower() in tags]
    assert order == sorted(order)


def test_c26_keyword_only_run_never_creates_a_client_even_with_a_key(capsys, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    factory = ForbiddenFactory()
    code, out, err, _ = run(capsys, BASE, factory)
    assert code == 0, err
    assert factory.calls == []
    assert titles_in(out) == []
    assert titles_in(err) == []


# ---------------------------------------------------------------------------
# LLM arm: gates and usage (criteria 27-29)
# ---------------------------------------------------------------------------

def _expected_upper_bound(model="claude-opus-5-5"):
    import listing_classifier
    import llm_client

    rows = json.loads(ITEMS.read_text(encoding="utf-8"))
    items_by_brand = {}
    for number, row in enumerate(rows, start=1):
        items_by_brand.setdefault(row["brand"], []).append(SimpleNamespace(
            id=number, name=row["item name"], keywords=row["keywords"],
            exclude_keywords=row.get("exclude_keywords", [])))
    prompt = listing_classifier.load_prompt()
    total, requests = 0.0, 0
    for brand in dict.fromkeys(r["brand"] for r in ROWS):
        titles = [r["title"] for r in ROWS if r["brand"] == brand]
        request = listing_classifier.build_request(
            brand, titles, items_by_brand[brand], prompt, model=model, effort="low")
        estimated_input = len(request["system"]) + len(request["messages"][0]["content"]) + 500
        total += llm_client.cost_usd(model, estimated_input, 6000)
        requests += 1
    # Amended 2026-11-18: each request can be billed again by one SDK retry.
    return requests, total * (1 + llm_client.MAX_RETRIES)


def test_c27_llm_without_confirm_prints_estimate_and_exits_0_without_a_client(capsys, prompts):
    code, out, err, factory = run(capsys, BASE + ["--llm"])
    assert code == 0, err
    assert factory.calls == []
    assert "Re-run with --confirm-spend to call the API." in out

    requests, upper_bound = _expected_upper_bound()
    assert requests == 2
    lines = out.splitlines()
    assert any("request" in l.lower() and re.search(r"\b2\b", l) for l in lines)
    assert any("title" in l.lower() and re.search(r"\b15\b", l) for l in lines)
    assert prints_number(out, upper_bound, 3), f"upper bound {upper_bound:.6f} not printed:\n{out}"
    assert titles_in(out) == []


def test_c27_llm_dry_run_needs_no_api_key(capsys, monkeypatch, prompts):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    code, out, err, factory = run(capsys, BASE + ["--llm"])
    assert code == 0, err
    assert factory.calls == []


def test_c27_upper_bound_follows_the_model_price(capsys, prompts):
    code, out, err, _ = run(capsys, BASE + ["--llm", "--model", "claude-haiku-5-5"])
    assert code == 0, err
    _, upper_bound = _expected_upper_bound("claude-haiku-5-5")
    assert prints_number(out, upper_bound, 3)


def test_c28_spend_cap_exits_1_before_creating_a_client(capsys, tmp_path, monkeypatch, prompts):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    saved = tmp_path / "pred.json"
    code, out, err, factory = run(
        capsys, BASE + ["--llm", "--confirm-spend", "--max-usd", "0.01", "--save-predictions", str(saved)])
    assert code == 1
    assert err.startswith("Error:")
    assert factory.calls == []
    assert not saved.exists()


def test_c28_header_only_prompt_exits_1_with_prompt_message(capsys, monkeypatch, prompts):
    (prompts / "listing_match_v1.md").write_text("<!-- only the header -->\n", encoding="utf-8")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    code, out, err, factory = run(capsys, BASE + ["--llm", "--confirm-spend"])
    assert code == 1
    assert "src/prompts/listing_match_v1.md has no instructions yet." in err
    assert err.startswith("Error:")
    assert factory.calls == []


def test_c28_missing_api_key_exits_1_before_creating_a_client(capsys, monkeypatch, prompts):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    code, out, err, factory = run(capsys, BASE + ["--llm", "--confirm-spend"])
    assert code == 1
    assert "Error: ANTHROPIC_API_KEY is not set." in err
    assert factory.calls == []


def test_c28_blank_api_key_counts_as_missing(capsys, monkeypatch, prompts):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "   ")
    code, out, err, factory = run(capsys, BASE + ["--llm", "--confirm-spend"])
    assert code == 1
    assert "Error: ANTHROPIC_API_KEY is not set." in err
    assert factory.calls == []


@pytest.mark.parametrize(
    "extra",
    [
        ["--llm", "--model", "unknown-model"],
        ["--llm", "--effort", "extreme"],
        ["--llm", "--max-usd", "-1"],
        ["--llm", "--from-predictions", "whatever.json"],
    ],
    ids=["unknown_model", "bad_effort", "negative_cap", "llm_with_from_predictions"],
)
def test_c29_usage_errors_exit_2_before_any_work(capsys, monkeypatch, prompts, extra):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    code, out, err, factory = run(capsys, BASE + extra)
    assert code == 2
    assert err.strip() != ""
    assert factory.calls == []


def test_c29_path_like_prompt_version_exits_1_with_one_error_line(capsys, monkeypatch, prompts):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    code, out, err, factory = run(capsys, BASE + ["--llm", "--confirm-spend", "--prompt", "../x"])
    assert code == 1
    assert len(error_lines(err)) == 1 and err.startswith("Error:")
    assert factory.calls == []


def test_c29_help_exits_0_without_reading_files_or_loading_dotenv(capsys, monkeypatch):
    import eval_matching

    calls = []
    monkeypatch.setattr(eval_matching, "load_dotenv", lambda *a, **k: calls.append(1), raising=False)
    code, out, err, factory = run(
        capsys, ["--labels", "/nonexistent/labels.jsonl", "--items", "/nonexistent/items.json", "--help"])
    assert code == 0
    assert "--llm" in out and "--confirm-spend" in out
    assert factory.calls == [] and calls == []


# ---------------------------------------------------------------------------
# LLM arm: requests, scoring, thresholds, predictions (criteria 30-34)
# ---------------------------------------------------------------------------

def test_c30_requests_are_grouped_by_brand_chunked_at_25_and_run_sequentially(
        capsys, tmp_path, monkeypatch, prompts):
    rows = [{"brand": "Maison Margiela", "title": "MM listing 00", "expected": None}]
    rows += [{"brand": "Rick Owens", "title": f"RO listing {i:02d}", "expected": None} for i in range(10)]
    rows += [{"brand": "Maison Margiela", "title": "MM listing 01", "expected": None}]
    rows += [{"brand": "Rick Owens", "title": f"RO listing {i:02d}", "expected": None} for i in range(10, 30)]
    rows += [{"brand": "Maison Margiela", "title": "MM listing 02", "expected": None}]
    labels = write_labels(tmp_path / "many.jsonl", rows)

    result = run_llm(capsys, tmp_path, monkeypatch, labels=labels,
                     script=[responder({}), responder({}), responder({})])
    assert result.code == 0, result.err
    calls = result.client.calls
    assert len(calls) == 3
    assert result.client.max_in_flight == 1
    assert result.client.script == []

    payloads = [payload_of(c) for c in calls]
    assert [p["brand"] for p in payloads] == ["Maison Margiela", "Rick Owens", "Rick Owens"]
    titles = [[t["title"] for t in p["titles"]] for p in payloads]
    assert titles[0] == ["MM listing 00", "MM listing 01", "MM listing 02"]
    assert titles[1] == [f"RO listing {i:02d}" for i in range(25)]
    assert titles[2] == [f"RO listing {i:02d}" for i in range(25, 30)]
    assert [[t["index"] for t in p["titles"]] for p in payloads] == [[0, 1, 2], list(range(25)), list(range(5))]
    assert [schema_item_ids(c) for c in calls] == [[3, None], [1, 2, None], [1, 2, None]]
    assert all(c["system"] == "Classify each title." for c in calls)
    assert len(result.factory.calls) == 1
    args, kwargs = result.factory.calls[0]
    assert args == ("sk-test-key",) or kwargs == {"api_key": "sk-test-key"}


def test_c30_brand_without_labeled_titles_sends_no_request_and_flags_pass_through(
        capsys, tmp_path, monkeypatch, prompts):
    labels = write_labels(tmp_path / "ro_only.jsonl", [
        {"brand": "Rick Owens", "title": "Rick Owens Ramones", "expected": "rick owens ramones"},
        {"brand": "Rick Owens", "title": "Rick Owens cargo pants", "expected": None},
    ])
    table = {"Rick Owens Ramones": (1, "high")}
    result = run_llm(capsys, tmp_path, monkeypatch, labels=labels, script=[responder(table)],
                     extra=["--model", "claude-haiku-5-5", "--effort", "high", "--prompt", "listing_match_v2"])
    assert result.code == 0, result.err
    assert len(result.client.calls) == 1
    call = result.client.calls[0]
    assert payload_of(call)["brand"] == "Rick Owens"
    assert call["model"] == "claude-haiku-5-5"
    assert call["output_config"]["effort"] == "high"
    assert call["system"] == "Classify each title, version two."
    run_facts = json.loads(result.saved.read_text(encoding="utf-8"))["run"]
    assert run_facts["model"] == "claude-haiku-5-5"
    assert run_facts["effort"] == "high"
    assert run_facts["prompt_version"] == "listing_match_v2"


def test_c31_scores_all_four_arms_with_hand_computed_values(capsys, tmp_path, monkeypatch, prompts):
    result = run_llm(capsys, tmp_path, monkeypatch)
    assert result.code == 0, result.err
    metrics = parse_report(result.out)["metrics"]
    assert metrics["keyword"] == ["5", "4", "3", "0.556", "0.625", "0.588"]
    # llm: failed Maison Margiela titles count as none -> two missed tabi boots
    assert metrics["llm"] == ["6", "0", "2", "1.000", "0.750", "0.857"]
    # hybrid: failed titles fall back to keywords (line 11 hit, line 12 false match, line 13 miss)
    assert metrics["hybrid"] == ["7", "1", "1", "0.875", "0.875", "0.875"]
    # union: keyword answers are never vetoed; the model only rescues (line 3)
    assert metrics["union"] == ["6", "4", "2", "0.600", "0.750", "0.667"]


def test_c31_failure_table_and_token_totals(capsys, tmp_path, monkeypatch, prompts):
    result = run_llm(capsys, tmp_path, monkeypatch)
    assert result.code == 0, result.err
    out = result.out
    refusal_lines = [l for l in out.splitlines() if "refusal" in l.lower()]
    assert refusal_lines, "failure table must name the reason code"
    assert any("4" in NUM.findall(l) for l in refusal_lines), "failure table must show 4 titles"
    # 1000 + 200 input tokens and 500 + 50 output tokens
    assert re.search(r"\b1,?200\b", out)
    assert re.search(r"\b550\b", out)
    # Opus 5.5: (1200 * 4 + 550 * 20) / 1e6 = 0.0158 dollars
    assert prints_number(out, 0.0158, 4)


def test_c31_failed_batch_does_not_stop_later_batches(capsys, tmp_path, monkeypatch, prompts):
    # Maison Margiela is classified second in scenario A, so fail the FIRST batch instead.
    result = run_llm(capsys, tmp_path, monkeypatch, script=[
        RuntimeError("HTTP 400: Schema is too complex for compilation"),
        responder({T[10]: (3, "high"), T[12]: (3, "high")}),
    ])
    assert result.code == 0, result.err
    assert len(result.client.calls) == 2
    metrics = parse_report(result.out)["metrics"]
    # llm sees only the Maison Margiela batch: lines 11 and 13 are hits, nothing else predicted.
    assert metrics["llm"][:3] == ["2", "0", "6"]
    assert any("request_error" in l for l in result.out.splitlines())


def test_c32_threshold_table_matches_hand_computed_values(capsys, tmp_path, monkeypatch, prompts):
    result = run_llm(capsys, tmp_path, monkeypatch)
    assert result.code == 0, result.err
    thresholds = parse_report(result.out)["thresholds"]
    assert set(thresholds) == {(a, l) for a in ("llm", "hybrid") for l in LEVELS}
    # Decisions below the threshold count as none (they do not fall back to keywords).
    assert thresholds[("llm", "low")] == ["6", "0", "2", "1.000", "0.750", "0.857"]
    assert thresholds[("llm", "medium")] == ["5", "0", "3", "1.000", "0.625", "0.769"]
    assert thresholds[("llm", "high")] == ["4", "0", "4", "1.000", "0.500", "0.667"]
    assert thresholds[("hybrid", "low")] == ["7", "1", "1", "0.875", "0.875", "0.875"]
    assert thresholds[("hybrid", "medium")] == ["6", "1", "2", "0.857", "0.750", "0.800"]
    assert thresholds[("hybrid", "high")] == ["5", "1", "3", "0.833", "0.625", "0.714"]


def test_c32_per_tag_table_has_one_column_group_per_arm(capsys, tmp_path, monkeypatch, prompts):
    result = run_llm(capsys, tmp_path, monkeypatch)
    assert result.code == 0, result.err
    tags = parse_report(result.out)["tags"]
    # Column order: count, then precision/recall/F1 for keyword, llm, hybrid, union.
    # accessory (lines 4 and 15, expected none): the model vetoes both, keywords and union do not.
    accessory = tags["accessory"]
    assert accessory[0] == "2"
    assert [accessory[i] for i in (1, 2)] == ["0.000", "n/a"]    # keyword precision, recall
    assert [accessory[i] for i in (4, 5)] == ["n/a", "n/a"]      # llm
    assert [accessory[i] for i in (7, 8)] == ["n/a", "n/a"]      # hybrid
    assert [accessory[i] for i in (10, 11)] == ["0.000", "n/a"]  # union
    # katakana (lines 2 and 13): line 13 sits in the failed batch for every arm
    assert tags["katakana"] == ["2"] + ["1.000", "0.500", "0.667"] * 4


def _prediction_rows(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_c33_predictions_file_has_documented_structure_and_no_title_text(
        capsys, tmp_path, monkeypatch, prompts):
    result = run_llm(capsys, tmp_path, monkeypatch)
    assert result.code == 0, result.err
    raw = result.saved.read_text(encoding="utf-8")
    document = json.loads(raw)
    assert set(document) == {"run", "predictions"}

    run_facts = document["run"]
    for key in ("model", "effort", "prompt_version", "prompt_sha256", "labels_sha256", "items_sha256",
                "max_titles_per_request", "started_at", "input_tokens", "output_tokens", "cost_usd"):
        assert key in run_facts, f"run is missing {key}"
    assert run_facts["model"] == "claude-opus-5-5"
    assert run_facts["effort"] == "low"
    assert run_facts["prompt_version"] == "listing_match_v1"
    assert run_facts["prompt_sha256"] == hashlib.sha256(
        (prompts / "listing_match_v1.md").read_bytes()).hexdigest()
    assert run_facts["labels_sha256"] == hashlib.sha256(LABELS.read_bytes()).hexdigest()
    assert run_facts["items_sha256"] == hashlib.sha256(ITEMS.read_bytes()).hexdigest()
    assert run_facts["max_titles_per_request"] == 25
    assert run_facts["input_tokens"] == 1200 and run_facts["output_tokens"] == 550
    assert run_facts["cost_usd"] == pytest.approx(0.0158)
    datetime.fromisoformat(run_facts["started_at"].replace("Z", "+00:00"))

    predictions = document["predictions"]
    assert len(predictions) == 15
    for number, prediction in enumerate(predictions, start=1):
        assert set(prediction) == {"line", "title_sha256", "status", "failure", "item", "confidence", "evidence"}
        assert prediction["line"] == number
        assert prediction["title_sha256"] == hashlib.sha256(T[number - 1].encode("utf-8")).hexdigest()

    by_line = {p["line"]: p for p in predictions}
    assert (by_line[1]["status"], by_line[1]["failure"]) == ("ok", None)
    assert (by_line[1]["item"], by_line[1]["confidence"]) == ("rick owens ramones", "high")
    assert by_line[1]["evidence"] == "evidence-text"
    assert by_line[10]["item"] == "pod shorts" and by_line[10]["confidence"] == "low"
    assert by_line[4]["status"] == "ok" and by_line[4]["item"] is None
    for line in (11, 12, 13, 14):  # the refused Maison Margiela batch
        assert by_line[line] == {"line": line, "title_sha256": by_line[line]["title_sha256"],
                                 "status": "failed", "failure": "refusal",
                                 "item": None, "confidence": None, "evidence": None}

    readable = json.dumps(document, ensure_ascii=False)
    for title in T:
        assert title not in raw and title not in readable


def test_c33_from_predictions_reproduces_the_report_without_a_client(
        capsys, tmp_path, monkeypatch, prompts):
    result = run_llm(capsys, tmp_path, monkeypatch)
    assert result.code == 0, result.err
    original = parse_report(result.out)
    assert original["metrics"].keys() == {"keyword", "llm", "hybrid", "union"}

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    code, out, err, factory = run(capsys, BASE + ["--from-predictions", str(result.saved)])
    assert code == 0, err
    assert factory.calls == []
    replay = parse_report(out)
    assert replay["metrics"] == original["metrics"]
    assert replay["thresholds"] == original["thresholds"]
    assert replay["confusion"] == original["confusion"]
    assert replay["tags"] == original["tags"]
    assert replay["confusion"], "confusion section is missing"


def test_c33_from_predictions_works_from_copies_with_identical_contents(
        capsys, tmp_path, monkeypatch, prompts):
    result = run_llm(capsys, tmp_path, monkeypatch)
    labels_copy = tmp_path / "copy_labels.jsonl"
    items_copy = tmp_path / "copy_items.json"
    labels_copy.write_bytes(LABELS.read_bytes())
    items_copy.write_bytes(ITEMS.read_bytes())
    code, out, err, _ = run(capsys, ["--labels", str(labels_copy), "--items", str(items_copy),
                                     "--from-predictions", str(result.saved)])
    assert code == 0, err
    assert parse_report(out)["metrics"] == parse_report(result.out)["metrics"]


def test_c33_from_predictions_rejects_changed_labels(capsys, tmp_path, monkeypatch, prompts):
    labels_copy = tmp_path / "labels_copy.jsonl"
    labels_copy.write_bytes(LABELS.read_bytes())
    result = run_llm(capsys, tmp_path, monkeypatch, labels=labels_copy)
    assert result.code == 0, result.err

    changed = labels_copy.read_text(encoding="utf-8").replace("cargo pants", "cargo pants II")
    assert changed != labels_copy.read_text(encoding="utf-8")
    labels_copy.write_text(changed, encoding="utf-8")
    code, out, err, factory = run(capsys, ["--labels", str(labels_copy), "--items", str(ITEMS),
                                           "--from-predictions", str(result.saved)])
    assert code == 1
    assert err.startswith("Error:")
    assert factory.calls == []
    assert parse_report(out)["metrics"] == {}  # no report was printed


def test_c33_from_predictions_rejects_changed_items(capsys, tmp_path, monkeypatch, prompts):
    items_copy = tmp_path / "items_copy.json"
    items_copy.write_bytes(ITEMS.read_bytes())
    result = run_llm(capsys, tmp_path, monkeypatch, items=items_copy)
    assert result.code == 0, result.err

    items_copy.write_text(items_copy.read_text(encoding="utf-8").replace('"ramones",', '"ramones", "ramonz",'),
                          encoding="utf-8")
    code, out, err, factory = run(capsys, ["--labels", str(LABELS), "--items", str(items_copy),
                                           "--from-predictions", str(result.saved)])
    assert code == 1
    assert err.startswith("Error:")
    assert factory.calls == []


def test_c34_no_title_text_in_stdout_in_any_mode_without_show_errors(
        capsys, tmp_path, monkeypatch, prompts):
    code, out, err, _ = run(capsys, BASE)
    assert code == 0 and titles_in(out) == []

    code, out, err, _ = run(capsys, BASE + ["--llm"])
    assert code == 0 and titles_in(out) == []

    result = run_llm(capsys, tmp_path, monkeypatch)
    assert result.code == 0 and titles_in(result.out) == []

    code, out, err, _ = run(capsys, BASE + ["--from-predictions", str(result.saved)])
    assert code == 0 and titles_in(out) == []


def test_c34_show_errors_lists_exactly_the_titles_the_keyword_arm_got_wrong(capsys):
    code, out, err, _ = run(capsys, BASE + ["--show-errors"])
    assert code == 0, err
    wrong_lines = [3, 4, 10, 12, 13, 15]  # misspelling, laces, wrong item, injection, katakana, box
    assert titles_in(out) == [T[n - 1] for n in wrong_lines]


def test_c34_show_errors_with_llm_lists_titles_any_arm_got_wrong(capsys, tmp_path, monkeypatch, prompts):
    result = run_llm(capsys, tmp_path, monkeypatch, extra=["--show-errors"])
    assert result.code == 0, result.err
    # keyword: 3 4 10 12 13 15; llm: 11 13; hybrid: 12 13; union: 4 10 12 13 15
    wrong_lines = {3, 4, 10, 11, 12, 13, 15}
    assert set(titles_in(result.out)) == {T[n - 1] for n in wrong_lines}


def test_c34_show_errors_works_from_predictions_too(capsys, tmp_path, monkeypatch, prompts):
    result = run_llm(capsys, tmp_path, monkeypatch)
    code, out, err, _ = run(capsys, BASE + ["--from-predictions", str(result.saved), "--show-errors"])
    assert code == 0, err
    assert set(titles_in(out)) == {T[n - 1] for n in (3, 4, 10, 11, 12, 13, 15)}


# ---------------------------------------------------------------------------
# Packaging and hygiene (criteria 35-38)
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent


def test_c35_requirements_cli_pins_anthropic_and_everything_else_with_double_equals():
    lines = [l.strip() for l in (ROOT / "requirements-cli.txt").read_text(encoding="utf-8").splitlines()]
    requirements = [l for l in lines if l and not l.startswith(("#", "-r", "-c"))]
    assert any(re.match(r"^anthropic==[\w.]+$", l) for l in requirements), "anthropic must be pinned with =="
    for requirement in requirements:
        assert "==" in requirement and not re.search(r"[<>~!]=?|\*", requirement.split("==", 1)[1]), (
            f"{requirement!r} must be an exact == pin")


def test_c35_lambda_requirements_and_makefile_do_not_mention_the_new_modules():
    assert "anthropic" not in (ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    for name in ("llm_client", "listing_classifier", "eval_matching", "prompts"):
        assert name not in makefile
    assert (ROOT / "src" / "eval_matching.py").exists(), "RC-23a modules are missing"


def test_c36_conftest_clears_anthropic_environment_variables():
    import conftest

    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"):
        assert name in conftest._ENV_TO_CLEAR


def test_c36_no_module_imports_anthropic_at_module_level():
    import ast

    modules = [ROOT / "src" / name for name in ("llm_client.py", "listing_classifier.py", "eval_matching.py")]
    missing = [m.name for m in modules if not m.exists()]
    assert not missing, f"RC-23a modules are missing: {missing}"

    def module_level_imports(statements):
        for node in statements:
            if isinstance(node, ast.Import):
                yield from (alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                yield node.module or ""
            elif isinstance(node, (ast.If, ast.Try, ast.With)):  # still module level
                nested = list(node.body) + list(getattr(node, "orelse", [])) + list(getattr(node, "finalbody", []))
                for handler in getattr(node, "handlers", []):
                    nested += handler.body
                yield from module_level_imports(nested)

    for path in sorted((ROOT / "src").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for name in module_level_imports(tree.body):
            assert name.split(".")[0] != "anthropic", f"{path.name} imports anthropic at module level"

    client_source = (ROOT / "src" / "llm_client.py").read_text(encoding="utf-8")
    assert "import anthropic" in client_source, "llm_client.make_client must import anthropic lazily"


def test_c36_importing_the_modules_does_not_import_anthropic():
    """With a poisoned `anthropic` entry, importing every RC-23a module must still work."""
    import subprocess

    code = (
        "import sys\n"
        f"sys.path.insert(0, {str(ROOT / 'src')!r})\n"
        "sys.modules['anthropic'] = None\n"
        "import llm_client, listing_classifier, eval_matching\n"
        "print('ok')\n"
    )
    completed = subprocess.run(
        [sys.executable, "-I", "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "ok"


def test_c37_gitignore_ignores_eval_runs():
    lines = [l.strip() for l in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()]
    assert any(l in ("eval/runs/", "/eval/runs/", "eval/runs", "/eval/runs") for l in lines)


def test_c37_eval_readme_documents_formats_tags_bundle_rule_and_commands():
    path = ROOT / "eval" / "listing_match" / "README.md"
    assert path.exists(), "eval/listing_match/README.md is missing"
    text = path.read_text(encoding="utf-8")
    lowered = text.lower()
    for needle in ("items.json", "labels.jsonl", "brand", "title", "expected", "tags", "notes",
                   "katakana", "misspelling", "model_code", "accessory", "lookalike", "bundle",
                   "english", "injection", "eval_matching.py", "--llm", "--confirm-spend",
                   "--from-predictions", "--show-errors"):
        assert needle in lowered, f"eval README must mention {needle!r}"
    assert "null" in lowered  # the expected: null convention


def test_c38_readme_section_documents_the_evaluation():
    readme = (ROOT / "README.md").read_text(encoding="utf-8").splitlines()
    starts = [i for i, l in enumerate(readme) if l.startswith("#") and "Listing recognition evaluation (RC-23a)" in l]
    assert starts, "README needs a 'Listing recognition evaluation (RC-23a)' section"
    start = starts[0]
    level = len(readme[start]) - len(readme[start].lstrip("#"))
    end = len(readme)
    for i in range(start + 1, len(readme)):
        if readme[i].startswith("#") and len(readme[i]) - len(readme[i].lstrip("#")) <= level:
            end = i
            break
    section = "\n".join(readme[start:end])
    lowered = section.lower()
    for needle in ("python3 src/eval_matching.py", "--llm", "--confirm-spend", "--max-usd",
                   "anthropic_api_key", ".env", "predictions"):
        assert needle in lowered, f"README section must mention {needle!r}"
    assert any("test" in l.lower() and "never" in l.lower() and "api" in l.lower() for l in section.splitlines()), (
        "README section must say the tests never call the API")

# ---------------------------------------------------------------------------
# Criterion 39 (amended 2026-11-18): robustness
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("confirm", [[], ["--confirm-spend"]], ids=["dry_run", "confirm_spend"])
def test_c39a_over_cap_exits_1_naming_both_amounts_before_the_factory(
        capsys, tmp_path, monkeypatch, prompts, confirm):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    saved = tmp_path / "pred.json"
    code, out, err, factory = run(
        capsys, BASE + ["--llm", *confirm, "--max-usd", "0.01", "--save-predictions", str(saved)])
    _, upper_bound = _expected_upper_bound()
    assert upper_bound > 0.01
    assert code == 1
    assert len(error_lines(err)) == 1 and err.startswith("Error:")
    assert prints_number(err, upper_bound, 3), f"estimate {upper_bound:.6f} not named:\n{err}"
    assert prints_number(err, 0.01, 2), f"cap not named:\n{err}"
    assert "Re-run with --confirm-spend" not in out
    assert factory.calls == []
    assert not saved.exists()


def test_c39b_existing_predictions_file_exits_1_before_the_factory_and_is_unchanged(
        capsys, tmp_path, monkeypatch, prompts):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    saved = tmp_path / "pred.json"
    saved.write_bytes(b'{"precious": "earlier run"}\n')
    code, out, err, factory = run(
        capsys, BASE + ["--llm", "--confirm-spend", "--save-predictions", str(saved)])
    assert code == 1
    assert len(error_lines(err)) == 1 and err.startswith("Error:")
    assert factory.calls == []
    assert saved.read_bytes() == b'{"precious": "earlier run"}\n'


def test_c39b_unwritable_predictions_directory_exits_1_before_the_factory(
        capsys, tmp_path, monkeypatch, prompts):
    import os
    import stat

    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root ignores directory permissions")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        if os.access(locked, os.W_OK):
            pytest.skip("directory is still writable on this system")
        saved = locked / "pred.json"
        code, out, err, factory = run(
            capsys, BASE + ["--llm", "--confirm-spend", "--save-predictions", str(saved)])
    finally:
        locked.chmod(stat.S_IRWXU)
    assert code == 1
    assert len(error_lines(err)) == 1 and err.startswith("Error:")
    assert "Traceback" not in err
    assert factory.calls == []
    assert not saved.exists()


def test_c39c_missing_anthropic_package_exits_1_with_install_message_and_no_traceback(
        capsys, tmp_path, monkeypatch, prompts):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    calls = []

    def factory(*args, **kwargs):
        calls.append(1)
        raise ModuleNotFoundError("No module named 'anthropic'", name="anthropic")

    saved = tmp_path / "pred.json"
    code, out, err, _ = run(
        capsys, BASE + ["--llm", "--confirm-spend", "--save-predictions", str(saved)], factory)
    assert code == 1
    assert calls == [1]
    assert ("Error: the anthropic package is not installed; "
            "run pip install -r requirements-cli.txt.") in err
    assert "Traceback" not in err and "Traceback" not in out
    assert len(error_lines(err)) == 1
    assert not saved.exists()


def test_c39d_prompt_path_that_is_a_directory_exits_1_with_one_error_line(
        capsys, monkeypatch, prompts):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    (prompts / "listing_match_v1.md").unlink()
    (prompts / "listing_match_v1.md").mkdir()
    code, out, err, factory = run(capsys, BASE + ["--llm", "--confirm-spend"])
    assert code == 1
    assert len(error_lines(err)) == 1 and err.startswith("Error:")
    assert "Traceback" not in err
    assert factory.calls == []


def _set_row(index, **changes):
    def mutate(document):
        document["predictions"][index].update(changes)
    return mutate


def _set_run(**changes):
    def mutate(document):
        document["run"].update(changes)
    return mutate


# predictions[0] is an ok row (line 1); predictions[10] is a failed row (line 11).
MALFORMED = {
    "status_not_ok_or_failed": _set_row(0, status="maybe"),
    "status_missing_value_none": _set_row(0, status=None),
    "failed_row_failure_none": _set_row(10, failure=None),
    "failed_row_failure_not_string": _set_row(10, failure=7),
    "ok_row_confidence_unknown": _set_row(0, confidence="extreme"),
    "ok_row_confidence_none": _set_row(0, confidence=None),
    "ok_row_confidence_uppercase": _set_row(0, confidence="HIGH"),
    "ok_row_confidence_list": _set_row(0, confidence=["high"]),
    "ok_row_item_object": _set_row(0, item={"a": 1}),
    "input_tokens_negative": _set_run(input_tokens=-1),
    "input_tokens_float": _set_run(input_tokens=1.5),
    "input_tokens_string": _set_run(input_tokens="1200"),
    "output_tokens_negative": _set_run(output_tokens=-5),
    "output_tokens_none": _set_run(output_tokens=None),
    "cost_negative": _set_run(cost_usd=-0.01),
    "cost_string": _set_run(cost_usd="0.0158"),
    "cost_none": _set_run(cost_usd=None),
}


@pytest.mark.parametrize("name", sorted(MALFORMED))
def test_c39e_malformed_prediction_values_exit_1_with_malformed_message(
        capsys, tmp_path, monkeypatch, prompts, name):
    result = run_llm(capsys, tmp_path, monkeypatch)
    assert result.code == 0, result.err
    document = json.loads(result.saved.read_text(encoding="utf-8"))
    MALFORMED[name](document)
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(document), encoding="utf-8")

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    code, out, err, factory = run(capsys, BASE + ["--from-predictions", str(bad)])
    assert code == 1
    assert err.startswith("Error:") and "predictions file is malformed" in err
    assert len(error_lines(err)) == 1
    assert "Traceback" not in err
    assert factory.calls == []
    assert parse_report(out)["metrics"] == {}  # no report was printed


def test_c39e_valid_boundary_values_are_still_accepted(capsys, tmp_path, monkeypatch, prompts):
    result = run_llm(capsys, tmp_path, monkeypatch)
    document = json.loads(result.saved.read_text(encoding="utf-8"))
    document["run"].update(input_tokens=0, output_tokens=0, cost_usd=0)
    ok = tmp_path / "zero.json"
    ok.write_text(json.dumps(document), encoding="utf-8")
    code, out, err, _ = run(capsys, BASE + ["--from-predictions", str(ok)])
    assert code == 0, err


def test_c39f_per_tag_header_has_no_trailing_spaces(capsys, tmp_path, monkeypatch, prompts):
    result = run_llm(capsys, tmp_path, monkeypatch)
    assert result.code == 0, result.err
    lines = result.out.splitlines()
    start = next(i for i, l in enumerate(lines) if "per-tag" in l.lower())
    first_tag = next(i for i, l in enumerate(lines)
                     if i > start and l.split() and l.split()[0].lower() in TAG_NAMES)
    header = lines[start:first_tag]
    assert len(header) >= 2, "expected a section title and a column header before the tag rows"
    for line in header:
        assert line == line.rstrip(), f"trailing spaces in {line!r}"


def test_c39_report_notes_billed_timeout_attempts_are_not_in_the_actual_cost(
        capsys, tmp_path, monkeypatch, prompts):
    result = run_llm(capsys, tmp_path, monkeypatch)
    assert result.code == 0, result.err
    lines = result.out.splitlines()
    cost_at = next(i for i, l in enumerate(lines) if "actual cost" in l.lower())
    notes = [l for l in lines[cost_at + 1:]
             if "timed" in l.lower() and not l.lower().startswith("predictions saved")]
    assert len(notes) == 1, f"expected one line about timed-out attempts under the cost:\n{result.out}"
    assert "not" in notes[0].lower()


def _factory_without_anthropic():
    def factory(*args, **kwargs):
        raise ModuleNotFoundError("No module named 'anthropic'", name="anthropic")
    return factory


def test_c39_failed_run_removes_the_claimed_file_and_the_directory_it_created(
        capsys, tmp_path, monkeypatch, prompts):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    newdir = tmp_path / "newdir"
    saved = newdir / "p.json"
    code, out, err, _ = run(
        capsys, BASE + ["--llm", "--confirm-spend", "--save-predictions", str(saved)],
        _factory_without_anthropic())
    assert code == 1
    assert err.startswith("Error:")
    assert not saved.exists()
    assert not newdir.exists()


def test_c39_failed_run_keeps_a_directory_that_existed_before_the_run(
        capsys, tmp_path, monkeypatch, prompts):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    existing = tmp_path / "olddir"
    existing.mkdir()
    saved = existing / "p.json"
    code, out, err, _ = run(
        capsys, BASE + ["--llm", "--confirm-spend", "--save-predictions", str(saved)],
        _factory_without_anthropic())
    assert code == 1
    assert err.startswith("Error:")
    assert not saved.exists()
    assert existing.is_dir()


def test_c39_failed_run_with_dotdot_path_removes_only_what_it_created(
        capsys, tmp_path, monkeypatch, prompts):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    monkeypatch.chdir(tmp_path)
    existing = tmp_path / "existing"
    existing.mkdir()
    code, out, err, _ = run(
        capsys, BASE + ["--llm", "--confirm-spend", "--save-predictions", "x/../existing/sub/f.json"],
        _factory_without_anthropic())
    assert code == 1
    assert err.startswith("Error:")
    assert existing.is_dir()
    assert not (existing / "sub").exists()
    assert not (tmp_path / "x").exists()


def test_c39_disk_full_while_saving_exits_1_with_one_error_and_cleans_up(
        capsys, tmp_path, monkeypatch, prompts):
    import errno
    import io
    import os

    target = tmp_path / "newdir" / "p.json"
    real_open = Path.open

    class FullRaw(io.RawIOBase):
        """Every write hits a full disk, so bytes stay buffered and flush/close both raise."""

        def __init__(self, fd, name):
            super().__init__()
            self.fd = fd
            self.name = name  # real file handles expose .name; error messages use it

        def writable(self):
            return True

        def write(self, data):
            raise OSError(errno.ENOSPC, "No space left on device")

        def close(self):
            if not self.closed:
                os.close(self.fd)
            super().close()

    def full_open(self, mode="r", *args, **kwargs):
        if self == target and "x" in mode:
            fd = os.open(self, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            return io.TextIOWrapper(io.BufferedWriter(FullRaw(fd, str(self))), encoding="utf-8")
        return real_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", full_open)
    result = run_llm(capsys, tmp_path, monkeypatch, predictions="newdir/p.json")
    assert result.code == 1
    assert len(error_lines(result.err)) == 1 and result.err.startswith("Error:")
    assert "cannot write predictions" in result.err
    assert "Traceback" not in result.err
    assert not target.exists()
    assert not target.parent.exists()
