"""API 層・サービス層がエージェント機能を使うための入口 (§7.2).

Copilot SDK 由来の型は `provider.py` に閉じ込めたまま、Settings と
`AgentProvider` を組み合わせて、起動状態とセッション登録を管理する。
`AgentProvider` はテストで差し替えられるよう Protocol として受け取る。

メッセージ送信・ストリーミング (§7.2 の send_message / stream_message) は
ターン収集とイベント正規化の実装と同時にこのクラスへ加える。
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import Any

from ..config import Settings
from .provider import (
    AgentProvider,
    AgentSessionHandle,
    CopilotAgentProvider,
    ProviderUnavailableError,
    SessionEventHandler,
)

__all__ = ["AgentHealth", "AgentRuntime", "AgentRuntimeError"]


class AgentRuntimeError(RuntimeError):
    """ランタイムが起動していない、または対象セッションが見つからない。"""


@dataclass(slots=True, frozen=True)
class AgentHealth:
    """health_check の結果 (§7.2). ライブな SDK 通信は行わない。"""

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
        self._sessions: dict[str, AgentSessionHandle] = {}
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
        return list(self._sessions)

    def _require_started(self) -> None:
        if not self._started:
            raise AgentRuntimeError("AgentRuntime が start() されていません")

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
        # 個別 close に失敗しても他の終了処理は止めない
        for session_id in list(self._sessions):
            with contextlib.suppress(Exception):
                await self.close_session(session_id)
        self._sessions.clear()
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
        on_event: SessionEventHandler,
        tools: list[Any] | None = None,
        system_message: str | None = None,
    ) -> AgentSessionHandle:
        """会話用のセッションを作り、登録して返す。

        ツール policy (Tools・custom tool 名・MCP/Connector 禁止) は provider と
        Settings 側で決める。UI 由来のツールだけを here で追加する (§6.2)。
        """
        self._require_started()
        handle = await self._provider.create_session(
            on_event=on_event,
            tools=tools,
            system_message=system_message,
        )
        self._sessions[handle.session_id] = handle
        return handle

    async def close_session(self, session_id: str) -> None:
        """セッションを登録から外して閉じる。

        §7.2 に合わせて非同期 API だが、Phase 0 のハンドル close はイベント購読を
        外すだけ (アイドル timeout 待ちを避めるため abort は Phase 1 で組み込む)。
        """
        handle = self._sessions.pop(session_id, None)
        if handle is None:
            raise AgentRuntimeError(f"登録されていないセッション: {session_id}")
        handle.close()

    # ------------------------------------------------------------------
    # 疎通確認 (§7.2 health_check)
    # ------------------------------------------------------------------
    async def health_check(self) -> AgentHealth:
        """起動状態を返す。SDK へ問い合わせはしないので安定的に呼べる。"""
        return AgentHealth(
            ready=self._started,
            provider=self._settings.provider,
            model=self._settings.model,
            sessions=len(self._sessions),
            detail=None if self._started else "start() されていません",
        )

    async def list_models(self) -> list[str]:
        """provider が利用できるモデル ID (§4 Phase 0 のモデル指定確認)。"""
        self._require_started()
        return await self._provider.list_models()
