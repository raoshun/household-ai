"""API 用の 読み出し query と server 採番 のテスト (§1 決定 1.28/1.30/1.31)。

一覧・version 一覧・審査対象 1 件取りは 実 DB (0001+0002) へ raw insert した
rules row と 実 create 経路 の 両方 で projection と並び を確認する。採番 と
UNIQUE collision の +1 retry は create_rule の transaction を経由して流す。
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
    ApprovalTargetNotFoundError,
    RuleIdSequenceExhaustedError,
    RuleNotFoundError,
    RuleRepositoryError,
)

T0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
T1 = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
T2 = datetime(2026, 2, 3, 4, 5, 6, tzinfo=UTC)

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
        "status": RuleStatus.PENDING_APPROVAL,
        "version": 1,
        "rule_type": RuleType.GUIDELINE,
        "applicable_to": ["kids"],
        "tags": ["おふろ"],
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


def _insert_rule(
    conn: sqlite3.Connection,
    rule_id: str,
    *,
    category: str = "parenting",
    status: str = "pending_approval",
    updated_at: datetime = T1,
) -> None:
    """md を作らず rules row だけ raw注入する 読み出し test 用の insert。"""
    ts = to_db_timestamp(updated_at)
    conn.execute(
        "INSERT INTO rules (id, title, category, status, rule_type, priority,"
        " current_version, file_path, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, 'guideline', 'normal', 1, ?, ?, ?)",
        (
            rule_id,
            f"rule {rule_id}",
            category,
            status,
            f"{category}/{rule_id}/v1.md",
            ts,
            ts,
        ),
    )


# --- list_rules (決定 1.30(4)/1.31(1)) -------------------------------------


class TestListRules:
    def test_empty(self, env: Any) -> None:
        conn, _ = env
        assert repo.list_rules(conn) == []

    def test_projection_and_updated_desc(self, env: Any) -> None:
        conn, _ = env
        _insert_rule(conn, "parenting-001", updated_at=T1)
        _insert_rule(conn, "parenting-002", updated_at=T2, status="active")
        _insert_rule(conn, "parenting-003", updated_at=T0, status="rejected")
        items = repo.list_rules(conn)
        assert [item.id for item in items] == [
            "parenting-002",
            "parenting-001",
            "parenting-003",
        ]
        first = items[0]
        assert first.updated_at == T2
        assert first.category == "parenting"
        assert first.status == "active"
        assert first.rule_type == "guideline"
        assert first.priority == "normal"
        assert first.current_version == 1
        assert first.title == "rule parenting-002"

    def test_filters(self, env: Any) -> None:
        conn, _ = env
        _insert_rule(conn, "parenting-001", status="active")
        _insert_rule(conn, "parenting-002", status="pending_approval")
        _insert_rule(conn, "education-001", category="education", status="active")
        # updated_at 同一なので id の text DESC: parenting-* > education-*
        assert [i.id for i in repo.list_rules(conn, status="active")] == [
            "parenting-001",
            "education-001",
        ]
        cats = repo.list_rules(conn, category="education")
        assert [i.id for i in cats] == ["education-001"]
        both = repo.list_rules(conn, status="active", category="parenting")
        assert [i.id for i in both] == ["parenting-001"]

    def test_unknown_filter_values_give_empty(self, env: Any) -> None:
        conn, _ = env
        _insert_rule(conn, "parenting-001")
        assert repo.list_rules(conn, status="nope") == []
        assert repo.list_rules(conn, category="nope") == []

    def test_same_updated_at_tiebreak_id_desc(self, env: Any) -> None:
        conn, _ = env
        _insert_rule(conn, "parenting-001", updated_at=T1)
        _insert_rule(conn, "parenting-002", updated_at=T1)
        assert [i.id for i in repo.list_rules(conn)] == [
            "parenting-002",
            "parenting-001",
        ]


# --- list_rule_versions (決定 1.31(2)) -------------------------------------


class TestListRuleVersions:
    def test_missing_rule_raises(self, env: Any) -> None:
        conn, _ = env
        with pytest.raises(RuleNotFoundError):
            repo.list_rule_versions(conn, "parenting-900")

    def test_rule_without_versions_is_empty(self, env: Any) -> None:
        conn, _ = env
        _insert_rule(conn, "parenting-001")
        assert repo.list_rule_versions(conn, "parenting-001") == []

    def test_newest_first_with_meta(self, env: Any) -> None:
        conn, rules_root = env
        repo.create_rule(
            conn,
            rules_root=rules_root,
            front_matter=_fm(),
            body=BODY,
            change_reason="作成",
        )
        repo.approve_pending_rule_version(
            conn,
            rules_root=rules_root,
            rule_id="parenting-001",
            reviewed_by="reviewer-x",
            reviewed_at=T2,
        )
        repo.revise_rule(
            conn,
            rules_root=rules_root,
            rule_id="parenting-001",
            front_matter=_fm(status=RuleStatus.ACTIVE, title="入浴は22時"),
            body=RuleBody(**{**BODY.model_dump(), "basic_policy": "22時までに風呂"}),
            expected_current_version=1,
            change_reason="時刻改訂",
        )
        items = repo.list_rule_versions(conn, "parenting-001")
        assert [i.version for i in items] == [2, 1]
        v1, v2 = items[1], items[0]
        assert v1.summary == "21時までに風呂に入る"
        assert v1.change_reason == "作成"
        assert v1.created_by == "parent-a"
        assert v1.approved_by == "reviewer-x"
        assert v2.summary == "22時までに風呂"
        assert v2.change_reason == "時刻改訂"
        assert v2.approved_by is None
        assert v1.created_at.tzinfo is UTC


# --- fetch_pending_approval (決定 1.28(1)) ---------------------------------


class TestFetchPendingApproval:
    def test_returns_projection(self, env: Any) -> None:
        conn, _ = env
        _insert_rule(conn, "parenting-001", updated_at=T2)
        item = repo.fetch_pending_approval(conn, "parenting-001")
        assert item.id == "parenting-001"
        assert item.status == "pending_approval"
        assert item.updated_at == T2

    def test_other_status_raises(self, env: Any) -> None:
        conn, _ = env
        _insert_rule(conn, "parenting-001", status="active")
        with pytest.raises(ApprovalTargetNotFoundError):
            repo.fetch_pending_approval(conn, "parenting-001")

    def test_missing_id_raises(self, env: Any) -> None:
        conn, _ = env
        with pytest.raises(ApprovalTargetNotFoundError):
            repo.fetch_pending_approval(conn, "parenting-900")


# --- next_rule_id (決定 1.30(2)) -------------------------------------------


class TestNextRuleId:
    def test_first_is_001(self, env: Any) -> None:
        conn, _ = env
        assert repo.next_rule_id(conn, RuleCategory.PARENTING) == "parenting-001"

    def test_max_plus_one_zero_padded(self, env: Any) -> None:
        conn, _ = env
        _insert_rule(conn, "parenting-005")
        _insert_rule(conn, "parenting-488")
        _insert_rule(conn, "parenting-008")
        assert repo.next_rule_id(conn, "parenting") == "parenting-489"

    def test_categories_are_independent(self, env: Any) -> None:
        conn, _ = env
        _insert_rule(conn, "education-007", category="education")
        assert repo.next_rule_id(conn, "parenting") == "parenting-001"
        assert repo.next_rule_id(conn, "education") == "education-008"

    def test_non3digit_ids_ignored(self, env: Any) -> None:
        conn, _ = env
        ts = to_db_timestamp(T1)
        conn.execute(
            "INSERT INTO rules (id, title, category, status, rule_type,"
            " priority, current_version, file_path, created_at, updated_at)"
            " VALUES ('parenting-1001', 'x', 'parenting', 'active',"
            " 'guideline', 'normal', 1, 'p', ?, ?)",
            (ts, ts),
        )
        assert repo.next_rule_id(conn, "parenting") == "parenting-001"

    def test_exhausted_over_999(self, env: Any) -> None:
        conn, _ = env
        _insert_rule(conn, "parenting-999")
        with pytest.raises(RuleIdSequenceExhaustedError):
            repo.next_rule_id(conn, "parenting")

    def test_unknown_category_raises(self, env: Any) -> None:
        conn, _ = env
        with pytest.raises(RuleRepositoryError):
            repo.next_rule_id(conn, "nope")


# --- create_rule_with_new_id (決定 1.30(2) の 採番 + retry) -----------------


class TestCreateRuleWithNewId:
    def test_sequential_numbering(self, env: Any) -> None:
        conn, rules_root = env
        first = repo.create_rule_with_new_id(
            conn, rules_root=rules_root, front_matter=_fm(), body=BODY
        )
        second = repo.create_rule_with_new_id(
            conn,
            rules_root=rules_root,
            front_matter=_fm(id="ignored-000", title="2件目"),
            body=BODY,
            change_reason="2件目",
        )
        assert first.rule_id == "parenting-001"
        assert second.rule_id == "parenting-002"
        assert (rules_root / second.file_path).read_text(encoding="utf-8")

    def test_unique_collision_retries_plus_one(
        self, env: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        conn, rules_root = env
        repo.create_rule_with_new_id(
            conn, rules_root=rules_root, front_matter=_fm(), body=BODY
        )
        real = repo.next_rule_id
        stale = iter(["parenting-001"])

        def flaky(c: sqlite3.Connection, category: Any) -> str:
            try:
                return next(stale)
            except StopIteration:
                return real(c, category)

        monkeypatch.setattr(repo, "next_rule_id", flaky)
        result = repo.create_rule_with_new_id(
            conn, rules_root=rules_root, front_matter=_fm(), body=BODY
        )
        assert result.rule_id == "parenting-002"
        ids = [r[0] for r in conn.execute("SELECT id FROM rules ORDER BY id")]
        assert ids == ["parenting-001", "parenting-002"]

    def test_collision_retry_exhausted_raises(
        self, env: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        conn, rules_root = env
        repo.create_rule_with_new_id(
            conn, rules_root=rules_root, front_matter=_fm(), body=BODY
        )
        monkeypatch.setattr(repo, "next_rule_id", lambda c, cat: "parenting-001")
        with pytest.raises(RuleIdSequenceExhaustedError) as excinfo:
            repo.create_rule_with_new_id(
                conn, rules_root=rules_root, front_matter=_fm(), body=BODY
            )
        assert isinstance(excinfo.value.__cause__, sqlite3.IntegrityError)
        count = conn.execute("SELECT COUNT(*) FROM rules").fetchone()[0]
        assert count == 1
        assert conn.in_transaction is False
