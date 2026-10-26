"""RC-03b criteria 1-7 (shared matcher) and 44-45 (real catalog keywords)."""
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def title_matches():
    # imported lazily so a missing module fails each test, not collection
    from matching import title_matches as fn

    return fn


def test_rc03b_c1_title_is_lowercased(title_matches):
    assert title_matches("Rick Owens RAMONES Leather", ("ramones",), ()) is True


def test_rc03b_c2_any_one_include_keyword_is_enough(title_matches):
    kws = ("ramones", "ramon")
    assert title_matches("Rick Owens Geobasket", kws, ()) is False
    assert title_matches("Rick Owens Ramon High", kws, ()) is True


def test_rc03b_c3_exclude_wins_over_include(title_matches):
    assert title_matches("Rick Owens DRKSHDW Ramones", ("ramones",), ("drkshdw",)) is False


def test_rc03b_c4_substring_semantics_are_pinned(title_matches):
    assert title_matches("Ramones Yellow", ("ramones",), ("low",)) is False
    assert (
        title_matches("リックオウエンス ポッドショーツ 46", ("pod short", "ポッドショーツ"), ("pants",))
        is True
    )


def test_rc03b_c5_keywords_are_not_normalized(title_matches):
    assert title_matches("rick owens ramones", ("RAMONES",), ()) is False


def test_rc03b_c6_empty_keywords_and_lists_accepted(title_matches):
    assert title_matches("anything", (), ()) is False
    assert title_matches("Ramones", ["ramones"], []) is True


def test_rc03b_c7_live_ebay_fixture_matches_the_old_inline_rule(title_matches, fixtures_dir):
    data = json.loads((fixtures_dir / "ebay" / "search_live.json").read_text(encoding="utf-8"))
    summaries = data["itemSummaries"]
    assert len(summaries) == 50
    keywords = ("ramones", "ramon")
    excludes = ("drkshdw", "canvas", "low")
    matched = 0
    for s in summaries:
        t = s["title"]
        old = not any(x in t.lower() for x in excludes) and any(k in t.lower() for k in keywords)
        assert title_matches(t, keywords, excludes) == old, t
        matched += old
    assert matched == 9


# ---- Real catalog (criteria 44-45) ---------------------------------------------------


def _real_entries():
    import db

    parsed = json.loads((REPO_ROOT / "src" / "catalog.json").read_text(encoding="utf-8"))
    return {e.name: e for e in db.validate_catalog(parsed)}


def test_rc03b_c44_real_catalog_ramones_keywords_in_catalog_order():
    entries = _real_entries()
    ramones = entries["rick owens ramones"]
    assert ramones.keywords == ("ramones", "ramon", "ラモーンズ")
    assert ramones.exclude_keywords == ("drkshdw", "canvas", "low", "ローカット")


def test_rc03b_c44_real_catalog_pod_shorts_unchanged():
    pods = _real_entries()["pod shorts"]
    assert pods.keywords == ("pod short", "ポッドショーツ")
    assert pods.exclude_keywords == ("pants",)
    assert pods.search_query == "rick owens pod shorts"


def test_rc03b_c45_live_yahoo_fixture_keeps_one_high_top(title_matches, fixtures_dir):
    import yahoo_scrape

    ramones = _real_entries()["rick owens ramones"]
    html = (fixtures_dir / "yahoo" / "brand_page_live.html").read_text(encoding="utf-8")
    titles = [l.title for l in yahoo_scrape.parse_listings(html)]

    kept = [t for t in titles if title_matches(t, ramones.keywords, ramones.exclude_keywords)]
    assert len(kept) == 1
    assert "ハイカット" in kept[0]

    # the includes alone catch 3 listings; the excludes must be what drops the other two
    included = [t for t in titles if title_matches(t, ramones.keywords, ())]
    assert len(included) == 3
    dropped = [t for t in included if t not in kept]
    assert any("drkshdw" in t.lower() for t in dropped)
    assert any("ローカット" in t for t in dropped)
