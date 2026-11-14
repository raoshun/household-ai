"""AgentRuntime.interrupt_session と busy 対応 (§5 interrupt / 409 行) の unit test。

失われた runtime adapter の再実装として以下を検証する:
- interrupt_session が provider.interrupt_session へ委譲する (§7.2)
- 未起動 / 未登録セッションの例外 (§7.2)
- provider の busy 拒否 -> AgentSessionBusyError -> ProblemEmitter で
  409 unknown_error.turn_in_progress (§5 409 turn_in_progress 行)
- busy 以外の session 失敗 -> 502 agent_error.provider_session_error
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from app.agent.events import AgentEvent, TurnResult
from app.agent.provider import (
    AgentSessionError,
    ProviderHealth,
    ProviderUnavailableError,
    SessionRequest,
    SessionSnapshot,
)
from app.agent.runtime import (
    AgentRuntime,
    AgentRuntimeError,
    AgentSessionBusyError,
    SessionNotFoundError,
)
from app.config import Settings
from app.services.exceptions import ProblemEmitter


class FakeSession:
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.interrupt_calls = 0
        self.abort_calls = 0
        self.interrupt_error: BaseException | None = None

    @property
    def model(self) -> str | None:
        return "gemma4:e2b"

    async def send(self, prompt: str) -> TurnResult:  # pragma: no cover
        raise AssertionError("unused in this test module")

    def stream(self, prompt: str) -> AsyncIterator[AgentEvent]:  # pragma: no cover
        raise AssertionError("unused in this test module")

    async def interrupt(self) -> None:
        self.interrupt_calls += 1
        if self.interrupt_error is not None:
            raise self.interrupt_error

    async def abort(self) -> None:
        self.abort_calls += 1

    async def aclose(self) -> None:
        return None

    def snapshot(self) -> SessionSnapshot:
        return SessionSnapshot(session_id=self.session_id, model="gemma4:e2b")


class FakeProvider:
    def __init__(
        self,
        *,
        busy_session: str | None = None,
        fail_session: str | None = None,
    ) -> None:
        self.sessions: dict[str, FakeSession] = {}
        self.interrupt_requests: list[str] = []
        self._busy_session = busy_session
        self._fail_session = fail_session
        self.started = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.started = False

    async def create_session(self, request: SessionRequest) -> FakeSession:
        session_id = request.session_id or "sess-1"
        session = FakeSession(session_id)
        self.sessions[session_id] = session
        return session

    async def close_session(self, session_id: str) -> None:
        await self.sessions.pop(session_id).aclose()

    async def interrupt_session(self, session_id: str) -> None:
        self.interrupt_requests.append(session_id)
        if session_id == self._busy_session:
            raise AgentSessionError(session_id, "session is busy (turn in progress)")
        if session_id == self._fail_session:
            raise AgentSessionError(session_id, "provider session died")
        await self.sessions[session_id].interrupt()

    def session_ids(self) -> list[str]:
        return list(self.sessions)

    async def list_models(self) -> list[str]:
        return ["gemma4:e2b"]

    async def health_check(self) -> ProviderHealth:
        return ProviderHealth(
            ready=self.started, provider="fake", sessions=len(self.sessions)
        )


def _settings() -> Settings:
    return Settings(provider="ollama", model="gemma4:e2b")


async def _started_runtime(
    **provider_kwargs: Any,
) -> tuple[AgentRuntime, FakeProvider, str]:
    provider = FakeProvider(**provider_kwargs)
    runtime = AgentRuntime(_settings(), provider=provider)
    await runtime.start()
    handle = await runtime.create_session()
    return runtime, provider, handle.session_id


# ---------------------------------------------------------------------------
# 委譲・前提条件
# ---------------------------------------------------------------------------


async def test_interrupt_session_delegates_to_provider() -> None:
    runtime, provider, session_id = await _started_runtime()
    await runtime.interrupt_session(session_id)
    assert provider.interrupt_requests == [session_id]
    assert provider.sessions[session_id].interrupt_calls == 1


async def test_interrupt_session_requires_started_runtime() -> None:
    runtime = AgentRuntime(_settings(), provider=FakeProvider())
    with pytest.raises(AgentRuntimeError):
        await runtime.interrupt_session("sess-1")


async def test_interrupt_session_unknown_session_is_session_not_found() -> None:
    runtime, _, _ = await _started_runtime()
    with pytest.raises(SessionNotFoundError):
        await runtime.interrupt_session("missing-session")


async def test_abort_session_reuses_interrupt_mapping() -> None:
    # abort (sync 相当の後方互換経路) も busy 変換を共有する (§7.2 interrupt)
    runtime, _, session_id = await _started_runtime()
    await runtime.abort_session(session_id)
    session = runtime._provider.sessions[session_id]  # type: ignore[attr-defined]
    assert session.abort_calls == 1


# ---------------------------------------------------------------------------
# busy / session 失敗の例外変換
# ---------------------------------------------------------------------------


async def test_provider_busy_raises_agent_session_busy_error() -> None:
    runtime, _, session_id = await _started_runtime(busy_session="sess-1")
    with pytest.raises(AgentSessionBusyError) as excinfo:
        await runtime.interrupt_session(session_id)
    assert isinstance(excinfo.value.__cause__, AgentSessionError)
    assert "busy" in str(excinfo.value)


async def test_agent_session_busy_error_maps_to_409_turn_in_progress() -> None:
    problem = ProblemEmitter.default().problem_for(
        AgentSessionBusyError("sess-1: busy")
    )
    assert problem.status_code == 409
    assert problem.error_type == "unknown_error"
    assert problem.code == "unknown_error.turn_in_progress"
    assert problem.recoverable is False


async def test_non_busy_session_failure_maps_to_502_provider_session_error() -> None:
    runtime, _, session_id = await _started_runtime(fail_session="sess-1")
    with pytest.raises(AgentSessionError) as excinfo:
        await runtime.interrupt_session(session_id)
    assert not isinstance(excinfo.value, AgentSessionBusyError)
    problem = ProblemEmitter.default().problem_for(excinfo.value)
    assert problem.status_code == 502
    assert problem.error_type == "agent_error"
    assert problem.code == "agent_error.provider_session_error"
    assert problem.recoverable is True


async def test_runtime_not_started_maps_to_502_runtime_start_failed() -> None:

    class Unavailable(FakeProvider):
        async def start(self) -> None:
            raise ProviderUnavailableError("runtime binary missing")

    runtime = AgentRuntime(_settings(), provider=Unavailable())
    with pytest.raises(ProviderUnavailableError):
        await runtime.start()
    problem = ProblemEmitter.default().problem_for(
        ProviderUnavailableError("runtime binary missing")
    )
    assert problem.status_code == 502
    assert problem.code == "agent_error.runtime_start_failed"
