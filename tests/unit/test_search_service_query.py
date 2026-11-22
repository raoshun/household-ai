"""search_service の MATCH builder・検索実行・refresh のテスト
(決定 1.7〜1.9・1.21・1.22)。

builder は pinned image の probe (契約 §2.6.1 実測 11-12) で確定した形を文字列で検証し、
検索実行は実マイグレーション適用済み DB (0002 で rule_search_index と
rule_versions.summary が入る。決定 1.22) で実 SQL を流す。join の
rule_versions を rules.current_version が指す版に限定する対応付けは probe に無い
拡張なので、この file が実 schema で唯一の gate になる。
"""

from __future__ import annotations

import sqlite3

import pytest

from app.db.database import init_database
from app.services.search_service import (
    MAX_SEARCH_LIMIT,
    SearchTerms,
    build_match_query,
    refresh_rule_index,
    search_rules,
    split_search_text,
)

VALID_TS = "2026-01-01T00:00:00.000000+00:00"


# --- builder (SQL 不要) ---------------------------------------------------


def test_cjk_group_becomes_quoted_or_group() -> None:
    terms = split_search_text("子ども")
    assert build_match_query(terms) == '("子ど" OR "ども" OR "子ども")'


def test_groups_join_with_and_cjk_before_ascii() -> None:
    terms = split_search_text("ねる abc")
    # cjk_groups の後に ascii_groups を並べ、group 間を AND で繋ぐ (決定 1.9・1.21)。
    assert build_match_query(terms) == '("ねる") AND ("abc")'


def test_multiple_cjk_runs_keep_left_to_right_order() -> None:
    terms = split_search_text("おふろ ねる")
    # 1 字語は除外され、用語群は (a OR b) AND (c) の AND 順で並ぶ
    assert build_match_query(terms) == '("おふ" OR "ふろ" OR "おふろ") AND ("ねる")'


def test_double_quote_inside_term_doubles() -> None:
    # splitter の語彙に `"` は出ないが、規定 (実測 12 の受理形) どおり 2 連続化する。
    terms = SearchTerms(
        cjk_terms=['x"y'],
        ascii_terms=[],
        cjk_groups=[['x"y']],
        ascii_groups=[],
    )
    assert build_match_query(terms) == '("x""y")'


def test_vocabulary_free_terms_yield_none() -> None:
    empty = SearchTerms(cjk_terms=[], ascii_terms=[], cjk_groups=[], ascii_groups=[])
    assert build_match_query(empty) is None
    assert build_match_query(split_search_text(" -- @@ ")) is None


# --- 検索実行 (実マイグレーション DB) --------------------------------------


def _insert_rule(
    conn: sqlite3.Connection,
    rule_id: str,
    *,
    status: str = "active",
    category: str = "parenting",
    current_version: int = 1,
    title: str = "ねるルール",
) -> None:
    conn.execute(
        "INSERT INTO rules (id, title, category, status, rule_type,"
        " current_version, file_path, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            rule_id,
            title,
            category,
            status,
            "strict",
            current_version,
            f"data/rules/{category}/{rule_id}/v1.md",
            VALID_TS,
            VALID_TS,
        ),
    )


def _insert_version(
    conn: sqlite3.Connection,
    rule_id: str,
    version: int,
    summary: str | None,
) -> None:
    conn.execute(
        "INSERT INTO rule_versions (rule_id, version, content_hash,"
        " created_by, created_at, summary)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (rule_id, version, f"sha{version}", "tester", VALID_TS, summary),
    )


def _index(conn: sqlite3.Connection, rule_id: str, text: str) -> None:
    refresh_rule_index(conn, rule_id, split_search_text(text))


@pytest.fixture
def db_conn(tmp_path):
    conn = init_database(tmp_path / "data" / "household.db")
    yield conn
    conn.close()


def test_search_projects_six_keys_from_current_version(db_conn) -> None:
    _insert_rule(db_conn, "parenting-001", current_version=2)
    _insert_version(db_conn, "parenting-001", 1, "古い要約")
    _insert_version(db_conn, "parenting-001", 2, "おふろの要約")
    _index(db_conn, "parenting-001", "おふろ")
    rows = search_rules(db_conn, "おふろ")
    assert len(rows) == 1
    row = rows[0]
    # 決定 1.8 の 6 key のみ (full Markdown body 等は入れない)
    assert set(row) == {
        "id",
        "title",
        "category",
        "status",
        "summary",
        "current_version",
    }
    assert row["id"] == "parenting-001"
    assert row["title"] == "ねるルール"
    assert row["category"] == "parenting"
    assert row["status"] == "active"
    assert row["summary"] == "おふろの要約"  # 現 version の summary (決定 1.8・1.22)
    assert row["current_version"] == 2


