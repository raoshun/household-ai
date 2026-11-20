"""マイグレーションファイルの収集と適用順の検証。

契約 §2.4.4 (README §3 L131-172 の構成) に置き場所を決めた
`db/migrations/NNNN_*.sql` について、採用した 4 桁連番と user_version による
既適用管理 (実装判断) を tmp_dir のダミー .sql で検証する。実ファイルの
昇順適用・スキップ・不正名拒否 (MigrationError) を対象とし、.schema 本文の
受理・拒否は tests/unit/test_database.py が実 DB で行う。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.db import database as database_module
from app.db.database import MIGRATIONS_DIR, MigrationError, apply_migrations, connect


def _write_migration(dir_path: Path, name: str, table: str) -> None:
    dir_path.joinpath(name).write_text(
        f"CREATE TABLE {table} (id TEXT PRIMARY KEY);\n",
        encoding="utf-8",
    )


def test_real_migrations_dir_holds_expected_scripts() -> None:
    names = sorted(p.name for p in MIGRATIONS_DIR.glob("*.sql"))
    assert names == ["0001_create_rule_tables.sql"]


def test_real_migrations_create_four_tables(tmp_path) -> None:
    conn = connect(tmp_path / "household.db")
    applied = apply_migrations(conn)
    assert applied == 1
    names = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert {"rules", "rule_versions", "approvals", "judgment_logs"} <= names
    conn.close()


def test_scripts_apply_in_ascending_order(tmp_path, monkeypatch) -> None:
    # 4 桁連番 (実装判断) の昇順で 0001 -> 0002 と適用されること。
    src = tmp_path / "migrations"
    src.mkdir()
    _write_migration(src, "0002_second.sql", "second")
    _write_migration(src, "0001_first.sql", "first")
    monkeypatch.setattr(database_module, "MIGRATIONS_DIR", src)

    conn = sqlite3.connect(tmp_path / "t.db")
    conn.row_factory = sqlite3.Row
    assert apply_migrations(conn) == 2
    applied_names = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert {"first", "second"} <= applied_names
    assert int(conn.execute("PRAGMA user_version").fetchone()[0]) == 2
    conn.close()


def test_already_applied_scripts_are_skipped(tmp_path, monkeypatch) -> None:
    src = tmp_path / "migrations"
    src.mkdir()
    _write_migration(src, "0001_first.sql", "first")
    monkeypatch.setattr(database_module, "MIGRATIONS_DIR", src)

    conn = sqlite3.connect(tmp_path / "t.db")
    assert apply_migrations(conn) == 1
    # 2 件目を追加して再実行: 0001 はスキップされ 0002 だけ昇る。
    _write_migration(src, "0002_second.sql", "second")
    assert apply_migrations(conn) == 2
    assert int(conn.execute("PRAGMA user_version").fetchone()[0]) == 2
    conn.close()


def test_invalid_migration_names_raise(tmp_path, monkeypatch) -> None:
    src = tmp_path / "migrations"
    src.mkdir()
    _write_migration(src, "01_short.sql", "short")
    monkeypatch.setattr(database_module, "MIGRATIONS_DIR", src)
    with pytest.raises(MigrationError):
        apply_migrations(sqlite3.connect(":memory:"))

    bad = tmp_path / "bad"
    bad.mkdir()
    bad.joinpath("0001-lexically_sorted.sql").write_text(
        "SELECT 1;\n", encoding="utf-8"
    )
    monkeypatch.setattr(database_module, "MIGRATIONS_DIR", bad)
    with pytest.raises(MigrationError):
        apply_migrations(sqlite3.connect(":memory:"))


def test_non_sql_files_are_ignored(tmp_path, monkeypatch) -> None:
    src = tmp_path / "migrations"
    src.mkdir()
    _write_migration(src, "0001_first.sql", "first")
    src.joinpath("README.md").write_text("説明\n", encoding="utf-8")
    monkeypatch.setattr(database_module, "MIGRATIONS_DIR", src)

    conn = sqlite3.connect(tmp_path / "t.db")
    assert apply_migrations(conn) == 1
    conn.close()
