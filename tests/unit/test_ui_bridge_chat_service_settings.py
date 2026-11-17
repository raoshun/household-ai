"""ChatService が contract §7 の設定値を実処理へ結線することを検証する.

backend/app/services/chat.py 内の固有 default 定数をやめ、Settings
(``monitor_replay_limit`` / ``monitor_heartbeat_seconds`` /
``max_concurrent_turns``) を monitor hub replay・heartbeat 配給・ターン受け入れ
へ使う。§4 の exception 形 (409 busy / 429 同時実行上限) も settings 値で決める。
"""

from __future__ import annotations

import asyncio
from typing import cast

import pytest

from app.agent.runtime import AgentRuntime
from app.clock import tokyo_iso
from app.config import (
    DEFAULT_MAX_CONCURRENT_TURNS,
    DEFAULT_MONITOR_HEARTBEAT_SECONDS,
    DEFAULT_MONITOR_REPLAY_LIMIT,
    Settings,
)
from app.services.chat import ChatService, Thread
from app.services.exceptions import ConcurrencyLimitError, TurnInProgressError
from app.services.hub import MonitorHub
from app.services.session_events import MonitorEventType

THREAD_ID = "settings-thread"
MODEL = "gemma4:e2b"
FAST_HEARTBEAT_SECONDS = 0.05


def _settings(**overrides: object) -> Settings:
    return Settings(
        model=MODEL,
        provider="ollama",
        runtime_mode="in-process",
        **overrides,
    )


def _service(
    settings: Settings | None = None,
    *,
    hub: MonitorHub | None = None,
    monitor_heartbeat_seconds: float | None = None,
) -> ChatService:
    """runtime を呼ばない設定結線検証用 service.

    settings 結線だけを見る test はターン本体を走らせないため、runtime には
    呼ばれない sentinel を入れる（broker test と同じ idiom）。
    """
    return ChatService(
        settings=settings if settings is not None else _settings(),
        runtime=cast(AgentRuntime, object()),
        hub=hub,
        monitor_heartbeat_seconds=monitor_heartbeat_seconds,
    )


def _register_thread(
    service: ChatService,
    thread_id: str,
    *,
    busy_turn_id: str | None = None,
) -> Thread:
    """run_turn / post_turn の受け入れ判定だけを見るため thread 状態を seed する."""
    thread = Thread(
        created_at=tokyo_iso(),
        forked_from=None,
        model=MODEL,
        session_id=thread_id,
        thread_id=thread_id,
        title="settings thread",
    )
    service._threads[thread_id] = thread
    service._sessions[thread_id] = thread.session_id
    if busy_turn_id is not None:
        service._active_turns[thread_id] = busy_turn_id
    return thread


# --------------------------------------------------------------------- replay 結線


def test_monitor_replay_limit_comes_from_settings() -> None:
    service = _service(_settings(monitor_replay_limit=3))
    assert service._hub.replay_limit == 3

    for index in range(5):
        service._hub.publish(
            MonitorEventType.CONTEXT_USAGE,
            conversation_id=THREAD_ID,
            payload={"index": index},
        )

    replayed = service._hub.replay_frames()
    assert [frame["payload"]["index"] for frame in replayed] == [2, 3, 4]


def test_monitor_replay_limit_default_matches_config() -> None:
    service = _service()
    assert service._hub.replay_limit == DEFAULT_MONITOR_REPLAY_LIMIT == 200


def test_injected_hub_keeps_its_own_replay_limit() -> None:
    hub = MonitorHub(replay_limit=7)
    service = _service(_settings(monitor_replay_limit=3), hub=hub)
    assert service._hub.replay_limit == 7


# ------------------------------------------------------------------ heartbeat 結線


def test_monitor_heartbeat_comes_from_settings() -> None:
    service = _service(_settings(monitor_heartbeat_seconds=1.0))
    assert service._monitor_heartbeat == 1.0


def test_monitor_heartbeat_default_matches_config() -> None:
    service = _service()
    assert service._monitor_heartbeat == DEFAULT_MONITOR_HEARTBEAT_SECONDS


