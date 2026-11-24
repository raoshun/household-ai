"""app.main.create_app の 起動 組立 テスト (契約 §2.7・決定 1.32)。

README §13.4 の `uvicorn app.main:app` が 立てる app を 対象 に、(1) Phase 1 の
11 endpoint だけ が mount さ れ てる か (hub の chat・HITL・history は 載せ ない)、
(2) 認証 users 未設定 の fail-closed、(3) startup の migration 適用、
(4) CORS (config.py の `api_cors_origins` 再利用)、を実 DB で 検証 する。
"""

from __future__ import annotations

import base64
import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.auth import AuthConfigError, sha256_hex
from app.config import DEFAULT_API_CORS_ORIGINS, Settings
from app.main import create_app

USER_ID = "parent-a"
PASSWORD = "household-test-password"

#: phase 1 API surface = 契約 §2.7.1 L305-L317 の 11 endpoint (method, path)。
PHASE1_ROUTES: set[tuple[str, str]] = {
    ("GET", "/api/rules"),
    ("POST", "/api/rules"),
    ("GET", "/api/rules/{rule_id}"),
    ("PUT", "/api/rules/{rule_id}"),
    ("GET", "/api/rules/{rule_id}/versions"),
    ("POST", "/api/rules/{rule_id}/restore"),
    ("GET", "/api/rules/search"),
    ("GET", "/api/approvals"),
    ("GET", "/api/approvals/{rule_id}"),
    ("POST", "/api/approvals/{rule_id}/approve"),
    ("POST", "/api/approvals/{rule_id}/reject"),
}


def endpoint_surface(app: FastAPI) -> set[tuple[str, str]]:
    """app が 公開 する (method, path) 集合。docs・openapi route を 除く 実体。"""

    paths: dict[str, Any] = app.openapi()["paths"]
    return {
        (method.upper(), path)
        for path, operations in paths.items()
        for method in operations
    }


def settings_for(tmp_path: Path, **overrides: Any) -> Settings:
    """tmp の data_dir を 指し た 設定 (認証 users は 既定 で 1 名 設定 済み)。"""

    users = json.dumps(
        [{"id": USER_ID, "password_sha256": sha256_hex(PASSWORD), "role": "member"}]
    )
    values: dict[str, Any] = {
        "data_dir": tmp_path,
        "api_users_json": users,
        "api_cors_origins": list(DEFAULT_API_CORS_ORIGINS),
    }
    values.update(overrides)
    return Settings(**values)


def auth_header() -> dict[str, str]:
    token = base64.b64encode(f"{USER_ID}:{PASSWORD}".encode()).decode("ascii")
    return {"Authorization": f"Basic {token}"}


def rule_md(title: str = "入浴は21時") -> str:
    front = yaml.safe_dump(
        {
            "title": title,
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
        },
        allow_unicode=True,
        default_flow_style=False,
    )
    body = (
        "# 基本方針\n\n平日の夜\n\n"
        "# 適用条件\n\n21時\n\n"
        "# 例外条件\n\n発熱時\n\n"
        "# 判断基準\n\n本人\n"
    )
    return f"---\n{front}---\n\n{body}"


def create_rule(client: TestClient) -> str:
    response = client.post(
        "/api/rules",
        content=rule_md().encode(),
        headers={**auth_header(), "Content-Type": "text/markdown; charset=utf-8"},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


@pytest.fixture()
def client(tmp_path: Path) -> Iterator[TestClient]:
    """startup (認証 検査・migration) を 通し た TestClient。"""

    with TestClient(create_app(settings_for(tmp_path))) as c:
        yield c


# ---------------------------------------------------------------------------
# mount 面
# ---------------------------------------------------------------------------


def test_phase1_endpoint_set_is_exactly_eleven(tmp_path: Path) -> None:
    """Phase 1 endpoint = 11。hub・history 由来 の route が 無 い 事 を 固定 する。"""

    assert endpoint_surface(create_app(settings_for(tmp_path))) == PHASE1_ROUTES


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/chat/threads"),
        ("POST", "/api/chat/threads"),
        ("GET", "/api/history/judgments"),
        ("GET", "/api/approvals/pending"),
    ],
)
def test_hub_and_history_routes_absent(
    client: TestClient, method: str, path: str
) -> None:
    """hub 本来 の chat・history・未 規定 approvals は 404 (決定 1.28(2)・1.29)。"""

    assert client.request(method, path, headers=auth_header()).status_code == 404


