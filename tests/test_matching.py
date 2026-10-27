"""RC-17 acceptance tests for the shared word-aware keyword matcher."""
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def title_matches():
    # Import lazily so missing matcher behavior fails each test, not collection.
    from matching import title_matches as fn

    return fn


def test_rc17_c1_nfkc_normalizes_title_and_keywords(title_matches):
    assert title_matches("ＲＡＭＯＮＥＳ", ("ramones",), ()) is True
    assert title_matches("Ramones", ("ＲＡＭＯＮＥＳ",), ()) is True


def test_rc17_c2_keywords_are_lowercased_at_match_time(title_matches):
    assert title_matches("rick owens ramones", ("RAMONES",), ()) is True


def test_rc17_c3_excludes_use_latin_word_boundaries(title_matches):
    assert title_matches("Ramones Yellow", ("ramones",), ("low",)) is True
    for title in ("Ramones low", "Ramones LOW-top", "Ramones low_top"):
        assert title_matches(title, ("ramones",), ("low",)) is False


def test_rc17_c4_latin_include_respects_ascii_letter_digit_edges(title_matches):
    for title in (
        "Ramones",
        "Rick Owens Ramones jacket",
        "ramones leather",
        "Leather, Ramones!",
        "ラモーンズRamonesスニーカー",
        "Ramones-low",
    ):
        assert title_matches(title, ("ramones",), ()) is True

    for title in ("superramones", "ramones42", "ramonesx"):
        assert title_matches(title, ("ramones",), ()) is False


def test_rc17_c5_latin_exclude_respects_same_word_boundaries(title_matches):
    for title in ("Low Ramones", "Ramones-low"):
        assert title_matches(title, ("ramones",), ("low",)) is False

    for title in ("Yellow Ramones", "Follow Ramones"):
        assert title_matches(title, ("ramones",), ("low",)) is True


def test_rc17_c6_japanese_keywords_keep_literal_substring_matching(title_matches):
    assert title_matches("リックオウエンスポッドショーツ", ("ポッドショーツ",), ()) is True
    assert title_matches("Rick Owens ローカット Ramones", ("ramones",), ("ローカット",)) is False


def test_rc17_c7_multiword_and_punctuation_are_literal_after_nfkc(title_matches):
    for title in ("POD SHORT", "pod short-42", "ポッドpod shortショーツ"):
        assert title_matches(title, ("pod short",), ()) is True

    assert title_matches("pod shorts", ("pod short",), ()) is False
    assert title_matches("pod  short", ("pod short",), ()) is False
    assert title_matches("pod　short", ("pod short",), ()) is True


def test_rc17_c8_empty_inputs_and_blank_keywords(title_matches):
    assert title_matches("Ramones", (), ()) is False
    assert title_matches("", ("ramones",), ()) is False
    assert title_matches("Ramones", ("", "   ", "\t\n"), ()) is False
    assert title_matches("Ramones", ("ramones",), ("", "   ")) is True
    assert title_matches("Ramones", ["ramones"], []) is True


@pytest.mark.parametrize(
    ("title", "includes", "excludes"),
    [
        (None, ("ramones",), ()),
        ("Ramones", (None,), ()),
        ("Ramones", ("ramones",), (None,)),
    ],
)
def test_rc17_edge_none_non_string_inputs_raise_type_error(title_matches, title, includes, excludes):
    with pytest.raises(TypeError):
        title_matches(title, includes, excludes)


def test_rc17_c9_exclude_precedence_and_duplicates(title_matches):
    assert title_matches("Ramones low", ("ramones", "ramones"), ("low", "low")) is False
    assert title_matches("Ramones", ("ramones", "ramones"), ("low", "low")) is True


def test_rc17_c9_accepts_one_shot_iterables(title_matches):
    includes = (value for value in ("unused", "ramones"))
    excludes = (value for value in ("canvas", "low"))
    assert title_matches("Ramones", includes, excludes) is True

    includes = (value for value in ("ramones", "ramones"))
    excludes = (value for value in ("low", "canvas"))
    assert title_matches("Ramones low", includes, excludes) is False


def test_rc17_c10_jobs_share_matcher_signature_and_do_not_make_requests(title_matches):
    import inspect
    import matching
    import ebay_pricing
    import yahoo_scrape

    assert yahoo_scrape.title_matches is matching.title_matches is title_matches
    assert ebay_pricing.title_matches is matching.title_matches
    assert tuple(inspect.signature(matching.title_matches).parameters) == (
        "title", "keywords", "exclude_keywords"
    )
    # The matcher has no network boundary; these calls execute with the suite's
    # socket and DNS network guards active.
    assert matching.title_matches("Ramones", ("ramones",), ()) is True


