"""§9.6 実行監視 stream（monitor frames + session events）を fan-out する hub.

Frame 語彙は ``app.services.session_events`` の単一 vocabulary に従う。
``MonitorEvent.to_frame()`` は payload を常に ``payload`` key へ nest する
（review 要求: hub は payload keys を frame 直下へ merge するため、ApprovalRecord
map をそのまま渡すと record が serialize される）。session_events の turn frame は
frame_style="merged" で ``turn_id`` 直下 key を保つ。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from app.agent.permissions import PermissionRecord
from app.clock import tokyo_iso

logger = logging.getLogger("household-ai.services.hub")

DEFAULT_MONITOR_REPLAY = 50
DEFAULT_SUBSCRIBER_BUFFER = 256
FrameStyle = Literal["merged", "nested"]


@dataclass(slots=True)
class MonitorEvent:
    """監視 stream 1 件。"""

    event_type: str
    event_id: str
    created_at: str
    conversation_id: str | None = None
    session_id: str | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)
    turn_id: str | None = None
    frame_style: FrameStyle = "nested"

    def seq(self) -> int:
        return int(self.event_id.rsplit("-", 1)[1])

    def to_frame(self) -> dict[str, Any]:
        """UI へ渡す frame dict（chat SSE / monitor SSE / AG-UI CustomEvent 共通）."""
        sequence = self.seq()
        if self.frame_style == "merged":
            frame: dict[str, Any] = {
                "conversation_id": self.conversation_id,
                "created_at": self.created_at,
                "event_id": self.event_id,
                "event_type": self.event_type,
                "seq": sequence,
                "session_id": self.session_id,
                "turn_id": self.turn_id,
            }
            frame.update(dict(self.payload))
            return frame
        return {
            "conversation_id": self.conversation_id,
            "created_at": self.created_at,
            "event_id": self.event_id,
            "event_type": self.event_type,
            "payload": dict(self.payload),
            "seq": sequence,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
        }


class MonitorSubscription:
    """bounded buffer + wakeup で SSE client 1 購読分."""

    def __init__(self, hub: MonitorHub, *, name: str, replay_limit: int) -> None:
        self._hub = hub
        self.name = name
        self._buffer: deque[MonitorEvent] = deque(
            hub.recent_events(limit=replay_limit), maxlen=hub.subscriber_buffer
        )
        self._wakeup = asyncio.Event()
        self._closed = False

    def deliver(self, event: MonitorEvent) -> None:
        if self._closed:
            return
        self._buffer.append(event)
        self._wakeup.set()

    def close(self) -> None:
        self._closed = True
        self._wakeup.set()

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def buffered(self) -> list[MonitorEvent]:
        return list(self._buffer)

    async def next_event(self, *, timeout: float | None = None) -> MonitorEvent | None:
        """buffer から 1 件取り出す。無ければ timeout まで待つ."""
        if self._closed:
            return None
        if not self._buffer:
            if timeout is None or timeout <= 0:
                return None
            try:
                await asyncio.wait_for(self._wakeup.wait(), timeout=timeout)
            except TimeoutError:
                return None
        self._wakeup.clear()
        if not self._buffer:
            return None
        return self._buffer.popleft()


class MonitorHub:
    """session-scoped + conversation-scoped な monitor fan-out（sync publish 対応）."""

    def __init__(
        self,
        *,
        subscriber_buffer: int = DEFAULT_SUBSCRIBER_BUFFER,
        replay_limit: int = DEFAULT_MONITOR_REPLAY,
    ) -> None:
        if subscriber_buffer <= 0:
            msg = "subscriber_buffer は 1 以上"
            raise ValueError(msg)
        if replay_limit < 0:
            msg = "replay_limit は 0 以上"
            raise ValueError(msg)
        self._replay_limit_value = replay_limit
        self._subscriber_buffer = subscriber_buffer
        self._recent: deque[MonitorEvent] = deque(maxlen=max(replay_limit, 1))
        self._subscriptions: list[MonitorSubscription] = []
        self._sequence = 0
        self._session_to_conversation: dict[str, str] = {}
        self._current_turn: dict[str, str] = {}
        self._session_frames: deque[dict[str, Any]] = deque(maxlen=max(replay_limit, 1))

    @property
    def replay_limit(self) -> int:
        return self._replay_limit_value

    @property
    def subscriber_buffer(self) -> int:
        return self._subscriber_buffer

    def recent_events(
        self,
        *,
        limit: int = DEFAULT_MONITOR_REPLAY,
    ) -> list[MonitorEvent]:
        events = list(self._recent)
        if limit <= 0:
            return []
        return events[-limit:]

    def bind_session(
        self, *, session_id: str | None, conversation_id: str | None
    ) -> None:
        if session_id and conversation_id:
            self._session_to_conversation[session_id] = conversation_id

    def unbind_session(self, *, session_id: str | None) -> None:
        if session_id:
            self._session_to_conversation.pop(session_id, None)
            self._current_turn.pop(session_id, None)

    def conversation_for_session(self, session_id: str | None) -> str | None:
        if not session_id:
            return None
        return self._session_to_conversation.get(session_id)

    def set_current_turn(self, *, session_id: str | None, turn_id: str | None) -> None:
        """turn 開始／終了で現在の turn を登録する。

        approval frame への turn_id 付与に使う。
        """
        if not session_id:
            return
        if turn_id is None:
            self._current_turn.pop(session_id, None)
        else:
            self._current_turn[session_id] = turn_id

    def current_turn(self, session_id: str | None) -> str | None:
        if not session_id:
            return None
        return self._current_turn.get(session_id)

    def subscribe(
        self, *, name: str = "monitor", replay_limit: int | None = None
    ) -> MonitorSubscription:
        limit = self._replay_limit_value if replay_limit is None else replay_limit
        subscription = MonitorSubscription(self, name=name, replay_limit=limit)
        self._subscriptions.append(subscription)
        return subscription

    def drop_subscription(self, subscription: MonitorSubscription) -> None:
        with contextlib.suppress(ValueError):
            self._subscriptions.remove(subscription)

    def subscription_count(self) -> int:
        return len(self._subscriptions)

    def publish(
        self,
        event_type: str,
        *,
        conversation_id: str | None = None,
        session_id: str | None = None,
        payload: Mapping[str, Any] | None = None,
        turn_id: str | None = None,
        frame_style: FrameStyle = "nested",
    ) -> MonitorEvent:
        """sync publish（broker listener など await できない場所からも使える）.

        buffer 超過はその subscription の最古 event を落とす（監視は best effort）。
        """
        self._sequence += 1
        resolved_turn = (
            turn_id if turn_id is not None else self.current_turn(session_id)
        )
        event = MonitorEvent(
            conversation_id=conversation_id,
            created_at=tokyo_iso(),
            event_id=f"evt-{self._sequence:06d}",
            event_type=event_type,
            frame_style=frame_style,
            payload=dict(payload or {}),
            session_id=session_id,
            turn_id=resolved_turn,
        )
        self._recent.append(event)
        frame = event.to_frame()
        self._session_frames.append(frame)
        for subscription in list(self._subscriptions):
            subscription.deliver(event)
        logger.debug("monitor event type=%s seq=%s", event_type, event.seq())
        return event

    def publish_for_session(
        self,
        event_type: str,
        *,
        session_id: str | None = None,
        conversation_id: str | None = None,
        payload: Mapping[str, Any] | None = None,
        turn_id: str | None = None,
        frame_style: FrameStyle = "nested",
    ) -> MonitorEvent:
        """conversation_id を session 対応表から解決して publish する（§9.6 join）."""
        resolved = conversation_id or self.conversation_for_session(session_id)
        return self.publish(
            event_type,
            conversation_id=resolved,
            frame_style=frame_style,
            payload=payload,
            session_id=session_id,
            turn_id=turn_id,
        )

    def replay_frames(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        """session event 用 frame replay（subscribe 前に遡って 1 系列で渡す）."""
        frames = list(self._session_frames)
        if limit is None or limit >= len(frames):
            return [dict(frame) for frame in frames]
        if limit <= 0:
            return []
        return [dict(frame) for frame in frames[-limit:]]


class ApprovalBroadcaster:
    """PermissionBroker listener を監視用承認 frame へ変換して fan-out する.

    `turn_id` は hub の ``current_turn``（chat service が turn 開始／終了で更新）から
    付与するので、UI は approval を turn 単位で join できる。payload は必ず nest。
    """

    def __init__(self, hub: MonitorHub) -> None:
        self._hub = hub

    def on_publish(self, record: PermissionRecord) -> None:
        self._hub.publish_for_session(
            "approval_requested",
            payload={"approval": record.to_dict()},
            session_id=record.prompt.session_id,
        )

    def on_resolve(self, record: PermissionRecord, *, resolved_by: str) -> None:
        self._hub.publish_for_session(
            "approval_resolved",
            payload={"action": resolved_by, "approval": record.to_dict()},
            session_id=record.prompt.session_id,
        )

    def publish_updated(self, record: PermissionRecord) -> None:
        """REST 判断後など broker 外からの更新通知に使う（sync・非同期両対応）。"""
        self._hub.publish_for_session(
            "approval_updated",
            payload={"approval": record.to_dict()},
            session_id=record.prompt.session_id,
        )


__all__ = [
    "ApprovalBroadcaster",
    "MonitorEvent",
    "MonitorHub",
    "MonitorSubscription",
]
