"""エージェント層が外部へ渡す正規化済みイベント。

SDK のイベント型は ``app.agent`` の内側に閉じ込め、API 層・サービス層へは
このモデルだけを渡す（README 2.3 章「SDK 依存を全体に広げない」）。
"""

from __future__ import annotations

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


@dataclass(slots=True)
class TurnResult:
    """1 回の発話に対する最終結果。"""

    text: str
    aborted: bool
    tool_calls: list[str] = field(default_factory=list)
    events: list[AgentEvent] = field(default_factory=list)
