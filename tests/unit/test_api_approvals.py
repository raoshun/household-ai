"""承認・却下 API 4 endpoint のテスト (契約 §2.7・決定 1.28〜1.31)。

一覧・1 件取得 は rules の pending_approval projection、承認・却下 は
repository 経由の state 遷移 (決定 1.11) を HTTP から検証する。 実 DB
(init_database) と実 md ファイルを使い、 rules.status・
rule_versions.approved_by・approvals row・md 再描写・problem JSON の
code を固定する。 rule の作成は rules API 経由 (実経路に近い固定)。
"""

from __future__ import annotations

import base64
import re
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.approvals import router as approvals_router
from app.api.auth import (
    AUTH_REALM,
    ApiUser,
    Authenticator,
    install_unauthenticated_handler,
    sha256_hex,
)
from app.api.rules import register_rule_problem_rules
from app.api.rules import router as rules_router
from app.db.database import init_database
from app.services.exceptions import ProblemEmitter

USER_ID = "parent-a"
PASSWORD = "household-test-password"
OTHER_USER = "parent-b"
OTHER_PASSWORD = "household-test-password-b"

#: 応答時刻が +09:00 の ms ISO かを見る (決定 1.31(4))。
TOKYO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}\+09:00$")

#: DB 保存 の 正準 text (決定 1.20: UTC の microsecond precision ISO)。

#: 一覧 1 件の key (rules 一覧と同一の projection、決定 1.28)。
LIST_ITEM_KEYS = {
    "id",
    "title",
    "category",
    "status",
    "rule_type",
    "priority",
    "current_version",
    "updated_at",
}

DB_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}\+00:00$")

APPROVAL_COLUMNS = (
    "status",
    "proposed_by",
    "reviewed_by",
    "review_comment",
    "created_at",
    "reviewed_at",
)

#: 過去時刻の承認用 (approved_at 不変の検証に使う)。
APPROVED_AT = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def _auth_header(username: str = USER_ID, password: str = PASSWORD) -> dict[str, str]:
    token = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
    return {"Authorization": f"Basic {token}"}


# ---------------------------------------------------------------------------
# rule Markdown の組み立て
# ---------------------------------------------------------------------------

_BODY_TEXT = (
    "# 基本方針\n\n平日の夜\n\n"
    "# 適用条件\n\n21時までに風呂に入る\n\n"
    "# 例外条件\n\n発熱時\n\n"
    "# 判断基準\n\n本人の状態\n"
)


def rule_md(**overrides: Any) -> str:
    base: dict[str, Any] = {
        "title": "入浴は21時",
        "category": "parenting",
        "status": "pending_approval",
        "version": 1,
        "rule_type": "guideline",
        "priority": "normal",
        "applicable_to": ["kids"],
        "tags": ["おふろ"],
        "created_at": "2026-01-25T00:00:00+09:00",
        "updated_at": "2026-01-25T10:00:00+09:00",
        "created_by": USER_ID,
        "approved_by": None,
    }
    base.update(overrides)
    front = yaml.safe_dump(base, allow_unicode=True, default_flow_style=False)
    return f"---\n{front}---\n\n{_BODY_TEXT}"


def _md_headers() -> dict[str, str]:
    return {**_auth_header(), "Content-Type": "text/markdown; charset=utf-8"}


def create_rule(client: TestClient, **overrides: Any) -> str:
    response = client.post(
        "/api/rules", content=rule_md(**overrides).encode(), headers=_md_headers()
    )
    assert response.status_code == 201, response.text
    rule_id: str = response.json()["id"]
    return rule_id


def revise_rule(client: TestClient, rule_id: str, **overrides: Any):
    return client.put(
        f"/api/rules/{rule_id}",
        content=rule_md(**overrides).encode(),
        headers=_md_headers(),
    )


def approve_by_api(client: TestClient, rule_id: str, body: Any = None):
    """POST する。 body=None 時はリクエストボディ自体を送らない。"""
    if body is None:
        return client.post(f"/api/approvals/{rule_id}/approve", headers=_auth_header())
    return client.post(
        f"/api/approvals/{rule_id}/approve", json=body, headers=_auth_header()
    )


def reject_by_api(client: TestClient, rule_id: str, body: Any = None):
    if body is None:
        return client.post(f"/api/approvals/{rule_id}/reject", headers=_auth_header())
    return client.post(
        f"/api/approvals/{rule_id}/reject", json=body, headers=_auth_header()
    )


def approve_by_repo(conn: sqlite3.Connection, rules_root: Path, rule_id: str) -> None:
    """固定時刻で承認する (承認履歴の approved_at が不変か見るため)。"""
    from app.repositories import rule_repository

    rule_repository.approve_pending_rule_version(
        conn,
        rules_root=rules_root,
        rule_id=rule_id,
        reviewed_by="approver-9",
        reviewed_at=APPROVED_AT,
    )
    conn.commit()


