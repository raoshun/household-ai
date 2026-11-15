"""AG-UI thread / chat session を agent-runtime へ bridge する service (§9 / §10).

- thread_id == conversation_id（transcript / audit / tool / approval の join key）
- 1 thread に 1 Copilot session（tool 群は thread 専属の RuleToolContext で bind）
- turn frames を asyncio.Queue で fan-out (REST monitor と AG-UI run が同一 series)
- HITL: 実行中の permission prompt は monitor frame と AG-UI interrupt
  (`outcome.type=interrupt`) の両方で提示し、解決は approval REST / `resume`
  どちらでも行える (README §10.3 / §12.4)
- README §7.1 準拠: terminal event で閉じ、中断時は partial + terminal の 1 系列
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass, field
from typing import Any

from app.agent import AgentRuntime
from app.agent.events import AgentEventType
from app.agent.permissions import PermissionBroker
from app.clock import tokyo_iso
from app.config import Settings, load_settings
from app.services.audit import AuditTrail, ConversationLog, JudgmentStore
from app.services.exceptions import (
    ChatServiceError,
    ConcurrencyLimitError,
    ForkNotFoundError,
    ThreadClosedError,
    UnknownApprovalError,
    UnknownResolverError,
    UnknownThreadError,
    UnknownTurnError,
)
from app.services.hub import ApprovalBroadcaster, MonitorHub
from app.services.rules import RuleStore
from app.services.session_events import (
    MonitorEventType,
    group_by_turn,
    latest_status_by_turn,
)
from app.services.tools import RuleToolContext

logger = logging.getLogger("household-ai.services.chat")

TERMINAL_TYPES: frozenset[AgentEventType] = frozenset(
    {AgentEventType.SESSION_ERROR, AgentEventType.SESSION_IDLE}
)
DEFAULT_TITLE = "新しい家族会話"
DEFAULT_CONCURRENCY_LIMIT = 4
DEFAULT_MONITOR_REPLAY = 200
DEFAULT_MONITOR_HEARTBEAT_SECONDS = 10.0
TERMINAL_FRAME_EVENTS: frozenset[str] = frozenset(
    {
        MonitorEventType.RUN_CANCELLED,
        MonitorEventType.RUN_FINISHED,
        MonitorEventType.ERROR,
    }
)
# README §12.4 / §6.5 で human/policy/system が区別できるように絞る approval 解決者。
RESOLVERS: frozenset[str] = frozenset(
    {
        "family",
        "policy",
        "policy-timeout",
        "session-closed",
        "stale-session",
        "turn-cancelled",
        "system",
        "agent",
    }
)


# chat service 系の例外は app.services.exceptions が canonical home
# (contract §4 の taxonomy)。
# chat.py は existing import 経路との互換のため同名を re-export する。
__all__ = [
    "ChatService",
    "ChatServiceError",
    "ConcurrencyLimitError",
    "ForkNotFoundError",
    "MonitorEventType",
    "Thread",
    "ThreadClosedError",
    "UnknownApprovalError",
    "UnknownResolverError",
    "UnknownThreadError",
    "UnknownTurnError",
]


@dataclass(slots=True)
class Thread:
    thread_id: str
    session_id: str | None
    title: str
    created_at: str
    closed_at: str | None = None
    forked_from: str | None = None
    model: str | None = None
    closed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "closed_at": self.closed_at,
            "created_at": self.created_at,
            "forked_from": self.forked_from,
            "model": self.model,
            "session_id": self.session_id,
            "thread_id": self.thread_id,
            "title": self.title,
        }


@dataclass(slots=True)
class TurnSubscription:
    frames: asyncio.Queue[Any]
    next_seq: int
    queue_limit: int
    thread_id: str


@dataclass(slots=True)
class _TurnRecord:
    conversation_id: str
    session_id: str | None
    text: str
    turn_id: str
    created_at: str = field(default_factory=tokyo_iso)
    completed_at: str | None = None
    stop_reason: str | None = None
    error: str | None = None
    usage: dict[str, Any] | None = None
    messages: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_calls_by_id: dict[str, dict[str, Any]] = field(default_factory=dict)
    cancel_reason: str | None = None
    interrupt_info: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "completed_at": self.completed_at,
            "conversation_id": self.conversation_id,
            "created_at": self.created_at,
            "error": self.error,
            "messages": list(self.messages),
            "session_id": self.session_id,
            "stop_reason": self.stop_reason,
            "text": self.text,
            "tool_calls": list(self.tool_calls),
            "turn_id": self.turn_id,
            "usage": dict(self.usage) if self.usage is not None else None,
        }


def thread_public_id(thread: Thread) -> str:
    return thread.thread_id


class ChatService:
    """AG-UI thread + chat session + turn fan-out を 1 か所で管理する."""

    def __init__(
        self,
        *,
        runtime: AgentRuntime | None = None,
        broker: PermissionBroker | None = None,
        rules: RuleStore | None = None,
        logs: ConversationLog | None = None,
        judgments: JudgmentStore | None = None,
        audit: AuditTrail | None = None,
        hub: MonitorHub | None = None,
        settings: Settings | None = None,
        monitor_heartbeat_seconds: float = DEFAULT_MONITOR_HEARTBEAT_SECONDS,
    ) -> None:
        self._settings = settings if settings is not None else load_settings()
        self._owns_runtime = runtime is None
        self._runtime = runtime if runtime is not None else AgentRuntime(self._settings)
        self._broker = (
            broker if broker is not None else PermissionBroker(self._settings)
        )
        self._rules = rules if rules is not None else RuleStore()
        self._logs = logs if logs is not None else ConversationLog()
        self._judgments = judgments if judgments is not None else JudgmentStore()
        self._audit = audit if audit is not None else AuditTrail()
        self._hub = (
            hub if hub is not None else MonitorHub(replay_limit=DEFAULT_MONITOR_REPLAY)
        )
        self._approvals = ApprovalBroadcaster(self._hub)
        self._threads: dict[str, Thread] = {}
        self._sessions: dict[str, str] = {}
        self._queues: dict[str, asyncio.Queue[Any]] = {}
        self._seq: dict[str, int] = {}
        self._turns: dict[str, list[_TurnRecord]] = {}
        self._turn_by_id: dict[str, _TurnRecord] = {}
        self._messages: dict[str, list[dict[str, Any]]] = {}
        self._active_pumps: dict[str, asyncio.Task[None]] = {}
        self._active_turns: dict[str, str] = {}
        self._queued: dict[str, asyncio.Queue[str]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._turn_seq: dict[str, int] = {}
        self._tool_ctx: dict[str, RuleToolContext] = {}
        self._last_usage: dict[str, dict[str, Any]] = {}
        self._monitor_heartbeat = monitor_heartbeat_seconds
        self._started = False
        self._reaper: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        if self._started:
            return
        if self._owns_runtime:
            await self._runtime.start()
        self._broker.set_listeners(
            on_publish=self._approvals.on_publish,
            on_resolve=self._approvals.on_resolve,
        )
        self._started = True
        if self._settings.session_idle_timeout_seconds > 0:
            self._reaper = asyncio.create_task(
                self._reap_idle_sessions(), name="session-reaper"
            )
        logger.info("chat service started provider=%s", self._settings.provider)

    async def stop(self) -> None:
        if self._reaper is not None:
            self._reaper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reaper
            self._reaper = None
        for task in list(self._active_pumps.values()):
            task.cancel()
        for thread in list(self._threads.values()):
            if thread.session_id is not None and not thread.closed:
                with contextlib.suppress(Exception):
                    await self._runtime.close_session(thread.session_id)
        self._active_pumps.clear()
        if self._owns_runtime:
            with contextlib.suppress(Exception):
                await self._runtime.stop()
        self._started = False

    # ------------------------------------------------------------------ threads

    async def create_thread(
        self,
        *,
        title: str | None = None,
        thread_id: str | None = None,
        model: str | None = None,
        forked_from: str | None = None,
    ) -> Thread:
        conversation_id = thread_id or self._logs.new_conversation_id()
        if (
            conversation_id in self._threads
            and not self._threads[conversation_id].closed
        ):
            return self._threads[conversation_id]
        session_id = await self._open_session(conversation_id, model=model)
        thread = Thread(
            created_at=tokyo_iso(),
            forked_from=forked_from,
            model=model or self._settings.model,
            session_id=session_id,
            thread_id=conversation_id,
            title=title or DEFAULT_TITLE,
        )
        self._threads[conversation_id] = thread
        self._sessions[session_id] = conversation_id
        self._logs.start_conversation(conversation_id, title=thread.title)
        self._hub.bind_session(conversation_id=conversation_id, session_id=session_id)
        self._publish_monitor(
            thread,
            MonitorEventType.SESSION_CREATED,
            {"session_id": session_id, "title": thread.title},
        )
        return thread

    async def close_thread(self, thread_id: str) -> Thread:
        thread = self._require_thread(thread_id)
        if thread.closed:
            return thread
        task = self._active_pumps.pop(thread_id, None)
        if task is not None:
            task.cancel()
        self._hub.set_current_turn(session_id=thread.session_id, turn_id=None)
        if thread.session_id is not None:
            self._broker.mark_stale(thread.session_id)
            with contextlib.suppress(Exception):
                await self._runtime.close_session(thread.session_id)
            self._sessions.pop(thread.session_id, None)
        thread.closed = True
        thread.closed_at = tokyo_iso()
        self._hub.unbind_session(session_id=thread.session_id)
        self._publish_monitor(
            thread,
            MonitorEventType.SESSION_CLOSED,
            {"session_id": thread.session_id},
        )
        return thread

    async def delete_thread(self, thread_id: str) -> None:
        await self.close_thread(thread_id)
        if thread_id in self._threads:
            self._threads[thread_id].closed_at = (
                self._threads[thread_id].closed_at or tokyo_iso()
            )

    async def fork_thread(
        self,
        thread_id: str,
        *,
        title: str | None = None,
        from_seq: int = 0,
    ) -> Thread:
        """§10 の fork (message replay で fork 前の文脈を新しい session へ複製)."""
        source = self._require_thread(thread_id)
        messages = [
            m for m in self._messages.get(thread_id, []) if m["seq"] >= from_seq
        ]
        forked = await self.create_thread(
            title=title or f"{source.title} (fork)", model=source.model
        )
        forked.forked_from = thread_id
        replayed = 0
        for message in messages:
            if message["role"] != "user":
                continue
            record = self._logs.record_message(
                conversation_id=forked.thread_id,
                content=message["text"],
                role="user",
                source="fork",
            )
            self._append_message(forked.thread_id, record)
            replayed += 1
        self._publish_monitor(
            forked,
            MonitorEventType.SESSION_FORKED,
            {"replayed_messages": replayed, "source_thread_id": thread_id},
        )
        return forked

    def rename_thread(self, thread_id: str, title: str) -> Thread:
        thread = self._require_thread(thread_id)
        thread.title = title
        self._logs.rename_conversation(thread_id, title)
        self._publish_monitor(
            thread, MonitorEventType.SESSION_EVENT_RECORDED, {"title": title}
        )
        return thread

    def threads(self) -> list[Thread]:
        return sorted(self._threads.values(), key=lambda t: t.created_at)

    def thread(self, thread_id: str) -> Thread:
        return self._require_thread(thread_id)

    def snapshot(self, thread_id: str) -> dict[str, Any]:
        thread = self._require_thread(thread_id)
        frames = self.frames(thread_id)
        grouped = group_by_turn(frames)
        turns = [t.to_dict() for t in self._turns.get(thread_id, [])]
        approvals = [record.to_dict() for record in self._broker.approvals()]
        latest = latest_status_by_turn(frames)
        return {
            "approvals": approvals,
            "context_usage": self.context_usage(thread),
            "frames": frames,
            "latest_status_by_turn": latest,
            "messages": list(self._messages.get(thread_id, [])),
            "session": thread.to_dict(),
            "session_events": self.monitor_frames(thread),
            "tool_calls_by_turn": {
                turn_id: [f for f in items if f.get("tool_call_id") is not None]
                for turn_id, items in grouped.items()
            },
            "turns": turns,
        }

    # ------------------------------------------------------------------ turns

    async def run_turn(
        self,
        thread_id: str,
        text: str,
        *,
        turn_id: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """1 turn を実行し frame series を yield する（terminal frame で閉じる）."""
        thread = self._require_thread(thread_id)
        started = await self._start_turn(thread, text, turn_id=turn_id)
        yield started
        async for frame_item in self.subscribe_frames(
            thread_id,
            next_seq=int(started["seq"]) + 1,
            turn_id=started["turn_id"],
        ):
            if frame_item["event_type"] == MonitorEventType.HEARTBEAT:
                continue
            yield frame_item
            if (
                frame_item.get("turn_id") == started["turn_id"]
                and frame_item["event_type"] in TERMINAL_FRAME_EVENTS
            ):
                return

    async def post_turn(
        self, thread_id: str, text: str, *, turn_id: str | None = None
    ) -> int:
        """background pump で turn を走らせ、起跑 frame dict を返す（SSE client 用）."""
        thread = self._require_thread(thread_id)
        return await self._start_turn(thread, text, turn_id=turn_id)

    async def subscribe_run(
        self,
        thread_id: str,
        *,
        next_seq: int = 0,
        turn_id: str | None = None,
        stop_at_terminal: bool = True,
        max_frames: int | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """run 系列 frame を replay + live で購読する（AG-UI run SSE 用）."""
        thread = self._require_thread(thread_id)
        limit = (
            max_frames
            if max_frames is not None
            else self._settings.max_frames_per_thread
        )
        count = 0
        async for frame_item in self.subscribe_frames(
            thread_id, next_seq=next_seq, turn_id=turn_id
        ):
            if frame_item["event_type"] == MonitorEventType.HEARTBEAT:
                yield frame_item
                continue
            yield frame_item
            count += 1
            if (
                stop_at_terminal
                and frame_item.get("turn_id") == turn_id
                and frame_item["event_type"] in TERMINAL_FRAME_EVENTS
            ):
                return
            if count >= limit:
                yield self._publish_monitor(
                    thread,
                    MonitorEventType.ERROR,
                    {
                        "code": "max_frames_exceeded",
                        "error_type": "frame_limit_exceeded",
                        "limit": limit,
                        "message": f"frame 上限 {limit} に達したため stream を閉じた",
                        "recoverable": False,
                        "turn_id": turn_id,
                    },
                    turn_id=turn_id,
                )
                return

    async def cancel_turn(
        self, thread_id: str, *, reason: str = "client_disconnected"
    ) -> dict[str, Any]:
        """進行中 turn を中断し cancel frame を返す（README §10.3 クライアント切断）."""
        thread = self._require_thread(thread_id)
        turn_id = self._active_turns.get(thread_id)
        if turn_id is None:
            msg = f"進行中 turn がありません: {thread_id}"
            raise UnknownTurnError(msg)
        record = self._turn_by_id.get(turn_id)
        if record is not None:
            record.cancel_reason = reason
        task = self._active_pumps.pop(thread_id, None)
        if task is not None:
            task.cancel()
        if thread.session_id is not None:
            for approval in self._broker.approvals(session_id=thread.session_id):
                if approval.status == "pending":
                    with contextlib.suppress(Exception):
                        await self._broker.resolve_permission(
                            approval.approval_id,
                            approved=False,
                            decided_by="turn-cancelled",
                            feedback=f"turn を中断 ({reason})",
                        )
            with contextlib.suppress(Exception):
                await self._runtime.interrupt_session(thread.session_id)
        return self._publish_monitor(
            thread,
            MonitorEventType.RUN_CANCELLED,
            {
                "message": f"turn を中断した: {reason}",
                "reason": reason,
                "session_id": thread.session_id,
                "turn_id": turn_id,
            },
            turn_id=turn_id,
        )

    # ------------------------------------------------------------------ HITL 承認

    def approvals(
        self, *, thread_id: str | None = None, status: str | None = None
    ) -> list[dict[str, Any]]:
        """PermissionBroker の approval record 一覧 (§10.3 /approvals 相当)."""
        session_id = None
        if thread_id is not None:
            session_id = self._require_thread(thread_id).session_id
        records = self._broker.approvals(session_id=session_id)
        out = [record.to_dict() for record in records]
        if status is not None:
            out = [item for item in out if item["status"] == status]
        return out

    def pending_approvals(
        self, *, thread_id: str | None = None
    ) -> list[dict[str, Any]]:
        return self.approvals(thread_id=thread_id, status="pending")

    def approval(self, approval_id: str) -> dict[str, Any]:
        record = self._broker.get_approval(approval_id)
        if record is None:
            msg = f"approval が見つかりません: {approval_id}"
            raise UnknownApprovalError(msg)
        return record.to_dict()

    async def resolve_approval(
        self,
        approval_id: str,
        *,
        approved: bool,
        decided_by: str = "family",
        feedback: str | None = None,
    ) -> dict[str, Any]:
        """§9.1 承認待ち list UI / §12.4 human approval の解決経路."""
        if decided_by not in RESOLVERS:
            msg = f"未Known の decided_by: {decided_by}"
            raise UnknownResolverError(msg)
        if self._broker.get_approval(approval_id) is None:
            msg = f"approval が見つかりません: {approval_id}"
            raise UnknownApprovalError(msg)
        record = await self._broker.resolve_permission(
            approval_id,
            approved=approved,
            decided_by=decided_by,
            feedback=feedback,
        )
        conversation_id = self._sessions.get(record.session_id)
        if conversation_id is not None and conversation_id in self._threads:
            self._audit.record(
                "approval_resolved" if approved else "approval_rejected",
                actor=decided_by,
                approval_id=approval_id,
                conversation_id=conversation_id,
                session_id=record.session_id,
                tool_name=record.action,
            )
        return record.to_dict()

    async def resolve_many(
        self, approval_ids: Iterable[str], *, approved: bool, decided_by: str = "policy"
    ) -> list[dict[str, Any]]:
        """session 断絶/timeout で pending 一括解決（§6.5 stale 処理）."""
        records = await self._broker.resolve_many(
            list(approval_ids), approved=approved, decided_by=decided_by
        )
        return [record.to_dict() for record in records]
