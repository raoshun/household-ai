"""ルール 書き込み API 3 endpoint のテスト (契約 §2.7・決定 1.25/1.27/1.30/1.31)。

作成 (POST)・改訂 (PUT)・復元 (POST restore) を 実 DB (init_database) と
実 md ファイル 経由 で 検証 する。server 管理 欄 (id 采番・version・status・
approved_by・created_by) の 上書き、承認 操作 の DB・md への 反映、
同内容 拒否 の 409、status gate の 409、problem JSON の code・details を
固定 する。承認・却下 は HTTP endpoint で は なく repository 直接 呼び出し
で 作る (承認 API は 後続 単位)。
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
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import rules as rules_api
from app.api.auth import (
    AUTH_REALM,
    ApiUser,
    Authenticator,
    install_unauthenticated_handler,
    sha256_hex,
)
from app.api.rules import register_rule_problem_rules, router
from app.db.database import init_database
from app.services.exceptions import ProblemEmitter

USER_ID = "parent-a"
PASSWORD = "household-test-password"

#: 応答 時刻 が +09:00 の ms ISO か を 見る (決定 1.31(4))。
TOKYO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}\+09:00$")

APPROVED_AT = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def _auth_header() -> dict[str, str]:
    token = base64.b64encode(f"{USER_ID}:{PASSWORD}".encode()).decode("ascii")
    return {"Authorization": f"Basic {token}"}


# ---------------------------------------------------------------------------
# rule Markdown の 組み立て
# ---------------------------------------------------------------------------

_BODY_TEXT = (
    "# 基本方針\n\n平日の夜\n\n"
    "# 適用条件\n\n21時までに風呂に入る\n\n"
    "# 例外条件\n\n発熱時\n\n"
    "# 判断基準\n\n本人の状態\n"
)

_FM_KEYS = {
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


def _fm_dict(**overrides: Any) -> dict[str, Any]:
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
    return base


def rule_md(**overrides: Any) -> str:
    """front matter + 4 見出し 本文 の raw Markdown を 返す。

    ``drop`` で key を 除外 できる (必須 欄 missing の 422 ため)。
    """
    drop: tuple[str, ...] = overrides.pop("drop", ())
    data = {k: v for k, v in _fm_dict(**overrides).items() if k not in drop}
    front = yaml.safe_dump(data, allow_unicode=True, default_flow_style=False)
    return f"---\n{front}---\n\n{_BODY_TEXT}"


def bad_rule_md(body: str, **overrides: Any) -> str:
    return rule_md(**overrides).replace("平日の夜", body)


def _md_headers() -> dict[str, str]:
    return {**_auth_header(), "Content-Type": "text/markdown; charset=utf-8"}


def create_rule(client: TestClient, text: str):
    return client.post("/api/rules", content=text.encode(), headers=_md_headers())


def revise_rule(client: TestClient, rule_id: str, text: str):
    return client.put(
        f"/api/rules/{rule_id}", content=text.encode(), headers=_md_headers()
    )


def restore_version(client: TestClient, rule_id: str, version: int | None):
    query = "" if version is None else f"?version={version}"
    return client.post(f"/api/rules/{rule_id}/restore{query}", headers=_auth_header())


# ---------------------------------------------------------------------------
# app fixture (reads テスト と 同一 wiring)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# repository 操作 helper (承認・却下・状態 確認)
# ---------------------------------------------------------------------------


# approve/reject helpers reference repo lazily via module import below
def _repo() -> Any:
    from app.repositories import rule_repository

    return rule_repository


def approve(conn: sqlite3.Connection, rules_root: Path, rule_id: str) -> None:
    _repo().approve_pending_rule_version(
        conn,
        rules_root=rules_root,
        rule_id=rule_id,
        reviewed_by="approver-9",
        reviewed_at=APPROVED_AT,
    )
    conn.commit()


def reject(conn: sqlite3.Connection, rules_root: Path, rule_id: str) -> None:
    _repo().reject_pending_rule_version(
        conn,
        rules_root=rules_root,
        rule_id=rule_id,
        reviewed_by="reviewer-9",
        reviewed_at=APPROVED_AT,
        review_comment="差し戻し",
    )
    conn.commit()


def created_id(response: Any) -> str:
    assert response.status_code == 201, response.text
    rule_id: str = response.json()["id"]
    return rule_id


def first_error(response: Any) -> dict[str, str]:
    errors: list[dict[str, str]] = response.json()["details"]["errors"]
    return errors[0]


def assert_problem(response: Any, status: int, code: str) -> Any:
    assert response.status_code == status, response.text
    assert response.json()["code"] == code
    return response.json()


def db_rule(conn: sqlite3.Connection, rule_id: str) -> dict[str, Any]:
    conn.commit()
    row = conn.execute(
        "SELECT status, current_version, category, approved_at, archived_at"
        " FROM rules WHERE id = ?",
        (rule_id,),
    ).fetchone()
    assert row is not None
    return {
        "status": row[0],
        "current_version": row[1],
        "category": row[2],
        "approved_at": row[3],
        "archived_at": row[4],
    }


def db_version(conn: sqlite3.Connection, rule_id: str, version: int) -> dict[str, Any]:
    conn.commit()
    row = conn.execute(
        "SELECT change_reason, created_by, approved_by FROM rule_versions"
        " WHERE rule_id = ? AND version = ?",
        (rule_id, version),
    ).fetchone()
    assert row is not None
    return {"change_reason": row[0], "created_by": row[1], "approved_by": row[2]}


def db_approvals(conn: sqlite3.Connection, rule_id: str) -> list[dict[str, Any]]:
    conn.commit()
    rows = conn.execute(
        "SELECT status, reviewed_by, review_comment FROM approvals"
        " WHERE rule_id = ? ORDER BY id",
        (rule_id,),
    ).fetchall()
    return [{"status": r[0], "reviewed_by": r[1], "comment": r[2]} for r in rows]


def read_md(conn: sqlite3.Connection, rules_root: Path, rule_id: str, version: int):
    """version の md を 読み 前 matter dict と 本文 text に 分解 する。

    server 書込 み 前 に 一旦 commit し、md の hash が DB と 一致 する 状態 に
    従って 読む (load 経路 は 通さ ず 素の 読み方)。
    """
    conn.commit()
    path = rules_root / "parenting" / rule_id / f"v{version}.md"
    text = path.read_text(encoding="utf-8")
    parts = text.split("---\n")
    front = yaml.safe_load(parts[1])
    body = "---\n".join(parts[2:]).lstrip("\n")
    return front, body


# ---------------------------------------------------------------------------
# 認証 (決定 1.27)
# ---------------------------------------------------------------------------

#: 認証 頭 を 付け ない markdown 頭 (write endpoint の 認証 要求 の 証拠)。
ANONYMOUS_MD_HEADERS = {"Content-Type": "text/markdown; charset=utf-8"}

WRITE_CASES = (
    (
        "post",
        lambda c: c.post(
            "/api/rules", content=rule_md().encode(), headers=ANONYMOUS_MD_HEADERS
        ),
    ),
    (
        "put",
        lambda c: c.put(
            "/api/rules/parenting-001",
            content=rule_md().encode(),
            headers=ANONYMOUS_MD_HEADERS,
        ),
    ),
    (
        "restore",
        lambda c: c.post("/api/rules/parenting-001/restore?version=1"),
    ),
)


@pytest.mark.parametrize("kind,call", WRITE_CASES, ids=[c[0] for c in WRITE_CASES])
def test_write_endpoints_require_basic_auth(env: Any, kind: str, call: Any) -> None:
    client, _conn, _root = env
    response = call(client)
    assert response.status_code == 401
    assert response.json()["code"] == "unknown_error.unauthenticated"
    assert response.headers["www-authenticate"] == f'Basic realm="{AUTH_REALM}"'


@pytest.mark.parametrize("kind,call", WRITE_CASES, ids=[c[0] for c in WRITE_CASES])
def test_write_endpoints_reject_wrong_password(env: Any, kind: str, call: Any) -> None:
    client, _conn, _root = env
    wrong = base64.b64encode(f"{USER_ID}:bad".encode()).decode("ascii")
    headers: dict[str, str] = {"Authorization": f"Basic {wrong}"}
    path = "/api/rules"
    if kind == "put":
        response = client.put(path + "/parenting-001", content=b"x", headers=headers)
    elif kind == "restore":
        response = client.post(
            path + "/parenting-001/restore?version=1", headers=headers
        )
    else:
        response = client.post(path, content=rule_md().encode(), headers=headers)
    assert response.status_code == 401
    assert response.json()["code"] == "unknown_error.unauthenticated"


# ---------------------------------------------------------------------------
# 作成 (POST /api/rules)
# ---------------------------------------------------------------------------


def test_create_returns_flat_detail_and_writes_db_and_md(env: Any) -> None:
    client, conn, root = env
    response = create_rule(client, rule_md())
    assert response.status_code == 201, response.text
    payload = response.json()
    assert set(payload) == {
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
        "current_version",
        "summary",
        "body",
        "versions",
    }
    rule_id = payload["id"]
    assert rule_id.startswith("parenting-")
    assert payload["title"] == "入浴は21時"
    assert payload["category"] == "parenting"
    assert payload["status"] == "pending_approval"
    assert payload["version"] == 1
    assert payload["current_version"] == 1
    assert payload["rule_type"] == "guideline"
    assert payload["priority"] == "normal"
    assert payload["applicable_to"] == ["kids"]
    assert payload["tags"] == ["おふろ"]
    assert payload["approved_by"] is None
    assert payload["created_by"] == USER_ID
    assert TOKYO_RE.fullmatch(payload["created_at"])
    assert TOKYO_RE.fullmatch(payload["updated_at"])
    assert payload["body"] == {
        "basic_policy": "平日の夜",
        "applicable_conditions": "21時までに風呂に入る",
        "exception_conditions": "発熱時",
        "decision_criteria": "本人の状態",
    }
    version_row = payload["versions"][0]
    assert version_row == {
        "version": 1,
        "summary": "平日の夜",
        "change_reason": "作成",
        "created_by": USER_ID,
        "approved_by": None,
        "created_at": version_row["created_at"],
    }
    # rule_versions.created_at は DB の µs 付き 値、front matter と rules row は
    # 秒 precision に 切り捨て る (決定 1.19) ため、created_at は 形 だけ 一致 さ せる。
    assert TOKYO_RE.fullmatch(version_row["created_at"])
    assert db_rule(conn, rule_id) == {
        "status": "pending_approval",
        "current_version": 1,
        "category": "parenting",
        "approved_at": None,
        "archived_at": None,
    }
    assert db_version(conn, rule_id, 1) == {
        "change_reason": "作成",
        "created_by": USER_ID,
        "approved_by": None,
    }
    assert db_approvals(conn, rule_id) == []
    front, body_text = read_md(conn, root, rule_id, 1)
    assert front["status"] == "pending_approval"
    assert front["version"] == 1
    assert front["approved_by"] is None
    assert "平日の夜" in body_text


def test_create_generates_sequential_ids_per_category(env: Any) -> None:
    client, _conn, _root = env
    first = created_id(create_rule(client, rule_md()))
    second = created_id(create_rule(client, rule_md()))
    health = created_id(create_rule(client, rule_md(category="health")))
    nums = [int(first.rsplit("-", 1)[1]), int(second.rsplit("-", 1)[1])]
    assert nums == [1, 2]
    assert health.startswith("health-")


def test_create_rejects_client_assigned_id(env: Any) -> None:
    client, conn, _root = env
    response = create_rule(client, rule_md(id="parenting-900"))
    body = assert_problem(response, 400, "unknown_error.invalid_request")
    assert body["recoverable"] is False
    err = body["details"]["errors"][0]
    assert err["location"] == "body.rule_text.front_matter.id"
    assert "id" in err["message"]
    conn.commit()
    assert conn.execute("SELECT count(*) FROM rules").fetchone()[0] == 0


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"category": "cooking"}, "category"),
        ({"bogus_key": "x"}, "bogus_key"),
        ({"drop": ("title",)}, "title"),
        ({"rule_type": "mystery"}, "rule_type"),
    ],
    ids=["bad-category", "extra-key", "missing-title", "missing-applicable-to"],
)
def test_create_invalid_front_matter_is_422(
    env: Any, overrides: Any, fragment: str
) -> None:
    client, conn, _root = env
    response = create_rule(client, rule_md(**overrides))
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["code"] == "validation_error.rule_text"
    assert fragment in body["message"]
    conn.commit()
    assert conn.execute("SELECT count(*) FROM rules").fetchone()[0] == 0


def test_create_invalid_body_sections_are_422(env: Any) -> None:
    client, conn, _root = env
    text = rule_md().replace("# 例外条件\n\n発熱時\n\n", "")
    response = create_rule(client, text)
    assert response.status_code == 422
    assert "4 見出し" in response.json()["message"]
    conn.commit()
    assert conn.execute("SELECT count(*) FROM rules").fetchone()[0] == 0


def test_create_broken_yaml_is_422(env: Any) -> None:
    client, conn, _root = env
    text = rule_md()
    broken = "---\ntitle: [未閉鎖\n---\n\n" + _BODY_TEXT
    assert broken != text
    response = create_rule(client, broken)
    assert response.status_code == 422
    assert "YAML" in response.json()["message"] or "yaml" in response.json()["message"]
    conn.commit()
    assert conn.execute("SELECT count(*) FROM rules").fetchone()[0] == 0


def test_create_missing_opening_fence_is_422(env: Any) -> None:
    client, conn, _root = env
    text = rule_md().removeprefix("---\n")
    response = create_rule(client, text)
    assert response.status_code == 422
    assert "1 行目" in response.json()["message"]
    conn.commit()
    assert conn.execute("SELECT count(*) FROM rules").fetchone()[0] == 0


def test_create_empty_body_is_400(env: Any) -> None:
    client, conn, _root = env
    response = client.post("/api/rules", content=b"", headers=_md_headers())
    body = assert_problem(response, 400, "unknown_error.invalid_request")
    assert body["message"] == "リクエストが不正です"
    err = first_error(response)
    assert err["location"] == "body"
    assert "空" in err["message"]
    conn.commit()
    assert conn.execute("SELECT count(*) FROM rules").fetchone()[0] == 0


def test_create_non_utf8_body_is_400(env: Any) -> None:
    client, conn, _root = env
    response = client.post(
        "/api/rules", content=rule_md().encode("shift_jis"), headers=_md_headers()
    )
    assert_problem(response, 400, "unknown_error.invalid_request")
    assert "UTF-8" in first_error(response)["message"]
    conn.commit()
    assert conn.execute("SELECT count(*) FROM rules").fetchone()[0] == 0


def test_create_oversize_body_is_400(env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    client, conn, _root = env
    monkeypatch.setattr(rules_api, "_RULE_TEXT_MAX_BYTES", 10)
    response = create_rule(client, rule_md())
    assert_problem(response, 400, "unknown_error.invalid_request")
    assert "10" in first_error(response)["message"]
    conn.commit()
    assert conn.execute("SELECT count(*) FROM rules").fetchone()[0] == 0


def test_create_server_owns_fields_against_spoofing(env: Any) -> None:
    client, conn, root = env
    response = create_rule(
        client,
        rule_md(
            version=42,
            status="active",
            approved_by="evil-admin",
            created_by="spoof-user",
            created_at="1999-01-01T00:00:00+00:00",
        ),
    )
    payload = response.json()
    assert payload["version"] == 1
    assert payload["status"] == "pending_approval"
    assert payload["approved_by"] is None
    assert payload["created_by"] == USER_ID
    assert not payload["created_at"].startswith("1999")
    assert payload["versions"][0]["change_reason"] == "作成"
    assert db_approvals(conn, payload["id"]) == []
    front, _body = read_md(conn, root, payload["id"], 1)
    assert front["status"] == "pending_approval"
    assert front["version"] == 1
    assert front["created_by"] == USER_ID


def test_create_withdrawn_rule_id_not_blocked(env: Any) -> None:
    # 撤回 (archived) は 対象 rule が 無い ため 採番 継続 の み 見る
    client, conn, root = env
    assert (
        db_rule(conn, created_id(create_rule(client, rule_md())))["status"]
        == "pending_approval"
    )
    assert (
        db_rule(conn, created_id(create_rule(client, rule_md())))["status"]
        == "pending_approval"
    )
    assert conn.execute("SELECT count(*) FROM rules").fetchone()[0] == 2
    del root


# ---------------------------------------------------------------------------
# 承認・却下 helper の 検証 (repository 直接。契約 §2.2)
# ---------------------------------------------------------------------------


def test_approve_flips_state_writes_db_and_md(env: Any) -> None:
    client, conn, root = env
    rule_id = created_id(create_rule(client, rule_md()))
    approve(conn, root, rule_id)
    row = db_rule(conn, rule_id)
    assert row["status"] == "active"
    assert row["approved_at"] is not None
    assert row["approved_at"].startswith("2026-01-02T03:04:05")
    assert db_version(conn, rule_id, 1)["approved_by"] == "approver-9"
    assert db_approvals(conn, rule_id) == [
        {"status": "approved", "reviewed_by": "approver-9", "comment": None}
    ]
    front, _body = read_md(conn, root, rule_id, 1)
    assert front["status"] == "active"


def test_reject_flips_state_writes_db_and_md(env: Any) -> None:
    client, conn, root = env
    rule_id = created_id(create_rule(client, rule_md()))
    reject(conn, root, rule_id)
    row = db_rule(conn, rule_id)
    assert row["status"] == "rejected"
    assert row["approved_at"] is None
    assert row["current_version"] == 1
    assert db_version(conn, rule_id, 1)["approved_by"] is None
    assert db_approvals(conn, rule_id) == [
        {"status": "rejected", "reviewed_by": "reviewer-9", "comment": "差し戻し"}
    ]
    front, _body = read_md(conn, root, rule_id, 1)
    assert front["status"] == "rejected"


# ---------------------------------------------------------------------------
# 改訂 (PUT /api/rules/{rule_id})
# ---------------------------------------------------------------------------


def test_revise_active_rule_creates_pending_version(env: Any) -> None:
    client, conn, root = env
    rule_id = created_id(create_rule(client, rule_md()))
    approve(conn, root, rule_id)
    response = revise_rule(client, rule_id, bad_rule_md("金曜の夜"))
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["version"] == 2
    assert payload["status"] == "pending_approval"
    assert payload["approved_by"] is None
    assert payload["current_version"] == 2
    assert payload["body"]["basic_policy"] == "金曜の夜"
    assert [v["version"] for v in payload["versions"]] == [2, 1]
    assert payload["versions"][0]["change_reason"] == "内容変更"
    assert payload["versions"][1]["change_reason"] == "作成"
    assert db_rule(conn, rule_id) == {
        "status": "pending_approval",
        "current_version": 2,
        "category": "parenting",
        "approved_at": db_rule(conn, rule_id)["approved_at"],
        "archived_at": None,
    }
    assert db_version(conn, rule_id, 2) == {
        "change_reason": "内容変更",
        "created_by": USER_ID,
        "approved_by": None,
    }
    assert len(db_approvals(conn, rule_id)) == 1
    front, body_text = read_md(conn, root, rule_id, 2)
    assert front["status"] == "pending_approval"
    assert front["version"] == 2
    assert "金曜の夜" in body_text
    assert (rules_md_dir(root, rule_id) / "v1.md").read_text().count("平日の夜") == 1


def rules_md_dir(root: Path, rule_id: str) -> Path:
    return root / "parenting" / rule_id


def test_revise_same_content_is_409(env: Any) -> None:
    """pending_approval 中 の 同一 内容 改訂 は 409 (決定 1.10・1.23)。"""
    client, conn, root = env
    text = rule_md()
    rule_id = created_id(create_rule(client, text))
    response = revise_rule(client, rule_id, text)
    assert_problem(response, 409, "unknown_error.rule_content_conflict")
    assert "同一" in response.json()["message"]
    row = db_rule(conn, rule_id)
    assert row["status"] == "pending_approval"
    assert row["current_version"] == 1
    front, _body = read_md(conn, root, rule_id, 1)
    assert front["version"] == 1


def test_revise_active_rule_with_same_body_waits_for_reapproval(env: Any) -> None:
    """active 中 の 同内容 submission は pending_approval へ の 状態 変化 な ので 通る。

    fingerprint に status・approved_by を 含める のは、決定 1.23 の 復元 row
    (rejected を active へ 戻す) を 同内容 guard で 止める な という トレード オフ の
    帰結 で、ここ が その 副作用 側 の 挙動 で ある こと を 登記 する。
    """
    client, conn, root = env
    text = rule_md()
    rule_id = created_id(create_rule(client, text))
    approve(conn, root, rule_id)
    ok = revise_rule(client, rule_id, text)
    assert ok.status_code == 200, ok.text
    payload = ok.json()
    assert payload["version"] == 2
    assert payload["status"] == "pending_approval"
    assert db_rule(conn, rule_id)["current_version"] == 2


def test_revise_after_fixing_conflict_succeeds(env: Any) -> None:
    client, _conn, _root = env
    rule_id = created_id(create_rule(client, rule_md()))
    assert revise_rule(client, rule_id, rule_md()).status_code == 409
    ok = revise_rule(client, rule_id, bad_rule_md("水曜の夜"))
    assert ok.status_code == 200, ok.text
    assert ok.json()["version"] == 2


def test_revise_pending_rule_is_allowed(env: Any) -> None:
    client, conn, root = env
    rule_id = created_id(create_rule(client, rule_md()))
    response = revise_rule(client, rule_id, bad_rule_md("水曜の夜"))
    assert response.status_code == 200, response.text
    assert response.json()["version"] == 2
    assert db_rule(conn, rule_id)["current_version"] == 2
    del root


def test_revise_rejected_rule_is_409(env: Any) -> None:
    """rejected の 改訂 は 不可 (決定 1.11 の 状態 gate)。"""

    # 再提案 は 復元 か 新規 作成 で 行う。
    client, conn, root = env
    rule_id = created_id(create_rule(client, rule_md()))
    reject(conn, root, rule_id)
    response = revise_rule(client, rule_id, bad_rule_md("木曜の夜"))
    assert_problem(response, 409, "unknown_error.rule_version_state")
    assert db_rule(conn, rule_id)["current_version"] == 1


def test_revise_category_change_is_400(env: Any) -> None:
    client, conn, root = env
    rule_id = created_id(create_rule(client, rule_md()))
    response = revise_rule(client, rule_id, rule_md(category="education"))
    assert_problem(response, 400, "unknown_error.invalid_request")
    err = first_error(response)
    assert err["location"] == "body.rule_text.front_matter.category"
    assert "category" in err["message"]
    assert db_rule(conn, rule_id)["current_version"] == 1
    del root


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"category": "cooking"}, "category"),
        ({"drop": ("title",)}, "title"),
    ],
    ids=["bad-category", "missing-title"],
)
def test_revise_invalid_front_matter_is_422(
    env: Any, overrides: Any, fragment: str
) -> None:
    client, conn, root = env
    rule_id = created_id(create_rule(client, rule_md()))
    response = revise_rule(client, rule_id, rule_md(**overrides))
    assert response.status_code == 422
    assert response.json()["code"] == "validation_error.rule_text"
    assert fragment in response.json()["message"]
    assert db_rule(conn, rule_id)["current_version"] == 1
    del root


def test_revise_unknown_rule_is_404(env: Any) -> None:
    client, _conn, _root = env
    response = revise_rule(client, "missing-001", rule_md())
    assert_problem(response, 404, "unknown_error.rule_not_found")


def test_revise_empty_body_is_400(env: Any) -> None:
    client, _conn, _root = env
    response = client.put(
        "/api/rules/parenting-001", content=b"", headers=_md_headers()
    )
    assert_problem(response, 400, "unknown_error.invalid_request")


# ---------------------------------------------------------------------------
# 復元 (POST /api/rules/{rule_id}/restore)
# ---------------------------------------------------------------------------


def _active_with_two_versions(env: Any) -> tuple[Any, sqlite3.Connection, Path, str]:
    client, conn, root = env
    rule_id = created_id(create_rule(client, rule_md()))
    approve(conn, root, rule_id)
    assert revise_rule(client, rule_id, bad_rule_md("金曜の夜")).status_code == 200
    approve(conn, root, rule_id)
    return client, conn, root, rule_id


def test_restore_past_version_ignores_approval_flow(env: Any) -> None:
    client, conn, root, rule_id = _active_with_two_versions(env)
    response = restore_version(client, rule_id, 1)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["version"] == 3
    assert payload["status"] == "active"
    assert payload["approved_by"] is None
    assert payload["current_version"] == 3
    assert payload["body"]["basic_policy"] == "平日の夜"
    assert payload["versions"][0]["change_reason"] == "v1 による復元"
    assert payload["versions"][0]["approved_by"] is None
    row = db_rule(conn, rule_id)
    assert row["status"] == "active"
    assert row["current_version"] == 3
    assert row["approved_at"] is not None  # 承認 時刻 は 無変更
    assert db_version(conn, rule_id, 3) == {
        "change_reason": "v1 による復元",
        "created_by": USER_ID,
        "approved_by": None,
    }
    assert len(db_approvals(conn, rule_id)) == 2  # approvals 追加 なし
    _front, body_text = read_md(conn, root, rule_id, 3)
    assert "平日の夜" in body_text
    assert "金曜の夜" not in body_text


def test_restore_flips_rejected_rule_to_active(env: Any) -> None:
    client, conn, root = env
    rule_id = created_id(create_rule(client, rule_md()))
    approve(conn, root, rule_id)
    assert revise_rule(client, rule_id, bad_rule_md("金曜の夜")).status_code == 200
    reject(conn, root, rule_id)
    assert db_rule(conn, rule_id)["status"] == "rejected"
    response = restore_version(client, rule_id, 1)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "active"
    assert db_rule(conn, rule_id)["status"] == "active"
    assert db_rule(conn, rule_id)["current_version"] == 3


def test_restore_current_version_is_409(env: Any) -> None:
    client, conn, root, rule_id = _active_with_two_versions(env)
    response = restore_version(client, rule_id, 2)
    assert_problem(response, 409, "unknown_error.rule_content_conflict")
    assert "同一" in response.json()["message"]
    assert db_rule(conn, rule_id)["current_version"] == 2
    del root


def test_restore_same_content_as_current_is_409(env: Any) -> None:
    client, conn, root, rule_id = _active_with_two_versions(env)
    assert restore_version(client, rule_id, 1).status_code == 200
    response = restore_version(client, rule_id, 1)
    assert_problem(response, 409, "unknown_error.rule_content_conflict")
    assert db_rule(conn, rule_id)["current_version"] == 3
    del root


def test_restore_pending_rule_is_409(env: Any) -> None:
    client, conn, root, rule_id = _active_with_two_versions(env)
    assert revise_rule(client, rule_id, bad_rule_md("土曜の朝")).status_code == 200
    response = restore_version(client, rule_id, 1)
    assert_problem(response, 409, "unknown_error.rule_version_state")
    assert db_rule(conn, rule_id)["current_version"] == 3
    del root


def test_restore_missing_version_is_404(env: Any) -> None:
    client, conn, root, rule_id = _active_with_two_versions(env)
    response = restore_version(client, rule_id, 99)
    body = assert_problem(response, 404, "unknown_error.rule_not_found")
    assert "99" in body["message"]
    assert db_rule(conn, rule_id)["current_version"] == 2
    del root


def test_restore_unknown_rule_is_404(env: Any) -> None:
    client, _conn, _root = env
    response = restore_version(client, "missing-001", 1)
    assert_problem(response, 404, "unknown_error.rule_not_found")


@pytest.mark.parametrize(
    ("raw", "type_name"),
    [("", "int_parsing"), ("abc", "int_parsing"), ("0", "greater_than_equal")],
    ids=["empty", "not-int", "zero"],
)
def test_restore_invalid_version_query_is_400(
    env: Any, raw: str, type_name: str
) -> None:
    client, conn, root, rule_id = _active_with_two_versions(env)
    response = client.post(
        f"/api/rules/{rule_id}/restore?version={raw}", headers=_auth_header()
    )
    assert_problem(response, 400, "unknown_error.invalid_request")
    err = first_error(response)
    assert err["location"] == "query.version"
    assert err["type"] == type_name
    assert db_rule(conn, rule_id)["current_version"] == 2
    del root


def test_restore_without_version_query_is_400(env: Any) -> None:
    client, _conn, _root = env
    response = client.post("/api/rules/missing-001/restore", headers=_auth_header())
    assert_problem(response, 400, "unknown_error.invalid_request")
