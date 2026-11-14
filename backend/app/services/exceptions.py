"""API problem JSON レイヤー (contract §3.5 / §4 / §5).

chat service / agent runtime 系の例外を problem JSON body
`{error_type, code, message, recoverable, details}` へ変換する。
code taxonomy は contract §4 の table 行に従う:

- `unknown_error.*` … リクエスト状態起因の 4xx (400/404/409/422/429)
- `agent_error.*` … provider / broker 側の 5xx (502/503)
- `validation_error.rule_text` … rule 本文の 422 validation

spec quirk (contract §4 検証済みのまま・実装判断をレポート記載): §4 の 400 行は
括弧書きで pydantic validation を `unknown_error.invalid_request` へ写し、別行の
422 行は `RuleValidationError -> validation_error.rule_text` を定める。行間で
`validation_error.invalid_text` らしき言い回しが紛れ込むため、§4 table の code を
文字列そのまま正とし `validation_error.invalid_text` は実装しない
(マーク付きテスト: tests/unit/test_ui_bridge_exceptions.py)。

また §3 terminal RUN_ERROR code (`max_frames_exceeded` / `permission_stale` /
`provider_session_error` / `turn_timeout`) は AG-UI event 系列側の値であり、
本モジュールの HTTP problem code (`agent_error.permission_stale` 等) とは別系列。
混同しないよう problem_for() では HTTP 系列のみ扱う。

本モジュールは chat.py を import しない (例外の依存方向は一方向: chat -> exceptions)。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.agent.provider import (
    AgentProviderError,
    AgentSessionError,
    ProviderError,
    ProviderUnavailableError,
)
from app.agent.runtime import AgentRuntimeError, SessionNotFoundError
from app.services.rules import RuleValidationError

__all__ = [
    "ApiProblem",
    "ChatServiceError",
    "ConcurrencyLimitError",
    "ForkNotFoundError",
    "InvalidRequestError",
    "NoActiveTurnError",
    "PermissionStaleError",
    "ProblemDetail",
    "ProblemEmitter",
    "ProblemRule",
    "RuleNotFoundError",
    "ThreadClosedError",
    "TurnIdConflictError",
    "TurnInProgressError",
    "UnknownApprovalError",
    "UnknownResolverError",
    "UnknownThreadError",
    "UnknownTurnError",
]


# ---------------------------------------------------------------------------
# problem JSON body (§5: error_type / code / message / recoverable / details)
# ---------------------------------------------------------------------------


class ProblemDetail(BaseModel):
    """problem JSON body の schema (§5 contract)。key 順を固定しない。"""

    error_type: str
    code: str
    message: str
    recoverable: bool = False
    details: dict[str, Any] = Field(default_factory=dict)


@dataclass(slots=True)
class ApiProblem:
    """HTTP 層で problem JSON へ整形される API problem (raise 可能)。"""

    status_code: int
    error_type: str
    code: str
    message: str
    recoverable: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # taxonomy: code prefix 必須 (unknown_error.x -> unknown_error)
        if not self.code.startswith(f"{self.error_type}."):
            raise ValueError(
                f"code {self.code!r} must start with {self.error_type!r} prefix"
            )

    def to_problem_detail(self) -> ProblemDetail:
        return ProblemDetail(
            error_type=self.error_type,
            code=self.code,
            message=self.message,
            recoverable=self.recoverable,
            details=dict(self.details),
        )

    def to_response(self) -> JSONResponse:
        return JSONResponse(
            status_code=self.status_code,
            content=self.to_problem_detail().model_dump(),
        )


# ---------------------------------------------------------------------------
# chat service / turn / permission 系の例外階層 (chat.py から移設)
# ---------------------------------------------------------------------------


class ChatServiceError(RuntimeError):
    """chat service 系の base error（HTTP layer で problem JSON code へ変換する）."""


class InvalidRequestError(ChatServiceError):
    """リクエストbody/パラメータが不正 (400 unknown_error.invalid_request)。"""


def _not_found(target: str, target_id: str | None) -> str:
    base = f"{target} が見つかりません"
    return f"{base}: {target_id}" if target_id else base


def _closed(target: str, target_id: str | None) -> str:
    base = f"{target} は既に閉じられています"
    return f"{base}: {target_id}" if target_id else base


class UnknownThreadError(ChatServiceError):
    """thread_id が不明 (404 unknown_error.thread_not_found)。"""

    def __init__(
        self,
        message: str | None = None,
        *,
        thread_id: str | None = None,
    ) -> None:
        if message is None:
            message = _not_found("thread", thread_id)
        super().__init__(message)
        self.thread_id = thread_id


class ForkNotFoundError(ChatServiceError):
    """fork 元 thread が存在しない (§4 では thread_not_found として扱う)。"""

    def __init__(
        self,
        message: str | None = None,
        *,
        forked_from_thread_id: str | None = None,
    ) -> None:
        if message is None:
            message = _not_found("fork 元の thread", forked_from_thread_id)
        super().__init__(message)
        self.forked_from_thread_id = forked_from_thread_id


class ThreadClosedError(ChatServiceError):
    """thread が閉じられている (409 unknown_error.thread_closed)。"""

    def __init__(
        self,
        message: str | None = None,
        *,
        thread_id: str | None = None,
    ) -> None:
        if message is None:
            message = _closed("thread", thread_id)
        super().__init__(message)
        self.thread_id = thread_id


class UnknownApprovalError(ChatServiceError):
    """approval_id が不明 (404 unknown_error.approval_not_found)。"""

    def __init__(
        self,
        message: str | None = None,
        *,
        approval_id: str | None = None,
    ) -> None:
        if message is None:
            message = _not_found("approval", approval_id)
        super().__init__(message)
        self.approval_id = approval_id


class UnknownTurnError(ChatServiceError):
    """thread に存在しない turn id を指定した (§4: no_active_turn として扱う)。"""

    def __init__(
        self,
        message: str | None = None,
        *,
        turn_id: str | None = None,
    ) -> None:
        if message is None:
            message = _not_found("turn", turn_id)
        super().__init__(message)
        self.turn_id = turn_id


class NoActiveTurnError(ChatServiceError):
    """thread に解決対象の active turn / interrupt が存在しない。"""

    def __init__(self, message: str = "解決対象の interrupt が存在しません") -> None:
        super().__init__(message)


class UnknownResolverError(ChatServiceError):
    """resolver し得ない approval (解決済み/取り消し済み) を解決しようとした。"""

    def __init__(self, message: str, *, approval_id: str | None = None) -> None:
        super().__init__(message)
        self.approval_id = approval_id


class RuleNotFoundError(ChatServiceError):
    """rule_id が不明 (404 unknown_error.rule_not_found)。"""

    def __init__(
        self,
        message: str | None = None,
        *,
        rule_id: str | None = None,
    ) -> None:
        if message is None:
            message = _not_found("rule", rule_id)
        super().__init__(message)
        self.rule_id = rule_id


class TurnInProgressError(ChatServiceError):
    """thread / session が busy で turn を開始できない (409 turn_in_progress)。"""

    def __init__(
        self,
        message: str | None = None,
        *,
        turn_id: str | None = None,
    ) -> None:
        super().__init__(message if message is not None else "session is busy")
        self.turn_id = turn_id


class TurnIdConflictError(ChatServiceError):
    """client 指定 turn_id が既存/canonical 規則と衝突 (422 turn_id_conflict)。"""

    def __init__(
        self,
        message: str | None = None,
        *,
        turn_id: str | None = None,
    ) -> None:
        if message is None:
            if turn_id:
                message = f"turn_id が衝突します: {turn_id}"
            else:
                message = "turn_id が衝突します"
        super().__init__(message)
        self.turn_id = turn_id


class ConcurrencyLimitError(ChatServiceError):
    """同時実行 turn 数が max_concurrent_turns に到達 (details は §4 の keys)."""

    running_turn_ids: list[str]
    retry_after_seconds: float

    def __init__(
        self,
        message: str | None = None,
        *,
        running_turn_ids: Iterable[str] = (),
        retry_after_seconds: float = 1.0,
    ) -> None:
        ids = list(running_turn_ids)
        if message is None:
            message = f"同時実行 turn 数が上限 {len(ids)} に到達しました"
        super().__init__(message)
        self.running_turn_ids = ids
        self.retry_after_seconds = float(retry_after_seconds)


class PermissionStaleError(ChatServiceError):
    """stale 化で grant/承認が解決不能 (503 agent_error.permission_stale)。"""

    def __init__(
        self,
        message: str | None = None,
        *,
        approval_ids: Iterable[str] = (),
    ) -> None:
        if message is None:
            message = "permission grant が stale で解決できません"
        super().__init__(message)
        self.approval_ids = list(approval_ids)


# ---------------------------------------------------------------------------
# details 抽出 helper (§4 details keys: {retry_after_seconds, running_turn_ids} 等)
# ---------------------------------------------------------------------------

_DETAIL_ATTRS: tuple[str, ...] = (
    "thread_id",
    "approval_id",
    "turn_id",
    "rule_id",
    "forked_from_thread_id",
    "running_turn_ids",
    "retry_after_seconds",
)


def _attrs(exc: BaseException) -> dict[str, Any]:
    details: dict[str, Any] = {}
    for name in _DETAIL_ATTRS:
        value = getattr(exc, name, None)
        if value is None or value == "":
            continue
        if isinstance(value, (str, bytes, bool, int, float)):
            details[name] = value
        elif isinstance(value, Iterable):
            details[name] = list(value)
    return details


def _request_validation_details(exc: RequestValidationError) -> dict[str, Any]:
    errors: list[dict[str, Any]] = []
    for err in exc.errors()[:20]:
        errors.append(
            {
                "location": ".".join(str(part) for part in err.get("loc", ())),
                "message": str(err.get("msg", "")),
                "type": str(err.get("type", "")),
            }
        )
    return {"errors": errors}


# ---------------------------------------------------------------------------
# ProblemEmitter (§3.5: exception -> ApiProblem 変換 + FastAPI exception handler)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProblemRule:
    status_code: int
    error_type: str
    code: str
    recoverable: bool = False
    message_of: Callable[[BaseException], str] = str
    details_of: Callable[[BaseException], dict[str, Any]] | None = None

    def problem_for(self, exc: BaseException) -> ApiProblem:
        details = self.details_of(exc) if self.details_of is not None else _attrs(exc)
        return ApiProblem(
            status_code=self.status_code,
            error_type=self.error_type,
            code=self.code,
            message=self.message_of(exc) or str(exc) or self.code,
            recoverable=self.recoverable,
            details=details,
        )


logger = logging.getLogger("household-ai.services.exceptions")


@dataclass(slots=True)
class ProblemEmitter:
    """exception -> problem JSON 変換器 (§4 の mapping table を内蔵)。"""

    rules: dict[type[BaseException], ProblemRule] = field(default_factory=dict)
    fallback: ProblemRule = ProblemRule(
        status_code=500,
        error_type="unknown_error",
        code="unknown_error.internal_error",
        recoverable=True,
    )

    def register(
        self,
        exc_type: type[BaseException],
        rule: ProblemRule,
        *,
        overwrite: bool = False,
    ) -> None:
        if not overwrite and exc_type in self.rules:
            raise ValueError(
                f"problem rule for {exc_type.__name__} is already registered"
            )
        self.rules[exc_type] = rule

    def problem_for(self, exc: BaseException) -> ApiProblem:
        """MRO を辿って最も具体的な登録 rule で problem を作る (§4 rule 準拠)。"""
        for exc_class in type(exc).__mro__:
            if exc_class is object:
                continue
            rule = self.rules.get(exc_class)
            if rule is not None:
                return rule.problem_for(exc)
        return self.fallback.problem_for(exc)

    # -- FastAPI wiring -----------------------------------------------------

    def install(self, app: FastAPI) -> None:
        """§3.5 の exception handler を app へ登録する (ProblemEmitter 実装の一部)。"""
        for exc_type, rule in self.rules.items():
            self._add_handler(app, exc_type, rule)
        request_validation_rule = ProblemRule(
            status_code=400,
            error_type="unknown_error",
            code="unknown_error.invalid_request",
            message_of=lambda exc: "リクエストが不正です",
            details_of=lambda exc: _request_validation_details(exc),
        )
        self._add_handler(app, RequestValidationError, request_validation_rule)
        self._add_handler(app, Exception, self.fallback)

    def _add_handler(
        self,
        app: FastAPI,
        exc_type: type[BaseException],
        rule: ProblemRule,
    ) -> None:
        def _handler(request: Request, exc: BaseException) -> JSONResponse:
            problem = rule.problem_for(exc)
            logger_request = getattr(request, "state", None)
            if logger_request is not None:
                logger_request.problem_code = problem.code
            return problem.to_response()

        app.add_exception_handler(exc_type, _handler)

    # -- §4 mapping table ----------------------------------------------------

    @classmethod
    def default(cls) -> ProblemEmitter:
        """contract §4 の status/code taxonomy をそのまま内蔵した emitter を作る。

        recoverable 既定値 (実装判断・レポート記載): 4xx は false。
        429 concurrency_limit と 502/503 の agent_error.* は true
        (§0 #7 の RUN_ERROR recoverable 記述は AG-UI frame 側の話であり、
        frame の recoverable は chat 側で明示値を使う)。
        """
        emitter = cls()

        def reg(exc_type: type[BaseException], rule: ProblemRule) -> None:
            emitter.register(exc_type, rule)

        reg(
            InvalidRequestError,
            ProblemRule(400, "unknown_error", "unknown_error.invalid_request"),
        )
        reg(
            UnknownThreadError,
            ProblemRule(404, "unknown_error", "unknown_error.thread_not_found"),
        )
        reg(
            ForkNotFoundError,
            ProblemRule(404, "unknown_error", "unknown_error.thread_not_found"),
        )
        # runtime の unknown session -> no_active_turn 扱い (chat 側 translation)
        reg(
            SessionNotFoundError,
            ProblemRule(404, "unknown_error", "unknown_error.no_active_turn"),
        )
        reg(
            UnknownApprovalError,
            ProblemRule(404, "unknown_error", "unknown_error.approval_not_found"),
        )
        reg(
            RuleNotFoundError,
            ProblemRule(404, "unknown_error", "unknown_error.rule_not_found"),
        )
        reg(
            NoActiveTurnError,
            ProblemRule(404, "unknown_error", "unknown_error.no_active_turn"),
        )
        reg(
            UnknownTurnError,
            ProblemRule(404, "unknown_error", "unknown_error.no_active_turn"),
        )
        reg(
            ThreadClosedError,
            ProblemRule(409, "unknown_error", "unknown_error.thread_closed"),
        )
        reg(
            UnknownResolverError,
            ProblemRule(409, "unknown_error", "unknown_error.approval_not_resolvable"),
        )
        reg(
            TurnInProgressError,
            ProblemRule(409, "unknown_error", "unknown_error.turn_in_progress"),
        )
        reg(
            ConcurrencyLimitError,
            ProblemRule(
                429,
                "unknown_error",
                "unknown_error.concurrency_limit",
                recoverable=True,
            ),
        )
        reg(
            TurnIdConflictError,
            ProblemRule(422, "unknown_error", "unknown_error.turn_id_conflict"),
        )
        # §4 table 行: RuleValidationError => 422 validation_error.rule_text
        # invalid_text is intentionally absent from §4 codes (spec 400-row note only)
        reg(
            RuleValidationError,
            ProblemRule(422, "validation_error", "validation_error.rule_text"),
        )
        reg(
            AgentSessionError,
            ProblemRule(
                502,
                "agent_error",
                "agent_error.provider_session_error",
                recoverable=True,
                details_of=lambda exc: {
                    "session_id": getattr(exc, "session_id", "unknown")
                },
            ),
        )
        # provider/runtime の開始失敗・実行不能系 (§4 502 行の 2 code)
        for exc_type in (
            AgentRuntimeError,
            ProviderUnavailableError,
            ProviderError,
            AgentProviderError,
        ):
            reg(
                exc_type,
                ProblemRule(
                    502,
                    "agent_error",
                    "agent_error.runtime_start_failed",
                    recoverable=True,
                ),
            )
        reg(
            PermissionStaleError,
            ProblemRule(
                503,
                "agent_error",
                "agent_error.permission_stale",
                recoverable=True,
            ),
        )
        return emitter


def install_problem_handlers(
    app: FastAPI,
    emitter: ProblemEmitter | None = None,
) -> ProblemEmitter:
    """§3.5 contract: ProblemEmitter.default() exception handlers を app へ登録する。"""

    active = emitter if emitter is not None else ProblemEmitter.default()
    active.install(app)
    return active


def default_problem_emitter() -> ProblemEmitter:
    """main.py / tests から使う §4 内蔵 emitter の factory。"""

    return ProblemEmitter.default()
