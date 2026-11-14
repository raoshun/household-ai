"""contract §4 problem JSON taxonomy の unit test (S1)。

ProblemEmitter.default() が §4 table の status/error_type/code を
例外クラスからそのまま導出すること、problem JSON body の keys
(§5: error_type/code/message/recoverable) と details keys
(§4 429 行: {retry_after_seconds, running_turn_ids}) を検証する。

spec quirk 実装判断 (レポート記載):
- §4 の pydantic 400 行は `unknown_error.invalid_request`、422 行は
  `RuleValidationError -> validation_error.rule_text` を正とする。
  行間で `validation_error.invalid_text` らしい言い回しが紛れ込むが
  §4 code に存在しないため実装しない (下記 test でマーク)。
- runtime の SessionNotFoundError (未知 session) は chat 側で
  no_active_turn へ翻訳するため 404 unknown_error.no_active_turn に写す。
- UnknownResolverError の approval_not_resolvable は §4 由来 code 不在の
  追加命名 (contract §5 interrupt lifecycle 由来の 409 相当) として追加。
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, ValidationError

from app.agent.provider import (
    AgentProviderError,
    AgentSessionError,
    ProviderError,
    ProviderUnavailableError,
)
from app.agent.runtime import AgentRuntimeError, SessionNotFoundError
from app.services.exceptions import (
    ApiProblem,
    ConcurrencyLimitError,
    ForkNotFoundError,
    InvalidRequestError,
    NoActiveTurnError,
    PermissionStaleError,
    ProblemDetail,
    ProblemEmitter,
    RuleNotFoundError,
    ThreadClosedError,
    TurnIdConflictError,
    TurnInProgressError,
    UnknownApprovalError,
    UnknownResolverError,
    UnknownThreadError,
    UnknownTurnError,
    install_problem_handlers,
)
from app.services.rules import RuleValidationError

PROBLEM_BODY_KEYS = {"error_type", "code", "message", "recoverable", "details"}

_MAP_CASES: list[tuple[BaseException, int, str, str, bool]] = [
    (
        InvalidRequestError("bad request"),
        400,
        "unknown_error",
        "unknown_error.invalid_request",
        False,
    ),
    (
        UnknownThreadError(thread_id="c:missing"),
        404,
        "unknown_error",
        "unknown_error.thread_not_found",
        False,
    ),
    (
        ForkNotFoundError(forked_from_thread_id="c:gone"),
        404,
        "unknown_error",
        "unknown_error.thread_not_found",
        False,
    ),
    (
        UnknownApprovalError(approval_id="ap-404"),
        404,
        "unknown_error",
        "unknown_error.approval_not_found",
        False,
    ),
    (
        RuleNotFoundError(rule_id="r-404"),
        404,
        "unknown_error",
        "unknown_error.rule_not_found",
        False,
    ),
    (NoActiveTurnError(), 404, "unknown_error", "unknown_error.no_active_turn", False),
    (
        UnknownTurnError(turn_id="c:t9"),
        404,
        "unknown_error",
        "unknown_error.no_active_turn",
        False,
    ),
    (
        SessionNotFoundError("sdk-1"),
        404,
        "unknown_error",
        "unknown_error.no_active_turn",
        False,
    ),
    (
        ThreadClosedError(thread_id="c:closed"),
        409,
        "unknown_error",
        "unknown_error.thread_closed",
        False,
    ),
    (
        UnknownResolverError("already resolved", approval_id="ap-1"),
        409,
        "unknown_error",
        "unknown_error.approval_not_resolvable",
        False,
    ),
    (
        TurnInProgressError(),
        409,
        "unknown_error",
        "unknown_error.turn_in_progress",
        False,
    ),
    (
        ConcurrencyLimitError(running_turn_ids=["c:t1"], retry_after_seconds=10.0),
        429,
        "unknown_error",
        "unknown_error.concurrency_limit",
        True,
    ),
    (
        TurnIdConflictError(turn_id="c:t1"),
        422,
        "unknown_error",
        "unknown_error.turn_id_conflict",
        False,
    ),
    (
        RuleValidationError("rule 本文が不正"),
        422,
        "validation_error",
        "validation_error.rule_text",
        False,
    ),
    (
        AgentSessionError("s-1", "ng"),
        502,
        "agent_error",
        "agent_error.provider_session_error",
        True,
    ),
    (
        AgentRuntimeError("runtime 起動失敗"),
        502,
        "agent_error",
        "agent_error.runtime_start_failed",
        True,
    ),
    (
        ProviderUnavailableError("provider 不在"),
        502,
        "agent_error",
        "agent_error.runtime_start_failed",
        True,
    ),
    (
        ProviderError("sdk 境界"),
        502,
        "agent_error",
        "agent_error.runtime_start_failed",
        True,
    ),
    (
        AgentProviderError("契約外"),
        502,
        "agent_error",
        "agent_error.runtime_start_failed",
        True,
    ),
    (PermissionStaleError(), 503, "agent_error", "agent_error.permission_stale", True),
]


def _emitter() -> ProblemEmitter:
    return ProblemEmitter.default()


@pytest.mark.parametrize(
    ("exc", "status_code", "error_type", "code", "recoverable"),
    _MAP_CASES,
)
def test_contract_s4_exception_mapping(
    exc: BaseException,
    status_code: int,
    error_type: str,
    code: str,
    recoverable: bool,
) -> None:
    """§4 table の status/error_type/code 組をそのまま導出できること。"""
    problem = _emitter().problem_for(exc)
    assert problem.status_code == status_code
    assert problem.error_type == error_type
    assert problem.code == code
    assert problem.recoverable == recoverable
    assert problem.code.startswith(f"{problem.error_type}.")
    assert problem.message


def test_problem_json_body_matches_contract_s5_keys() -> None:
    body = ApiProblem(
        status_code=409,
        error_type="unknown_error",
        code="unknown_error.turn_in_progress",
        message="session is busy",
        recoverable=False,
        details={"turn_id": "c:t9"},
    ).to_problem_detail().model_dump()
    assert set(body) == PROBLEM_BODY_KEYS
    assert body == {
        "error_type": "unknown_error",
        "code": "unknown_error.turn_in_progress",
        "message": "session is busy",
        "recoverable": False,
        "details": {"turn_id": "c:t9"},
    }


def test_api_problem_rejects_inconsistent_code_taxonomy() -> None:
    with pytest.raises(ValueError):
        ApiProblem(
            status_code=404,
            error_type="unknown_error",
            code="agent_error.thread_not_found",
            message="ng",
        )


def test_problem_detail_schema_is_pydantic_model() -> None:
    detail = ProblemDetail(
        error_type="validation_error",
        code="validation_error.rule_text",
        message="ng",
    )
    assert detail.recoverable is False
    assert detail.details == {}
    with pytest.raises(ValidationError):
        ProblemDetail(error_type="x")  # code/message 必須


def test_concurrency_limit_details_contract_shape() -> None:
    problem = _emitter().problem_for(
        ConcurrencyLimitError(
            "上限到達",
            running_turn_ids=("c:t1", "c:t2"),
            retry_after_seconds=10.0,
        )
    )
    assert problem.status_code == 429
    assert problem.code == "unknown_error.concurrency_limit"
    assert problem.recoverable is True
    assert problem.details == {
        "running_turn_ids": ["c:t1", "c:t2"],
        "retry_after_seconds": 10.0,
    }


def test_agent_session_error_keeps_session_id_detail() -> None:
    problem = _emitter().problem_for(AgentSessionError("sdk-9", "history RPC 失敗"))
    assert problem.status_code == 502
    assert problem.code == "agent_error.provider_session_error"
    assert problem.details == {"session_id": "sdk-9"}
    assert "sdk-9" in problem.message


def test_runtime_session_not_found_maps_to_no_active_turn() -> None:
    """実装判断: runtime の未知 session は chat 翻訳経路に合わせ 404 no_active_turn。"""
    problem = _emitter().problem_for(SessionNotFoundError("sdk-1"))
    assert problem.status_code == 404
    assert problem.code == "unknown_error.no_active_turn"


def test_unmapped_exception_falls_back_to_internal_error() -> None:
    problem = _emitter().problem_for(ValueError("unexpected"))
    assert problem.status_code == 500
    assert problem.code == "unknown_error.internal_error"
    assert problem.recoverable is True


def test_rule_validation_uses_rule_text_code_and_emits_no_invalid_text() -> None:
    """spec quirk マーク: §4 422 行 (validation_error.rule_text) を正とし、
    400 行括弧書きの validation_error.invalid_text は code 不在で実装しない。"""
    problem = _emitter().problem_for(RuleValidationError("本文が空"))
    assert problem.status_code == 422
    assert problem.code == "validation_error.rule_text"
    registered_codes = {rule.code for rule in _emitter().rules.values()}
    assert "validation_error.invalid_text" not in registered_codes


def test_problem_emitter_overwrite_guard() -> None:
    emitter = ProblemEmitter.default()
    from app.services.exceptions import ProblemRule

    with pytest.raises(ValueError):
        emitter.register(
            InvalidRequestError,
            ProblemRule(400, "unknown_error", "unknown_error.invalid_request"),
        )
    emitter.register(
        InvalidRequestError,
        ProblemRule(400, "unknown_error", "unknown_error.ng_registration"),
        overwrite=True,
    )
    problem = emitter.problem_for(InvalidRequestError())
    assert problem.code == "unknown_error.ng_registration"


class _TurnBody(BaseModel):
    text: str
    turn_id: str | None = None


def _build_app() -> FastAPI:
    app = FastAPI()
    install_problem_handlers(app)

    @app.post("/api/chat/sessions/{thread_id}/turns")
    def post_turn(thread_id: str, body: _TurnBody) -> dict[str, object]:
        if body.text == "busy":
            raise TurnInProgressError("session is busy", turn_id="c:busy")
        if body.text == "conflict":
            raise TurnIdConflictError(turn_id="c:t1")
        if body.text == "limit":
            raise ConcurrencyLimitError(
                running_turn_ids=["c:t1"],
                retry_after_seconds=10.0,
            )
        if body.text == "stale":
            raise PermissionStaleError()
        return {"turn_id": "c:t1", "started": True}

    @app.get("/api/chat/sessions/{thread_id}")
    def get_thread(thread_id: str) -> dict[str, object]:
        if thread_id == "closed":
            raise ThreadClosedError(thread_id=thread_id)
        raise UnknownThreadError(thread_id=thread_id)

    @app.post("/api/approvals/{approval_id}/resolve-pending")
    def resolve_pending(approval_id: str) -> dict[str, object]:
        raise UnknownResolverError("解決済み approval です", approval_id=approval_id)

    @app.get("/api/boom")
    def boom() -> dict[str, object]:
        raise RuntimeError("internal boom")

    return app


def _client() -> TestClient:
    return TestClient(_build_app(), raise_server_exceptions=False)


def _assert_problem_shape(response_body: object, status_code: int) -> dict[str, object]:
    assert isinstance(response_body, dict)
    assert set(response_body) == PROBLEM_BODY_KEYS
    assert isinstance(response_body["error_type"], str)
    assert isinstance(response_body["code"], str)
    assert isinstance(response_body["message"], str)
    assert isinstance(response_body["recoverable"], bool)
    assert isinstance(response_body["details"], dict)
    assert status_code in (400, 404, 409, 422, 429, 500, 502, 503)
    return response_body


def test_fastapi_post_turn_busy_returns_409_problem_json() -> None:
    client = _client()
    response = client.post("/api/chat/sessions/c:t/turns", json={"text": "busy"})
    assert response.status_code == 409
    body = _assert_problem_shape(response.json(), 409)
    assert body["error_type"] == "unknown_error"
    assert body["code"] == "unknown_error.turn_in_progress"
    assert body["details"] == {"turn_id": "c:busy"}


def test_fastapi_post_turn_conflict_returns_422_problem_json() -> None:
    client = _client()
    response = client.post("/api/chat/sessions/c:t/turns", json={"text": "conflict"})
    assert response.status_code == 422
    body = _assert_problem_shape(response.json(), 422)
    assert body["error_type"] == "unknown_error"
    assert body["code"] == "unknown_error.turn_id_conflict"
    assert body["details"] == {"turn_id": "c:t1"}


def test_fastapi_post_turn_concurrency_429_details_contract() -> None:
    client = _client()
    response = client.post("/api/chat/sessions/c:t/turns", json={"text": "limit"})
    assert response.status_code == 429
    body = _assert_problem_shape(response.json(), 429)
    assert body["code"] == "unknown_error.concurrency_limit"
    assert body["recoverable"] is True
    assert set(body["details"]) == {"running_turn_ids", "retry_after_seconds"}
    assert body["details"]["running_turn_ids"] == ["c:t1"]
    assert body["details"]["retry_after_seconds"] == 10.0


def test_fastapi_permission_stale_503() -> None:
    client = _client()
    response = client.post("/api/chat/sessions/c:t/turns", json={"text": "stale"})
    assert response.status_code == 503
    body = _assert_problem_shape(response.json(), 503)
    assert body["code"] == "agent_error.permission_stale"


def test_fastapi_unknown_thread_404_and_closed_409() -> None:
    client = _client()
    missing = client.get("/api/chat/sessions/c:nope")
    assert missing.status_code == 404
    body = _assert_problem_shape(missing.json(), 404)
    assert body["code"] == "unknown_error.thread_not_found"
    assert body["details"] == {"thread_id": "c:nope"}

    closed = client.get("/api/chat/sessions/closed")
    assert closed.status_code == 409
    closed_body = _assert_problem_shape(closed.json(), 409)
    assert closed_body["code"] == "unknown_error.thread_closed"
    assert closed_body["details"] == {"thread_id": "closed"}


def test_fastapi_resolver_rejected_409() -> None:
    client = _client()
    response = client.post("/api/approvals/ap-1/resolve-pending")
    assert response.status_code == 409
    body = _assert_problem_shape(response.json(), 409)
    assert body["code"] == "unknown_error.approval_not_resolvable"
    assert body["details"] == {"approval_id": "ap-1"}


def test_pydantic_invalid_request_maps_to_400_invalid_request_choice() -> None:
    """spec quirk マーク付き: §4 table の 400 行 (unknown_error.invalid_request) を
    pydantic validation 失敗へそのまま使う (validation_error.* を 400 には使わない)。"""
    client = _client()
    response = client.post("/api/chat/sessions/c:t/turns", json={"turn_id": "c:t1"})
    assert response.status_code == 400
    body = _assert_problem_shape(response.json(), 400)
    assert body["error_type"] == "unknown_error"
    assert body["code"] == "unknown_error.invalid_request"
    errors = body["details"]["errors"]
    assert isinstance(errors, list) and errors
    assert errors[0]["location"] == "body.text"
    assert errors[0]["type"] == "missing"


def test_fastapi_unhandled_error_500_internal_problem() -> None:
    client = _client()
    response = client.get("/api/boom")
    assert response.status_code == 500
    body = _assert_problem_shape(response.json(), 500)
    assert body["code"] == "unknown_error.internal_error"
    assert body["recoverable"] is True