def test_search_default_status_active_excludes_others(db_conn) -> None:
    for rule_id, status in [
        ("parenting-001", "active"),
        ("parenting-002", "draft"),
        ("parenting-003", "pending_approval"),
        ("parenting-004", "rejected"),
        ("parenting-005", "archived"),
    ]:
        _insert_rule(db_conn, rule_id, status=status)
        _insert_version(db_conn, rule_id, 1, "おふろ")
        _index(db_conn, rule_id, "おふろ")
    rows = search_rules(db_conn, "おふろ")
    assert [row["id"] for row in rows] == ["parenting-001"]  # 既定 active (決定 1.6)
    rows = search_rules(db_conn, "おふろ", status="draft")
    assert [row["id"] for row in rows] == ["parenting-002"]


def test_search_category_filter_on_rules_side(db_conn) -> None:
    # 実測 16: category・status は rules との join 側で絞る (fts 側ではない)
    _insert_rule(db_conn, "parenting-001", category="parenting")
    _insert_version(db_conn, "parenting-001", 1, "おふろ")
    _index(db_conn, "parenting-001", "おふろ")
    assert search_rules(db_conn, "おふろ", category="parenting") != []
    assert search_rules(db_conn, "おふろ", category="housework") == []


def test_search_limit_clamp_between_zero_and_twenty(db_conn) -> None:
    # 決定 1.7: limit は既定 10・上限 20 で clamp。負値は 0 (SQLite の負 LIMIT は
    # 無制限になるため通さない)。
    for i in range(1, MAX_SEARCH_LIMIT + 6):  # 25 件一致
        rule_id = f"parenting-{i:03d}"
        _insert_rule(db_conn, rule_id)
        _insert_version(db_conn, rule_id, 1, "ねる")
        _index(db_conn, rule_id, "ねる")
    assert len(search_rules(db_conn, "ねる")) == 10  # 既定 10
    assert len(search_rules(db_conn, "ねる", limit=25)) == MAX_SEARCH_LIMIT
    assert len(search_rules(db_conn, "ねる", limit=-1)) == 0
    assert len(search_rules(db_conn, "ねる", limit=3)) == 3


def test_search_orders_by_bm25_then_rule_id(db_conn) -> None:
    # score 同士の差が出ない同一文書を 2 件入れ、tiebreak の rule_id ASC を見る。
    for rule_id in ("parenting-010", "parenting-002"):
        _insert_rule(db_conn, rule_id)
        _insert_version(db_conn, rule_id, 1, "おふろ")
        _index(db_conn, rule_id, "おふろ")
    rows = search_rules(db_conn, "おふろ")
    assert [row["id"] for row in rows] == ["parenting-002", "parenting-010"]


def test_search_vocabulary_free_query_skips_sql(db_conn) -> None:
    # 空 MATCH は FTS5 が syntax error (実測 12)。schema の無い接続でも呼べる
    # = SQL 実行前に short-circuit している証左。
    assert search_rules(sqlite3.connect(":memory:"), " @@ -- ") == []
    assert db_conn.execute("SELECT COUNT(*) FROM rule_search_index").fetchone()[0] == 0


def test_refresh_replaces_previous_index_rows(db_conn) -> None:
    # FTS5 に upsert は無い (実測 7・18)。DELETE→INSERT で古い行を残さない。
    _insert_rule(db_conn, "parenting-001")
    _insert_version(db_conn, "parenting-001", 1, "ねる")
    _index(db_conn, "parenting-001", "ねる")
    assert len(search_rules(db_conn, "ねる")) == 1
    _index(db_conn, "parenting-001", "おふろ")
    assert search_rules(db_conn, "ねる") == []  # 古い term の行は消える
    assert len(search_rules(db_conn, "おふろ")) == 1
    count = db_conn.execute("SELECT COUNT(*) FROM rule_search_index").fetchone()[0]
    assert count == 1


def test_search_ascii_term_hits_ascii_cell(db_conn) -> None:
    _insert_rule(db_conn, "parenting-001")
    _insert_version(db_conn, "parenting-001", 1, "ねる")
    _index(db_conn, "parenting-001", "ねる Sleep")
    # index 側の大文字 Sleep は splitter で小文字 sleep になり (実測 5)、
    # query も同一 splitter を通る (実測 3)。
    assert [row["id"] for row in search_rules(db_conn, "sleep")] == ["parenting-001"]
    assert search_rules(db_conn, "sleeping") == []