def test_monitor_heartbeat_kwarg_overrides_settings() -> None:
    service = _service(
        _settings(monitor_heartbeat_seconds=5.0),
        monitor_heartbeat_seconds=FAST_HEARTBEAT_SECONDS,
    )
    assert service._monitor_heartbeat == FAST_HEARTBEAT_SECONDS


async def test_settings_heartbeat_gap_publishes_heartbeat_frame() -> None:
    """settings の heartbeat 秒 (1s) が subscribe_frames の待ち窓になる (§7)."""
    service = _service(_settings(monitor_heartbeat_seconds=1.0))
    _register_thread(service, THREAD_ID)

    frames = service.subscribe_frames(THREAD_ID)
    try:
        frame = await asyncio.wait_for(frames.__anext__(), timeout=5.0)
    finally:
        await frames.aclose()

    assert frame["event_type"] == MonitorEventType.HEARTBEAT
    assert frame["thread_id"] == THREAD_ID
    assert frame["seq"] == 1
    assert frame["payload"]["conversation_id"] == THREAD_ID
    assert frame["payload"]["pending_approval_ids"] == []


async def test_zero_heartbeat_override_disables_heartbeat_frames() -> None:
    service = _service(
        _settings(monitor_heartbeat_seconds=1.0),
        monitor_heartbeat_seconds=0.0,
    )
    _register_thread(service, THREAD_ID)

    frames = service.subscribe_frames(THREAD_ID)
    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(frames.__anext__(), timeout=0.3)
    finally:
        await frames.aclose()


# ------------------------------------------------------------- 同時実行上限の結線


def test_max_concurrent_turns_default_matches_config() -> None:
    service = _service()
    assert service._max_concurrent_turns == DEFAULT_MAX_CONCURRENT_TURNS == 4


def test_max_concurrent_turns_comes_from_settings() -> None:
    service = _service(_settings(max_concurrent_turns=2))
    assert service._max_concurrent_turns == 2


async def test_capacity_guard_follows_settings_limit() -> None:
    """settings.max_concurrent_turns の値が turn 受け入れを止める位置を決める (§7)."""
    strict = _service(_settings(max_concurrent_turns=2))
    _seed_busy_pair(strict)
    free_strict = _register_thread(strict, THREAD_ID)
    with pytest.raises(ConcurrencyLimitError) as exc_info:
        strict._require_turn_capacity(free_strict)
    assert exc_info.value.running_turn_ids == ["other-1:t1", "other-2:t1"]

    relaxed = _service(_settings(max_concurrent_turns=3))
    _seed_busy_pair(relaxed)
    relaxed._require_turn_capacity(_register_thread(relaxed, THREAD_ID))


def _seed_busy_pair(service: ChatService) -> None:
    for thread_id in ("other-1", "other-2"):
        _register_thread(service, thread_id, busy_turn_id=f"{thread_id}:t1")


async def test_post_turn_rejects_busy_thread_as_turn_in_progress() -> None:
    service = _service(_settings(max_concurrent_turns=4))
    _register_thread(service, THREAD_ID, busy_turn_id=f"{THREAD_ID}:running")

    with pytest.raises(TurnInProgressError) as exc_info:
        await service.post_turn(THREAD_ID, "hello")

    assert exc_info.value.turn_id == f"{THREAD_ID}:running"


async def test_run_turn_rejects_busy_thread_as_turn_in_progress() -> None:
    service = _service(_settings(max_concurrent_turns=4))
    _register_thread(service, THREAD_ID, busy_turn_id=f"{THREAD_ID}:running")

    frames = service.run_turn(THREAD_ID, "hello")
    with pytest.raises(TurnInProgressError):
        await frames.__anext__()


async def test_post_turn_rejects_third_thread_at_settings_limit() -> None:
    service = _service(_settings(max_concurrent_turns=2))
    _seed_busy_pair(service)
    _register_thread(service, THREAD_ID)

    with pytest.raises(ConcurrencyLimitError) as exc_info:
        await service.post_turn(THREAD_ID, "hello")

    assert exc_info.value.running_turn_ids == ["other-1:t1", "other-2:t1"]
