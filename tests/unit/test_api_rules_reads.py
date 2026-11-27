"""ルール 読み取り API 4 endpoint のテスト (契約 §2.7・決定 1.30/1.31)。

auth 付き の 一覧・詳細・version 一覧・search を 実 DB (init_database) と
実 create 経路 の md で 検証 する。response の 時刻 は +09:00 の ms ISO、
wrapper key・key 集合・problem JSON の code を 固定 する (決定 1.31)。
"""

from __future__ import annotations

import base64
import re
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.auth import (
    AUTH_REALM,
    ApiUser,
    Authenticator,
    install_unauthenticated_handler,
    sha256_hex,
)
from app.api.rules import register_rule_problem_rules, router
from app.clock import tokyo_iso
from app.db.database import init_database
from app.models.rule import (
    RuleBody,
    RuleCategory,
    RuleFrontMatter,
    RuleStatus,
    RuleType,
)
from app.repositories import rule_repository as repo
from app.services.exceptions import ProblemEmitter

USER_ID = "parent-a"
PASSWORD = "household-test-password"
T0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
T1 = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
T2 = datetime(2026, 2, 3, 4, 5, 6, tzinfo=UTC)

#: 応答 時刻 が +09:00 の ms ISO か を 見る (決定 1.31(4))。
TOKYO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}\+09:00$")

BODY = RuleBody(
    basic_policy="21時までに風呂に入る",
    applicable_conditions="平日の夜",
    exception_conditions="発熱時",
    decision_criteria="本人の状態",
)
BODY2 = RuleBody(
    basic_policy="22時までに風呂に出る",
    applicable_conditions="土日を含む夜",
    exception_conditions="発熱時",
    decision_criteria="本人の希望",
)


def _auth_header(username: str = USER_ID, password: str = PASSWORD) -> dict[str, str]:
    token = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
    return {"Authorization": f"Basic {token}"}


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
        "created_by": USER_ID,
    }
    base.update(overrides)
    return RuleFrontMatter(**base)


def _build_app(db_path: Path, rules_root: Path) -> FastAPI:
    app = FastAPI()
    app.state.rules_db_path = db_path
    app.state.rules_root = rules_root
    user = ApiUser(id=USER_ID, password_sha256=sha256_hex(PASSWORD), role="admin")
    app.state.authenticator = Authenticator(users=(user,))
    emitter = ProblemEmitter.default()
    register_rule_problem_rules(emitter)
    emitter.install(app)
    install_unauthenticated_handler(app)
    app.include_router(router)
    return app


@pytest.fixture()
def env(tmp_path: Path) -> Iterator[tuple[TestClient, sqlite3.Connection, Path]]:
    rules_root = tmp_path / "data" / "rules"
    rules_root.mkdir(parents=True)
    db_path = tmp_path / "rules.db"
    conn = init_database(db_path)
    client = TestClient(_build_app(db_path, rules_root), raise_server_exceptions=False)
    try:
        yield client, conn, rules_root
    finally:
        conn.close()


def _create(
    conn: sqlite3.Connection,
    rules_root: Path,
    front_matter: RuleFrontMatter,
    body: RuleBody = BODY,
) -> repo.RuleWriteResult:
    return repo.create_rule(
        conn,
        rules_root=rules_root,
        front_matter=front_matter,
        body=body,
        change_reason="作成",
    )


def _set_status(conn: sqlite3.Connection, rule_id: str, status: str) -> None:
    conn.execute("UPDATE rules SET status = ? WHERE id = ?", (status, rule_id))
    conn.commit()


# ---------------------------------------------------------------------------
# 認証 (決定 1.27)
# ---------------------------------------------------------------------------

READ_PATHS = (
    "/api/rules",
    "/api/rules/search?q=方針",
    "/api/rules/parenting-001",
    "/api/rules/parenting-001/versions",
)


@pytest.mark.parametrize("path", READ_PATHS)
def test_reads_require_basic_auth(env: Any, path: str) -> None:
    client, _conn, _root = env
    response = client.get(path)
    assert response.status_code == 401
    assert response.json()["code"] == "unknown_error.unauthenticated"
    assert response.headers["www-authenticate"] == f'Basic realm="{AUTH_REALM}"'


@pytest.mark.parametrize(
    ("username", "password"),
    [("stranger", PASSWORD), (USER_ID, "wrong-password")],
)
def test_invalid_basic_credentials_are_401(
    env: Any, username: str, password: str
) -> None:
    client, _conn, _root = env
    response = client.get("/api/rules", headers=_auth_header(username, password))
    assert response.status_code == 401
    assert response.json()["code"] == "unknown_error.unauthenticated"


# ---------------------------------------------------------------------------
# 一覧
# ---------------------------------------------------------------------------


