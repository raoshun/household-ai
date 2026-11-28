"""ChatService が払い出す thread frame series の振る舞い (contract §1 / §2).

ChatService は thread (conversation_id) 内で単調 increasing な ``seq`` を採番し、同じ
``event_id`` / ``turn_id`` の写しを hub へも publish する。UI 側は turn 系列と monitor
系列を ``(seq, event_type)`` で join できるので、ここではその frame 系列の契約
(keys・ring buffer・replay・fan-out・heartbeat・usage 集計) を固定する。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest

from app.agent.provider import ProviderHealth, SessionRequest
from app.agent.runtime import AgentRuntime
from app.config import Settings
from app.services.chat import ChatService, _TurnRecord
from app.services.exceptions import UnknownThreadError
from app.services.session_events import SESSION_EVENT_FRAME_TYPES, MonitorEventType

MODEL = "gemma4:e2b"
#: contract §1 / README §10.3 の frame 名 (UI が join に使う series 毎の event_type).
STARTED = MonitorEventType.AGENT_RUN_STARTED
FINISHED = MonitorEventType.RUN_FINISHED
TURN_ID = "thread-1:t1"
HEARTBEAT_SECONDS = 0.05

#: contract §2 の nested frame keys.
FRAME_KEYS = frozenset(
    {
        "conversation_id",
        "created_at",
        "event_id",
        "event_type",
        "payload",
        "seq",
        "session_id",
        "thread_id",
        "turn_id",
    }
)


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {"model": MODEL, "provider": "ollama"}
    values.update(overrides)
    return Settings(**values)


class FakeSession:
    """runtime が保持するだけなので最小構成 (close だけ検証する)."""

    def __init__(self, session_id: str, model: str) -> None:
        self.session_id = session_id
        self.model = model
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class FakeProvider:
    """AgentRuntime を起動可能にする fake. create_session の引数を記録する."""

    def __init__(self) -> None:
        self.sessions: dict[str, FakeSession] = {}
        self.requests: list[SessionRequest] = []
        self.closed_session_ids: list[str] = []
        self.start_calls = 0

    async def start(self) -> None:
        self.start_calls += 1

    async def stop(self) -> None:
        self.sessions.clear()

    async def create_session(self, request: SessionRequest) -> FakeSession:
        self.requests.append(request)
        session = FakeSession(f"session-{len(self.requests)}", request.model or MODEL)
        self.sessions[session.session_id] = session
        return session

    async def close_session(self, session_id: str) -> None:
        self.closed_session_ids.append(session_id)
        session = self.sessions.pop(session_id, None)
        if session is not None:
            await session.close()

    async def interrupt_session(self, session_id: str) -> None:
        return None

    async def session_ids(self) -> Sequence[str]:
        return list(self.sessions)

    async def list_models(self) -> Sequence[str]:
        return [MODEL]

    async def health_check(self) -> ProviderHealth:
        return ProviderHealth(
            ready=True,
            provider="fake",
            model=MODEL,
            sessions=len(self.sessions),
            detail="fake provider",
        )


def _make_service(
    *,
    max_frames_per_thread: int = 50,
    demo_rule_tools: bool = True,
    monitor_heartbeat_seconds: float = 0.0,
) -> tuple[ChatService, FakeProvider]:
    """frame series 検証用の service を作る.

    runtime は注入する場合 start() しない (start() は service 所有 runtime だけ起こす)
    ので、provider 側をここで起動しておく。
    """
    settings = _settings(
        demo_rule_tools=demo_rule_tools,
        max_frames_per_thread=max_frames_per_thread,
    )
    provider = FakeProvider()
    runtime = AgentRuntime(settings, provider=provider)
    service = ChatService(
        monitor_heartbeat_seconds=monitor_heartbeat_seconds,
        runtime=runtime,
        settings=settings,
    )
    return service, provider


async def _started_service(**kwargs: Any) -> tuple[ChatService, FakeProvider]:
    service, provider = _make_service(**kwargs)
    await service._runtime.start()
    return service, provider


async def test_thread_frame_series_uses_contract_keys_and_local_seq() -> None:
    service, _ = await _started_service()

    thread = await service.create_thread(title="相談")
    service._publish_monitor(thread, STARTED, {"prompt": "相談"}, turn_id=TURN_ID)
    service.rename_thread(thread.thread_id, "相談 2")

    frames = service.frames(thread.thread_id)
    assert [item["seq"] for item in frames] == [1, 2, 3]
    assert set(FRAME_KEYS).issubset(frames[0])
    assert {item["thread_id"] for item in frames} == {thread.thread_id}
    assert [item["event_type"] for item in frames] == [
        MonitorEventType.SESSION_CREATED,
        STARTED,
        MonitorEventType.SESSION_EVENT_RECORDED,
    ]


async def test_seq_is_monotone_per_thread_and_isolated_between_threads() -> None:
    service, _ = await _started_service()

    first = await service.create_thread()
    second = await service.create_thread()
    service._publish_monitor(first, STARTED, {"prompt": "a"})
    service._publish_monitor(second, STARTED, {"prompt": "b"})
    service._publish_monitor(second, FINISHED, {})

    assert [item["seq"] for item in service.frames(first.thread_id)] == [1, 2]
    assert [item["seq"] for item in service.frames(second.thread_id)] == [1, 2, 3]


async def test_frames_after_seq_returns_tail_in_order() -> None:
    service, _ = await _started_service()
    thread = await service.create_thread()
    service._publish_monitor(thread, STARTED, {"prompt": "a"})

    assert [item["seq"] for item in service.frames(thread.thread_id, after_seq=1)] == [
        2
    ]
    assert service.frames(thread.thread_id, after_seq=2) == []


async def test_frame_history_is_ring_buffer_but_seq_keeps_counting() -> None:
    service, _ = await _started_service(max_frames_per_thread=2)
    thread = await service.create_thread()
    service._publish_monitor(thread, STARTED, {"prompt": "a"})
    service._publish_monitor(thread, FINISHED, {})

    assert [item["seq"] for item in service.frames(thread.thread_id)] == [2, 3]
    assert service._seq[thread.thread_id] == 3


async def test_monitor_frames_only_include_session_event_frame_types() -> None:
    service, _ = await _started_service()
    thread = await service.create_thread()
    service._publish_monitor(thread, STARTED, {"prompt": "a"})

    assert [item["event_type"] for item in service.monitor_frames(thread)] == [
        MonitorEventType.SESSION_CREATED
    ]
    assert STARTED not in SESSION_EVENT_FRAME_TYPES


async def test_hub_copy_keeps_same_event_id_as_thread_frame() -> None:
    service, _ = await _started_service()
    thread = await service.create_thread()
    frame_dict = service._publish_monitor(
        thread, STARTED, {"prompt": "相談"}, turn_id=TURN_ID
    )

    hub_events = [
        event
        for event in service._hub.recent_events(limit=20)
        if event.event_type == STARTED
    ]
    assert [event.event_id for event in hub_events] == [frame_dict["event_id"]]
    assert hub_events[0].created_at == frame_dict["created_at"]
    assert hub_events[0].turn_id == TURN_ID
    assert service._hub.conversation_for_session(thread.session_id) == thread.thread_id


async def test_open_session_starts_runtime_and_binds_hub_and_tool_context() -> None:
    service, provider = _make_service(demo_rule_tools=False)
    await service._runtime.start()

    thread = await service.create_thread(title="相談", model="qwen3:8b")

    assert provider.start_calls == 1
    assert provider.requests[0].model == "qwen3:8b"
    names = {declaration.name for declaration in provider.requests[0].tools}
    assert {"search_rules", "get_rule", "propose_rule", "evaluate_rule"} == names
    assert thread.session_id in provider.sessions
    assert service._sessions[thread.session_id] == thread.thread_id
    assert service._hub.conversation_for_session(thread.session_id) == thread.thread_id
    assert thread.session_id in service._tool_ctx


async def test_demo_rule_tools_control_session_tool_declarations() -> None:
    service, provider = _make_service()
    await service._runtime.start()
    await service.create_thread()

    names = {declaration.name for declaration in provider.requests[0].tools}
    assert {"detect_rule_conflicts", "read_rules", "request_rule_approval"} <= names


async def test_context_usage_aggregates_cost_and_usage_from_frames() -> None:
    service, _ = await _started_service()
    thread = await service.create_thread()
    first_usage = {"completion_tokens": 20, "cost_unit": "JPY", "input_tokens": 100}
    second_usage = {
        "completion_tokens": 30,
        "cost_unit": "JPY",
        "input_tokens": 50,
        "total_cost": 7.5,
    }
    service._publish_monitor(
        thread,
        FINISHED,
        {"stop_reason": "end_turn", "usage": first_usage},
        turn_id="thread-1:t1",
    )
    service._publish_monitor(
        thread,
        FINISHED,
        {"stop_reason": "end_turn", "usage": second_usage},
        turn_id="thread-1:t2",
    )

    usage = service.context_usage(thread)

    assert usage["session_id"] == thread.session_id
    assert usage["turn_count"] == 2
    assert usage["cost_available"] is True
    assert usage["usage_unavailable_reason"] is None
    assert usage["total_cost"] == {"currency": "JPY", "unit": "JPY", "value": 7.5}
    assert usage["total_usage"] == {"completion_tokens": 50.0, "input_tokens": 150.0}
    assert usage["total_costs"] == []


async def test_context_usage_prefers_turn_record_over_frames() -> None:
    service, _ = await _started_service()
    thread = await service.create_thread()
    usage = {"completion_tokens": 20, "cost_unit": "JPY", "input_tokens": 100}
    service._turns.setdefault(thread.thread_id, []).append(
        _TurnRecord(
            conversation_id=thread.thread_id,
            session_id=thread.session_id,
            text="相談",
            turn_id=TURN_ID,
            usage=dict(usage),
        )
    )
    service._publish_monitor(
        thread,
        FINISHED,
        {"stop_reason": "end_turn", "usage": dict(usage)},
        turn_id=TURN_ID,
    )

    result = service.context_usage(thread)

    assert result["turn_count"] == 1
    assert result["total_usage"] == {"completion_tokens": 20.0, "input_tokens": 100.0}
    assert [item["turn_id"] for item in result["turns"]] == [TURN_ID]


async def test_snapshot_exposes_latest_frame_per_turn_for_join() -> None:
    service, _ = await _started_service()
    thread = await service.create_thread()
    service._publish_monitor(thread, STARTED, {"prompt": "相談"}, turn_id=TURN_ID)
    finished = service._publish_monitor(
        thread,
        FINISHED,
        {"stop_reason": "end_turn"},
        turn_id=TURN_ID,
    )

    latest = service.snapshot(thread.thread_id)["latest_status_by_turn"]

    assert list(latest) == [TURN_ID]
    assert latest[TURN_ID]["event_type"] == FINISHED
    assert latest[TURN_ID]["seq"] == finished["seq"]
    assert service.snapshot(thread.thread_id)["session_events"][-1]["seq"] == 1


async def test_subscribe_frames_replays_history_then_delivers_live_frames() -> None:
    service, _ = await _started_service()
    thread = await service.create_thread()
    replayed = service._publish_monitor(
        thread, STARTED, {"prompt": "相談"}, turn_id=TURN_ID
    )

    agen = service.subscribe_frames(thread.thread_id, next_seq=1).__aiter__()
    first = await agen.__anext__()
    assert (first["seq"], first["event_type"]) == (2, STARTED)
    assert first["event_id"] == replayed["event_id"]

    service._publish_monitor(
        thread,
        FINISHED,
        {"stop_reason": "end_turn"},
        turn_id=TURN_ID,
    )
    live = await agen.__anext__()
    assert (live["seq"], live["event_type"]) == (3, FINISHED)
    assert live["turn_id"] == TURN_ID

    await agen.aclose()
    assert service._subscriptions == {}
    assert service._queues == {}


async def test_publish_fans_out_the_same_frame_to_every_subscription() -> None:
    service, _ = await _started_service()
    thread = await service.create_thread()
    service._publish_monitor(thread, STARTED, {"prompt": "相談"}, turn_id=TURN_ID)

    first = service.subscribe_frames(thread.thread_id, next_seq=1).__aiter__()
    second = service.subscribe_frames(thread.thread_id, next_seq=1).__aiter__()
    replay_a = await first.__anext__()
    replay_b = await second.__anext__()
    assert replay_a == replay_b

    service._publish_monitor(
        thread, FINISHED, {"stop_reason": "end_turn"}, turn_id=TURN_ID
    )
    live_a = await first.__anext__()
    live_b = await second.__anext__()
    assert (live_a["seq"], live_a["event_type"]) == (3, FINISHED)
    assert live_a == live_b

    await first.aclose()
    await second.aclose()
    assert service._subscriptions == {}


async def test_subscribe_frames_can_be_scoped_to_one_turn() -> None:
    service, _ = await _started_service()
    thread = await service.create_thread()
    service._publish_monitor(thread, STARTED, {"prompt": "a"}, turn_id="turn-1")
    service._publish_monitor(thread, STARTED, {"prompt": "b"}, turn_id="turn-2")

    # next_seq の cursor に対して別 turn の frame は scope filter で skip する
    # (run_turn / subscribe_run が自分の turn だけ受け取るための挙動)。
    agen = service.subscribe_frames(
        thread.thread_id, next_seq=2, turn_id="turn-2"
    ).__aiter__()
    replayed = await agen.__anext__()
    assert (replayed["seq"], replayed["turn_id"]) == (3, "turn-2")

    service._publish_monitor(thread, FINISHED, {}, turn_id="turn-1")
    service._publish_monitor(
        thread,
        FINISHED,
        {"stop_reason": "end_turn"},
        turn_id="turn-2",
    )
    live = await agen.__anext__()
    assert (live["seq"], live["turn_id"]) == (5, "turn-2")
    await agen.aclose()


async def test_idle_subscription_receives_heartbeat_frames_from_service() -> None:
    service, _ = await _started_service(monitor_heartbeat_seconds=HEARTBEAT_SECONDS)
    thread = await service.create_thread()

    agen = service.subscribe_frames(thread.thread_id).__aiter__()
    replayed = await agen.__anext__()
    first = await agen.__anext__()
    second = await agen.__anext__()
    await agen.aclose()

    assert replayed["seq"] == 1
    assert first["event_type"] == MonitorEventType.HEARTBEAT
    assert second["event_type"] == MonitorEventType.HEARTBEAT
    assert first["seq"] > replayed["seq"]
    assert second["seq"] > first["seq"]
    assert first["payload"]["active_turn_id"] is None
    assert first["payload"]["conversation_id"] == thread.thread_id
    assert first["payload"]["pending_approval_ids"] == []
    assert first["payload"]["session_id"] == thread.session_id
    assert len(service.frames(thread.thread_id)) > 1


async def test_unknown_thread_is_rejected_by_frames_and_subscription() -> None:
    service, _ = await _started_service()

    with pytest.raises(UnknownThreadError):
        service.frames("missing-thread")
    with pytest.raises(UnknownThreadError):
        await service.subscribe_frames("missing-thread").__aiter__().__anext__()
