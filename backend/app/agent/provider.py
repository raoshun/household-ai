"""Copilot SDK 适応層 (README 2.3 / 7.2).

この module だけが Copilot SDK を import する。ここで使う API は インストール済み
`github-copilot-sdk==1.0.29` に対して `.spike/` の probe で実測確認したものだけ:

- `CopilotClient(mode=, base_directory=, log_level=, session_idle_timeout_seconds=)`
  / `start()` / `stop()` / `force_stop()` / `list_models() -> list[ModelInfo]` (`.id`)
- `create_session(...)` が受け取る kwargs: `on_event`, `on_permission_request`,
  `available_tools`, `tools`, `model`, `provider`, `enable_skills`,
  `skill_directories`, `disabled_skills`, `streaming`, `disabled_mcp_servers`,
  `infinite_sessions`, `system_message`, `enable_session_store`
  (1.4.0 にある `permission_handler`, `session_store_dir`, `sessions_dir`,
  `permission_mode`, `provider_options` は 1.0.29 では TypeError になるため使わない)
- `SessionEvent` の `type` (列挙, `.value` が wire 名) / `data` / `raw_type` / `id`
- 許可判断の戻り値は `PermissionDecisionApproveOnce()` /
  `PermissionDecisionReject(feedback=...)` (`PermissionRequestResult` は 1.4.0 由来)
- `session.abort()` / `session.disconnect()` (1.0.29 の `CopilotSession` に
  `close()` は無い) / `session.send()` / `session.session_id`
- 禁止既定は `ToolSet().add_custom("*")` (`add_builtin("*")` を入れない) で実証済み:
  内蔵 tool bash / create / edit / web_fetch 等は session.create の payload 上
  `disabledBuiltInTools` として 18 件が並び、Custom Tool と `builtin:skill` だけが残る
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from copilot import CopilotClient, Tool, ToolResult, ToolSet
from copilot.generated.rpc import (
    PermissionDecisionApproveOnce,
    PermissionDecisionReject,
)
from copilot.session import PermissionInvocation, PermissionRequest, SessionEvent

from ..config import Settings
from .events import (
    AgentEvent,
    TurnAccumulator,
    TurnResult,
    history_tail_start,
    wire_type,
)

logger = logging.getLogger(__name__)

_DENY_MESSAGE = "Custom Tool として登録されていない tool_name は利用できません。"
# 中断後に後続イベントを拾う猶予秒数 / send task の後始末を待つ上限秒数
# accumulator の interrupt grace (events._ABORT_GRACE_SECONDS = 2.0) より長く置く
_EVENT_POLL_SECONDS = 0.05  # queue / 完了待機を覗く間隔
_HISTORY_TAIL_SECONDS = 5.0  # history RPC 1 回の待機上限
_RECOVER_SETTLE_SECONDS = 3.0  # 中断した send 待機を止める上限
_SKILLS_TERMINAL_TYPES: frozenset[str] = frozenset(
    {"session.idle", "session.shutdown", "abort", "agent.interrupted"}
)

__all__ = [
    "AgentProvider",
    "AgentProviderError",
    "AgentSessionError",
    "AgentSessionHandle",
    "CopilotAgentProvider",
    "CopilotAgentSession",
    "CustomToolExecutionError",
    "HistoryRPCUnavailableError",
    "ProviderError",
    "ProviderHealth",
    "ProviderUnavailableError",
    "SessionEventHandler",
    "SessionRequest",
    "SessionSnapshot",
    "SkillInfo",
]


class ProviderUnavailableError(RuntimeError):
    """Provider を起動・利用できない (README 4 章 Phase 0 の検証でも使う)。"""


class ProviderError(ProviderUnavailableError):
    """SDK 境界の汎用エラー (§7.2 の ProviderError)。"""


class AgentProviderError(ProviderError):
    """Provider 契約に沿わない操作を要求した (§7.2 の AgentProviderError)。"""


class AgentSessionError(RuntimeError):
    """セッション単位の実行失敗。"""

    def __init__(self, session_id: str, detail: str) -> None:
        super().__init__(f"{session_id}: {detail}")
        self.session_id = session_id
        self.detail = detail


class HistoryRPCUnavailableError(RuntimeError):
    """送信完了後_required な history RPC が実行できないことを示す (§7.1)。"""

    def __init__(self, session_id: str, detail: str) -> None:
        super().__init__(f"セッション {session_id} の history RPC 失敗: {detail}")
        self.session_id = session_id
        self.detail = detail


class CustomToolExecutionError(RuntimeError):
    """Custom Tool 実行中の失敗 (§6.2)。"""

    def __init__(self, tool_name: str, detail: str) -> None:
        super().__init__(f"Custom Tool {tool_name} の実行に失敗しました: {detail}")
        self.tool_name = tool_name
        self.detail = detail


@dataclass(frozen=True, slots=True)
class SkillInfo:
    """SDK が返す skill の表示用スナップショット。"""

    name: str
    source: str
    enabled: bool
    user_invocable: bool
    path: str | None = None


@dataclass(frozen=True, slots=True)
class SessionSnapshot:
    """セッションの現状 (Skills 読み込み確認に使う)。"""

    session_id: str
    model: str | None
    skills: list[SkillInfo] = field(default_factory=list)
    tool_names: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class SessionRequest:
    """セッション作成要求 (§7.2)。provider が policy を決める。"""

    session_id: str | None = None
    model: str | None = None
    tools: Sequence[Any] = ()
    system_message: str | None = None


@dataclass(frozen=True, slots=True)
class ProviderHealth:
    """health_check 結果 (§7.2)."""

    ready: bool
    provider: str
    model: str | None = None
    sessions: int = 0
    detail: str | None = None


# `AgentRuntime.create_session(on_event=...)` が受け取る callback の型 (§7.2)。
SessionEventHandler = Callable[..., None]


@runtime_checkable
class AgentSessionHandle(Protocol):
    """アプリ側が使えるセッション操作 (§6.1)。"""

    @property
    def session_id(self) -> str: ...

    @property
    def model(self) -> str | None: ...

    async def send(self, prompt: str) -> TurnResult: ...

    def stream(self, prompt: str) -> AsyncIterator[AgentEvent]: ...

    async def interrupt(self) -> None: ...

    async def abort(self) -> None: ...

    def close(self) -> None: ...

    def snapshot(self) -> SessionSnapshot: ...


@runtime_checkable
class AgentProvider(Protocol):
    """Provider 契約。テストでは fake を差し替える (§7.2)。"""

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def create_session(self, request: SessionRequest) -> AgentSessionHandle: ...

    async def close_session(self, session_id: str) -> None: ...

    async def interrupt_session(self, session_id: str) -> None: ...

    def session_ids(self) -> list[str]: ...

    async def list_models(self) -> Sequence[str]: ...

    async def health_check(self) -> ProviderHealth: ...


def build_provider_config(settings: Settings) -> dict[str, Any] | None:
    """Settings から SDK の `provider` 引数を作る (BYOK のときのみ)。"""
    if not settings.byok:
        return None
    provider: dict[str, Any] = {
        "type": settings.byok_provider_type,
        "base_url": settings.byok_base_url,
    }
    if settings.api_key:
        provider["api_key"] = settings.api_key
    return provider


def build_client_kwargs(settings: Settings) -> dict[str, Any]:
    """`CopilotClient` へ渡す引数 (§4: idle timeout は Settings の値をそのまま使う)。

    認証は使わない (`use_logged_in_user=False`) ので、BYOK 設定だけで動く。
    """
    return {
        "mode": "empty",
        "base_directory": _absolute_path(settings.runtime_dir),
        "log_level": settings.log_level,
        "session_idle_timeout_seconds": settings.session_idle_timeout_seconds,
        "use_logged_in_user": False,
    }


ApprovalMode = Literal["ask", "auto"]
"""許可方針 (§2.5.3)。ask は SDK 判断へ委ね、auto は provider が自動承認する。"""

ToolHandlerCallback = Callable[[str, Mapping[str, Any]], Awaitable[Any]]
"""アプリ側 handler の形: `async def handler(tool_name, arguments)` (§6.2)。"""


@dataclass(frozen=True, slots=True)
class CustomToolDeclaration:
    """apps から provider へ渡す Custom Tool 宣言 (SDK 型を apps へ出さない §6.2 境界)。

    provider はこれを SDK の `copilot.Tool` 変換し、実行時は provider 経由で
    handler を呼ぶ (§2.5.1)。`parameters` は JSON Schema dict (1.0.29 フィールド)。
    """

    name: str
    description: str
    handler: ToolHandlerCallback | None = None
    parameters: Mapping[str, Any] | None = None
    approval_mode: ApprovalMode = "ask"


@dataclass(slots=True)
class CustomToolBinding:
    """Custom Tool 宣言と SDK Tool を結びつけ、実行回数と許可方針を持つ。"""

    declaration: CustomToolDeclaration
    sdk_tool: Any
    executions: int = 0
    """handler へ渡した呼び出し回数 (失敗を含む)。"""

    @property
    def name(self) -> str:
        return self.declaration.name

    @property
    def auto_approved(self) -> bool:
        return self.declaration.approval_mode == "auto"

    async def run(self, invocation: Any) -> Any:
        """SDK tool handler の入口。失敗は tool result として返す (§2.5.1)。

        SDK は handler を `ToolHandler = Callable[[ToolInvocation], ToolResult]` の
        形で **1 引数** (`copilot.tools.ToolInvocation`) で呼ぶ。apps 側 handler の
        `(tool_name, arguments)` 形へここで変換する (§6.2 境界)。

        §2.8 の「tool 実行失敗は Assistant Message を終端させない」に従い、例外は
        result 型失败 (`ToolResult(result_type='failure')`) へ変換する。
        """
        arguments = normalize_tool_arguments(
            getattr(invocation, "arguments", None), invocation
        )
        # executions は handler へ渡した呼び出し回数 (失敗を含む)。tool result の
        # 成否ではなく handler 到達を数える。
        self.executions += 1
        logger.info(
            "custom_tool_executed tool_name=%s arguments=%s", self.name, arguments
        )
        handler = self.declaration.handler
        if handler is None:
            return _tool_result("failure", f"Custom Tool {self.name} に handler が無い")
        try:
            outcome = handler(self.name, arguments)
            # 同期 handler も受け付ける (§6.2: async / sync 両対応)。
            result = await outcome if inspect.isawaitable(outcome) else outcome
        except Exception as exc:  # tool 失敗は event として apps へ返す (§2.8)
            logger.warning(
                "custom_tool_failed tool_name=%s error_type=%s error=%s",
                self.name,
                type(exc).__name__,
                exc,
                exc_info=True,
            )
            return _tool_result("failure", f"{type(exc).__name__}: {exc}")
        return _result_from_handler(result)


def normalize_tool_arguments(params: Any, invocation: Any = None) -> dict[str, Any]:
    """SDK が handler へ渡す引数を apps 用の dict へ正規化する (§6.2)。

    SDK の tool handler は `ToolInvocation` 1 個だけを受け取る (`ToolHandler`)。
    apps 側 handler は常に `(tool_name, arguments dict)` で受け取るため、ここで
    `invocation.arguments` (JSON 文字列を含む) を dict へ寄せる。
    """
    invocation_arguments: Any = None
    if isinstance(invocation, Mapping):
        invocation_arguments = invocation.get("arguments")
    else:
        invocation_arguments = getattr(invocation, "arguments", None)
    for candidate in (params, invocation_arguments):
        if candidate is None:
            continue
        if isinstance(candidate, Mapping):
            return {str(key): value for key, value in candidate.items()}
        model_dump = getattr(candidate, "model_dump", None)
        if callable(model_dump):
            dumped = model_dump()
            if isinstance(dumped, Mapping):
                return {str(key): value for key, value in dumped.items()}
    if params is None:
        return {}
    return {"value": params}


def _tool_result(status: str, text: str) -> ToolResult:
    return ToolResult(
        text_result_for_llm=text,
        result_type=status,  # type: ignore[arg-type]
        error=None if status == "success" else text,
    )


def _result_from_handler(result: Any) -> ToolResult:
    """apps handler の返り値を SDK の tool result へ変換する (§2.5.1)."""
    if isinstance(result, ToolResult):
        return result
    if isinstance(result, str):
        return _tool_result("success", result)
    if result is None:
        return _tool_result("success", "")
    try:
        return _tool_result(
            "success", json.dumps(result, ensure_ascii=False, default=str)
        )
    except (TypeError, ValueError):
        return _tool_result(
            "failure",
            f"tool result を JSON 化できません: {type(result).__name__}",
        )


def coerce_custom_tools(
    tools: Sequence[Any],
    *,
    session_id: str = "<未作成>",
) -> list[CustomToolBinding]:
    """Custom Tool 宣言を SDK の `copilot.Tool` 変換する (§6.2 / gating ②).

    1.0.29 の `create_session(tools=...)` は `copilot.Tool` を要求する。名前文字列は
    SDK 内部で `'str' object has no attribute 'defer'` になるためここで検知する。
    実行される handler は必ず provider 経由 (`CustomToolBinding.run`) なので、実行回数と
    失敗を §2.5 の contract として observe できる。
    """
    bindings: list[CustomToolBinding] = []
    names: set[str] = set()
    for index, tool in enumerate(tools):
        name, description, handler, parameters, approval_mode = _read_tool_fields(
            tool, index
        )
        if name in names:
            raise AgentSessionError(
                session_id, f"Custom Tool '{name}' が重複しています"
            )
        names.add(name)
        declaration = CustomToolDeclaration(
            name=name,
            description=description,
            handler=handler,
            parameters=parameters,
            approval_mode=approval_mode,
        )
        binding = CustomToolBinding(declaration=declaration, sdk_tool=None)
        binding.sdk_tool = Tool(
            name=name,
            description=description,
            handler=binding.run,
            parameters=dict(parameters) if parameters else None,
            skip_permission=declaration.approval_mode == "auto",
        )
        bindings.append(binding)
    return bindings


def _read_tool_fields(
    tool: Any, index: int
) -> tuple[
    str, str, ToolHandlerCallback | None, Mapping[str, Any] | None, ApprovalMode
]:
    """宣言オブジェクトから tool 定義フィールドを読み出し、契約を検証する。"""
    label = f"index {index} の {type(tool).__name__}"
    if isinstance(tool, str):
        raise AgentSessionError(
            "<未作成>",
            "Custom Tool を名前文字列では渡せません (copilot.Tool 相当の宣言が必要)",
        )
    name = str(getattr(tool, "name", getattr(tool, "tool_name", "")) or "").strip()
    if not name:
        raise AgentSessionError(
            "<未作成>", f"Custom Tool に name が無い (受け取った対象: {label})"
        )
    handler = getattr(tool, "handler", None) or getattr(tool, "_handler", None)
    if not callable(handler):
        raise AgentSessionError("<未作成>", f"Custom Tool '{name}' に handler が無い")
    description = str(getattr(tool, "description", "") or "")
    if not description:
        raise AgentSessionError(
            "<未作成>", f"Custom Tool '{name}' に description が無い"
        )
    parameters = getattr(tool, "parameters", None) or getattr(
        tool, "input_schema", None
    )
    mode = str(getattr(tool, "approval_mode", "ask") or "ask").lower()
    if mode not in ("ask", "auto"):
        raise AgentSessionError(
            "<未作成>", f"Custom Tool '{name}' の approval_mode が不正: {mode}"
        )
    return name, description, handler, parameters, mode  # type: ignore[return-value]


class CopilotAgentProvider:
    """Copilot SDK を使う Provider 実装。"""

    def __init__(
        self,
        settings: Settings,
        *,
        sdk_client_factory: Any | None = None,
    ) -> None:
        self._settings = settings
        self._client_factory = sdk_client_factory
        self._client: Any | None = None
        self._sessions: dict[str, CopilotAgentSession] = {}

    # ------------------------------------------------------------------
    # ライフサイクル
    # ------------------------------------------------------------------
    async def start(self) -> None:
        if self._client is not None:
            return
        settings = self._settings
        try:
            if self._client_factory is not None:
                client = self._client_factory()
            else:
                client = CopilotClient(**build_client_kwargs(settings))
            await client.start()
        except Exception as exc:
            raise ProviderUnavailableError(
                f"Copilot ランタイムを起動できません: {exc}"
            ) from exc
        self._client = client

    async def stop(self) -> None:
        for session in list(self._sessions.values()):
            with contextlib.suppress(Exception):
                await session.aclose()
        self._sessions.clear()
        client = self._client
        if client is None:
            return
        self._client = None
        try:
            await client.stop()
        except Exception:
            # stop 失敗でも force_stop で確実に落とす
            with contextlib.suppress(Exception):
                await client.force_stop()

    # ------------------------------------------------------------------
    # セッション
    # ------------------------------------------------------------------
    async def create_session(self, request: SessionRequest) -> CopilotAgentSession:
        client = self._client
        if client is None:
            raise AgentProviderError(
                "start() されていない provider ではセッションを作れません"
            )
        session = CopilotAgentSession(provider=self, request=request)
        await session.open(client)
        self._sessions[session.session_id] = session
        return session

    async def close_session(self, session_id: str) -> None:
        session = self._sessions.get(session_id)
        if session is None:
            raise AgentSessionError(session_id, "登録されていないセッション")
        self._sessions.pop(session_id, None)
        await session.aclose()

    async def interrupt_session(self, session_id: str) -> None:
        session = self._sessions.get(session_id)
        if session is None:
            raise AgentSessionError(session_id, "登録されていないセッション")
        await session.interrupt()

    def session_ids(self) -> list[str]:
        return list(self._sessions)

    def get_session(self, session_id: str) -> CopilotAgentSession:
        session = self._sessions.get(session_id)
        if session is None:
            raise AgentSessionError(session_id, "登録されていないセッション")
        return session

    # ------------------------------------------------------------------
    # 一覧・健全性
    # ------------------------------------------------------------------
    async def list_models(self) -> Sequence[str]:
        client = self._client
        if client is None:
            raise AgentProviderError("start() されていない provider では一覧できません")
        try:
            models = await client.list_models()
        except Exception as exc:
            # BYOK では runtime が models.list を認証なしで扱い JsonRpcError(-32603,
            # "Not authenticated") になる (1.0.29 で実測)。app は model を settings から
            # 選んでいるため BYOK では settings.model を返す。BYOK 以外の経路は認証後に
            # 一覧できる (Phase 1 で認証まわりを実装する)。
            if self._settings.byok:
                logger.warning(
                    "models.list は BYOK では失敗するため settings.model を返す: %s",
                    exc,
                )
                return [self._settings.model] if self._settings.model else []
            raise AgentProviderError(f"models.list に失敗: {exc}") from exc
        # 1.0.29 の list_models() は ModelInfo (.id) を返す
        return [str(getattr(model, "id", model)) for model in models]

    async def health_check(self) -> ProviderHealth:
        return ProviderHealth(
            ready=self._client is not None,
            provider=self._settings.provider,
            model=self._settings.model,
            sessions=len(self._sessions),
            detail=None if self._client is not None else "start() されていません",
        )


class CopilotAgentSession:
    """1 会話の session。ターン収集と許可 policy をここで行う (§6.1)。"""

    def __init__(
        self,
        *,
        provider: CopilotAgentProvider,
        request: SessionRequest,
    ) -> None:
        self._provider = provider
        self._settings = provider._settings
        self._request = request
        self._sdk_session: Any | None = None
        self._session_id: str = ""
        self._model: str | None = request.model or provider._settings.model
        self._queue: asyncio.Queue[SessionEvent] = asyncio.Queue()
        self._last_accumulator: TurnAccumulator | None = None
        self._skills: list[SkillInfo] = []
        self._tool_bindings: list[CustomToolBinding] = coerce_custom_tools(
            request.tools, session_id=request.session_id or "<未作成>"
        )
        self._tool_names: list[str] = [
            str(binding.sdk_tool.name) for binding in self._tool_bindings
        ]
        self._turn_lock = asyncio.Lock()
        self._cleanup_tasks: set[asyncio.Task[None]] = set()
        self._closed = False

    def _available_tool_policy(self) -> ToolSet:
        """session が使える tool を決める allow-list (§2.5.4 / gating ②)。

        spike 実測では `available_tools = ToolSet().add_custom("<registered 名>")`
        だけで内蔵 file/shell tool がすべて無効化された (session から `Disabled tools:
        bash, edit, grep, skill, ...` の info event が出る)。 そのため既定は
        registered 名で絞る deny-by-default とし、registered 名 session では wildcard
        を立てない。registered が 0 件の session と wildcard 検証
        (`HOUSEHOLD_TOOL_POLICY=wildcard`) だけが fallback を使う。
        """
        policy = ToolSet()
        if self._tool_names:
            for tool_name in self._tool_names:
                policy.add_custom(tool_name)
        if _policy_wildcard_enabled() or not self._tool_names:
            # registered が 0 件の session は allow-list を空にする (空 ToolSet の
            # 扱いは SDK 側で検証していない) ため、wildcard fallback で skill 参照だけ
            # 許す形に倒す。registered 名 session では wildcard を立てない。
            policy.add_custom("*")
        if self._settings.enable_skills:
            policy.add_builtin("skill")
        return policy

    # ------------------------------------------------------------------
    # 作成
    # ------------------------------------------------------------------
    async def open(self, client: Any) -> None:
        settings = self._settings
        available = self._available_tool_policy()
        kwargs: dict[str, Any] = {
            "on_event": self._handle_sdk_event,
            "on_permission_request": self._decide_permission,
            "available_tools": available,
            "streaming": True,
            "infinite_sessions": {"enabled": False},
            "disabled_mcp_servers": list(settings.disabled_mcp_servers),
            "enable_session_store": settings.session_store_enabled,
        }
        if settings.enable_skills:
            kwargs["enable_skills"] = True
            kwargs["skill_directories"] = [_absolute_path(settings.skills_dir)]
            if settings.disabled_skills:
                kwargs["disabled_skills"] = list(settings.disabled_skills)
        # session 実効の model を常に送る (既定 model は settings から拾う)
        if self._model:
            kwargs["model"] = self._model
        provider_config = build_provider_config(settings)
        if provider_config is not None:
            kwargs["provider"] = provider_config
        # `create_session` に渡すのは SDK の `Tool` 物件である (§6.2)。app 側の宣言や
        # 名前文字列をそのまま渡すと SDK 内部で AttributeError になる。
        if self._tool_bindings:
            kwargs["tools"] = [binding.sdk_tool for binding in self._tool_bindings]
        if self._request.system_message:
            # system_message は str ではなく dict を期待する (1.0.29 で AttributeError)
            kwargs["system_message"] = {
                "mode": "append",
                "content": self._request.system_message,
            }

        try:
            session = await client.create_session(**kwargs)
        except Exception as exc:
            raise AgentSessionError(
                self._request.session_id or "<未作成>",
                f"セッション作成に失敗: {exc}",
            ) from exc
        self._sdk_session = session
        self._session_id = str(session.session_id)

    # ------------------------------------------------------------------
    # 状態
    # ------------------------------------------------------------------
    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def model(self) -> str | None:
        return self._model

    @property
    def closed(self) -> bool:
        return self._closed

    def snapshot(self) -> SessionSnapshot:
        return SessionSnapshot(
            session_id=self._session_id,
            model=self._model,
            skills=list(self._skills),
            tool_names=list(self._tool_names),
        )

    # ------------------------------------------------------------------
    # 送信 (§6.1 send / stream)
    # ------------------------------------------------------------------
    async def send(self, prompt: str) -> TurnResult:
        """1 ターン送って完了まで待ち、応答本文・ツール実行・終了を返す。"""
        async for _ in self.stream(prompt):
            pass
        accumulator = self._last_accumulator
        if accumulator is None:  # pragma: no cover - stream が常に作る
            return TurnResult(text="", aborted=False, tool_calls=[], events=[])
        return accumulator.finalize()

    async def stream(self, prompt: str) -> AsyncIterator[AgentEvent]:
        """1 ターンを streaming し、`AgentEvent` を逐次 yield する。

        1.0.29 で turn 完了を待つ API は `send_and_wait(prompt, timeout=...)`。
        `send()` は message id を返すだけで完了を待たないため、完了待機とは別に
        `on_event` queue から逐次 yield する。完了 (または中断) を確認した後で
        history RPC `get_events()` を 1 回だけ実行し、streaming で拾えなかった
        後発 event を accumulator 経由で回収する (§7.1)。
        turn 終端 (terminal) は常に 1 件 yield する (§7.1 の guarantees)。
        """
        session = self._require_open()
        if self._closed:
            raise AgentSessionError(self._session_id, "閉じたセッションへの操作です")
        timeout = self._settings.turn_timeout_seconds
        async with self._turn_lock:
            accumulator = TurnAccumulator()
            self._last_accumulator = accumulator
            wait_task = asyncio.create_task(
                session.send_and_wait(prompt, timeout=timeout)
            )
            settled = False
            try:
                while True:
                    get_task: asyncio.Future[SessionEvent] = asyncio.ensure_future(
                        self._queue.get()
                    )
                    done, _ = await asyncio.wait(
                        {get_task, wait_task},
                        timeout=_EVENT_POLL_SECONDS,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if get_task in done:
                        app_event = accumulator.feed(get_task.result())
                        if app_event is not None:
                            yield app_event
                        continue
                    _cancel_pending_get(get_task)
                    if wait_task.done() and self._queue.empty():
                        break

                # 送信完了 (または中断) を確かめて interrupt -> tail 回収 (§7.1)
                failure = await self._settle_send_and_wait(
                    wait_task, session, accumulator
                )
                settled = True  # 以降の失敗は turn 中断としての回収を要求しない
                for event in self._drain_queue(accumulator):
                    yield event
                for event in await self._collect_history(accumulator):
                    yield event
                tail_event = self._finalize_terminal(accumulator, failure)
                if tail_event is not None:
                    yield tail_event
            except BaseException as exc:
                # 送信途中の中断 (§6.1: 中断 -> error -> interrupt -> history RPC)
                if not settled:
                    if isinstance(exc, GeneratorExit):
                        # close 中は await できないので回収は background task に委ねる
                        task = asyncio.create_task(
                            self._recover_interrupted_turn(
                                wait_task, session, accumulator
                            )
                        )
                        self._cleanup_tasks.add(task)  # 参照を保ち aclose() で待つ
                        task.add_done_callback(self._cleanup_tasks.discard)
                    else:
                        await self._recover_interrupted_turn(
                            wait_task, session, accumulator
                        )
                raise

    async def _settle_send_and_wait(
        self,
        wait_task: asyncio.Task[SessionEvent | None],
        session: Any,
        accumulator: TurnAccumulator,
    ) -> BaseException | None:
        """完了待機の結果を取得し、timeout には interrupt して中断を確定する。

        戻り値が None なら turn は正常完了。exception なら中断として扱う。
        SDK の TimeoutError は agent 作業を停止しないので `abort()` を明に呼ぶ。
        """
        try:
            final_event = await wait_task
        except asyncio.CancelledError:
            raise
        except TimeoutError as exc:
            await _interrupt_quietly(session)
            return exc
        except Exception as exc:
            await _interrupt_quietly(session)
            return exc
        if final_event is not None:
            # send_and_wait が返す確定応答は重複検出で 1 度だけ織り込む
            accumulator.feed(final_event)
        return None

    def _finalize_terminal(
        self, accumulator: TurnAccumulator, failure: BaseException | None
    ) -> AgentEvent | None:
        """turn 終端を 1 件保証する (§7.1)。失敗時は error を 1 件足す。"""
        if failure is None:
            if accumulator.terminal_event_seen():
                return None
            return accumulator.synthesize_idle()
        error_type = (
            "turn_timeout" if isinstance(failure, TimeoutError) else "send_failed"
        )
        # send 未完了の turn は常に partial として保持する (§7.1)
        accumulator.mark_aborted()
        if accumulator.terminal_event_seen() or accumulator.terminal_error_seen():
            return None  # SDK 側 (または history tail) の terminal をそのまま使う
        return accumulator.synthesize_error(
            error_type=error_type,
            error_message=f"ターンが {error_type} で完了しませんでした: {failure}",
        )

    def _drain_queue(self, accumulator: TurnAccumulator) -> list[AgentEvent]:
        """queue に残っている event を accumulator へ织り込んで返す。"""
        events: list[AgentEvent] = []
        while True:
            try:
                sdk_event = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            app_event = accumulator.feed(sdk_event)
            if app_event is not None:
                events.append(app_event)
        return events

    async def _collect_history(self, accumulator: TurnAccumulator) -> list[AgentEvent]:
        """history RPC を 1 回実行し、このターンの tail だけを accumulator で拾う。

        前ターンの event を再取り込みしないよう、このターンで id を確認済みの event
        以降だけを食べる (§7.1: tail 名を snapshot 名へ映射しない)。id が取得できない
        場合は history を信用しない (streaming で拾えた分に依存する)。
        """
        session = self._sdk_session
        get_events = getattr(session, "get_events", None)
        if not callable(get_events):
            raise HistoryRPCUnavailableError(
                self._session_id, "history RPC (get_events) を実行できません"
            )
        try:
            history = list(
                await asyncio.wait_for(get_events(), timeout=_HISTORY_TAIL_SECONDS)
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise HistoryRPCUnavailableError(
                self._session_id, f"history RPC 実行に失敗: {exc}"
            ) from exc
        tail_start = history_tail_start(
            history,
            seen_ids=accumulator.seen_ids(),
            seen_descriptors=accumulator.seen_descriptors(),
        )
        events: list[AgentEvent] = []
        for sdk_event in history[tail_start:]:
            app_event = accumulator.feed(sdk_event)
            if app_event is not None:
                events.append(app_event)
        return events

    async def _recover_interrupted_turn(
        self,
        wait_task: asyncio.Task[SessionEvent | None],
        session: Any,
        accumulator: TurnAccumulator,
    ) -> None:
        """送信途中で中断した turn を SDK 側で確定させ、tail を accumulator へ残す。

        SDK 作業が走り続ける のまま session を閉じない (§6.1: 送信済み operation
        からは interrupt して interrupt された turn として確定する)。history RPC 相当の
        状態確認は accumulator に tail として残し、session を閉じる (§7.1)。
        """
        _cancel_pending_get(wait_task)
        await asyncio.wait({wait_task}, timeout=_RECOVER_SETTLE_SECONDS)
        await _interrupt_quietly(session)
        self._drain_queue(accumulator)
        if accumulator.seen_ids():
            with contextlib.suppress(Exception):
                await self._collect_history(accumulator)
        if not accumulator.terminal_error_seen():
            accumulator.mark_aborted()
            accumulator.synthesize_error(
                error_type="interrupted",
                error_message="ターン送信が caller 側で中断されました",
            )

    # ------------------------------------------------------------------
    # 中断・終了 (§6.1)
    # ------------------------------------------------------------------
    async def interrupt(self) -> None:
        session = self._require_open()
        await session.abort()

    async def abort(self) -> None:
        await self.interrupt()

    def close(self) -> None:
        """同期 close (後方互換)。非同期終了は `aclose()`。"""
        self._closed = True
        session = self._sdk_session
        if session is None:
            return
        with contextlib.suppress(Exception):
            asyncio.get_running_loop().create_task(session.disconnect())

    async def aclose(self) -> None:
        self._closed = True
        # 回収中に history RPC が必要になるので、session を閉じる前に待つ (§7.1)
        pending = set(self._cleanup_tasks)
        self._cleanup_tasks.clear()
        if pending:
            await asyncio.wait(pending, timeout=_RECOVER_SETTLE_SECONDS)
        session = self._sdk_session
        self._sdk_session = None
        if session is None:
            return
        with contextlib.suppress(Exception):
            await session.disconnect()

    # ------------------------------------------------------------------
    # SDK イベント受信
    # ------------------------------------------------------------------
    def _handle_sdk_event(self, sdk_event: SessionEvent) -> None:
        """`create_session(on_event=...)` から呼ばれる同期 callback。"""
        wire = wire_type(
            getattr(sdk_event, "type", None), getattr(sdk_event, "raw_type", None)
        )
        if wire == "session.skills_loaded":
            self._collect_skills(getattr(sdk_event, "data", None))
        self._queue.put_nowait(sdk_event)

    def _collect_skills(self, data: Any) -> None:
        collected: list[SkillInfo] = []
        for skill in getattr(data, "skills", []) or []:
            collected.append(
                SkillInfo(
                    name=str(getattr(skill, "name", "")),
                    source=str(getattr(skill, "source", "")),
                    enabled=bool(getattr(skill, "enabled", False)),
                    user_invocable=bool(getattr(skill, "user_invocable", False)),
                    path=(
                        str(skill.path)
                        if getattr(skill, "path", None) is not None
                        else None
                    ),
                )
            )
        if collected:
            self._skills = collected

    # ------------------------------------------------------------------
    # 許可判断 (README 244 行の deny-by-default)
    # ------------------------------------------------------------------
    async def _decide_permission(
        self,
        request: PermissionRequest,
        invocation: PermissionInvocation,
    ) -> PermissionDecisionApproveOnce | PermissionDecisionReject:
        """registered Custom Tool だけ承認し、他は理由付きで拒否する (§2.5.4)。

        SDK は managed settings と無関係にこの handler を呼ぶ (1.0.29 では
        managed approval を使わない構成)。invocation は SDK が必ず渡す 2 番目の
        引数で、拒否時の feedback 送信には使わない。
        """
        tool_name = str(
            getattr(request, "tool_name", None)
            or getattr(invocation, "tool_name", None)
            or ""
        )
        if tool_name and tool_name in self._tool_names:
            # 承認も deny と同じ audit log を残す (Phase 1 の PermissionHandler port が
            # 要求する監査証跡)
            logger.info(
                "permission_approved session=%s tool=%s", self._session_id, tool_name
            )
            return PermissionDecisionApproveOnce()
        logger.info(
            "permission_denied session=%s tool=%s reason=not-registered",
            self._session_id,
            tool_name or "<unnamed>",
        )
        return PermissionDecisionReject(feedback=_DENY_MESSAGE)

    def _require_open(self) -> Any:
        if self._sdk_session is None:
            reason = (
                "閉じたセッションへの操作です"
                if self._closed
                else "セッションが開いていません"
            )
            raise AgentSessionError(self._session_id or "<未作成>", reason)
        return self._sdk_session


def _absolute_path(path: Path | str) -> str:
    """Settings の相対 path を SDK が要求する絶対 path へ変換する (§7.2)。"""
    return str(Path(path).expanduser().resolve())


def _policy_wildcard_enabled() -> bool:
    """registered 名 policy の検証のために wildcard へ戻す switch。

    本番 policy は registered 名 (§2.5.4)。spike / 検証では環境変数
    `HOUSEHOLD_TOOL_POLICY=wildcard` で `custom:*` に戻し、registered 名 policy と
    実行結果が変わらないことを確かめる (§6.3 の gating 検証)。
    """
    mode = os.environ.get("HOUSEHOLD_TOOL_POLICY", "registered").strip().lower()
    return mode == "wildcard"


def build_available_tools(settings: Settings) -> ToolSet:
    """設定から作る wildcard policy (`custom:*` + 内蔵 `skill`)。

    spike 実測 (`probe_evidence.py` / `probe4.py` の出力) で確認した形:
    `available_tools` に server wildcard `custom:*` (entries 形式の names 制限は custom
    server の分類と噛み合わない) を置き、内蔵 `skill` を個別許可すると、内蔵 file/shell
    tool と MCP は runtime 側で拒否される。sessions は registered 名で絞るため
    (`CopilotAgentSession._available_tool_policy`)、この wildcard は skills 専用 session
    などの fallback として使う。
    """
    available = ToolSet()
    available.add_custom("*")
    if settings.enable_skills:
        available.add_builtin("skill")
    return available


def _cancel_pending_get(task: asyncio.Future[Any]) -> None:
    """未完了の future を取り消し、取消例外を retrieved 扱いにして warning を出さない。

    `queue.get()` は取り消しても queue 内の event は失わない (§7.1)。
    """

    def _consume(pending: asyncio.Future[Any]) -> None:
        if pending.cancelled():
            return
        with contextlib.suppress(BaseException):
            pending.exception()

    task.add_done_callback(_consume)
    task.cancel()
    if task.done():
        _consume(task)


async def _interrupt_quietly(session: Any) -> None:
    with contextlib.suppress(Exception):
        await session.abort()
