"""GitHub Copilot SDK を扱う層。

README 2.3 章の方針に従い、``copilot`` パッケージの import はこのモジュール
（``app.agent`` パッケージ内）だけに限定する。API 層・サービス層はこのモジュール
が返すハンドルと正規化済みイベントだけを使う。
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from copilot import CopilotClient, ToolSet
from copilot.session import (
    PermissionHandler,
    PermissionInvocation,
    PermissionRequest,
    PermissionRequestResult,
    SessionEvent,
)

from ..config import GITHUB_MCP_SERVER_NAME, Settings
from .events import AgentEvent

SessionEventHandler = Callable[[SessionEvent], None]


class ProviderUnavailableError(RuntimeError):
    """プロバイダー・モデル・ランタイムが利用できない場合のエラー。"""


class AgentSessionHandle(Protocol):
    """1 回の会話に紐づくセッションハンドル。"""

    session_id: str

    async def send(self, prompt: str) -> None: ...

    async def abort(self) -> None: ...


@dataclass(slots=True)
class AgentSession:
    """SDK セッションをラップしたハンドル。"""

    session_id: str
    _session: Any
    _detach: Callable[[], None] = field(repr=False)

    async def send(self, prompt: str) -> None:
        await self._session.send(prompt)

    async def abort(self) -> None:
        await self._session.abort()

    def close(self) -> None:
        """イベント購読を外す（SDK のセッション自体は abort で止める）。"""
        self._detach()


class AgentProvider(Protocol):
    """エージェント基盤を提供するインターフェース。"""

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def create_session(
        self,
        *,
        on_event: SessionEventHandler,
        tools: list[Any] | None = None,
        custom_tool_names: list[str] | None = None,
        system_message: str | None = None,
    ) -> AgentSession: ...

    async def list_models(self) -> list[str]: ...


def build_provider_config(settings: Settings) -> dict[str, Any] | None:
    """Settings から SDK の ``provider`` 引数を作る。

    BYOK（Ollama / OpenAI / Anthropic）の場合はプロバイダー設定を返し、
    Copilot 既定（BYOK なし）の場合は ``None`` を返す。
    """
    if not settings.byok:
        return None
    provider: dict[str, Any] = {
        "type": settings.byok_provider_type,
        "base_url": settings.byok_base_url,
    }
    if settings.api_key:
        provider["api_key"] = settings.api_key
    return provider


class CopilotAgentProvider:
    """GitHub Copilot SDK を使うエージェント基盤。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: CopilotClient | None = None
        self._sessions: list[AgentSession] = []

    # ------------------------------------------------------------------
    # ライフサイクル
    # ------------------------------------------------------------------
    async def start(self) -> None:
        if self._client is not None:
            return
        settings = self._settings
        try:
            client = CopilotClient(
                mode="empty",
                base_directory=str(settings.runtime_dir),
                log_level=settings.log_level,
                session_idle_timeout_seconds=settings.session_idle_timeout_seconds,
                use_logged_in_user=False,
            )
        except Exception as exc:  # pragma: no cover - 初期化失敗は稀
            raise ProviderUnavailableError(
                f"Copilot ランタイムを初期化できません: {exc}"
            ) from exc
        try:
            await client.start()
        except Exception as exc:
            raise ProviderUnavailableError(
                f"Copilot ランタイムを起動できません: {exc}"
            ) from exc
        self._client = client

    async def stop(self) -> None:
        # 後始末は最優先しない: 個別の close 失敗で止まらず他に続ける
        for session in list(self._sessions):
            with contextlib.suppress(Exception):
                session.close()
        # SDK セッション自体の close は Phase 1 (SessionManager) で扱う
        self._sessions.clear()
        if self._client is not None:
            try:
                await self._client.stop()
            finally:
                self._client = None

    # ------------------------------------------------------------------
    # セッション
    # ------------------------------------------------------------------
    async def create_session(
        self,
        *,
        on_event: SessionEventHandler,
        tools: list[Any] | None = None,
        custom_tool_names: list[str] | None = None,
        system_message: str | None = None,
    ) -> AgentSession:
        client = self._client
        if client is None:
            raise ProviderUnavailableError("Provider が start() されていません")
        settings = self._settings
        provider_config = build_provider_config(settings)
        if settings.byok and provider_config is None:  # pragma: no cover
            raise ProviderUnavailableError("BYOK プロバイダー設定を作れません")

        available_tools = ToolSet().add_custom("*")
        if settings.enable_skills:
            available_tools = available_tools.add_builtin("skill")

        kwargs: dict[str, Any] = {
            "on_permission_request": _permission_policy,
            "available_tools": available_tools,
            "working_directory": str(settings.data_dir.parent),
            "disabled_mcp_servers": list(settings.disabled_mcp_servers),
            "infinite_sessions": {"enabled": False},
            "enable_session_store": settings.session_store_enabled,
            "streaming": True,
        }
        if settings.model:
            kwargs["model"] = settings.model
        if provider_config is not None:
            kwargs["provider"] = provider_config
        if settings.enable_skills:
            kwargs["skill_directories"] = [str(settings.skills_dir)]
            kwargs["enable_skills"] = True
        if tools:
            kwargs["tools"] = tools
        if custom_tool_names:
            kwargs["custom_tool_names"] = custom_tool_names
        if system_message:
            kwargs["system_message"] = {"mode": "append", "content": system_message}

        try:
            session = await client.create_session(**kwargs)
        except Exception as exc:
            raise ProviderUnavailableError(
                f"セッションを作成できません: {exc}"
            ) from exc

        detach = session.on(on_event)
        handle = AgentSession(
            session_id=session.session_id,
            _session=session,
            _detach=detach,
        )
        self._sessions.append(handle)
        return handle

    async def list_models(self) -> list[str]:
        if self._client is None:
            raise ProviderUnavailableError("Provider が start() されていません")
        try:
            models = await self._client.list_models()
        except Exception as exc:
            raise ProviderUnavailableError(
                f"モデル一覧を取得できません: {exc}"
            ) from exc
        return [m.id for m in models]

    # ------------------------------------------------------------------
    # 検証用ヘルパー
    # ------------------------------------------------------------------
    async def probe(self, prompt: str, timeout: float = 180.0) -> str:
        """BYOK 疎通確認のための 1 ターン実行。"""
        queue: asyncio.Queue[AgentEvent] = asyncio.Queue()

        def handler(event: SessionEvent) -> None:
            queue.put_nowait(AgentEvent(type=AgentEventTypeRaw(event)))

        session = await self.create_session(on_event=handler)
        try:
            await session.send(prompt)
            return await _drain_until_idle(queue, timeout)
        finally:
            session.close()


def AgentEventTypeRaw(event: SessionEvent) -> Any:  # pragma: no cover - probe 用
    return event.type


async def _drain_until_idle(
    queue: asyncio.Queue[AgentEvent],
    timeout: float,
) -> str:  # pragma: no cover - probe 用
    parts: list[str] = []
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise TimeoutError("セッションが idle になりません")
        event = await asyncio.wait_for(queue.get(), timeout=remaining)
        if event.type == "assistant_message" and event.text:
            parts.append(event.text)
        if event.type == "session_idle":
            break
    return "".join(parts)


def _permission_policy(
    request: PermissionRequest,
    invocation: PermissionInvocation,
) -> PermissionRequestResult | Awaitable[PermissionRequestResult]:
    """組み込みのファイル・シェル系ツールは許可しない方針。

    家庭内ルール用のカスタムツールは ``skip_permission=True`` で定義するため、
    ここへは到達しない。到達した場合は安全側に倒して拒否する。
    """
    return PermissionHandler.approve_all(request, invocation)


def default_disabled_mcp_servers() -> list[str]:
    """常に無効化する組み込み MCP サーバー名。"""
    return [GITHUB_MCP_SERVER_NAME]
