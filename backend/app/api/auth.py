"""HTTP Basic 認証基盤 (契約 決定 1.27・2026-11-23 user 決定)。

- users = 環境変数 `API_USERS_JSON` = JSON 配列 `[{"id","password_sha256","role"}]`。
  password は SHA-256 lower hex で保持し `secrets.compare_digest` で比較する
  (§12.3 L1044-L1047 の趣旨に沿い平文・API key を Git 管理しない)。
- role = `admin` / `member`。Phase 1 では permission 分離に 未使用
  (§12.1 L1033 の admin/一般 案 の 将来 用。ここでは 妥当性 検証 の み)。
- users 未設定・空 list = fail-closed (app は 起動 しない → `AuthConfigError`)。
- 認証 失敗 = 401 + `WWW-Authenticate: Basic` + problem JSON
  (code = `unknown_error.unauthenticated`)。全 endpoint 適用 前提。
- 認証 成功 の id は created_by / updated_by / reviewed_by に 自動 設定 する
  (決定 1.11。router 側 は dependency の 戻り値 ApiUser.id を 使う)。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import secrets
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Final

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from app.config import Settings
from app.services.exceptions import ProblemDetail

#: RFC 7617 の realm 名 (README §10.3 の人間操作限定を満たすための識別子)。
AUTH_REALM: Final = "household-ai"

#: role は 決定 1.27 の 2 値 の み 受け付ける (将来 の 権限 分離 に 備え 厳密)。
API_USER_ROLES: Final[frozenset[str]] = frozenset({"admin", "member"})

#: 64 桁 lower hex を 高速 に 検査 する ため の 文字 集合。
_HEX_DIGITS: Final = frozenset("0123456789abcdef")

#: 不明 user へ の timing side channel を 慣らす 比較 用 疑似 digest。
_DUMMY_SHA256: Final = hashlib.sha256(b"household-ai-dummy-secret").hexdigest()


class AuthConfigError(RuntimeError):
    """`API_USERS_JSON` が fail-closed 条件 に かかった ことを 表す 起動 時 エラー。

    メッセージ は 起動 ログ に 残る ため、users 要素 の 値 (password_sha256 など) を
    反映 しない field 名・理由 だけ の 簡潔 な 文言 に 留める。
    """


class UnauthenticatedError(RuntimeError):
    """Basic 認証 の missing・不正・失敗 (401 problem JSON へ 変換 される)。"""


class ApiUser(BaseModel, frozen=True):
    """`API_USERS_JSON` の 1 要素 (id / password_sha256 / role)。"""

    model_config = ConfigDict(extra="forbid")

    id: str
    password_sha256: str
    role: str

    @field_validator("id")
    @classmethod
    def _id_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("id は 空 でない 文字 列 で ある ことが 必要 です")
        return value

    @field_validator("password_sha256")
    @classmethod
    def _hash_is_lower_hex64(cls, value: str) -> str:
        if len(value) != 64 or any(ch not in _HEX_DIGITS for ch in value):
            raise ValueError(
                "password_sha256 は 64 桁 の lower hex で ある ことが 必要 です"
            )
        return value

    @field_validator("role")
    @classmethod
    def _role_is_known(cls, value: str) -> str:
        if value not in API_USER_ROLES:
            raise ValueError(
                f"role は {sorted(API_USER_ROLES)} の いずれか で ある ことが 必要 です"
            )
        return value


def _format_validation_error(exc: ValidationError) -> str:
    """Pydantic 検証 失敗 を 「field 名: 理由」 だけ の 簡潔 文言 に 整形 する。

    `input_value` を 含む 標準 表現 は 起動 ログ に secret digest を
    漏らす ため 使わない。
    """

    parts = [
        f"{'.'.join(str(loc) for loc in err['loc']) or '(object 全体)'}: {err['msg']}"
        for err in exc.errors()
    ]
    return "; ".join(parts)


def sha256_hex(secret: str) -> str:
    """平文 secret の SHA-256 lower hex digest を 返す (env 登録 値 の 生成 用)。"""

    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def parse_api_users(raw_json: str) -> tuple[ApiUser, ...]:
    """`API_USERS_JSON` 文字 列 を 検証 済み ApiUser 列 へ 変換 する (決定 1.27)。

    未設定・空白・空 配列・JSON で ない・object で ない 要素・未 知 field・
    重複 id は すべて `AuthConfigError` と する (fail-closed: app を 起動 しない)。
    """

    if not raw_json.strip():
        raise AuthConfigError(
            "API_USERS_JSON が 未設定 です (fail-closed で 起動 不可)"
        )
    try:
        loaded: Any = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise AuthConfigError(f"API_USERS_JSON が JSON で ありません: {exc}") from exc
    if not isinstance(loaded, list):
        raise AuthConfigError("API_USERS_JSON は JSON 配列 で ありません")
    if not loaded:
        raise AuthConfigError("API_USERS_JSON の users が 空 list です (fail-closed)")
    users: list[ApiUser] = []
    seen: set[str] = set()
    for index, item in enumerate(loaded):
        if not isinstance(item, dict):
            raise AuthConfigError(f"users[{index}] が JSON object で ありません")
        try:
            user = ApiUser.model_validate(item)
        except ValidationError as exc:
            raise AuthConfigError(
                f"users[{index}] が 不正 です: {_format_validation_error(exc)}"
            ) from exc
        if user.id in seen:
            raise AuthConfigError(f"users[{index}].id が 重複 です: {user.id!r}")
        seen.add(user.id)
        users.append(user)
    return tuple(users)


@dataclass(frozen=True)
class Authenticator:
    """検証 済み users から Basic 認証 を 検査 する secret-compare 係わりの 所有者。"""

    users: tuple[ApiUser, ...]

    def user_ids(self) -> tuple[str, ...]:
        """登録 id 列 (startup 検査・ログ 用。secret digest は 露出 しない)。"""

        return tuple(user.id for user in self.users)

    def verify(self, username: str, secret: str) -> ApiUser | None:
        """Basic の username/secret を SHA-256 lower hex compare_digest で 検証 する。

        知られて いない username でも 1 回 compare_digest を 実行 し、
        応答 時間で user の 存在 が わから ない よう に する。
        """

        digest = sha256_hex(secret)
        user = next(
            (candidate for candidate in self.users if candidate.id == username), None
        )
        if user is None:
            secrets.compare_digest(digest, _DUMMY_SHA256)
            return None
        if not secrets.compare_digest(digest, user.password_sha256):
            return None
        return user


def build_authenticator(settings: Settings) -> Authenticator:
    """Settings から fail-closed で Authenticator を 生成 する (起動時 検査 用)。"""

    return Authenticator(users=parse_api_users(settings.api_users_json))


def parse_basic_credentials(header_value: str | None) -> tuple[str, str] | None:
    """`Authorization` ヘッダ から Basic の (username, secret) を 抽出 する (RFC 7617)。

    scheme は 大文字・小文字 を 問わない。base64 復号・utf-8・`user:pass` を
    最初 の `:` 1 つ だけ で の 分割 に 失敗 する 場合 は None を 返す。
    """

    if header_value is None:
        return None
    parts = header_value.split()
    if len(parts) != 2 or parts[0].lower() != "basic":
        return None
    try:
        decoded = base64.b64decode(parts[1], validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    username, sep, secret = decoded.partition(":")
    if not sep:
        return None
    return username, secret


def unauthenticated_problem_response() -> JSONResponse:
    """決定 1.27 準拠 の 401 応答 (problem JSON + `WWW-Authenticate: Basic`)。"""

    body = ProblemDetail(
        error_type="unknown_error",
        code="unknown_error.unauthenticated",
        message="認証 が 必要 です",
        recoverable=False,
    )
    return JSONResponse(
        status_code=401,
        content=body.model_dump(),
        headers={"WWW-Authenticate": f'Basic realm="{AUTH_REALM}"'},
    )


def install_unauthenticated_handler(app: FastAPI) -> None:
    """UnauthenticatedError → 401 problem 応答 の handler を app に 登録 する。

    Phase 0 の ProblemEmitter の rule は per-rule headers を 持て ない ため、
    WWW-Authenticate を 付与 する 専用 handler を ここ で 登録 する (決定 1.31)。
    """

    async def _unauthenticated_exception_handler(
        request: Request, exc: UnauthenticatedError
    ) -> JSONResponse:
        del request, exc
        return unauthenticated_problem_response()

    app.add_exception_handler(UnauthenticatedError, _unauthenticated_exception_handler)


def create_auth_dependency(
    authenticator: Authenticator,
) -> Callable[[Request], ApiUser]:
    """全 endpoint 共通 の 「認証 された 人間」 dependency を 生成 する (決定 1.27)。

    router 側 は 生成 済み dependency を 共通 の `dependencies=`・`Depends()` で
    使い、戻り値 ApiUser.id を created_by / updated_by / reviewed_by に 渡す。
    """

    def require_authenticated_human(request: Request) -> ApiUser:
        parsed = parse_basic_credentials(request.headers.get("Authorization"))
        if parsed is None:
            raise UnauthenticatedError("Basic 認証 ヘッダ が ありません")
        user = authenticator.verify(parsed[0], parsed[1])
        if user is None:
            raise UnauthenticatedError("Basic 認証 に 失敗 しました")
        return user

    return require_authenticated_human


def user_roles(users: Iterable[ApiUser]) -> dict[str, str]:
    """id → role 対応 (startup 監査 用。role は Phase 1 では permission に 未 使用)。"""

    return {user.id: user.role for user in users}