# ---------------------------------------------------------------------------
# app fixture (rules + approvals を同一 app へ mount)
# ---------------------------------------------------------------------------


def _build_app(db_path: Path, rules_root: Path) -> FastAPI:
    app = FastAPI()
    app.state.rules_db_path = db_path
    app.state.rules_root = rules_root
    users = (
        ApiUser(id=USER_ID, password_sha256=sha256_hex(PASSWORD), role="member"),
        ApiUser(
            id=OTHER_USER,
            password_sha256=sha256_hex(OTHER_PASSWORD),
            role="admin",
        ),
    )
    app.state.authenticator = Authenticator(users=users)
    emitter = ProblemEmitter.default()
    register_rule_problem_rules(emitter)
    emitter.install(app)
    install_unauthenticated_handler(app)
    app.include_router(rules_router)
    app.include_router(approvals_router)
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


# ---------------------------------------------------------------------------
# DB・md 観測 helper
# ---------------------------------------------------------------------------


def db_rule(conn: sqlite3.Connection, rule_id: str) -> dict[str, Any]:
    conn.commit()
    row = conn.execute(
        "SELECT status, current_version, approved_at FROM rules WHERE id = ?",
        (rule_id,),
    ).fetchone()
    assert row is not None
    return {"status": row[0], "current_version": row[1], "approved_at": row[2]}


def db_version(conn: sqlite3.Connection, rule_id: str, version: int) -> dict[str, Any]:
    conn.commit()
    row = conn.execute(
        "SELECT created_by, approved_by FROM rule_versions"
        " WHERE rule_id = ? AND version = ?",
        (rule_id, version),
    ).fetchone()
    assert row is not None
    return {"created_by": row[0], "approved_by": row[1]}


def db_approvals(conn: sqlite3.Connection, rule_id: str) -> list[dict[str, Any]]:
    conn.commit()
    rows = conn.execute(
        "SELECT status, proposed_by, reviewed_by, review_comment,"
        " created_at, reviewed_at FROM approvals WHERE rule_id = ?"
        " ORDER BY created_at",
        (rule_id,),
    ).fetchall()
    return [dict(zip(APPROVAL_COLUMNS, row, strict=True)) for row in rows]


def md_front_matter(
    rules_root: Path, rule_id: str, version: int, category: str = "parenting"
) -> dict[str, Any]:
    """server が 書い た md の front matter を 素の 読み方 で 返す。"""
    path = rules_root / category / rule_id / f"v{version}.md"
    parts = path.read_text(encoding="utf-8").split("---\n")
    data: dict[str, Any] = yaml.safe_load(parts[1])
    return data


def assert_problem(response: Any, status: int, code: str) -> Any:
    assert response.status_code == status, response.text
    payload = response.json()
    assert payload["code"] == code
    return payload


def assert_ok(response: Any) -> Any:
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------
# Basic auth (決定 1.28・1.31(1))
# ---------------------------------------------------------------------------

ENDPOINTS = (
    ("get", "/api/approvals"),
    ("get", "/api/approvals/any-id"),
    ("post", "/api/approvals/any-id/approve"),
    ("post", "/api/approvals/any-id/reject"),
)


@pytest.mark.parametrize(("method", "path"), ENDPOINTS)
def test_endpoints_require_basic_auth(env: Any, method: str, path: str) -> None:
    client, _conn, _root = env
    response = client.request(method, path)
    assert response.status_code == 401
    assert response.json()["code"] == "unknown_error.unauthenticated"
    assert response.headers["www-authenticate"] == f'Basic realm="{AUTH_REALM}"'


def test_invalid_credentials_are_401(env: Any) -> None:
    client, _conn, _root = env
    response = client.get(
        "/api/approvals", headers=_auth_header(OTHER_USER, "wrong-password")
    )
    assert response.status_code == 401
    assert response.json()["code"] == "unknown_error.unauthenticated"


# ---------------------------------------------------------------------------
# 一覧 (決定 1.30(5): 引数なし)
# ---------------------------------------------------------------------------


def test_list_empty(env: Any) -> None:
    client, _conn, _root = env
    payload = assert_ok(client.get("/api/approvals", headers=_auth_header()))
    assert payload == {"count": 0, "items": []}


def test_list_shows_only_pending_rules(env: Any) -> None:
    client, conn, _root = env
    pending_id = create_rule(client)
    decided_id = create_rule(client, title="すでに承認")
    approve_by_api(client, decided_id)

    payload = assert_ok(client.get("/api/approvals", headers=_auth_header()))

    assert payload["count"] == 1
    (item,) = payload["items"]
    assert set(item) == LIST_ITEM_KEYS
    assert item["id"] == pending_id
    assert item["status"] == "pending_approval"
    assert item["current_version"] == 1
    assert TOKYO_RE.match(item["updated_at"])
    assert db_rule(conn, decided_id)["status"] == "active"


