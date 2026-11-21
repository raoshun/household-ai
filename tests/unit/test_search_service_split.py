"""search_service の term 分割規則のテスト（決定 1.9・決定 1.21、実測 §2.6.1）。"""

from __future__ import annotations

from app.services.search_service import split_search_text


def test_cjk_run_produces_contiguous_bigrams_and_trigrams() -> None:
    terms = split_search_text("子ども")
    assert terms.cjk_terms == ["子ど", "ども", "子ども"]
    assert terms.cjk_groups == [["子ど", "ども", "子ども"]]
    assert terms.ascii_terms == []


def test_single_cjk_char_run_is_kept_as_one_char_term() -> None:
    terms = split_search_text("ね")
    assert terms.cjk_terms == ["ね"]
    assert terms.cjk_groups == [["ね"]]


def test_two_cjk_runs_split_into_separate_groups() -> None:
    terms = split_search_text("子ども ねる")
    assert terms.cjk_groups == [["子ど", "ども", "子ども"], ["ねる"]]
    # OR group を跨いだ union にはならない（intersection 重視、実測 11）。
    assert terms.cjk_terms[:2] == ["子ど", "ども"]


def test_ascii_runs_are_split_lowered_and_short_words_dropped() -> None:
    terms = split_search_text("Sleep ab uFx123 a1b2c3")
    assert terms.ascii_terms == ["sleep", "ufx123", "a1b2c3"]
    assert terms.ascii_groups == [["sleep"], ["ufx123"], ["a1b2c3"]]


def test_ascii_hyphen_and_punctuation_are_boundaries() -> None:
    terms = split_search_text("pull-request v2.0.0")
    assert terms.ascii_terms == ["pull", "request"]
    assert terms.ascii_groups == [["pull"], ["request"]]


def test_long_mark_is_cjk_and_indexed_but_symbols_are_dropped() -> None:
    terms = split_search_text("ソースコード @#100%")
    assert "ー" in "".join(t for g in terms.cjk_groups for t in g)
    assert any("ード" in t for group in terms.cjk_groups for t in group)
    joined = "".join(terms.cjk_terms)
    assert "@" not in joined and "#" not in joined and "%" not in joined


def test_mixed_text_keeps_both_layers_separate() -> None:
    terms = split_search_text("ねる Sleep")
    assert terms.cjk_groups == [["ねる"]]
    assert terms.ascii_groups == [["sleep"]]


def test_empty_and_symbol_only_text_is_empty() -> None:
    assert split_search_text("").is_empty
    assert split_search_text("  --  @@ ## ").is_empty


def test_cjk_query_uses_same_terms_as_index_side() -> None:
    indexed = split_search_text("おふろ ねる")
    query = split_search_text("おふろ")
    query_terms = [t for group in query.cjk_groups for t in group]
    index_terms = [t for group in indexed.cjk_groups for t in group]
    assert all(term in index_terms for term in query_terms)