def test_list_empty(env: Any) -> None:
    client, _conn, _root = env
    response = client.get("/api/rules", headers=_auth_header())
    assert response.status_code == 200
    assert response.json() == {"count": 0, "items": []}


def test_list_order_keys_and_tokyo_datetimes(env: Any) -> None:
    client, conn, root = env
    _create(conn, root, _fm(id="parenting-001", updated_at=T0))
    _create(conn, root, _fm(id="parenting-002", updated_at=T2))
    _create(conn, root, _fm(id="parenting-003", title="方針", updated_at=T2))

    response = client.get("/api/rules", headers=_auth_header())
    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 3
    item = payload["items"][0]
    assert set(item) == {
        "id",
        "title",
        "category",
        "status",
        "rule_type",
        "priority",
        "current_version",
        "updated_at",
    }
    # updated_at DESC・同値 は id DESC (契約 決定 1.30(4) + 実装 判断)。
    assert [entry["id"] for entry in payload["items"]] == [
        "parenting-003",
        "parenting-002",
        "parenting-001",
    ]
    assert payload["items"][0]["updated_at"] == tokyo_iso(T2)
    assert payload["items"][2]["updated_at"] == tokyo_iso(T0)
    assert TOKYO_RE.fullmatch(payload["items"][0]["updated_at"])
    assert item["status"] == "pending_approval"
    assert item["current_version"] == 1
    assert item["priority"] == "normal"


def test_list_status_and_category_filters(env: Any) -> None:
    client, conn, root = env
    _create(conn, root, _fm(id="parenting-001"))
    _create(conn, root, _fm(id="parenting-002"))
    _create(conn, root, _fm(id="education-001", category=RuleCategory.EDUCATION))
    _create(conn, root, _fm(id="education-002", category=RuleCategory.EDUCATION))
    _set_status(conn, "parenting-001", "active")
    _set_status(conn, "parenting-002", "rejected")
    _set_status(conn, "education-001", "active")

    def ids(params: dict[str, str]) -> set[str]:
        response = client.get("/api/rules", params=params, headers=_auth_header())
        assert response.status_code == 200
        return {entry["id"] for entry in response.json()["items"]}

    assert ids({"status": "active"}) == {"parenting-001", "education-001"}
    assert ids({"category": "parenting"}) == {"parenting-001", "parenting-002"}
    assert ids({"status": "pending_approval", "category": "education"}) == {
        "education-002"
    }
    assert ids({"status": "rejected", "category": "education"}) == set()


def test_list_invalid_enum_is_400_invalid_request(env: Any) -> None:
    client, _conn, _root = env
    response = client.get(
        "/api/rules", params={"status": "bogus"}, headers=_auth_header()
    )
    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "unknown_error.invalid_request"
    assert "errors" in body["details"]


# ---------------------------------------------------------------------------
# 詳細
# ---------------------------------------------------------------------------

_DETAIL_EXTRA = {"current_version", "summary", "body", "versions"}
_FRONT_MATTER_KEYS = {
    "id",
    "title",
    "category",
    "status",
    "version",
    "rule_type",
    "priority",
    "applicable_to",
    "tags",
    "created_at",
    "updated_at",
    "created_by",
    "approved_by",
}


def test_detail_full_shape(env: Any) -> None:
    client, conn, root = env
    _create(conn, root, _fm())
    response = client.get("/api/rules/parenting-001", headers=_auth_header())
    assert response.status_code == 200
    body = response.json()
    assert set(body) == _FRONT_MATTER_KEYS | _DETAIL_EXTRA
    assert body["status"] == "pending_approval"
    assert body["approved_by"] is None
    assert body["current_version"] == 1
    assert body["created_at"] == tokyo_iso(T0)
    assert body["updated_at"] == tokyo_iso(T1)
    assert body["summary"] == "21時までに風呂に入る"
    assert body["body"] == {
        "basic_policy": "21時までに風呂に入る",
        "applicable_conditions": "平日の夜",
        "exception_conditions": "発熱時",
        "decision_criteria": "本人の状態",
    }
    assert body["versions"][0] == {
        "version": 1,
        "summary": "21時までに風呂に入る",
        "change_reason": "作成",
        "created_by": USER_ID,
        "approved_by": None,
        "created_at": body["versions"][0]["created_at"],
    }
    assert TOKYO_RE.fullmatch(body["versions"][0]["created_at"])


def test_detail_unknown_rule_is_404(env: Any) -> None:
    client, _conn, _root = env
    response = client.get("/api/rules/parenting-999", headers=_auth_header())
    assert response.status_code == 404
    assert response.json()["code"] == "unknown_error.rule_not_found"