def test_list_ignores_unknown_query_params(env: Any) -> None:
    client, _conn, _root = env
    create_rule(client)
    create_rule(client, category="health")
    payload = assert_ok(
        client.get("/api/approvals?category=health", headers=_auth_header())
    )
    assert payload["count"] == 2


# ---------------------------------------------------------------------------
# 1 件取得
# ---------------------------------------------------------------------------


def test_get_one_returns_detail(env: Any) -> None:
    client, _conn, _root = env
    rule_id = create_rule(client)
    payload = assert_ok(client.get(f"/api/approvals/{rule_id}", headers=_auth_header()))
    assert payload["id"] == rule_id
    assert payload["status"] == "pending_approval"
    assert payload["approved_by"] is None
    assert payload["body"]["basic_policy"] == "平日の夜"


def test_get_one_unknown_rule_is_approval_not_found(env: Any) -> None:
    client, _conn, _root = env
    body = assert_problem(
        client.get("/api/approvals/does-not-exist", headers=_auth_header()),
        404,
        "unknown_error.approval_not_found",
    )
    assert body["details"]["approval_id"] == "does-not-exist"


@pytest.mark.parametrize("path", ["/approve", "/reject"])
def test_get_one_decided_rule_is_approval_not_found(env: Any, path: str) -> None:
    client, _conn, _root = env
    rule_id = create_rule(client)
    assert_ok(
        approve_by_api(client, rule_id)
        if path == "/approve"
        else reject_by_api(client, rule_id)
    )
    assert_problem(
        client.get(f"/api/approvals/{rule_id}", headers=_auth_header()),
        404,
        "unknown_error.approval_not_found",
    )


# ---------------------------------------------------------------------------
# 承認 (決定 1.28・1.30(4)・1.31(3))
# ---------------------------------------------------------------------------


def test_approve_marks_rule_active(env: Any) -> None:
    client, conn, root = env
    rule_id = create_rule(client)

    response = approve_by_api(client, rule_id)
    payload = assert_ok(response)

    assert payload["id"] == rule_id
    assert payload["status"] == "active"
    assert payload["version"] == 1
    assert payload["approved_by"] == USER_ID
    assert (
        client.get(f"/api/approvals/{rule_id}", headers=_auth_header()).status_code
        == 404
    )
    assert payload == assert_ok(
        client.get(f"/api/rules/{rule_id}", headers=_auth_header())
    )

    row = db_rule(conn, rule_id)
    assert row["status"] == "active"
    assert row["current_version"] == 1
    assert DB_TS_RE.match(row["approved_at"])
    assert datetime.fromisoformat(row["approved_at"]) >= datetime.now(UTC) - timedelta(
        minutes=5
    )
    assert db_version(conn, rule_id, 1)["approved_by"] == USER_ID
    (approval,) = db_approvals(conn, rule_id)
    assert approval["status"] == "approved"
    assert approval["proposed_by"] == USER_ID
    assert approval["reviewed_by"] == USER_ID
    assert approval["review_comment"] is None
    assert approval["created_at"] == approval["reviewed_at"]
    fm = md_front_matter(root, rule_id, 1)
    assert fm["status"] == "active"
    assert fm["approved_by"] == USER_ID


def test_approve_accepts_request_without_body(env: Any) -> None:
    client, conn, _root = env
    rule_id = create_rule(client)
    assert_ok(approve_by_api(client, rule_id))
    assert db_rule(conn, rule_id)["status"] == "active"


def test_approve_accepts_empty_json_object(env: Any) -> None:
    client, conn, _root = env
    rule_id = create_rule(client)
    assert_ok(approve_by_api(client, rule_id, body={}))
    assert db_rule(conn, rule_id)["status"] == "active"
    (approval,) = db_approvals(conn, rule_id)
    assert approval["review_comment"] is None


def test_approve_records_review_comment(env: Any) -> None:
    client, conn, _root = env
    rule_id = create_rule(client)
    assert_ok(
        approve_by_api(client, rule_id, body={"review_comment": "方針を確認済み"})
    )
    (approval,) = db_approvals(conn, rule_id)
    assert approval["review_comment"] == "方針を確認済み"


def test_approve_rejects_rule_fields_in_body(env: Any) -> None:
    """決定 1.30(4): 審査側が送るのは任意の comment のみ。 rule 欄は不可。"""
    client, conn, _root = env
    rule_id = create_rule(client)
    body = assert_problem(
        approve_by_api(client, rule_id, body={"status": "rejected"}),
        400,
        "unknown_error.invalid_request",
    )
    (error,) = body["details"]["errors"]
    assert error["location"].endswith("status")
    assert error["type"] == "extra_forbidden"
    assert db_rule(conn, rule_id)["status"] == "pending_approval"


