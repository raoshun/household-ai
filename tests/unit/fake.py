"""SDK を模した fake.

github-copilot-sdk 1.0.29 の実測挙動を模倣する:

* ``session.send(prompt)`` は id を返すだけで完了は待たない。待機は
  ``session.send_and_wait(prompt, timeout=...)`` が行い、待機中に ``on_event``
  経由でイベントを送出し、terminal イベント (通常は ``session.idle``) を返す。
  期限までに terminal が来ないと :exc:`TimeoutError` を返し、実行中の作業は
  止めない (provider 側が ``abort()`` する)。
* Custom Tool は ``session.send_tool_call(...)`` で実行し、返り値の
  ``.result()`` に ``{"results": [{"text_result_for_llm": ...}]}`` が戻る。
* permission 判定は ``PermissionDecision*`` (ApproveOnce / Reject) を返す。
* 終端後の補完取得は ``session.get_events()`` (history RPC)。切断は ``disconnect()``。
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

__all__ = [
    "Data",
    "FakeClient",
    "FakeSdkSession",
    "ModelInfo",
    "PermissionRequest",
    "RecordingToolHandler",
    "SdkEvent",
    "SdkToolInvocation",
]


class Data:
    """SDK の events()/to_dict() が返す様なマッピング."""

    def __init__(self, **kwargs: Any) -> None:
        self.__dict__.update(kwargs)

    def __getattr__(self, name: str) -> Any:
        raise AttributeError(name)

    def __repr__(self) -> str:
        inner = ", ".join(f"{k}={v!r}" for k, v in self.__dict__.items())
        return f"Data({inner})"


class SdkEvent:
    """SessionEvent の minimal な模倣."""

    def __init__(
        self,
        event_type: str,
        data: Any = None,
        session_id: str = "sdk-session",
        timestamp: datetime | None = None,
        event_id: Any = None,
    ) -> None:
        self.type = event_type
        self.data = data if data is not None else Data()
        self.session_id = session_id
        self.timestamp = timestamp or datetime.now(UTC)
        # 1.0.29 の SessionEvent.id は uuid.UUID。境界判定検証用に渡せるようにする
        self.id = event_id

    def __repr__(self) -> str:
        return f"SdkEvent({self.type!r}, data={self.data!r})"


class ModelInfo:
    def __init__(self, model_id: str, name: str | None = None) -> None:
        self.id = model_id
        self.name = name or model_id


class PermissionRequest:
    """PermissionRequest の minimal な模倣 (1.0.29 では tool_* フィールド)."""

    def __init__(
        self,
        tool_name: str,
        *,
        tool_type: str = "custom",
        tool_input: dict[str, Any] | None = None,
        session_id: str = "sdk-session",
    ) -> None:
        self.tool_name = tool_name
        self.tool_type = tool_type
        self.tool_input = {} if tool_input is None else tool_input
        self.session_id = session_id


class RecordingToolHandler:
    """Custom Tool handler の検証 object: 実行履歴を記録して result を返す。

    provider の callback 契約 `(tool_name, arguments)` で呼ぶ (同期・非同期両対応)。
    """

    def __init__(
        self,
        result: Any = "ok",
        *,
        raises: Exception | None = None,
    ) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._result = result
        self._raises = raises

    def __call__(self, tool_name: str, arguments: Mapping[str, Any]) -> Any:
        self.calls.append((tool_name, dict(arguments)))
        if self._raises is not None:
            raise self._raises
        return self._result

    @property
    def call_count(self) -> int:
        return len(self.calls)


@dataclass(slots=True)
class SdkToolInvocation:
    """``copilot.tools.ToolInvocation`` と同じ形の実行情報 (SDK 由来の値).

    SDK は Custom Tool の handler をこの 1 個の引数だけで呼ぶ。provider はそこから
    ``arguments`` を読んで apps 側の ``(tool_name, arguments)`` 契約へ変換する。
    """

    arguments: Any = None
    tool_name: str = ""
    session_id: str = "s-1"
    tool_call_id: str = "tc-1"
    available_tools: Any = field(default=None, repr=False)
    agent_id: Any = field(default=None, repr=False)
    request_id: Any = field(default=None, repr=False)


TERMINAL_TYPES = frozenset(
    {"session.idle", "session.shutdown", "abort", "agent.interrupted", "session.error"}
)
TURN_DONE = object()
TURN_PENDING = object()
TURN_TIMEOUT = object()
TURN_PENDING_RESOLVED = TURN_PENDING  # 旧名への互換
_FAKE_ABORT_WAIT = 0.2
_HISTORY_UNSET = object()  # queue_turn(history=...) の既定 (変更しない)


class FakeSdkSession:
    """1.0.29 の CopilotSession 相当の fake。

    Attributes:
        turn_events: 次のターンで ``on_event`` 経由で送出するイベント列。
        history: ``get_events()`` (history RPC) が返す一覧。None にすると
            SDK 側で history が無効なケース (AttributeError) を模倣する。
        send_calls / send_and_wait_calls / get_events_calls / abort_calls /
        disconnect_calls: 各 API の呼び出し回数。
        abort_after_send: True だと turn 配信中に ``abort()`` された扱いになる。

    Modes (``queue_turn(result=...)``):
        TURN_DONE    全 events を送出し、terminal が無ければ ``session.idle`` を補う。
        TURN_TIMEOUT events を送出後に :exc:`TimeoutError` (1.0.29 の文言)。
        TURN_PENDING events 送出後に ``abort()`` まで待つ (送信は完了しない)。
        BaseException 例外で events を送出後にその exception を raise。
    """

    def __init__(
        self,
        session_id: str = "sdk-session",
        *,
        events: Iterable[SdkEvent] | None = None,
        on_event: Callable[[SdkEvent], Any] | None = None,
        model: str | None = None,
    ) -> None:
        self.session_id = session_id
        self.model = model
        self.on_event = on_event
        self.turn_events: list[SdkEvent] = list(events or [])
        self.history: Sequence[Any] | None = []
        self.abort_calls = 0
        self.disconnect_calls = 0
        self.get_events_calls = 0
        self.send_calls = 0
        self.send_and_wait_calls = 0
        self.tool_call_calls: list[tuple[str, dict[str, Any]]] = []
        self.sent: list[str] = []
        self.calls: list[str] = []  # SDK API が呼ばれた順序
        self.abort_after_send = False
        self._mode: Any = TURN_DONE
        self._abort = asyncio.Event()
        self._closed = False

    @property
    def is_closed(self) -> bool:
        """``disconnect()`` された session か。"""
        return self._closed

    # --- event pump -------------------------------------------------
    async def _emit(self, event: SdkEvent) -> None:
        if self.on_event is not None:
            result = self.on_event(event)
            if inspect.isawaitable(result):
                await result

    async def _emit_all(self, events: Iterable[SdkEvent]) -> None:
        for event in list(events):
            await self._emit(event)

    def queue_turn(
        self,
        events: Iterable[SdkEvent] = (),
        *,
        result: Any = TURN_DONE,
        history: Sequence[Any] | None = _HISTORY_UNSET,
    ) -> None:
        """次に送られる.turn の event 列・完了の形・history RPC の戻り値を設定する。"""
        self.turn_events = list(events)
        self._mode = result
        if history is not _HISTORY_UNSET:
            self.history = history

    # --- turn API (1.0.29) ------------------------------------------
    def send(self, prompt: str) -> str:
        """送信だけ行う (完了は待たない)。turn 収集は ``send_and_wait`` 側。"""
        self.send_calls += 1
        self.sent.append(prompt)
        self.calls.append("send")
        return f"msg-{self.send_calls:04d}"

    async def send_and_wait(self, prompt: str, *, timeout: float = 60.0) -> Any:
        """send して terminal を待ち、待機中に event を ``on_event`` へ渡す。"""
        self.send_and_wait_calls += 1
        self.send_calls += 1
        self.sent.append(prompt)
        self.calls.append("send_and_wait")
        mode = self._mode
        events = list(self.turn_events)
        self.turn_events = []
        self._abort.clear()

        if isinstance(mode, BaseException):
            await self._emit_all(events)
            raise mode

        if mode is TURN_PENDING:
            await self._emit_all(events)
            try:
                await asyncio.wait_for(self._abort.wait(), timeout=_FAKE_ABORT_WAIT)
            except TimeoutError:
                raise TimeoutError(
                    f"Timeout after {timeout}s waiting for session.idle"
                ) from None
            await self._emit_all(
                [SdkEvent("agent.interrupted", data=None, session_id=self.session_id)]
            )
            raise TimeoutError("session was aborted")

        if mode is TURN_TIMEOUT:
            await self._emit_all(events)
            raise TimeoutError(f"Timeout after {timeout}s waiting for session.idle")

        await self._emit_all(events)
        if self.abort_after_send:
            self.abort()
            events = [*events, SdkEvent("agent.interrupted", data=None)]
        terminal = next(
            (event for event in events if event.type in TERMINAL_TYPES),
            None,
        )
        if terminal is None:
            terminal = SdkEvent("session.idle", data=None, session_id=self.session_id)
            await self._emit_all([terminal])
        return terminal

    async def send_tool_call(
        self,
        tool_name: str,
        tool_call_id: str,
        arguments: dict[str, Any],
    ) -> Any:
        self.tool_call_calls.append((tool_name, dict(arguments)))
        return {"toolCallId": tool_call_id, "toolName": tool_name, "results": []}

    async def abort(self) -> None:
        self.abort_calls += 1
        self.calls.append("abort")
        self._abort.set()

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        self.calls.append("disconnect")
        self._closed = True

    async def get_events(self) -> Sequence[Any]:
        """送信中の event を含む session 全体の履歴を返す。"""
        self.get_events_calls += 1
        self.calls.append("get_events")
        if self.history is None:
            raise RuntimeError("session store 無効")
        return list(self.history)

    def emit(self, event: SdkEvent) -> Any:
        """provider の on_event を直接呼ぶ (SDK が裏側で送出するケースを模倣)。"""
        if self.on_event is None:
            return None
        result = self.on_event(event)
        if inspect.isawaitable(result):
            return asyncio.ensure_future(result)
        return result


class FakeClient:
    """CopilotClient (1.0.29 相当) を模倣."""

    def __init__(
        self,
        *,
        sessions_dir: str | None = None,
        model_ids: Sequence[str] = ("gemma4:e2b",),
        start_error: Exception | None = None,
        create_error: Exception | None = None,
        list_models_error: Exception | None = None,
        stop_error: Exception | None = None,
    ) -> None:
        self.sessions_dir = sessions_dir
        self.model_ids = list(model_ids)
        self._start_error = start_error
        self._create_error = create_error
        self._list_models_error = list_models_error
        self._stop_error = stop_error
        self.started = False
        self.stopped = False
        self.start_calls = 0
        self.stop_calls = 0
        self.force_stop_calls = 0
        self.create_kwargs: list[dict[str, Any]] = []
        self.sessions: list[FakeSdkSession] = []

    async def start(self) -> None:
        self.start_calls += 1
        if self._start_error is not None:
            raise self._start_error
        self.started = True

    async def create_session(self, **kwargs: Any) -> FakeSdkSession:
        if self._create_error is not None:
            raise self._create_error
        session = FakeSdkSession(
            str(kwargs.get("session_id") or "sdk-session"),
            on_event=kwargs.get("on_event"),
            model=kwargs.get("model"),
        )
        self.create_kwargs.append(kwargs)
        self.sessions.append(session)
        return session

    async def list_models(self) -> list[ModelInfo]:
        # BYOK では real runtime が JsonRpcError(-32603, "Not authenticated") を投げる
        if self._list_models_error is not None:
            raise self._list_models_error
        return [ModelInfo(model_id) for model_id in self.model_ids]

    async def stop(self, timeout_ms: int | None = None) -> None:
        self.stop_calls += 1
        if self._stop_error is not None:
            raise self._stop_error
        self.stopped = True

    async def force_stop(self) -> None:
        self.force_stop_calls += 1

    async def health_check(self) -> Data:
        return Data(ready=True, provider="fake")

    @property
    def last_session(self) -> FakeSdkSession:
        if not self.sessions:
            raise AssertionError("まだセッションが作られていません")
        return self.sessions[-1]
