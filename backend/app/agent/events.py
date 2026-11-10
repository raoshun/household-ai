"""エージェント層が外部へ渡す正規化済みイベント。

SDK のイベント型は ``app.agent`` の内側に閉じ込め、API 層・サービス層へは
このモデルだけを渡す（README 2.3 章「SDK 依存を全体に広げない」）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class AgentEventType(StrEnum):
    """UI や永続化へ渡すイベントの種類。"""

    DELTA = "delta"
    ASSISTANT_MESSAGE = "assistant_message"
    REASONING = "reasoning"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    PERMISSION_REQUEST = "permission_request"
    SESSION_ERROR = "session_error"
    SESSION_IDLE = "session_idle"


@dataclass(slots=True)
class AgentEvent:
    """1 件のセッションイベント。"""

    type: AgentEventType
    text: str | None = None
    tool_name: str | None = None
    tool_arguments: dict[str, Any] | None = None
    error_type: str | None = None
    error_message: str | None = None
    aborted: bool | None = None
    raw: dict[str, Any] = field(default_factory=dict)


_APP_TERMINAL_TYPES = frozenset(
    {AgentEventType.SESSION_IDLE, AgentEventType.SESSION_ERROR}
)


def is_terminal_event(event: AgentEvent) -> bool:
    """turn を閉じる event かなど (§7.1: 常に terminal な event で閉じる)."""
    return event.type in _APP_TERMINAL_TYPES


@dataclass(slots=True)
class TurnResult:
    """1 回の発話に対する最終結果。

    `events` は turn 中に yield した event を時系列で並べ、必ず terminal な event
    で閉じる。中断した turn では `partial_events` にも deadline までに完了した
    Assistant 応答・tool call event を保持する (§7.1)。
    """

    text: str
    aborted: bool
    tool_calls: list[str] = field(default_factory=list)
    events: list[AgentEvent] = field(default_factory=list)
    partial_events: list[AgentEvent] = field(default_factory=list)

    def include_partial_events(self, partial_events: list[AgentEvent]) -> TurnResult:
        """interrupt された error 応答へ partial を両方に包含させる (§7.1)。

        追加後も `events` は terminal な event で閉じ、`partial_events` の全 event を
        含む。返す results の件数は partial 以上であり、terminal が末尾を占める。
        """
        events = list(self.events)
        for event in partial_events:
            if not any(existing is event or existing == event for existing in events):
                events.append(event)
        terminal_indexes = [
            i for i, event in enumerate(events) if is_terminal_event(event)
        ]
        if terminal_indexes:
            last_terminal = events[terminal_indexes[-1]]
            events = [e for i, e in enumerate(events) if i not in set(terminal_indexes)]
            events.append(last_terminal)  # terminal は必ず 1 件・末尾 (§7.1)
        return TurnResult(
            text=self.text,
            aborted=self.aborted,
            tool_calls=list(self.tool_calls),
            events=events,
            partial_events=[event for event in events if not is_terminal_event(event)],
        )


# ---------------------------------------------------------------------------
# SDK イベント → AgentEvent 変換 (SDK 1.0.29 の wire 名に対応)
# ---------------------------------------------------------------------------

# 1.0.29 では `SessionEvent.type` は列挙、未知の wire 名は `raw_type` に入る。
_DELTA_TEXT_FIELDS: dict[str, str] = {"assistant.message_delta": "delta_content"}
_MESSAGE_TEXT_FIELDS: dict[str, str] = {"assistant.message": "content"}
_REASONING_TEXT_FIELDS: dict[str, str] = {
    "assistant.reasoning": "content",
    "assistant.reasoning_delta": "delta_content",
}
_TOOL_START_TYPES: frozenset[str] = frozenset(
    {"tool.execution_start", "external_tool.requested"}
)
_TOOL_RESULT_TYPES: frozenset[str] = frozenset(
    {"tool.execution_complete", "external_tool.completed"}
)
_INTERRUPT_TYPES: frozenset[str] = frozenset({"abort", "agent.interrupted"})
_SESSION_ERROR_TYPES: frozenset[str] = frozenset({"session.error", "session.shutdown"})
_IDLE_TYPES: frozenset[str] = frozenset({"session.idle", "assistant.idle"})
# `session.idle` をターン完了の正とし、中断後は後続イベントを短く待つ。
_ABORT_GRACE_SECONDS: float = 2.0
_RESULT_TEXT_FIELDS: tuple[str, ...] = (
    "text_result",
    "text",
    "content",
    "result",
    "message",
)

__all__ = [
    "AgentEvent",
    "AgentEventType",
    "TurnAccumulator",
    "TurnResult",
    "extract_event_text",
    "to_agent_event",
    "wire_type",
]


def wire_type(sdk_type: Any, raw_type: Any = None) -> str:
    """SDK イベントの wire 名 (`session.idle` など) を文字列で返す。"""
    if isinstance(sdk_type, str):
        return sdk_type
    value = getattr(sdk_type, "value", None)
    if isinstance(value, str):
        return value
    name = getattr(sdk_type, "name", None)
    if isinstance(name, str):
        return name.lower()
    if isinstance(raw_type, str):
        return raw_type
    return "unknown"


def _first_str(data: Any, *names: str) -> str | None:
    """dataclass または dict から最初の文字列値を取り出す。"""
    for name in names:
        value = data.get(name) if isinstance(data, dict) else getattr(data, name, None)
        if isinstance(value, str):
            return value
    return None


def _first_non_empty(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value:
            return value
    return None


def extract_event_text(sdk_type: str, data: Any) -> str | None:
    """payload から表示テキストを抽出する (1.4.0 世代の `text` 名に依存しない)。"""
    for fields in (_DELTA_TEXT_FIELDS, _MESSAGE_TEXT_FIELDS, _REASONING_TEXT_FIELDS):
        if sdk_type in fields:
            return _first_str(data, fields[sdk_type])
    if sdk_type == "system.message":
        return _first_str(data, "content")
    return None


def _tool_arguments(data: Any) -> dict[str, Any]:
    args = getattr(data, "arguments", None)
    if args is None and isinstance(data, dict):
        args = data.get("arguments")
    if isinstance(args, dict):
        return args
    if isinstance(args, str):
        return {"arguments": args}
    return {}


def _tool_result_text(data: Any) -> str | None:
    """`ToolExecutionCompleteData.result` 等の文字列を名前を問わず拾う。"""
    inner = getattr(data, "result", None)
    for candidate in (inner, data):
        direct = _first_str(candidate, *_RESULT_TEXT_FIELDS)
        if direct is not None:
            return direct
    return None


def _error_message(sdk_type: str, data: Any) -> str | None:
    if sdk_type in _INTERRUPT_TYPES:
        action = _first_non_empty(getattr(data, "action", None)) or "interrupted"
        return f"{action} ({sdk_type})"
    return _first_non_empty(
        getattr(data, "message", None),
        getattr(data, "reason", None),
        sdk_type,
    )


def _fill_delta(event: AgentEvent, name: str, data: Any) -> None:
    event.text = _first_str(data, _DELTA_TEXT_FIELDS[name])


def _fill_message(event: AgentEvent, name: str, data: Any) -> None:
    # 空の最終応答は本文と見なさない (BYOK で多く観測された)
    content = _first_str(data, _MESSAGE_TEXT_FIELDS[name])
    event.text = content if content else None
    message_id = getattr(data, "message_id", None)
    if isinstance(message_id, str):
        event.raw["message_id"] = message_id


def _fill_reasoning(event: AgentEvent, name: str, data: Any) -> None:
    event.text = _first_str(data, _REASONING_TEXT_FIELDS[name])


def _fill_tool_call(event: AgentEvent, name: str, data: Any) -> None:
    event.tool_name = _first_non_empty(getattr(data, "tool_name", None))
    event.tool_arguments = _tool_arguments(data)


def _fill_tool_result(event: AgentEvent, name: str, data: Any | None) -> None:
    event.tool_name = _first_non_empty(getattr(data, "tool_name", None))
    event.tool_arguments = _tool_arguments(data)
    event.error_message = _tool_result_text(data)
    if getattr(data, "success", True) is False:
        event.error_type = "tool_failed"


def _fill_permission(event: AgentEvent, name: str, data: Any) -> None:
    request = getattr(data, "request", None)
    source = request if request is not None else data
    event.tool_name = _first_non_empty(
        getattr(source, "tool_name", None), getattr(data, "tool_name", None)
    )
    event.tool_arguments = _tool_arguments(source)


def _fill_session_error(event: AgentEvent, name: str, data: Any) -> None:
    if name in _INTERRUPT_TYPES:
        event.aborted = True
    event.error_type = name
    event.error_message = _error_message(name, data)


def _fill_session_idle(event: AgentEvent, name: str, data: Any) -> None:
    return None


_BASE_BY_SDK_TYPE: dict[str, AgentEventType] = {
    **{name: AgentEventType.DELTA for name in _DELTA_TEXT_FIELDS},
    **{name: AgentEventType.ASSISTANT_MESSAGE for name in _MESSAGE_TEXT_FIELDS},
    **{name: AgentEventType.REASONING for name in _REASONING_TEXT_FIELDS},
    **{name: AgentEventType.TOOL_CALL for name in _TOOL_START_TYPES},
    **{name: AgentEventType.TOOL_RESULT for name in _TOOL_RESULT_TYPES},
    **{name: AgentEventType.SESSION_ERROR for name in _INTERRUPT_TYPES},
    **{name: AgentEventType.SESSION_ERROR for name in _SESSION_ERROR_TYPES},
    **{name: AgentEventType.SESSION_IDLE for name in _IDLE_TYPES},
    "permission.requested": AgentEventType.PERMISSION_REQUEST,
}

_FILLERS: dict[AgentEventType, Any] = {
    AgentEventType.DELTA: _fill_delta,
    AgentEventType.ASSISTANT_MESSAGE: _fill_message,
    AgentEventType.REASONING: _fill_reasoning,
    AgentEventType.TOOL_CALL: _fill_tool_call,
    AgentEventType.TOOL_RESULT: _fill_tool_result,
    AgentEventType.PERMISSION_REQUEST: _fill_permission,
    AgentEventType.SESSION_ERROR: _fill_session_error,
    AgentEventType.SESSION_IDLE: _fill_session_idle,
}


def to_agent_event(
    sdk_type: Any,
    *,
    data: Any = None,
    raw_type: Any = None,
) -> AgentEvent | None:
    """SDK イベントを 1 件の `AgentEvent` へ変換する。不要なら None。"""
    name = wire_type(sdk_type, raw_type)
    base = _BASE_BY_SDK_TYPE.get(name)
    if base is None:
        return None
    event = AgentEvent(type=base)
    event.raw["sdk_type"] = name
    sdk_name = getattr(sdk_type, "name", None)
    if isinstance(sdk_name, str):
        event.raw["sdk_event_name"] = sdk_name
    _FILLERS[base](event, name, data)
    return event


def history_tail_start(
    history: list[Any],
    *,
    seen_ids: set[str],
    seen_descriptors: set[str] = frozenset(),
) -> int:
    """history のうち、このターンの tail 先頭 index を返す (§7.1)。

    境界はこのターンで取り込んだ event の直後。前ターンの event を再取り込みしない
    ことが優先なので、境界を特定できない場合は tail を食べない (history を信用しない)。
    """

    def matches(event: Any) -> bool:
        event_id = getattr(event, "id", None)
        if event_id is not None and str(event_id) in seen_ids:
            return True
        if seen_descriptors:
            data = getattr(event, "data", None)
            raw_type = getattr(event, "raw_type", None)
            mapped = to_agent_event(
                getattr(event, "type", None), data=data, raw_type=raw_type
            )
            return mapped is not None and _describe(mapped) in seen_descriptors
        return False

    boundary: int | None = None
    for index, event in enumerate(history):
        if matches(event):
            boundary = index
    return len(history) if boundary is None else boundary + 1


def _describe(event: AgentEvent) -> str:
    """重複検出に使う短い正規化キー (本文・ツール名・エラー内容)。"""
    parts = [event.type.value]
    if event.text:
        parts.append(repr(event.text))
    if event.tool_name:
        parts.append(f"tool={event.tool_name}")
    if event.error_message:
        parts.append(f"error={event.error_message}")
    return "|".join(parts)


class TurnAccumulator:
    """1 ターンの SDK イベントから応答本文・ツール実行・終了を組み立てる。

    終了は `session.idle` を正とし、中断を受けた場合は後続イベントを short grace
    だけ待ってから終える。 deltas の `message_id` は per-part で確定応答と一致しない
    ため、本文は逐次の順序で組み立てて重複だけを排除する。
    """

    def __init__(
        self,
        *,
        max_events: int = 1_000,
        abort_grace: float = _ABORT_GRACE_SECONDS,
    ) -> None:
        if max_events < 1:
            raise ValueError("max_events は 1 以上である必要があります")
        self._max_events = max_events
        self._abort_grace = max(0.0, float(abort_grace))
        self._events: list[AgentEvent] = []
        self._seen: set[str] = set()
        self._tool_calls: list[str] = []
        self._segments: list[str] = []
        self._pending_delta: list[str] = []
        self._aborted = False
        self._interrupted_at: float | None = None
        self._terminal = False
        self._terminal_error = False
        self._seen_ids: set[str] = set()
        self._seen_descriptors: set[str] = set()

    # ------------------------------------------------------------------
    # 公開 API
    # ------------------------------------------------------------------
    def feed(self, sdk_event: Any) -> AgentEvent | None:
        """SDK イベント (または `AgentEvent`) を 1 件受け取る。"""
        if isinstance(sdk_event, AgentEvent):
            event = sdk_event
            name = str(sdk_event.raw.get("sdk_type") or sdk_event.type.value)
            data: Any = None
        else:
            data = getattr(sdk_event, "data", None)
            raw_type = getattr(sdk_event, "raw_type", None)
            name = wire_type(getattr(sdk_event, "type", None), raw_type)
            event = to_agent_event(
                getattr(sdk_event, "type", name),
                data=data,
                raw_type=raw_type,
            )

        self._absorb(name, data, sdk_event)
        if event is None:
            return None

        # 1.0.29 の event id は UUID。id があれば id で、無ければ内容で重複を見る
        event_id = getattr(sdk_event, "id", None)
        key = f"id:{event_id}" if event_id is not None else _describe(event)
        if key in self._seen:
            return None
        self._seen.add(key)
        if event_id is not None:
            self._seen_ids.add(str(event_id))
        self._seen_descriptors.add(_describe(event))

        if len(self._events) < self._max_events:
            self._events.append(event)
        return event

    def is_terminal(self) -> bool:
        """ターン完了条件を満たしたかを返す。

        `session.idle` (または `session.shutdown`) を正とする。中断後は
        `agent.interrupted` 等在室を待ってから猶予切れで終える。
        """
        if self._terminal:
            return True
        if self._aborted and self._interrupted_at is not None:
            return (time.monotonic() - self._interrupted_at) >= self._abort_grace
        return False

    def terminal_event_seen(self) -> bool:
        """wire 側の turn 終端 (`session.idle` / `session.shutdown`) を拾ったか。

        `is_terminal()` は interrupt grace でも True になるため、SDK が本当に turn
        終端を送ったかを区別したい provider はこちらを使う。
        """
        return self._terminal

    def partial_events(self) -> list[AgentEvent]:
        """deadline までに完了した partial 応答 (terminal event を除く, §7.1)。"""
        return [event for event in self._events if not is_terminal_event(event)]

    def finalize(self) -> TurnResult:
        """収集結果を `TurnResult` へ確定する (ターン完了語に呼ぶ)。"""
        result = self._result()
        if result.aborted:
            result = result.include_partial_events(self.partial_events())
        return result

    def partial_result(self) -> TurnResult:
        """待ち切れの場合に現在の状態だけで結果を組み立てる (タイムアウト用)。"""
        result = self._result()
        if result.aborted:
            result = result.include_partial_events(self.partial_events())
        return result

    def snapshot(self) -> list[AgentEvent]:
        """これまでに写像した `AgentEvent` を返す (provider 側の再送用)。"""
        return list(self._events)

    def tool_calls(self) -> list[str]:
        """このターンで開始されたCustom Tool / 内蔵ツールの名前。"""
        return list(self._tool_calls)

    def aborted(self) -> bool:
        return self._aborted

    def terminal_error_seen(self) -> bool:
        """turn を閉じる error 系 event (`session.error` / 中断系) を拾ったか。"""
        return self._terminal_error

    def seen_ids(self) -> set[str]:
        """このターンで取り込んだ event の id (history tail 境界用, §7.1)。"""
        return set(self._seen_ids)

    def seen_descriptors(self) -> set[str]:
        """id が無い event の内容キー (history tail 境界の fallback)。"""
        return set(self._seen_descriptors)

    def synthesize_idle(self) -> AgentEvent:
        """SDK が turn 終端を送らなかった Success とみなす terminal を 1 件補う (§7.1)。

        本文は足さない。wire 側の終端確認 (`terminal_event_seen()`) は True にしない。
        """
        return self._append_synthetic(
            AgentEventType.SESSION_IDLE,
            error_type=None,
            error_message=None,
            aborted=self._aborted,
        )

    def synthesize_error(self, *, error_type: str, error_message: str) -> AgentEvent:
        """provider 検知の失敗で turn を閉じる error event を 1 件補う (§7.2)。"""
        self._aborted = True
        self._interrupted_at = time.monotonic()
        return self._append_synthetic(
            AgentEventType.SESSION_ERROR,
            error_type=error_type,
            error_message=error_message,
            aborted=True,
        )

    def mark_aborted(self) -> None:
        """アプリ側が割り込んだ (例: ターン timeout で abort) ことを記録する。"""
        if not self._aborted:
            self._aborted = True
            self._interrupted_at = time.monotonic()

    def _append_synthetic(
        self,
        event_type: AgentEventType,
        *,
        error_type: str | None,
        error_message: str | None,
        aborted: bool,
    ) -> AgentEvent:
        key = f"synthetic:{event_type.value}:{error_type or ''}"
        event = AgentEvent(
            type=event_type,
            error_type=error_type,
            error_message=error_message,
            aborted=aborted,
            raw={"sdk_event_name": "provider:synthetic", "source": "provider"},
        )
        if key in self._seen:
            return event
        self._seen.add(key)
        if len(self._events) < self._max_events:
            self._events.append(event)
        return event

    # ------------------------------------------------------------------
    # 内部処理
    # ------------------------------------------------------------------
    def _absorb(self, name: str, data: Any, sdk_event: Any) -> None:
        """イベントを内部状態 (本文・ツール実行・終了) へ織り込む。"""
        text = extract_event_text(name, data)
        if name in _DELTA_TEXT_FIELDS:
            if text:
                self._pending_delta.append(text)
        elif name in _MESSAGE_TEXT_FIELDS:
            if text:
                self._absorb_message(text)
        elif name in _TOOL_START_TYPES:
            tool_name = _first_non_empty(
                getattr(data, "tool_name", None), getattr(sdk_event, "tool_name", None)
            )
            if tool_name and tool_name not in self._tool_calls:
                self._tool_calls.append(tool_name)

        if name in _INTERRUPT_TYPES:
            self._aborted = True
            self._interrupted_at = time.monotonic()
            self._terminal_error = True
        elif name == "session.error":
            self._aborted = True
            self._terminal_error = True
        elif name == "session.shutdown" or name == "session.idle":
            self._terminal = True

    def _absorb_message(self, content: str) -> None:
        """確定応答を deltas と突き合わせて 1 度だけ本文へ加える。"""
        self._flush_pending()
        self._add_segment(content, compare_pending=True)

    def _flush_pending(self) -> None:
        if not self._pending_delta:
            return
        merged = "".join(self._pending_delta)
        self._pending_delta.clear()
        self._add_segment(merged, compare_pending=False)

    def _add_segment(self, text: str, *, compare_pending: bool) -> None:
        if not text:
            return
        already_flowing = text in self._segments or text in "".join(self._segments)
        if compare_pending and already_flowing:
            return
        if not compare_pending and already_flowing:
            return
        self._segments.append(text)

    def _result(self) -> TurnResult:
        """accumulator 時点の応答を作る。partial を载せるのは中断した turn だけ。"""
        self._flush_pending()
        aborted = self._aborted
        return TurnResult(
            text="\n\n".join(self._segments),
            aborted=aborted,
            tool_calls=list(self._tool_calls),
            events=list(self._events),
            partial_events=self.partial_events() if aborted else [],
        )