def test_approve_malformed_json_is_invalid_request(env: Any) -> None:
    client, conn, _root = env
    rule_id = create_rule(client)
    body = assert_problem(
        client.post(
            f"/api/approvals/{rule_id}/approve",
            content=b"{",
            headers={**_auth_header(), "Content-Type": "application/json"},
        ),
        400,
        "unknown_error.invalid_request",
    )
    assert body["details"]["errors"]
    assert db_rule(conn, rule_id)["status"] == "pending_approval"


def test_approve_unknown_rule_is_approval_not_found(env: Any) -> None:
    client, _conn, _root = env
    body = assert_problem(
        approve_by_api(client, "does-not-exist"),
        404,
        "unknown_error.approval_not_found",
    )
    assert body["details"]["approval_id"] == "does-not-exist"


def test_approve_twice_second_is_approval_not_found(env: Any) -> None:
    """審査済み rule は承認待ちではないので 404 (決定 1.31(3) 審査対象不在)。"""
    client, conn, _root = env
    rule_id = create_rule(client)
    assert_ok(approve_by_api(client, rule_id))
    assert_problem(
        approve_by_api(client, rule_id), 404, "unknown_error.approval_not_found"
    )
    assert len(db_approvals(conn, rule_id)) == 1


def test_approve_after_other_approver_is_approval_not_found(env: Any) -> None:
    """他審査者が先に決着させた場合、 approvals namespace の外なので 404。"""
    client, conn, root = env
    rule_id = create_rule(client)
    approve_by_repo(conn, root, rule_id)
    assert_problem(
        approve_by_api(client, rule_id), 404, "unknown_error.approval_not_found"
    )


def test_approver_identity_comes_from_basic_auth(env: Any) -> None:
    """決定 1.30(4): reviewed_by は認証 id。 body からは読まない。"""
    client, conn, _root = env
    rule_id = create_rule(client)
    response = client.post(
        f"/api/approvals/{rule_id}/approve",
        json={"review_comment": "ok"},
        headers=_auth_header(OTHER_USER, OTHER_PASSWORD),
    )
    assert_ok(response)
    (approval,) = db_approvals(conn, rule_id)
    assert approval["reviewed_by"] == OTHER_USER
    assert db_version(conn, rule_id, 1)["approved_by"] == OTHER_USER


# ---------------------------------------------------------------------------
# 却下
# ---------------------------------------------------------------------------


def test_reject_marks_rule_rejected(env: Any) -> None:
    client, conn, root = env
    rule_id = create_rule(client)

    payload = assert_ok(
        reject_by_api(client, rule_id, body={"review_comment": "例外が広すぎる"})
    )

    assert payload["status"] == "rejected"
    assert payload["version"] == 1
    assert payload["approved_by"] is None
    assert payload["body"], "却下でも md 本文は消さない"

    row = db_rule(conn, rule_id)
    assert row["status"] == "rejected"
    assert row["approved_at"] is None
    assert db_version(conn, rule_id, 1)["approved_by"] is None
    (approval,) = db_approvals(conn, rule_id)
    assert approval["status"] == "rejected"
    assert approval["reviewed_by"] == USER_ID
    assert approval["review_comment"] == "例外が広すぎる"
    assert md_front_matter(root, rule_id, 1)["status"] == "rejected"


def test_reject_keeps_prior_approved_at(env: Any) -> None:
    """決定 1.11: 却下でも過去の承認履歴 (rules.approved_at) は変えない。"""
    client, conn, root = env
    rule_id = create_rule(client)
    approve_by_repo(conn, root, rule_id)
    before = db_rule(conn, rule_id)
    assert before["approved_at"]

    revised = revise_rule(client, rule_id, updated_at="2026-01-25T12:00:00+09:00")
    assert revised.status_code == 200, revised.text
    assert db_rule(conn, rule_id)["status"] == "pending_approval"

    assert_ok(reject_by_api(client, rule_id))
    after = db_rule(conn, rule_id)
    assert after["status"] == "rejected"
    assert after["approved_at"] == before["approved_at"]


def test_reject_active_rule_is_approval_not_found(env: Any) -> None:
    client, conn, _root = env
    rule_id = create_rule(client)
    approve_by_api(client, rule_id)
    assert_problem(
        reject_by_api(client, rule_id), 404, "unknown_error.approval_not_found"
    )
    assert db_rule(conn, rule_id)["status"] == "active"


def test_approvers_endpoint_not_implemented(env: Any) -> None:
    """決定 1.28: approvers table は対象外なので専用 endpoint も存在しない。"""
    client, _conn, _root = env
    for path in ("/api/approvers", "/api/approvals/any-id/approvers"):
        assert client.get(path, headers=_auth_header()).status_code == 404