def test_detail_markdown_corruption_is_500_catch_all(env: Any) -> None:
    client, conn, root = env
    result = _create(conn, root, _fm())
    (root / result.file_path).write_text(
        "---\n破壊 された md\n---\n本文", encoding="utf-8"
    )
    response = client.get("/api/rules/parenting-001", headers=_auth_header())
    assert response.status_code == 500
    assert response.json()["code"] == "unknown_error.internal_error"


# ---------------------------------------------------------------------------
# version 一覧
# ---------------------------------------------------------------------------


def test_versions_newest_first_and_unknown_404(env: Any) -> None:
    client, conn, root = env
    _create(conn, root, _fm())
    repo.revise_rule(
        conn,
        rules_root=root,
        rule_id="parenting-001",
        front_matter=_fm(updated_at=T2),
        body=BODY2,
        expected_current_version=1,
        expected_status="pending_approval",
        change_reason="見直し",
    )
    response = client.get("/api/rules/parenting-001/versions", headers=_auth_header())
    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 2
    assert [entry["version"] for entry in payload["items"]] == [2, 1]
    newest = payload["items"][0]
    assert set(newest) == {
        "version",
        "summary",
        "change_reason",
        "created_by",
        "approved_by",
        "created_at",
    }
    assert newest["summary"] == "22時までに風呂に出る"
    assert newest["change_reason"] == "見直し"
    assert newest["approved_by"] is None
    assert TOKYO_RE.fullmatch(newest["created_at"])

    missing = client.get("/api/rules/parenting-999/versions", headers=_auth_header())
    assert missing.status_code == 404
    assert missing.json()["code"] == "unknown_error.rule_not_found"


# ---------------------------------------------------------------------------
# search (決定 1.6/1.7・1.21・契約 §2.7)
# ---------------------------------------------------------------------------


def test_search_projection_and_filters(env: Any) -> None:
    client, conn, root = env
    _create(conn, root, _fm(id="parenting-001", title="予防接種の予定"))
    _create(
        conn,
        root,
        _fm(
            id="health-001",
            title="予防接種の記録",
            category=RuleCategory.HEALTH,
        ),
    )
    _create(
        conn,
        root,
        _fm(
            id="lifestyle-001",
            title="予防接種の相談",
            category=RuleCategory.LIFESTYLE,
        ),
    )
    _set_status(conn, "parenting-001", "active")
    _set_status(conn, "health-001", "active")

    def ids(params: dict[str, str]) -> list[dict[str, Any]]:
        response = client.get(
            "/api/rules/search", params=params, headers=_auth_header()
        )
        assert response.status_code == 200
        return response.json()["results"]

    results = ids({"q": "予防接種"})
    assert {entry["id"] for entry in results} == {"parenting-001", "health-001"}
    entry = results[0]
    assert set(entry) == {
        "id",
        "title",
        "category",
        "status",
        "summary",
        "current_version",
    }
    assert entry["status"] == "active"
    assert entry["summary"] == "21時までに風呂に入る"
    assert entry["current_version"] == 1

    assert {e["id"] for e in ids({"q": "予防接種", "category": "health"})} == {
        "health-001"
    }
    assert {e["id"] for e in ids({"q": "予防接種", "status": "pending_approval"})} == {
        "lifestyle-001"
    }
    assert {e["id"] for e in ids({"q": "入浴", "category": "parenting"})} == set()


def test_search_terms_without_vocab_is_200_empty(env: Any) -> None:
    client, conn, root = env
    _create(conn, root, _fm())
    response = client.get(
        "/api/rules/search", params={"q": "..."}, headers=_auth_header()
    )
    assert response.status_code == 200
    assert response.json() == {"count": 0, "results": []}


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"q": ""},
        {"q": "方針", "limit": 0},
        {"q": "方針", "limit": 21},
        {"q": "方針", "status": "bogus"},
        {"q": "方針", "category": "bogus"},
    ],
)
def test_search_invalid_params_are_400(env: Any, params: dict[str, Any]) -> None:
    client, _conn, _root = env
    response = client.get("/api/rules/search", params=params, headers=_auth_header())
    assert response.status_code == 400
    assert response.json()["code"] == "unknown_error.invalid_request"


def test_search_default_limit_is_ten(env: Any) -> None:
    client, conn, root = env
    for index in range(1, 13):
        _create(
            conn,
            root,
            _fm(
                id=f"parenting-{index:03d}",
                title=f"見守り方針{index:02d}",
                tags=[],
            ),
        )
        _set_status(conn, f"parenting-{index:03d}", "active")

    response = client.get(
        "/api/rules/search", params={"q": "見守り方針"}, headers=_auth_header()
    )
    assert response.status_code == 200
    assert response.json()["count"] == 10
    limited = client.get(
        "/api/rules/search",
        params={"q": "見守り方針", "limit": 20},
        headers=_auth_header(),
    )
    assert limited.json()["count"] == 12