# ---------------------------------------------------------------------------
# 起動 時 fail-closed と migration
# ---------------------------------------------------------------------------


def test_startup_fails_closed_without_auth_users(tmp_path: Path) -> None:
    """API_USERS_JSON 未設定 は 起動 失敗 (決定 1.27・1.32)。"""

    app = create_app(settings_for(tmp_path, api_users_json=""))
    with pytest.raises(AuthConfigError), TestClient(app):
        pytest.fail("users 未設定 で startup が 通ッ た")


def test_startup_fails_closed_for_empty_user_list(tmp_path: Path) -> None:
    """空 list も fail-closed (決定 1.27)。"""

    app = create_app(settings_for(tmp_path, api_users_json="[]"))
    with pytest.raises(AuthConfigError), TestClient(app):
        pytest.fail("空 list で startup が 通ッ た")


def test_startup_creates_database_and_rule_dirs(
    client: TestClient, tmp_path: Path
) -> None:
    """startup で DB が 作 ら れ migration が 適用 済み、rules dir も 存在。"""

    db_path = tmp_path / "household.db"
    assert db_path.exists()
    assert (tmp_path / "rules").is_dir()
    conn = sqlite3.connect(db_path)
    try:
        assert int(conn.execute("PRAGMA user_version").fetchone()[0]) == 2
        tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert {"rules", "rule_versions", "approvals"} <= tables
    finally:
        conn.close()


def test_rules_root_is_settings_rules_dir(client: TestClient) -> None:
    """rule が settings.rules_dir (data_dir/rules) へ 作 ら れ る wiring で ある 事。"""

    rule_id = create_rule(client)
    assert (client.app.state.rules_root / "parenting" / rule_id / "v1.md").exists()


# ---------------------------------------------------------------------------
# 認証・CORS・スモーク
# ---------------------------------------------------------------------------


def test_endpoints_require_basic_auth(client: TestClient) -> None:
    """無 認証 は 401 + WWW-Authenticate + problem JSON (決定 1.27)。"""

    response = client.get("/api/rules")
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"].startswith("Basic")
    assert response.json()["code"] == "unknown_error.unauthenticated"


def test_phase1_flow_smoke(client: TestClient) -> None:
    """作成 → 承認待ち 一覧 → 承認 が app 組立 経路 で 通る (create_app で 結線)。"""

    rule_id = create_rule(client)
    listed = client.get("/api/approvals", headers=auth_header())
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["items"]] == [rule_id]

    approved = client.post(f"/api/approvals/{rule_id}/approve", headers=auth_header())
    assert approved.status_code == 200
    assert approved.json()["status"] == "active"
    assert client.get("/api/approvals", headers=auth_header()).json()["count"] == 0


def test_preflight_allowed_for_configured_origin(client: TestClient) -> None:
    """設定 origin の preflight だ け 受理 さ れる (決定 1.32 = config 再利用)。"""

    allowed = client.options(
        "/api/rules",
        headers={
            "Origin": "http://127.0.0.1:3000",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == "http://127.0.0.1:3000"
    assert allowed.headers["access-control-allow-credentials"] == "true"

    denied = client.options(
        "/api/rules",
        headers={
            "Origin": "http://evil.example.com",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert "access-control-allow-origin" not in denied.headers


def test_cors_origins_follow_settings(tmp_path: Path) -> None:
    """api_cors_origins を 変える と 許可 origin も 追従 す る。"""

    settings = settings_for(tmp_path, api_cors_origins=["https://home.example.net"])
    with TestClient(create_app(settings)) as c:
        response = c.options(
            "/api/rules",
            headers={
                "Origin": "https://home.example.net",
                "Access-Control-Request-Method": "GET",
            },
        )
    assert response.headers["access-control-allow-origin"] == "https://home.example.net"
