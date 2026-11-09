"""Copilot SDK への依存をこの package 内に閉じる。

仕様書 §2.3 「Copilot SDKへの依存をアプリケーション全体に広げない」および
§7.2 「Copilot SDKの実際のAPIをそのまま抽象インターフェースに流用せず」に対応する。

`provider.py` が SDK を import するのはこの package 内だけであるべきで、
`api/`・`services/` は `AgentEvent` / `AgentSessionHandle` だけを扱う。
"""

from __future__ import annotations

from .events import AgentEvent, AgentEventType, TurnResult
from .provider import AgentProvider, CopilotAgentProvider, ProviderUnavailableError
from .runtime import AgentRuntime, AgentRuntimeError

__all__ = [
    "AgentEvent",
    "AgentEventType",
    "AgentProvider",
    "AgentRuntime",
    "AgentRuntimeError",
    "CopilotAgentProvider",
    "ProviderUnavailableError",
    "TurnResult",
]