def _real_entries():
    import db

    parsed = json.loads((REPO_ROOT / "src" / "catalog.json").read_text(encoding="utf-8"))
    return {entry.name: entry for entry in db.validate_catalog(parsed)}


def test_rc03b_c44_real_catalog_ramones_keywords_in_catalog_order():
    ramones = _real_entries()["rick owens ramones"]
    assert ramones.keywords == ("ramones", "ramon", "ラモーンズ")
    assert ramones.exclude_keywords == ("drkshdw", "canvas", "low", "ローカット")


def test_rc03b_c44_real_catalog_pod_shorts_unchanged():
    pods = _real_entries()["pod shorts"]
    assert pods.keywords == ("pod short", "ポッドショーツ")
    assert pods.exclude_keywords == ("pants",)
    assert pods.search_query == "rick owens pod shorts"


def _old_substring_match(title, keywords, exclude_keywords):
    normalized = title.lower()
    return not any(keyword in normalized for keyword in exclude_keywords) and any(
        keyword in normalized for keyword in keywords
    )


@pytest.mark.parametrize(
    ("item_name", "expected_old", "expected_new", "expected_changed_ids"),
    [
        ("rick owens ramones", 1, 1, set()),
        ("pod shorts", 0, 0, set()),
    ],
)
def test_rc17_c11_yahoo_fixture_old_new_counts_and_changed_ids(
    title_matches, fixtures_dir, item_name, expected_old, expected_new, expected_changed_ids
):
    import yahoo_scrape

    entries = _real_entries()
    item = entries[item_name]
    html = (fixtures_dir / "yahoo" / "brand_page_live.html").read_text(encoding="utf-8")
    listings = yahoo_scrape.parse_listings(html)
    assert len(listings) == 100

    old_ids = {
        listing.auction_id
        for listing in listings
        if _old_substring_match(listing.title, item.keywords, item.exclude_keywords)
    }
    new_ids = {
        listing.auction_id
        for listing in listings
        if title_matches(listing.title, item.keywords, item.exclude_keywords)
    }
    assert len(old_ids) == expected_old
    assert len(new_ids) == expected_new
    assert old_ids ^ new_ids == expected_changed_ids


def test_rc03b_c45_yahoo_fixture_include_matches_and_excludes_drop_bad_titles(title_matches, fixtures_dir):
    import yahoo_scrape

    item = _real_entries()["rick owens ramones"]
    html = (fixtures_dir / "yahoo" / "brand_page_live.html").read_text(encoding="utf-8")
    listings = yahoo_scrape.parse_listings(html)

    included = [listing for listing in listings if title_matches(listing.title, item.keywords, ())]
    kept = [
        listing
        for listing in listings
        if title_matches(listing.title, item.keywords, item.exclude_keywords)
    ]
    assert len(included) == 3
    assert len(kept) == 1
    dropped = [listing.title for listing in included if listing not in kept]
    assert any("drkshdw" in title.lower() for title in dropped)
    assert any("ローカット" in title for title in dropped)


@pytest.mark.parametrize(
    ("item_name", "expected_old", "expected_new", "expected_changed_ids"),
    [
        ("rick owens ramones", 9, 8, {"v1|178432099849|0"}),
        ("pod shorts", 0, 0, set()),
    ],
)
def test_rc17_c12_ebay_fixture_old_new_counts_and_changed_ids(
    title_matches, fixtures_dir, item_name, expected_old, expected_new, expected_changed_ids
):
    data = json.loads((fixtures_dir / "ebay" / "search_live.json").read_text(encoding="utf-8"))
    summaries = data["itemSummaries"]
    assert len(summaries) == 50

    item = _real_entries()[item_name]
    old_ids = {
        summary["itemId"]
        for summary in summaries
        if _old_substring_match(summary["title"], item.keywords, item.exclude_keywords)
    }
    new_ids = {
        summary["itemId"]
        for summary in summaries
        if title_matches(summary["title"], item.keywords, item.exclude_keywords)
    }
    assert len(old_ids) == expected_old
    assert len(new_ids) == expected_new
    assert old_ids ^ new_ids == expected_changed_ids
    if item_name == "rick owens ramones":
        removed_id = "v1|178432099849|0"
        removed = next(summary for summary in summaries if summary["itemId"] == removed_id)
        assert removed["title"] == (
            "Size 41.5EU/8.5US - Rick Owens Textured Leather Ramone High Top"
        )
