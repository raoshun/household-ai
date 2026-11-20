"""SQLite 接続・マイグレーション適用・timestamp 変換層。

対応先は README のリポジトリツリー L171 の `db/database.py` で、schema 定義は
L172 の `migrations/` 配下の .sql file に置き、README に無い `db/schema.py` は
作らない (契約 §2.4.4 L173)。

実装判断 (README・契約に該当規定の記載が無いことを grep で確認済み):

- マイグレーションの 4 桁連番 (`0001_*.sql`) と、適用管理を DB 側の
  `PRAGMA user_version` に持たせる方式。順序はファイル名先頭 4 桁の数値順。
- 外部キー制約は接続ごとに `PRAGMA foreign_keys = ON` を有効化する。
  本 repo 実行環境 (sqlite 3.40.1) で実測した限り既定は 0 (off) で、
  per-connection 設定なので接続生成時に適用する。

timestamp は 契約 §1 決定 1.20 のとおり DB が clock を一切読まない形で、
aware な datetime を UTC microsecond text (`isoformat(timespec="microseconds")`)
として保存する。
"""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

_MIGRATION_RE = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


class MigrationError(RuntimeError):
    """マイグレーションファイルの不備 (不正名・適用不能)。"""


def _migration_number(path: Path) -> int:
    match = _MIGRATION_RE.match(path.name)
    if match is None:
        raise MigrationError(
            f"マイグレーション名が `NNNN_*.sql` (4 桁連番) に一致しません: {path.name}"
        )
    return int(match.group(1))


def connect(db_path: Path) -> sqlite3.Connection:
    """指定パスの DB に接続する。親ディレクトリが無ければ作る (実装判断)。

    実測で既定 0 (off) を確認した `PRAGMA foreign_keys` を接続ごとに ON へ
    有効化する。行は `sqlite3.Row` で返し、カラム名アクセスを可能にする
    (実装判断)。
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def apply_migrations(conn: sqlite3.Connection) -> int:
    """`migrations/*.sql` を 4 桁番号順に適用し、適用した最新番号を返す。

    適用済み (`user_version` 以下の番号) はスキップするので再実行しても
    idempotent。`PRAGMA user_version` はバインド不可のため、
    `_migration_number()` が数値化済みの値だけを埋め込む
    (文字列補間だが 4 桁数字しか通り得ない)。
    """
    conn.execute("PRAGMA foreign_keys = ON")
    current = int(conn.execute("PRAGMA user_version").fetchone()[0])
    applied = current
    migration_paths = sorted(MIGRATIONS_DIR.glob("*.sql"), key=_migration_number)
    for path in migration_paths:
        number = _migration_number(path)
        if number <= applied:
            continue
        conn.executescript(path.read_text(encoding="utf-8"))
        conn.execute(f"PRAGMA user_version = {number}")
        applied = number
    conn.commit()
    return applied


def init_database(db_path: Path) -> sqlite3.Connection:
    """接続してマイグレーションまで適用する (起動時初期化の入口)。"""
    conn = connect(db_path)
    apply_migrations(conn)
    return conn


def to_db_timestamp(value: datetime) -> str:
    """aware な datetime を CHECK 受理済みの正準テキストへ変換する (決定 1.20)。

    保存 text は「`value.astimezone(datetime.UTC).isoformat(
    timespec="microseconds")` の出力」と確定している (契約 §1 決定 1.20) ため、
    UTC 変換の基準時刻を持たない naive な datetime は拒否する。
    """
    if value.tzinfo is None:
        raise ValueError("naive な datetime は DB timestamp に変換できません")
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def from_db_timestamp(text: str) -> datetime:
    """正準テキストを aware な datetime に戻す (決定 1.20 の変換の逆変換)。"""
    return datetime.fromisoformat(text)
