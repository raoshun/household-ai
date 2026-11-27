"""version 記述層の作成・切替・承認・却下・load のテスト。

実 DB (0001+0002 適用済み) と実 md file で契約 §2.2 の state machine と
§1 決定 1.23-1.26 の transaction・hash 検証を流す。孤児 md と commit 失敗の
atomicity (決定 1.24) は search index refresh を crash させて検証する。
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from app.db.database import init_database, to_db_timestamp
from app.models.rule import (
    RuleBody,
    RuleCategory,
    RuleFrontMatter,
    RuleStatus,
    RuleType,
)
from app.repositories import rule_repository as repo
from app.repositories.rule_repository import (
    RuleConsistencyError,
    RuleContentConflictError,
    RuleNotFoundError,
    RuleRepositoryError,
    RuleVersionStateError,
)

T0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
T1 = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
T2 = datetime(2026, 2, 3, 4, 5, 6, tzinfo=UTC)
NAIVE_TS = datetime(2026, 3, 1, 12, 0, 0)

BODY = RuleBody(
    basic_policy="21時までに風呂に入る",
    applicable_conditions="平日の夜",
    exception_conditions="発熱時",
    decision_criteria="本人の状態",
)


def _fm(**overrides: Any) -> RuleFrontMatter:
    base: dict[str, Any] = {
        "id": "parenting-001",
        "title": "入浴は21時",
        "category": RuleCategory.PARENTING,
        "status": RuleStatus.ACTIVE,
        "version": 1,
        "rule_type": RuleType.GUIDELINE,
        "applicable_to": ["kids"],
        "tags": ["おふろ", "night"],
        "created_at": T0,
        "updated_at": T1,
        "created_by": "parent-a",
    }
    base.update(overrides)
    return RuleFrontMatter(**base)


@pytest.fixture()
def env(tmp_path: Path) -> Any:
    rules_root = tmp_path / "data" / "rules"
    conn = init_database(tmp_path / "t.db")
    try:
        yield conn, rules_root
    finally:
        conn.close()


def _rows(
    conn: sqlite3.Connection,
    sql: str,
    args: tuple[Any, ...] = (),
) -> list[dict]:
    return [dict(row) for row in conn.execute(sql, args).fetchall()]


def _row(conn: sqlite3.Connection, sql: str, args: tuple[Any, ...] = ()) -> dict:
    rows = _rows(conn, sql, args)
    assert len(rows) == 1
    return rows[0]


def _create(
    conn: sqlite3.Connection,
    root: Path,
    **over: Any,
) -> repo.RuleWriteResult:
    return repo.create_rule(
        conn,
        rules_root=root,
        front_matter=_fm(**over),
        body=BODY,
        change_reason="作成一枚目",
    )


def _approve(
    conn: sqlite3.Connection,
    root: Path,
    rule_id: str = "parenting-001",
) -> repo.RuleWriteResult:
    return repo.approve_pending_rule_version(
        conn,
        rules_root=root,
        rule_id=rule_id,
        reviewed_by="reviewer-x",
        reviewed_at=T2,
        review_comment="よい",
    )


def _md(root: Path, relative_path: str) -> Path:
    return root / relative_path


# --- pure helper ----------------------------------------------------------


def test_content_hash_known_sha256_vectors() -> None:
    assert (
        repo.compute_content_hash("abc")
        == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )
    assert (
        repo.compute_content_hash("")
        == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )


def test_summarize_rule_body_first_line_and_none() -> None:
    assert repo.summarize_rule_body(BODY) == "21時までに風呂に入る"
    multiline = BODY.model_copy(
        update={"basic_policy": "1行目\n2行目"},
    )
    assert repo.summarize_rule_body(multiline) == "1行目"
    blank = RuleBody.model_construct(
        basic_policy="   ",
        applicable_conditions="x",
        exception_conditions="x",
        decision_criteria="x",
    )
    assert repo.summarize_rule_body(blank) is None


def test_search_text_fields_and_join() -> None:
    fm = _fm()
    text = repo.compute_rule_search_text(fm, BODY)
    assert text == "\n".join(
        [
            fm.title,
            BODY.basic_policy,
            BODY.applicable_conditions,
            BODY.exception_conditions,
            BODY.decision_criteria,
            "おふろ",
            "night",
        ]
    )
    assert "kids" not in text


def test_build_rule_file_path_template() -> None:
    assert (
        repo.build_rule_file_path("parenting-001", "parenting", 1)
        == "parenting/parenting-001/v1.md"
    )
    assert (
        repo.build_rule_file_path("health-012", "health", 12)
        == "health/health-012/v12.md"
    )


@pytest.mark.parametrize(
    "rule_id",
    ["parenting-001", "health-012", "education-999"],
)
def test_rule_id_accepts_category_slug(rule_id: str) -> None:
    assert repo._RULE_ID_RE.fullmatch(rule_id)


@pytest.mark.parametrize(
    "rule_id",
    ["Parenting-001", "parenting-01", "bad-001", "parenting001", "parenting-0001"],
)
def test_rule_id_rejects_other_forms(rule_id: str) -> None:
    assert not repo._RULE_ID_RE.fullmatch(rule_id)


def test_validate_rule_identity_prefix_mismatch() -> None:
    with pytest.raises(RuleRepositoryError):
        repo._validate_rule_identity(_fm(id="health-001"))


def test_as_row_rejects_tuple() -> None:
    with pytest.raises(RuleRepositoryError, match="row_factory"):
        repo._as_row((("id", "parenting-001"),))


# --- create_rule ----------------------------------------------------------


def test_create_rule_writes_rows_md_and_index(env: Any) -> None:
    conn, root = env
    fm = _fm(status=RuleStatus.DRAFT, approved_by="caller-ignored")
    result = _create(conn, root)
    assert (result.rule_id, result.version, result.file_path) == (
        "parenting-001",
        1,
        "parenting/parenting-001/v1.md",
    )
    rule = _row(conn, "SELECT * FROM rules")
    assert rule["title"] == fm.title
    assert rule["category"] == "parenting"
    assert rule["status"] == "pending_approval"
    assert rule["rule_type"] == "guideline"
    assert rule["priority"] == "normal"
    assert rule["current_version"] == 1
    assert rule["file_path"] == result.file_path
    assert rule["created_at"] == to_db_timestamp(fm.created_at)
    assert rule["updated_at"] == to_db_timestamp(fm.updated_at)
    assert rule["approved_at"] is None
    assert rule["archived_at"] is None
    version_row = _row(conn, "SELECT * FROM rule_versions")
    assert version_row["version"] == 1
    assert version_row["content_hash"] == result.content_hash
    assert version_row["change_reason"] == "作成一枚目"
    assert version_row["created_by"] == "parent-a"
    assert version_row["approved_by"] is None
    assert version_row["created_at"] == to_db_timestamp(fm.created_at)
    assert version_row["summary"] == "21時までに風呂に入る"
    content = _md(root, result.file_path).read_text(encoding="utf-8")
    assert repo.compute_content_hash(content) == result.content_hash
    parsed_fm, parsed_body = repo.parse_rule_markdown(content)
    assert parsed_fm.status is RuleStatus.PENDING_APPROVAL
    assert parsed_fm.version == 1
    assert parsed_fm.approved_by is None
    assert parsed_body == BODY
    assert _row(conn, "SELECT COUNT(*) AS c FROM rule_search_index")["c"] == 1
    assert _rows(conn, "SELECT * FROM approvals") == []


def test_create_rule_layer_overrides_version_status(env: Any) -> None:
    conn, root = env
    _create(conn, root, status=RuleStatus.ACTIVE, approved_by="x")
    rule = _row(conn, "SELECT status FROM rules")
    assert rule["status"] == "pending_approval"


def test_create_rule_duplicate_id_raises_integrity_error(env: Any) -> None:
    conn, root = env
    _create(conn, root)
    with pytest.raises(sqlite3.IntegrityError):
        _create(conn, root)


def test_create_rule_naive_timestamp_leaves_no_trace(env: Any) -> None:
    conn, root = env
    with pytest.raises(ValueError):
        repo.create_rule(
            conn,
            rules_root=root,
            front_matter=_fm(updated_at=NAIVE_TS),
            body=BODY,
        )
    assert _rows(conn, "SELECT * FROM rules") == []
    assert _rows(conn, "SELECT * FROM rule_versions") == []
    assert not _md(root, "parenting/parenting-001/v1.md").exists()


# --- revise_rule ----------------------------------------------------------


def test_revise_rule_advances_pointer_writes_new_md(env: Any) -> None:
    conn, root = env
    _create(conn, root)
    _approve(conn, root)
    fm2 = _fm(status=RuleStatus.ACTIVE, title="入浴は21時半", updated_at=T2)
    body2 = BODY.model_copy(update={"basic_policy": "21時半までに風呂に入る"})
    result = repo.revise_rule(
        conn,
        rules_root=root,
        rule_id="parenting-001",
        front_matter=fm2,
        body=body2,
        change_reason="時刻緩和",
    )
    assert (result.version, result.file_path) == (2, "parenting/parenting-001/v2.md")
    rule = _row(conn, "SELECT * FROM rules")
    assert rule["title"] == "入浴は21時半"
    assert rule["status"] == "active"
    assert rule["current_version"] == 2
    assert rule["file_path"] == result.file_path
    assert rule["updated_at"] == to_db_timestamp(T2)
    assert rule["approved_at"] == to_db_timestamp(T2)
    versions = _rows(conn, "SELECT * FROM rule_versions ORDER BY version")
    assert [v["version"] for v in versions] == [1, 2]
    assert versions[1]["approved_by"] is None
    assert versions[1]["created_at"] == to_db_timestamp(T2)
    v2_text = _md(root, result.file_path).read_text(encoding="utf-8")
    assert repo.compute_content_hash(v2_text) == result.content_hash
    fm_load, body_load = repo.load_current_rule(
        conn, rules_root=root, rule_id="parenting-001"
    )
    assert fm_load.version == 2
    assert fm_load.title == "入浴は21時半"
    assert body_load == BODY.model_copy(
        update={"basic_policy": "21時半までに風呂に入る"}
    )


def test_revise_rule_rejects_id_mismatch(env: Any) -> None:
    conn, root = env
    _create(conn, root)
    with pytest.raises(RuleRepositoryError, match="不一致"):
        repo.revise_rule(
            conn,
            rules_root=root,
            rule_id="parenting-002",
            front_matter=_fm(),
            body=BODY,
        )


def test_revise_rule_expected_state_mismatch(env: Any) -> None:
    conn, root = env
    _create(conn, root)
    for kwargs in (
        {"expected_current_version": 99},
        {"expected_status": "active"},
    ):
        with pytest.raises(RuleVersionStateError):
            repo.revise_rule(
                conn,
                rules_root=root,
                rule_id="parenting-001",
                front_matter=_fm(title="別タイトル"),
                body=BODY,
                **kwargs,
            )
    assert _row(conn, "SELECT COUNT(*) AS c FROM rule_versions")["c"] == 1


def test_revise_rule_same_content_hash_conflict(env: Any) -> None:
    conn, root = env
    fm2 = _fm(id="parenting-002", status=RuleStatus.ACTIVE)
    body2 = BODY.model_copy(update={"basic_policy": "21時半まで"})
    conn.execute(
        "INSERT INTO rules (id, title, category, status, rule_type,"
        " current_version, file_path, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "parenting-002",
            fm2.title,
            "parenting",
            "active",
            "guideline",
            1,
            "parenting/parenting-002/v1.md",
            to_db_timestamp(T0),
            to_db_timestamp(T1),
        ),
    )
    fm2_v2 = fm2.model_copy(update={"version": 2})
    rendered_v2 = repo.render_rule_markdown(fm2_v2, body2)
    conn.execute(
        "INSERT INTO rule_versions (rule_id, version, content_hash,"
        " created_by, created_at)"
        " VALUES (?, 1, ?, ?, ?)",
        (
            "parenting-002",
            repo.compute_content_hash(rendered_v2),
            "parent-a",
            to_db_timestamp(T0),
        ),
    )
    conn.commit()  # クラフト行の暗黙トランザクションを確定し書き込み層に渡す
    with pytest.raises(RuleContentConflictError):
        repo.revise_rule(
            conn,
            rules_root=root,
            rule_id="parenting-002",
            front_matter=fm2,
            body=body2,
        )
    assert _row(conn, "SELECT COUNT(*) AS c FROM rule_versions")["c"] == 1
    assert not _md(root, "parenting/parenting-002/v2.md").exists()


# --- approve / reject -----------------------------------------------------


def test_approve_updates_rules_md_version_and_approval(env: Any) -> None:
    conn, root = env
    created = _create(conn, root)
    result = _approve(conn, root)
    assert (result.version, result.file_path) == (1, created.file_path)
    assert result.content_hash != created.content_hash
    rule = _row(conn, "SELECT * FROM rules")
    assert rule["status"] == "active"
    assert rule["updated_at"] == to_db_timestamp(T2)
    assert rule["approved_at"] == to_db_timestamp(T2)
    version_row = _row(conn, "SELECT * FROM rule_versions")
    assert version_row["approved_by"] == "reviewer-x"
    content = _md(root, result.file_path).read_text(encoding="utf-8")
    assert repo.compute_content_hash(content) == result.content_hash
    fm_load, body_load = repo.load_current_rule(
        conn, rules_root=root, rule_id="parenting-001"
    )
    assert fm_load.status is RuleStatus.ACTIVE
    assert fm_load.approved_by == "reviewer-x"
    assert fm_load.updated_at == T2
    assert body_load == BODY
    approval = _row(conn, "SELECT * FROM approvals")
    assert approval["status"] == "approved"
    assert approval["proposed_by"] == "parent-a"
    assert approval["reviewed_by"] == "reviewer-x"
    assert approval["review_comment"] == "よい"
    assert approval["created_at"] == to_db_timestamp(T2)
    assert approval["reviewed_at"] == to_db_timestamp(T2)


def test_reject_keeps_approved_at_null_and_rejects(env: Any) -> None:
    conn, root = env
    created = _create(conn, root)
    result = repo.reject_pending_rule_version(
        conn,
        rules_root=root,
        rule_id="parenting-001",
        reviewed_by="reviewer-x",
        reviewed_at=T2,
        review_comment="不可",
    )
    rule = _row(conn, "SELECT * FROM rules")
    assert rule["status"] == "rejected"
    assert rule["approved_at"] is None
    assert rule["updated_at"] == to_db_timestamp(T2)
    version_row = _row(conn, "SELECT * FROM rule_versions")
    assert version_row["approved_by"] is None
    content = _md(root, result.file_path).read_text(encoding="utf-8")
    fm_load, _ = repo.parse_rule_markdown(content)
    assert fm_load.status is RuleStatus.REJECTED
    assert fm_load.approved_by is None
    approval = _row(conn, "SELECT * FROM approvals")
    assert approval["status"] == "rejected"
    assert approval["reviewed_at"] == to_db_timestamp(T2)
    assert repo.compute_content_hash(content) == version_row["content_hash"]
    assert repo.compute_content_hash(content) != created.content_hash


def test_decide_requires_pending_state(env: Any) -> None:
    conn, root = env
    _create(conn, root)
    _approve(conn, root)
    with pytest.raises(RuleVersionStateError, match="pending_approval"):
        repo.reject_pending_rule_version(
            conn,
            rules_root=root,
            rule_id="parenting-001",
            reviewed_by="reviewer-x",
            reviewed_at=T2,
        )
    assert _row(conn, "SELECT COUNT(*) AS c FROM approvals")["c"] == 1


def test_decide_rejects_naive_reviewed_atomically(env: Any) -> None:
    conn, root = env
    _create(conn, root)
    with pytest.raises(ValueError):
        repo.approve_pending_rule_version(
            conn,
            rules_root=root,
            rule_id="parenting-001",
            reviewed_by="reviewer-x",
            reviewed_at=NAIVE_TS,
        )
    rule = _row(conn, "SELECT * FROM rules")
    assert rule["status"] == "pending_approval"
    assert _rows(conn, "SELECT * FROM approvals") == []


def test_decide_detects_tampered_md(env: Any) -> None:
    conn, root = env
    created = _create(conn, root)
    stale = _md(root, created.file_path)
    stale.write_text("改ざん後", encoding="utf-8")
    with pytest.raises(RuleConsistencyError, match="hash"):
        _approve(conn, root)
    rule = _row(conn, "SELECT * FROM rules")
    assert rule["status"] == "pending_approval"
    assert _rows(conn, "SELECT * FROM approvals") == []
    assert stale.read_text(encoding="utf-8") == "改ざん後"


# --- load ------------------------------------------------------------------


def test_load_current_rule_round_trip_pending(env: Any) -> None:
    conn, root = env
    _create(conn, root)
    fm_load, body_load = repo.load_current_rule(
        conn, rules_root=root, rule_id="parenting-001"
    )
    assert fm_load.id == "parenting-001"
    assert fm_load.status is RuleStatus.PENDING_APPROVAL
    assert fm_load.updated_at == T1
    assert fm_load.created_by == "parent-a"
    assert body_load == BODY


def test_load_rule_version_reads_specific_version(env: Any) -> None:
    conn, root = env
    _create(conn, root)
    _approve(conn, root)
    repo.revise_rule(
        conn,
        rules_root=root,
        rule_id="parenting-001",
        front_matter=_fm(status=RuleStatus.ACTIVE, title="入浴は21時半"),
        body=BODY.model_copy(update={"basic_policy": "21時半まで"}),
    )
    fm1, _ = repo.load_rule_version(
        conn, rules_root=root, rule_id="parenting-001", version=1
    )
    assert fm1.version == 1
    assert fm1.status is RuleStatus.ACTIVE
    fm2, body2 = repo.load_rule_version(
        conn, rules_root=root, rule_id="parenting-001", version=2
    )
    assert fm2.title == "入浴は21時半"
    assert body2.basic_policy == "21時半まで"


def test_load_errors_missing_rows_and_files(env: Any) -> None:
    conn, root = env
    with pytest.raises(RuleNotFoundError):
        repo.load_current_rule(conn, rules_root=root, rule_id="parenting-001")
    with pytest.raises(RuleNotFoundError):
        repo.load_rule_version(
            conn, rules_root=root, rule_id="parenting-001", version=1
        )
    created = _create(conn, root)
    with pytest.raises(RuleNotFoundError):
        repo.load_rule_version(
            conn, rules_root=root, rule_id="parenting-001", version=5
        )
    _md(root, created.file_path).write_text("x", encoding="utf-8")
    with pytest.raises(RuleConsistencyError, match="hash"):
        repo.load_current_rule(conn, rules_root=root, rule_id="parenting-001")
    with pytest.raises(RuleConsistencyError, match="hash"):
        repo.load_rule_version(
            conn, rules_root=root, rule_id="parenting-001", version=1
        )
    _md(root, created.file_path).unlink()
    with pytest.raises(RuleConsistencyError, match="読めない"):
        repo.load_current_rule(conn, rules_root=root, rule_id="parenting-001")


# --- transaction semantics ---------------------------------------------------


def test_commit_failure_leaves_orphan_md_and_retry(env: Any, monkeypatch: Any) -> None:
    conn, root = env

    def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("fts down")

    monkeypatch.setattr(repo, "refresh_rule_index", boom)
    with pytest.raises(RuntimeError, match="fts down"):
        _create(conn, root)
    assert _rows(conn, "SELECT * FROM rules") == []
    assert _rows(conn, "SELECT * FROM rule_versions") == []
    orphan = _md(root, "parenting/parenting-001/v1.md")
    assert orphan.exists()
    monkeypatch.undo()
    result = _create(conn, root)
    assert result.version == 1
    assert _row(conn, "SELECT COUNT(*) AS c FROM rules")["c"] == 1
    assert repo.compute_content_hash(orphan.read_text(encoding="utf-8")) == (
        result.content_hash
    )


def test_caller_inside_transaction_is_rejected(env: Any) -> None:
    conn, root = env
    conn.execute(
        "INSERT INTO rules (id, title, category, status, rule_type,"
        " current_version, file_path, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "health-001",
            "先住row",
            "health",
            "pending_approval",
            "guideline",
            1,
            "health/health-001/v1.md",
            to_db_timestamp(T0),
            to_db_timestamp(T1),
        ),
    )
    with pytest.raises(sqlite3.OperationalError):
        _create(conn, root)
    assert _rows(conn, "SELECT * FROM rules") == []
