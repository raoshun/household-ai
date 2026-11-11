"""AgentRuntime の起動状態とセッション登録の振る舞いテスト (§7.2).

provider は `AgentProvider` 契約を満たす fake へ差し替える。SDK 由来の型は
`app.agent.provider` の中に閉じ込めているので、このテストは SDK を起こさない。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from app.agent.events import AgentEvent, AgentEventType, TurnResult
from app.agent.provider import (
    AgentProviderError,
    AgentSessionError,
    ProviderHealth,
    ProviderUnavailableError,
    SessionRequest,
    SessionSnapshot,
)
from app.agent.runtime import AgentRuntime, AgentRuntimeError, SessionNotFoundError
from app.config import Settings


class FakeSession:
    """`AgentSessionHandle` 契約を満たす検証 object。"""

    def __init__(self, session_id: str, request: SessionRequest) -> None:
        self._session_id = session_id
        self.request = request
        self.closed_calls = 0
        self.abort_calls = 0
        self.sent: list[str] = []

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def model(self) -> str | None:
        return self.request.model

    async def send(self, prompt: str) -> TurnResult:
        self.sent.append(prompt)
        events = [
            AgentEvent(type=AgentEventType.DELTA, text=prompt.lower()),
            AgentEvent(type=AgentEventType.SESSION_IDLE),
        ]
        return TurnResult(
            text=prompt.lower(),
            aborted=False,
            tool_calls=[],
            events=events,
        )

    async def stream(self, prompt: str) -> AsyncIterator[AgentEvent]:
        self.sent.append(prompt)
        yield AgentEvent(type=AgentEventType.DELTA, text="part-1")
        yield AgentEvent(type=AgentEventType.DELTA, text="part-2")
        yield AgentEvent(type=AgentEventType.SESSION_IDLE)

    async def interrupt(self) -> None:
        self.abort_calls += 1

    async def abort(self) -> None:
        await self.interrupt()

    def close(self) -> None:
        self.closed_calls += 1

    async def aclose(self) -> None:
        self.close()

    def snapshot(self) -> SessionSnapshot:
        return SessionSnapshot(
            session_id=self._session_id,
            model=self.request.model,
            tool_names=[],
            skills=[],
        )


class FakeProvider:
    """`AgentProvider` 契約を満たす検証 object。"""

    def __init__(self, *, models: tuple[str, ...] = ("gemma4:e2b",)) -> None:
        self.models = list(models)
        self.start_calls = 0
        self.stop_calls = 0
        self.created: list[SessionRequest] = []
        self.sessions: dict[str, FakeSession] = {}
        self.closed: list[FakeSession] = []
        self.close_error: Exception | None = None

    async def start(self) -> None:
        self.start_calls += 1

    async def stop(self) -> None:
        self.stop_calls += 1

    async def create_session(self, request: SessionRequest) -> FakeSession:
        if not self.start_calls:
            raise AgentProviderError("start() されていない provider")
        self.created.append(request)
        session = FakeSession(f"session-{len(self.created)}", request)
        self.sessions[session.session_id] = session
        return session

    async def close_session(self, session_id: str) -> None:
        if session_id not in self.sessions:
            raise AgentSessionError(session_id, "登録されていないセッション")
        session = self.sessions.pop(session_id)
        session.close()
        self.closed.append(session)
        if self.close_error is not None:
            raise self.close_error

    async def interrupt_session(self, session_id: str) -> None:
        self.sessions[session_id].interrupt()

    def session_ids(self) -> list[str]:
        return list(self.sessions)

    async def list_models(self) -> list[str]:
        return list(self.models)

    async def health_check(self) -> ProviderHealth:
        return ProviderHealth(
            ready=bool(self.start_calls),
            model="gemma4:e2b",
            sessions=len(self.sessions),
            detail=None,
        )


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {"provider": "ollama", "model": "gemma4:e2b"}
    values.update(overrides)
    return Settings(**values)


def _make_runtime(
    **provider_kwargs: Any,
) -> tuple[AgentRuntime, FakeProvider]:
    provider = FakeProvider(**provider_kwargs)
    return AgentRuntime(_settings(), provider=provider), provider


# ---------------------------------------------------------------------------
# ライフサイクル
# ---------------------------------------------------------------------------
async def test_start_and_stop_are_idempotent() -> None:
    runtime, provider = _make_runtime()
    await runtime.start()
    await runtime.start()
    await runtime.stop()
    await runtime.stop()
    assert provider.start_calls == 1
    assert provider.stop_calls == 1
    assert runtime.started is False


async def test_start_propagates_provider_unavailable() -> None:
    class Unavailable(FakeProvider):
        async def start(self) -> None:
            raise ProviderUnavailableError("Copilot ランタイムを起動できません")

    runtime = AgentRuntime(_settings(), provider=Unavailable())
    with pytest.raises(ProviderUnavailableError):
        await runtime.start()
    assert runtime.started is False


async def test_async_context_manager_stops_runtime() -> None:
    runtime, provider = _make_runtime()
    async with runtime as started:
        assert started is runtime
        assert runtime.started is True
    assert runtime.started is False
    assert provider.stop_calls == 1


# ---------------------------------------------------------------------------
# セッション登録
# ---------------------------------------------------------------------------
async def test_create_session_requires_start() -> None:
    runtime, _ = _make_runtime()
    with pytest.raises(AgentRuntimeError, match="start"):
        await runtime.create_session()


async def test_create_session_registers_handle() -> None:
    runtime, provider = _make_runtime()
    await runtime.start()
    handle = await runtime.create_session(
        model="qwen3:8b", system_message="家族向けの指示"
    )
    assert runtime.session_ids == [handle.session_id]
    request = provider.created[0]
    assert request.model == "qwen3:8b"
    assert request.system_message == "家族向けの指示"
    assert handle.snapshot().session_id == handle.session_id


async def test_create_session_maps_session_error() -> None:
    class Failing(FakeProvider):
        async def create_session(self, request: SessionRequest) -> Any:
            raise AgentSessionError("x", "モデルが見つからない")

    runtime = AgentRuntime(_settings(), provider=Failing())
    await runtime.start()
    with pytest.raises(AgentRuntimeError, match="モデルが見つからない"):
        await runtime.create_session()


async def test_close_session_detaches_only_once() -> None:
    runtime, provider = _make_runtime()
    await runtime.start()
    handle = await runtime.create_session()
    await runtime.close_session(handle.session_id)
    assert runtime.session_ids == []
    assert [session.session_id for session in provider.closed] == [handle.session_id]
    assert provider.closed[0].closed_calls == 1
    with pytest.raises(SessionNotFoundError):
        await runtime.close_session(handle.session_id)


async def test_stop_closes_open_sessions() -> None:
    runtime, provider = _make_runtime()
    await runtime.start()
    first = await runtime.create_session()
    second = await runtime.create_session()
    await runtime.close_session(first.session_id)
    provider.close_error = RuntimeError("切断に失敗")
    await runtime.stop()
    # close に失敗しても他の終了処理は止めない
    assert runtime.session_ids == []
    assert provider.stop_calls == 1
    assert runtime.started is False
    closed_ids = [session.session_id for session in provider.closed]
    assert second.session_id in closed_ids  # stop が残ったセッションを閉じている


async def test_close_unknown_session_raises() -> None:
    runtime, _ = _make_runtime()
    await runtime.start()
    with pytest.raises(SessionNotFoundError):
        await runtime.close_session("nope")


# ---------------------------------------------------------------------------
# メッセージ送信・ストリーミング・中断
# ---------------------------------------------------------------------------
async def test_send_message_returns_turn_result() -> None:
    runtime, _ = _make_runtime()
    await runtime.start()
    handle = await runtime.create_session()
    result = await runtime.send_message(handle.session_id, "写真の一覧は？")
    assert result.text == "写真の一覧は？"
    assert result.aborted is False
    assert [event.type for event in result.events] == [
        AgentEventType.DELTA,
        AgentEventType.SESSION_IDLE,
    ]


async def test_stream_message_yields_events() -> None:
    runtime, _ = _make_runtime()
    await runtime.start()
    handle = await runtime.create_session()
    events = [event async for event in runtime.stream_message(handle.session_id, "go")]
    assert [event.text for event in events[:-1]] == ["part-1", "part-2"]
    assert events[-1].type is AgentEventType.SESSION_IDLE


async def test_send_message_requires_started_session() -> None:
    runtime, _ = _make_runtime()
    with pytest.raises(AgentRuntimeError, match="start"):
        await runtime.send_message("session-1", "go")


async def test_abort_session_interrupts_handle() -> None:
    runtime, provider = _make_runtime()
    await runtime.start()
    handle = await runtime.create_session()
    await runtime.abort_session(handle.session_id)
    assert provider.sessions[handle.session_id].abort_calls == 1
    with pytest.raises(SessionNotFoundError):
        await runtime.abort_session("nope")


async def test_operations_on_closed_session_raise() -> None:
    runtime, _ = _make_runtime()
    await runtime.start()
    handle = await runtime.create_session()
    await runtime.close_session(handle.session_id)
    for coro in (
        runtime.send_message(handle.session_id, "go"),
        runtime.abort_session(handle.session_id),
    ):
        with pytest.raises(SessionNotFoundError):
            await coro
    with pytest.raises(SessionNotFoundError):
        runtime.snapshot(handle.session_id)


# ---------------------------------------------------------------------------
# 疎通確認
# ---------------------------------------------------------------------------
async def test_health_check_reflects_state() -> None:
    runtime, _ = _make_runtime()
    before = await runtime.health_check()
    assert (before.ready, before.sessions, before.model) == (False, 0, "gemma4:e2b")
    await runtime.start()
    handle = await runtime.create_session()
    after = await runtime.health_check()
    assert (after.ready, after.sessions) == (True, 1)
    await runtime.close_session(handle.session_id)
    assert (await runtime.health_check()).sessions == 0


async def test_list_models_requires_start() -> None:
    runtime, provider = _make_runtime(models=("gemma4:e2b", "qwen3:8b"))
    with pytest.raises(AgentRuntimeError, match="start"):
        await runtime.list_models()
    await runtime.start()
    assert await runtime.list_models() == ["gemma4:e2b", "qwen3:8b"]
    assert provider.start_calls == 1
