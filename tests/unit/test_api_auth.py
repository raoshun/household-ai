"""HTTP Basic 認証基盤のテスト (契約 決定 1.27 / Issue #8 unit-6)。

parse_api_users の fail-closed 検証、parse_basic_credentials の RFC 7617 準拠、
Authenticator の compare_digest 比較、401 problem JSON + WWW-Authenticate を 固定 する。
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from pytest import MonkeyPatch

from app.api.auth import (
    AUTH_REALM,
    ApiUser,
    AuthConfigError,
    Authenticator,
    UnauthenticatedError,
    build_authenticator,
    create_auth_dependency,
    install_unauthenticated_handler,
    parse_api_users,
    parse_basic_credentials,
    sha256_hex,
    unauthenticated_problem_response,
    user_roles,
)
from app.config import Settings, load_settings

_ADMIN_PASSWORD = "alice-secret-pw"
_MEMBER_PASSWORD = "bob-secret-pw:punct"

VALID_USERS = (
    {
        "id": "shun",
        "password_sha256": sha256_hex(_ADMIN_PASSWORD),
        "role": "admin",
    },
    {
        "id": "hana",
        "password_sha256": sha256_hex(_MEMBER_PASSWORD),
        "role": "member",
    },
)


def _users_json(entries: list[dict[str, Any]]) -> str:
    return json.dumps(entries)


def _valid_users_json() -> str:
    return _users_json(list(VALID_USERS))


def _basic(username: str, password: str) -> str:
    token = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
    return f"Basic {token}"


def _build_app(users_json: str) -> tuple[FastAPI, Authenticator]:
    authenticator = build_authenticator(Settings(api_users_json=users_json))
    app = FastAPI()
    install_unauthenticated_handler(app)
    require = create_auth_dependency(authenticator)

    @app.get("/api/ping", dependencies=[Depends(require)])
    def ping() -> dict[str, str]:  # pragma: no cover - assertions 対象 は 外側
        return {"pong": "ok"}

    # from __future__ import annotations 配下ではアノテーション文字列内の
    # ローカル変数 (require) が eval できないため、既定値形式の Depends を使う
    # (FastAPI 公式の回避策。実装上のルートは Annotated 形式を使う)。
    @app.get("/api/whoami")
    def whoami(
        user: ApiUser = Depends(require),  # noqa: B008
    ) -> dict[str, str]:
        return {"id": user.id, "role": user.role}

    return app, authenticator


def _client(users_json: str) -> TestClient:
    app, _ = _build_app(users_json)
    return TestClient(app)


# --- sha256_hex ------------------------------------------------------------


def test_sha256_hex_matches_hashlib_lowercase_hex() -> None:
    assert sha256_hex("") == hashlib.sha256(b"").hexdigest()
    assert sha256_hex("ä") == hashlib.sha256("ä".encode()).hexdigest()
    digest = sha256_hex(_ADMIN_PASSWORD)
    assert len(digest) == 64
    assert digest == digest.lower()


# --- parse_api_users: fail-closed ------------------------------------------


def test_parse_api_users_empty_raw_is_fail_closed() -> None:
    with pytest.raises(AuthConfigError):
        parse_api_users("")
    with pytest.raises(AuthConfigError):
        parse_api_users("   ")


def test_parse_api_users_empty_list_is_fail_closed() -> None:
    with pytest.raises(AuthConfigError, match="空 list"):
        parse_api_users("[]")


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        '"just-a-string"',
        '{"id":"a"}',
        "[{}]",  # object 内 は 空 → 必須 field 欠落
        '["a"]',  # 要素 が object で ない
        "[1]",
    ],
)
def test_parse_api_users_malformed_forms_are_fail_closed(raw: str) -> None:
    with pytest.raises(AuthConfigError):
        parse_api_users(raw)


@pytest.mark.parametrize(
    ("entry", "reason"),
    [
        (
            {"id": "", "password_sha256": "ab" * 32, "role": "admin"},
            "空 id",
        ),
        (
            {"id": "  ", "password_sha256": "ab" * 32, "role": "admin"},
            "空白の み id",
        ),
        (
            {"id": "u", "password_sha256": "AB" * 32, "role": "admin"},
            "大文字 hex",
        ),
        (
            {"id": "u", "password_sha256": "abcd", "role": "admin"},
            "短すぎる hash",
        ),
        (
            {"id": "u", "password_sha256": "zz" * 32, "role": "admin"},
            "hex 以外",
        ),
        (
            {"id": "u", "password_sha256": "ab" * 32, "role": "guest"},
            "未登録 role",
        ),
        (
            {"id": "u", "password_sha256": "ab" * 32},
            "role 欠落",
        ),
        (
            {
                "id": "u",
                "password_sha256": "ab" * 32,
                "role": "admin",
                "password": "明文",
            },
            "未知 field (平文 password)",
        ),
    ],
)
def test_parse_api_users_invalid_elements_are_fail_closed(
    entry: dict[str, Any], reason: str
) -> None:
    with pytest.raises(AuthConfigError, match=r"users\[0\]"):
        parse_api_users(_users_json([entry]))
    assert reason  # 命名パラメータ の 説明 用。検証 は 上 の raises


def test_parse_api_users_rejects_duplicate_ids() -> None:
    entry = VALID_USERS[0]
    with pytest.raises(AuthConfigError, match="重複"):
        parse_api_users(_users_json([entry, dict(entry)]))


def test_parse_api_users_accepts_valid_users_in_order() -> None:
    users = parse_api_users(_valid_users_json())
    assert isinstance(users, tuple)
    assert [user.id for user in users] == ["shun", "hana"]
    assert users[0].role == "admin"
    assert users[1].role == "member"
    assert users[0].password_sha256 == sha256_hex(_ADMIN_PASSWORD)


def test_parse_api_users_error_never_leaks_password_digest() -> None:
    raw = _users_json([{"id": "u", "password_sha256": "short", "role": "admin"}])
    with pytest.raises(AuthConfigError) as excinfo:
        parse_api_users(raw)
    assert "short" not in str(excinfo.value)


# --- Authenticator ----------------------------------------------------------


def _authenticator() -> Authenticator:
    return Authenticator(users=parse_api_users(_valid_users_json()))


def test_authenticator_user_ids_exposes_ids_only() -> None:
    authenticator = _authenticator()
    ids = authenticator.user_ids()
    assert ids == ("shun", "hana")
    assert all("password" not in str(item) for item in ids)


def test_authenticator_verify_accepts_correct_password() -> None:
    user = _authenticator().verify("shun", _ADMIN_PASSWORD)
    assert user is not None
    assert user.id == "shun"
    assert user.role == "admin"


def test_authenticator_verify_rejects_wrong_password() -> None:
    assert _authenticator().verify("shun", _ADMIN_PASSWORD + "x") is None


def test_authenticator_verify_rejects_unknown_user() -> None:
    assert _authenticator().verify("eve", _ADMIN_PASSWORD) is None


def test_authenticator_verify_rejects_cross_user_password() -> None:
    assert _authenticator().verify("hana", _ADMIN_PASSWORD) is None


def test_user_roles_maps_ids_to_roles() -> None:
    assert user_roles(parse_api_users(_valid_users_json())) == {
        "shun": "admin",
        "hana": "member",
    }


# --- build_authenticator: Settings 経由 の fail-closed ----------------------


def test_build_authenticator_from_settings_env(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("API_USERS_JSON", _valid_users_json())
    authenticator = build_authenticator(load_settings(tmp_path))
    assert authenticator.user_ids() == ("shun", "hana")


def test_build_authenticator_fail_closed_when_env_unset(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("API_USERS_JSON", raising=False)
    with pytest.raises(AuthConfigError, match="未設定"):
        build_authenticator(load_settings(tmp_path))


def test_build_authenticator_fail_closed_for_empty_list(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("API_USERS_JSON", "[]")
    with pytest.raises(AuthConfigError, match="空 list"):
        build_authenticator(load_settings(tmp_path))


# --- parse_basic_credentials -------------------------------------------------


def _b64(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


def test_parse_basic_credentials_none_header() -> None:
    assert parse_basic_credentials(None) is None


@pytest.mark.parametrize(
    "header",
    [
        "",
        "Basic",
        "Basic aW1wb3J0YW50OnNlY3JldA== extra",
        "Bearer abc123",
        "Digest aW1wb3J0YW50OnNlY3JldA==",
        "Basic not!base64!",
        f"Basic {_b64('no-colon-here')}",
    ],
)
def test_parse_basic_credentials_rejects_malformed_headers(header: str) -> None:
    assert parse_basic_credentials(header) is None


def test_parse_basic_credentials_accepts_valid_basic_header() -> None:
    header = f"Basic {_b64('shun:pw123')}"
    assert parse_basic_credentials(header) == ("shun", "pw123")


def test_parse_basic_credentials_scheme_is_case_insensitive() -> None:
    header = f"basic {_b64('shun:pw123')}"
    assert parse_basic_credentials(header) == ("shun", "pw123")


def test_parse_basic_credentials_splits_on_first_colon_only() -> None:
    header = f"Basic {_b64('hana:a:b:c')}"
    assert parse_basic_credentials(header) == ("hana", "a:b:c")


def test_parse_basic_credentials_accepts_unicode_and_empty_password() -> None:
    assert parse_basic_credentials(f"Basic {_b64('しゅん:パスワード')}") == (
        "しゅん",
        "パスワード",
    )
    assert parse_basic_credentials(f"Basic {_b64('shun:')}") == ("shun", "")


# --- 401 problem 応答 ---------------------------------------------------------


def test_unauthenticated_problem_response_matches_decision_1_27() -> None:
    response = unauthenticated_problem_response()
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == f'Basic realm="{AUTH_REALM}"'
    body = json.loads(response.body)
    assert body["error_type"] == "unknown_error"
    assert body["code"] == "unknown_error.unauthenticated"
    assert body["recoverable"] is False


def _assert_401_problem(response: Any) -> None:
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == 'Basic realm="household-ai"'
    body = response.json()
    assert body["error_type"] == "unknown_error"
    assert body["code"] == "unknown_error.unauthenticated"


# --- HTTP 層 (dependency + handler) ------------------------------------------


@pytest.fixture()
def client() -> TestClient:
    return _client(_valid_users_json())


@pytest.mark.parametrize("path", ["/api/ping", "/api/whoami"])
def test_requests_without_authorization_get_401_problem(
    client: TestClient, path: str
) -> None:
    _assert_401_problem(client.get(path))


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "Bearer abc",
        "Basic not!base64!",
        f"Basic {_b64('no-colon')}",
        _basic("shun", "wrong-password"),
        _basic("eve", _ADMIN_PASSWORD),
        _basic("hana", _ADMIN_PASSWORD),
        _basic("shun", ""),
    ],
)
def test_failed_authentications_get_401_problem(
    client: TestClient, header: str | None
) -> None:
    headers = {} if header is None else {"Authorization": header}
    _assert_401_problem(client.get("/api/ping", headers=headers))


def test_valid_credentials_are_accepted_by_dependency(client: TestClient) -> None:
    response = client.get(
        "/api/ping", headers={"Authorization": _basic("shun", _ADMIN_PASSWORD)}
    )
    assert response.status_code == 200
    assert response.json() == {"pong": "ok"}


def test_dependency_exposes_authenticated_identity(client: TestClient) -> None:
    response = client.get(
        "/api/whoami", headers={"Authorization": _basic("hana", _MEMBER_PASSWORD)}
    )
    assert response.status_code == 200
    assert response.json() == {"id": "hana", "role": "member"}


def test_each_dependency_is_bound_to_its_own_authenticator() -> None:
    def _basic_shun() -> dict[str, str]:
        return {"Authorization": _basic("shun", _ADMIN_PASSWORD)}

    own = _client(_valid_users_json())
    other = _client(_users_json([VALID_USERS[1]]))
    assert own.get("/api/whoami", headers=_basic_shun()).status_code == 200
    assert other.get("/api/whoami", headers=_basic_shun()).status_code == 401


def test_error_classes_are_runtime_errors() -> None:
    assert issubclass(UnauthenticatedError, RuntimeError)
    assert issubclass(AuthConfigError, RuntimeError)
