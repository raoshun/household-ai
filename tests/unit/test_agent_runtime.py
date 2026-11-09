"""AgentRuntime の起動状態とセッション登録の振る舞いテスト (§7.2).

実際の Copilot SDK セッションは使わず、`AgentProvider` を検証用の偽物へ差し替えて
起動・セッション管理の契約だけを検証する。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from app.agent import AgentRuntime, AgentRuntimeError
from app.agent.events import AgentEvent
from app.agent.provider import AgentSession
from app.config import Settings


class _NullSdkSession:
    """SDK セッションの代わりにハンドルへ入れる空のオブジェクト。"""


class FakeProvider:
    """`AgentProvider` Protocol を満たす検証用 provider。"""

    def __init__(self) -> None:
        self.start_calls = 0
        self.stop_calls = 0
        self.detach_calls = 0
        self.created: list[AgentSession] = []
        self.kwargs: list[dict[str, Any]] = []

    async def start(self) -> None:
        self.start_calls += 1

    async def stop(self) -> None:
        # provider も同じハンドルを閉じる (close は冪等)
        for handle in self.created:
            handle.close()
        self.stop_calls += 1

    async def create_session(
        self,
        *,
        on_event: Callable[[AgentEvent], None],
        tools: list[Any] | None = None,
        custom_tool_names: list[str] | None = None,
        system_message: str | None = None,
    ) -> AgentSession:
        def detach() -> None:
            self.detach_calls += 1

        handle = AgentSession(
            session_id=f"fake-{len(self.created) + 1}",
            _session=_NullSdkSession(),
            _detach=detach,
        )
        self.kwargs.append(
            {"on_event": on_event, "tools": tools, "system_message": system_message}
        )
        self.created.append(handle)
        return handle

    async def list_models(self) -> list[str]:
        return ["gemma4:e2b"]


def _make_runtime() -> tuple[AgentRuntime, FakeProvider]:
    provider = FakeProvider()
    runtime = AgentRuntime(Settings(model="gemma4:e2b"), provider)
    return runtime, provider


def _noop_handler(event: AgentEvent) -> None:
    return None


async def test_start_and_stop_are_idempotent() -> None:
    runtime, provider = _make_runtime()
    assert runtime.started is False

    await runtime.start()
    await runtime.start()
    assert provider.start_calls == 1

    await runtime.stop()
    await runtime.stop()
    assert runtime.started is False
    assert provider.stop_calls == 1


async def test_create_session_requires_start() -> None:
    runtime, provider = _make_runtime()
    with pytest.raises(AgentRuntimeError):
        await runtime.create_session(on_event=_noop_handler)
    assert provider.created == []


async def test_create_session_registers_handle() -> None:
    runtime, provider = _make_runtime()
    await runtime.start()
    events: list[AgentEvent] = []

    handle = await runtime.create_session(
        on_event=events.append,
        tools=[{"name": "search_rules"}],
        system_message="家庭内ルールの判断支援を行う",
    )

    assert runtime.session_ids == [handle.session_id]
    assert provider.created == [handle]
    assert provider.kwargs[0]["on_event"] == events.append
    assert provider.kwargs[0]["tools"] == [{"name": "search_rules"}]
    assert provider.kwargs[0]["system_message"] == "家庭内ルールの判断支援を行う"


async def test_close_session_detaches_only_once() -> None:
    runtime, provider = _make_runtime()
    await runtime.start()
    handle = await runtime.create_session(on_event=_noop_handler)

    await runtime.close_session(handle.session_id)
    assert handle.closed is True
    assert runtime.session_ids == []

    await runtime.stop()
    assert provider.detach_calls == 1


async def test_stop_closes_open_sessions() -> None:
    runtime, provider = _make_runtime()
    await runtime.start()
    handles = [
        await runtime.create_session(on_event=_noop_handler) for _ in range(2)
    ]

    await runtime.stop()

    assert all(handle.closed for handle in handles)
    assert runtime.session_ids == []
    assert provider.detach_calls == 2


async def test_close_unknown_session_raises() -> None:
    runtime, _ = _make_runtime()
    await runtime.start()
    with pytest.raises(AgentRuntimeError):
        await runtime.close_session("unknown-session")


async def test_health_check_reflects_state() -> None:
    runtime, _ = _make_runtime()

    down = await runtime.health_check()
    assert down.ready is False
    assert down.sessions == 0
    assert down.detail is not None

    await runtime.start()
    await runtime.create_session(on_event=_noop_handler)

    up = await runtime.health_check()
    assert up.ready is True
    assert up.sessions == 1
    assert up.model == "gemma4:e2b"
    assert up.provider == "ollama"
    assert up.detail is None


async def test_list_models_requires_start() -> None:
    runtime, _ = _make_runtime()
    with pytest.raises(AgentRuntimeError):
        await runtime.list_models()

    await runtime.start()
    assert await runtime.list_models() == ["gemma4:e2b"]


async def test_async_context_manager_stops_runtime() -> None:
    runtime, provider = _make_runtime()
    async with runtime:
        assert runtime.started is True
    assert runtime.started is False
    assert provider.stop_calls == 1
