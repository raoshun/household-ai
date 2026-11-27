"""`app.db.database` の接続・マイグレーション・timestamp 変換の検証。

契約 §1 決定 1.20 と 契約 §2.4.5 実測 13-14 で確定した timestamp の
TEXT + CHECK (3 節 pattern) を、実マイグレーション適用済みの DB で受理・
拒否の実値として検証する。拒否する実値は schema のコメントに書いた
GLOB / 範囲外 / calendar 不能 (2026-02-30 など) の落とし穴 (実測 14) に
対応させる。DB は tmp_path に作り、実行環境の `MIGRATIONS_DIR` の実 .sql を
読む (container では backend/app が read-only mount なので書き込みは DB だけ)。
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.db.database import (
    apply_migrations,
    from_db_timestamp,
    init_database,
    to_db_timestamp,
)

VALID_TS = "2026-01-01T00:00:00.000000+00:00"

# 実測 13 で受理を確認した境界 (うるう日・末尾 6 桁)
ACCEPT_TS = [
    "2000-01-01T00:00:00.000000+00:00",
    "2028-02-29T23:59:59.999999+00:00",
    VALID_TS,
]

# 実測 14・契約 §2.1 の規定から落とす実値 (offset は +00:00 のみ・秒以降の
# 形・calendar 不能・空 text など)
REJECT_TS = [
    "2026-01-01T00:00:00+00:00",
    "2026-01-01T00:00:00.123+00:00",
    "2026-01-01T00:00:00.000000+09:00",
    "2026-01-01T00:00:00.000000Z",
    "2026-01-01 00:00:00.000000+00:00",
    "2026-02-29T00:00:00.000000+00:00",
    "2026-02-30T00:00:00.000000+00:00",
    "2026-13-01T00:00:00.000000+00:00",
    "9999-99-99T99:99:99.000000+00:00",
    "",
    "not-a-timestamp",
]


def _insert_rule(conn: sqlite3.Connection, rule_id: str, ts: str) -> None:
    """rules に 1 行入れる (NOT NULL 列をすべて渡す)。"""
    conn.execute(
        "INSERT INTO rules (id, title, category, status, rule_type,"
        " current_version, file_path, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            rule_id,
            "テスト",
            "parenting",
            "draft",
            "strict",
            1,
            "data/rules/parenting/test/v1.md",
            ts,
            ts,
        ),
    )


def _insert_rule_version(
    conn: sqlite3.Connection,
    rule_id: str,
    version: int,
    hash_value: str,
) -> None:
    """rule_versions に 1 行入れる (id は rowid 別名なので自動採番に任せる)。"""
    conn.execute(
        "INSERT INTO rule_versions (rule_id, version, content_hash,"
        " created_by, created_at) VALUES (?, ?, ?, ?, ?)",
        (rule_id, version, hash_value, "tester", VALID_TS),
    )


@pytest.fixture
def db_conn(tmp_path):
    """tmp_path に実マイグレーションを適用した接続を返す。"""
    conn = init_database(tmp_path / "data" / "household.db")
    yield conn
    conn.close()


def test_init_database_creates_parent_and_tables(tmp_path, db_conn) -> None:
    # connect() が親ディレクトリを作る (実装判断) ことと 4 table の存在
    assert (tmp_path / "data" / "household.db").exists()
    names = {
        row["name"]
        for row in db_conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert {"rules", "rule_versions", "approvals", "judgment_logs"} <= names


def test_migrations_record_user_version_and_are_idempotent(db_conn) -> None:
    version = int(db_conn.execute("PRAGMA user_version").fetchone()[0])
    assert version == 2  # 0001 と 0002 の 2 file 分 (契約 §1 決定 1.21・1.22)
    assert apply_migrations(db_conn) == 2  # 再実行しても昇らず table も壊れない
    count = db_conn.execute("SELECT COUNT(*) FROM rules").fetchone()[0]
    assert count == 0


def test_foreign_keys_are_enforced(db_conn) -> None:
    assert int(db_conn.execute("PRAGMA foreign_keys").fetchone()[0]) == 1
    with pytest.raises(sqlite3.IntegrityError):
        # 存在しない rules.id を参照する rule_versions は弾かれる
        _insert_rule_version(db_conn, "parenting-999", 1, "sha")


def test_version_unique_per_rule(db_conn) -> None:
    _insert_rule(db_conn, "parenting-001", VALID_TS)
    _insert_rule_version(db_conn, "parenting-001", 1, "sha1")
    with pytest.raises(sqlite3.IntegrityError):
        _insert_rule_version(db_conn, "parenting-001", 1, "sha2")
    _insert_rule_version(db_conn, "parenting-001", 2, "sha3")  # 別 version は可


@pytest.mark.parametrize("ts", ACCEPT_TS)
def test_timestamp_check_accepts(db_conn, ts: str) -> None:
    _insert_rule(db_conn, "parenting-001", ts)


@pytest.mark.parametrize("ts", REJECT_TS)
def test_timestamp_check_rejects(db_conn, ts: str) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _insert_rule(db_conn, "parenting-001", ts)
    db_conn.rollback()


def test_no_ddl_clock_default(db_conn) -> None:
    # DDL に既定の時刻式を置かない規定 (契約 §2.1 L119): created_at 抜きが
    # NOT NULL で落ち、NULL 可能列 (approved_at / archived_at) は省略すると
    # NULL になり、代入は呼び出し側が行うことを確認する。
    with pytest.raises(sqlite3.IntegrityError):
        db_conn.execute(
            "INSERT INTO rules (id, title, category, status, rule_type,"
            " current_version, file_path, updated_at)"
            " VALUES ('parenting-001', 't', 'parenting', 'draft', 'strict',"
            " 1, 'data/rules/parenting/parenting-001/v1.md',"
            " '2026-01-01T00:00:00.000000+00:00')"
        )
    _insert_rule(db_conn, "parenting-002", VALID_TS)
    row = db_conn.execute(
        "SELECT approved_at, archived_at FROM rules WHERE id = 'parenting-002'"
    ).fetchone()
    assert row["approved_at"] is None and row["archived_at"] is None


def test_to_db_timestamp_rejects_naive() -> None:
    with pytest.raises(ValueError):
        to_db_timestamp(datetime(2026, 1, 1, 0, 0, 0))


def test_to_db_timestamp_converts_offset_to_utc() -> None:
    jst = timezone(timedelta(hours=9))
    value = datetime(2026, 1, 1, 0, 0, 0, tzinfo=jst)
    assert to_db_timestamp(value) == "2025-12-31T15:00:00.000000+00:00"


def test_timestamp_roundtrip() -> None:
    value = datetime(2026, 7, 2, 13, 45, 12, 123456, tzinfo=UTC)
    text = to_db_timestamp(value)
    assert from_db_timestamp(text) == value
    assert from_db_timestamp(text).tzinfo is not None
