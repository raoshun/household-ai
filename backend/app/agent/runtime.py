"""API 層・サービス層がエージェント機能を使うための入口 (§7.2)。

Copilot SDK 由来の型は `provider.py` に閉じ込めたまま、Settings と `AgentProvider`
を組み合わせて、起動状態とセッション登録を管理する。`AgentProvider` はテストで
差し替えられるよう Protocol として受け取る。

メッセージ送信 (§7.2 send_message) とストリーミング (stream_message) は provider の
セッションが受け取る SDK イベントを `events.py` で正規化して返す。割り込みは
`abort_session` (§4 interrupt_and_restart と同じ入口)。
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Any

from ..config import Settings
from .events import AgentEvent, TurnResult
from .provider import (
    AgentProvider,
    AgentSessionError,
    AgentSessionHandle,
    CopilotAgentProvider,
    ProviderUnavailableError,
    SessionRequest,
)

__all__ = [
    "AgentHealth",
    "AgentRuntime",
    "AgentRuntimeError",
    "SessionNotFoundError",
]


class AgentRuntimeError(RuntimeError):
    """ランタイムが起動していない、または要求を満たせない。"""


class SessionNotFoundError(AgentRuntimeError):
    """登録されていないセッションが指定された。"""


@dataclass(slots=True, frozen=True)
class AgentHealth:
    """health_check の結果 (§7.2)。ライブな SDK 通信は行わない。"""

    ready: bool
    provider: str
    model: str | None = None
    sessions: int = 0
    detail: str | None = None


class AgentRuntime:
    """provider の寿命を管理し、アプリ側から見えるセッション表を保つ。"""

    def __init__(
        self,
        settings: Settings,
        provider: AgentProvider | None = None,
    ) -> None:
        self._settings = settings
        self._provider: AgentProvider = (
            provider if provider is not None else CopilotAgentProvider(settings)
        )
        self._handles: dict[str, AgentSessionHandle] = {}
        self._started = False

    # ------------------------------------------------------------------
    # 状態
    # ------------------------------------------------------------------
    @property
    def settings(self) -> Settings:
        return self._settings

    @property
    def started(self) -> bool:
        return self._started

    @property
    def session_ids(self) -> list[str]:
        return list(self._handles)

    def _require_started(self) -> None:
        if not self._started:
            raise AgentRuntimeError("AgentRuntime が start() されていません")

    def _require_handle(self, session_id: str) -> AgentSessionHandle:
        handle = self._handles.get(session_id)
        if handle is None:
            raise SessionNotFoundError(f"登録されていないセッション: {session_id}")
        return handle

    # ------------------------------------------------------------------
    # ライフサイクル
    # ------------------------------------------------------------------
    async def start(self) -> None:
        """provider を起動する。二重起動しても何もしない。"""
        if self._started:
            return
        try:
            await self._provider.start()
        except ProviderUnavailableError:
            # 原因 (モデル・ランタイム欠如) を隠さず引き継ぐ
            raise
        self._started = True

    async def stop(self) -> None:
        """開いているセッションを閉じて provider を止める (二度目は no-op)。"""
        if not self._started:
            return
        for session_id in list(self._handles):
            # 個別 close に失敗しても他の終了処理は止めない
            with contextlib.suppress(Exception):
                await self._provider.close_session(session_id)
        self._handles.clear()
        try:
            await self._provider.stop()
        finally:
            self._started = False

    async def __aenter__(self) -> AgentRuntime:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: Any,
    ) -> None:
        await self.stop()

    # ------------------------------------------------------------------
    # セッション (§7.2 create_session / close_session)
    # ------------------------------------------------------------------
    async def create_session(
        self,
        *,
        model: str | None = None,
        tools: Sequence[Any] = (),
        system_message: str | None = None,
    ) -> AgentSessionHandle:
        """会話用のセッションを作り、登録して返す。

        ツール policy (Custom Tool だけ許可・MCP/Connector 禁止) は provider と
        Settings 側で決める。ここで受け取る `tools` はアプリ側が用意する Custom
        Tool 限定 (§6.2)。
        """
        self._require_started()
        request = SessionRequest(
            model=model,
            tools=list(tools),
            system_message=system_message,
        )
        try:
            handle = await self._provider.create_session(request)
        except AgentSessionError as exc:
            raise AgentRuntimeError(str(exc)) from exc
        self._handles[handle.session_id] = handle
        return handle

    async def close_session(self, session_id: str) -> None:
        """セッションを終了し、登録から外す。"""
        self._require_handle(session_id)
        self._handles.pop(session_id, None)
        await self._provider.close_session(session_id)

    def snapshot(self, session_id: str) -> Any:
        """セッションの現状 (models・tools・skills)。登録されていれば安定的に読める。"""
        handle = self._require_handle(session_id)
        return handle.snapshot()

    # ------------------------------------------------------------------
    # メッセージ送信・ストリーミング (§7.2)
    # ------------------------------------------------------------------
    async def send_message(self, session_id: str, prompt: str) -> TurnResult:
        """1 ターン送って完成した応答を返す (§7.2 send_message)。"""
        self._require_started()
        handle = self._require_handle(session_id)
        return await handle.send(prompt)

    def stream_message(self, session_id: str, prompt: str) -> AsyncIterator[AgentEvent]:
        """1 ターンを event 単位で返す (§7.2 stream_message / SSE)。"""
        self._require_started()
        handle = self._require_handle(session_id)
        return handle.stream(prompt)

    async def abort_session(self, session_id: str) -> None:
        """実行中のターンを中断する (§4 abort / §7.2 interrupt)。"""
        self._require_started()
        handle = self._require_handle(session_id)
        await handle.abort()

    # ------------------------------------------------------------------
    # 疎通確認 (§7.2 health_check / list_models)
    # ------------------------------------------------------------------
    async def health_check(self) -> AgentHealth:
        """起動状態を返す。SDK へ問い合わせはしないので安定的に呼べる。"""
        return AgentHealth(
            ready=self._started,
            provider=self._settings.provider,
            model=self._settings.model,
            sessions=len(self._handles),
            detail=None if self._started else "start() されていません",
        )

    async def list_models(self) -> list[str]:
        """provider が利用できるモデル ID (§4 Phase 0 のモデル指定確認)。"""
        self._require_started()
        models = await self._provider.list_models()
        return [str(model) for model in models]
