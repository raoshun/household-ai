"""Copilot SDK 由来の provider が 1.0.29 の実際の API 表面に沿っているかの回帰テスト。

`CopilotClient` / `CopilotSession` と同じ形をした検証 object (`tests/unit/fake.py`)
へ差し替えて実行する (§2.3: SDK 依存を app.agent package 内に閉じる)。
"""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from fake import (
    TURN_PENDING,
    Data,
    FakeClient,
    PermissionRequest,
    RecordingToolHandler,
    SdkEvent,
    SdkToolInvocation,
)

from app.agent.events import AgentEvent, AgentEventType, TurnResult
from app.agent.provider import (
    AgentProviderError,
    AgentSessionError,
    CopilotAgentProvider,
    CopilotAgentSession,
    CustomToolDeclaration,
    HistoryRPCUnavailableError,
    ProviderHealth,
    ProviderUnavailableError,
    SessionRequest,
    Tool,
    ToolResult,
    build_available_tools,
    build_client_kwargs,
    coerce_custom_tools,
    normalize_tool_arguments,
)
from app.config import Settings

GITHUB_MCP = "github-mcp-server"


def _settings(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "provider": "ollama",
        "model": "gemma4:e2b",
        "data_dir": tmp_path / "data",
        "skills_dir": tmp_path / "skills",
        "turn_timeout_seconds": 10,
        "session_idle_timeout_seconds": 42,
    }
    values.update(overrides)
    return Settings(**values)


def _provider(
    tmp_path: Path, settings: Settings | None = None, **client_kwargs: Any
) -> tuple[CopilotAgentProvider, FakeClient]:
    """Settings と fake client を injected した provider を作る (SDK を起こさない)。"""
    fake = FakeClient(**client_kwargs)
    provider = CopilotAgentProvider(
        settings or _settings(tmp_path), sdk_client_factory=lambda: fake
    )
    return provider, fake


async def _open(
    tmp_path: Path, **kwargs: Any
) -> tuple[CopilotAgentSession, FakeClient]:
    provider, fake = _provider(tmp_path)
    await provider.start()
    session = await provider.create_session(SessionRequest(**kwargs))
    return session, fake


def _declaration(
    name: str = "list_household_skills",
    *,
    description: str | None = None,
    handler: Any = None,
    parameters: dict[str, Any] | None = None,
    approval_mode: str = "ask",
) -> CustomToolDeclaration:
    """SessionRequest.tools へ渡す Custom Tool 宣言 (README §2.2.1 の形)。"""
    return CustomToolDeclaration(
        name=name,
        description=description if description is not None else f"{name} を実行する。",
        handler=handler if handler is not None else RecordingToolHandler(),
        parameters=parameters,
        approval_mode=approval_mode,
    )


# ---------------------------------------------------------------------------
# CopilotClient へ渡す引数 (§4 Phase 0 検証)
# ---------------------------------------------------------------------------
def test_client_kwargs_は_認証を使わず_byok_only() -> None:
    kwargs = build_client_kwargs(Settings(session_idle_timeout_seconds=777))
    assert kwargs["mode"] == "empty"
    assert kwargs["use_logged_in_user"] is False
    assert kwargs["log_level"] == "info"
    # clamp しない: Settings の値をそのまま渡す
    assert kwargs["session_idle_timeout_seconds"] == 777


def test_client_kwargs_runtime_dir(tmp_path: Path) -> None:
    kwargs = build_client_kwargs(_settings(tmp_path))
    assert Path(kwargs["base_directory"]).is_absolute()


async def test_start_失敗は_provider_unavailable(tmp_path: Path) -> None:
    provider, _ = _provider(
        tmp_path, start_error=RuntimeError("ランタイムが見つからない")
    )
    try:
        await provider.start()
    except ProviderUnavailableError as exc:
        assert "ランタイム" in str(exc)
    else:
        raise AssertionError("ProviderUnavailableError を期待した")


async def test_stop_失敗時は_force_stopへフォールバック(tmp_path: Path) -> None:
    provider, fake = _provider(tmp_path, stop_error=RuntimeError("stop 失敗"))
    await provider.start()
    await provider.stop()
    assert fake.stop_calls == 1
    assert fake.force_stop_calls == 1


async def test_start_is_idempotent(tmp_path: Path) -> None:
    provider, fake = _provider(tmp_path)
    await provider.start()
    await provider.start()
    assert fake.start_calls == 1


async def test_provider_health_and_list_models(tmp_path: Path) -> None:
    provider, _ = _provider(tmp_path, model_ids=("gemma4:e2b", "qwen3:8b"))
    health = await provider.health_check()
    assert isinstance(health, ProviderHealth)
    assert health.ready is False
    await provider.start()
    health = await provider.health_check()
    assert (health.ready, health.model, health.sessions) == (True, "gemma4:e2b", 0)
    assert await provider.list_models() == ["gemma4:e2b", "qwen3:8b"]


# ---------------------------------------------------------------------------
# セッション作成 kwargs (deny-by-default)
# ---------------------------------------------------------------------------
async def test_create_session_は_custom_tool_onlyで開く(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    provider, fake = _provider(tmp_path)
    await provider.start()
    session = await provider.create_session(SessionRequest())
    kwargs = fake.create_kwargs[0]
    assert kwargs["model"] == "gemma4:e2b"
    # ToolSet は __eq__ を持たないので to_list() の allow-list 内容で比較する
    assert (
        kwargs["available_tools"].to_list() == build_available_tools(settings).to_list()
    )
    assert kwargs["available_tools"].to_list() == ["custom:*", "builtin:skill"]
    assert kwargs["streaming"] is True
    assert kwargs["infinite_sessions"] == {"enabled": False}
    assert kwargs["disabled_mcp_servers"] == [GITHUB_MCP]
    assert kwargs["enable_session_store"] is False
    assert kwargs["enable_skills"] is True
    assert kwargs["skill_directories"] == [str(Path(settings.skills_dir).resolve())]
    assert callable(kwargs["on_event"])
    assert callable(kwargs["on_permission_request"])
    assert "tools" not in kwargs  # Custom Tool が無いなら渡さない
    assert session.session_id == "sdk-session"
    assert provider.session_ids() == ["sdk-session"]


def test_available_tools_skills_込み(tmp_path: Path) -> None:
    tools = build_available_tools(_settings(tmp_path))
    # ToolSet は __eq__ / __iter__ ではないので to_list() の allow-list で比較する
    assert tools.to_list() == ["custom:*", "builtin:skill"]


def test_available_tools_skills_無し(tmp_path: Path) -> None:
    tools = build_available_tools(_settings(tmp_path, enable_skills=False))
    assert tools.to_list() == ["custom:*"]


async def test_create_session_with_byok_and_custom_tools(tmp_path: Path) -> None:
    provider, fake = _provider(
        tmp_path, _settings(tmp_path, provider="anthropic", api_key="sk-test")
    )
    await provider.start()
    tool = CustomToolDeclaration(
        name="list_household_skills",
        description="世帯skill 一覧を返す。",
        handler=RecordingToolHandler(),
        parameters={"type": "object", "properties": {}},
    )
    await provider.create_session(SessionRequest(tools=[tool]))
    kwargs = fake.create_kwargs[0]
    assert kwargs["provider"] == {
        "type": "anthropic",
        "base_url": "https://api.anthropic.com",
        "api_key": "sk-test",
    }
    # 生の SDK tool として create_session へ渡る (名前・description・parameters)
    (sent,) = kwargs["tools"]
    assert sent.name == "list_household_skills"
    assert sent.description == "世帯skill 一覧を返す。"
    assert sent.parameters == {"type": "object", "properties": {}}


async def test_create_session_system_message_is_dict(tmp_path: Path) -> None:
    provider, fake = _provider(tmp_path)
    await provider.start()
    await provider.create_session(SessionRequest(system_message="家族向けの指示"))
    assert fake.create_kwargs[0]["system_message"] == {
        "mode": "append",
        "content": "家族向けの指示",
    }


async def test_create_session_without_start_raises(tmp_path: Path) -> None:
    provider, _ = _provider(tmp_path)
    try:
        await provider.create_session(SessionRequest())
    except AgentProviderError as exc:
        assert "start()" in str(exc)
    else:
        raise AssertionError("AgentProviderError を期待した")


async def test_create_session_failure_becomes_session_error(tmp_path: Path) -> None:
    provider, _ = _provider(tmp_path, create_error=RuntimeError("model が見つからない"))
    await provider.start()
    try:
        await provider.create_session(SessionRequest())
    except AgentSessionError as exc:
        assert "model が見つからない" in str(exc)
    else:
        raise AssertionError("AgentSessionError を期待した")


# ---------------------------------------------------------------------------
# 送信・ストリーミング (1.0.29: turn 完了まで待つ)
# ---------------------------------------------------------------------------
def _turn_events() -> list[SdkEvent]:
    """1 turn ぶんの SDK event 列 (BYOK/Ollama で観測した wire 名)。"""
    return [
        SdkEvent("assistant.turn_start", Data(model="gemma4:e2b"), event_id="e1"),
        SdkEvent(
            "assistant.message_delta",
            Data(delta_content="家族の"),
            event_id="e1-delta-1",
        ),
        SdkEvent(
            "assistant.message_delta",
            Data(delta_content="写真の一覧を作ります"),
            event_id="e1-delta-2",
        ),
        SdkEvent(
            "external_tool.requested",
            Data(tool_name="list_household_skills", arguments={"folder": "skills"}),
            event_id="e1-tool-call",
        ),
        SdkEvent(
            "external_tool.completed",
            Data(
                tool_name="list_household_skills",
                results=[{"type": "text", "text": "2 件"}],
            ),
            event_id="e1-tool-result",
        ),
        SdkEvent(
            "assistant.usage",
            Data(input_tokens=10, output_tokens=4),
            event_id="e1-usage",
        ),
        SdkEvent("session.idle", event_id="e1-idle"),
    ]


async def test_send_returns_turn_result(tmp_path: Path) -> None:
    session, fake = await _open(tmp_path)
    fake.last_session.queue_turn(_turn_events())
    result = await session.send("skill 一覧を説明して")
    assert fake.last_session.sent == ["skill 一覧を説明して"]
    assert result.text == "家族の写真の一覧を作ります"
    assert result.aborted is False
    assert result.tool_calls == ["list_household_skills"]
    assert result.partial_events == []  # 正常完了では partial はない
    kinds = [event.type.value for event in result.events]
    assert kinds[-1] == "session_idle"  # 常に terminal event
    # assistant.turn_start に対応する app event 種別は無い (events.py の写像)
    assert kinds[0] == "delta"
    # history RPC (get_events) は send 完了後に 1 回だけ
    assert fake.last_session.calls == ["send_and_wait", "get_events"]
    assert fake.last_session.get_events_calls == 1


async def test_stream_yields_events_until_idle(tmp_path: Path) -> None:
    session, fake = await _open(tmp_path)
    fake.last_session.queue_turn(_turn_events())
    events = [event async for event in session.stream("go")]
    kinds = [event.type.value for event in events]
    assert kinds.count("delta") == 2
    assert "tool_call" in kinds and "tool_result" in kinds
    assert kinds[-1] == "session_idle"  # terminal event で閉じる (§7.1)
    tool_call = next(event for event in events if event.type.value == "tool_call")
    assert tool_call.tool_name == "list_household_skills"
    assert tool_call.tool_arguments == {"folder": "skills"}
    assert fake.last_session.calls == ["send_and_wait", "get_events"]


async def test_send_matches_stream_plus_finalize(tmp_path: Path) -> None:
    """send() の events は stream() + finalize() と同じ列 (§7.1)."""
    session, fake = await _open(tmp_path)
    fake.last_session.queue_turn(_turn_events(), history=list(_turn_events()))
    streamed = [event async for event in session.stream("go")]
    stream_kinds = [event.type.value for event in streamed]
    fake.last_session.queue_turn(_turn_events(), history=list(_turn_events()))
    result = await session.send("go")
    assert [event.type.value for event in result.events] == stream_kinds
    assert len(streamed) <= len(result.events)  # 取りこぼしなし


async def test_sessionless_history_rpc_raises(tmp_path: Path) -> None:
    """history RPC (get_events) を使えない session では defined error を投げる."""
    session, fake = await _open(tmp_path)
    fake.last_session.queue_turn(_turn_events())
    fake.last_session.get_events = None  # capability 欠落 (history RPC 実行不能)
    try:
        await session.send("history が取れない")
    except HistoryRPCUnavailableError as exc:
        assert session.session_id in str(exc)
        assert fake.last_session.send_and_wait_calls == 1
        # send は完了している -> partial を保持したまま defined error で失敗する
        assert session._last_accumulator is not None
        kinds = [event.type.value for event in session._last_accumulator.snapshot()]
        assert kinds[-1] == "session_idle"  # streaming で拾えた分は保持する
        assert "delta" in kinds and "tool_result" in kinds
    else:
        raise AssertionError("HistoryRPCUnavailableError を期待した")


async def test_turn_timeout_preserves_partial_and_aborts_turn(tmp_path: Path) -> None:
    """deadline 内に成果も send 完了もない turn を interrupt して partial を残す."""
    session, fake = await _open(tmp_path)
    fake.last_session.queue_turn(
        [
            SdkEvent("assistant.turn_start", event_id="t1"),
            SdkEvent(
                "assistant.message_delta",
                Data(delta_content="考えて"),
                event_id="t1-d1",
            ),
        ],
        result=TURN_PENDING,  # send_and_wait は timeout 前に part だけ送出する
        history=[],  # SDK は partial turn の events を history へ返さない
    )
    events = [event async for event in session.stream("待つ")]
    kinds = [event.type.value for event in events]
    assert kinds[0] == "delta"  # 中断前に届いた part は保持する
    assert kinds[-1] == "session_error"
    assert events[-1].aborted is True
    assert events[-1].error_type == "turn_timeout"  # timeout 由来だと示す (§7.2)
    assert "timeout" in (events[-1].error_message or "").lower()
    assert fake.last_session.abort_calls == 1  # agent 作業を停止させた
    assert fake.last_session.calls == ["send_and_wait", "abort", "get_events"]
    assert not fake.last_session.is_closed  # turn 中断で session は閉じない


async def test_interrupted_result_carries_partial_events() -> None:
    """interrupted error 応答は partial_events を events に加えて含む (§7.1)."""
    partial = [
        AgentEvent(type=AgentEventType.DELTA, text="途中"),
        AgentEvent(
            type=AgentEventType.SESSION_ERROR,
            error_type="turn_timeout",
            error_message="timeout",
            aborted=True,
        ),
    ]
    terminal = partial[-1]
    base = TurnResult(text="途中", aborted=True, events=[terminal])
    result = base.include_partial_events(partial)
    assert result.events[-1] is terminal  # 常に terminal event が最後
    assert all(event in result.events for event in partial)
    assert len(partial) <= len(result.events)


async def test_interrupt_preserves_partial_and_adds_history_tail(
    tmp_path: Path,
) -> None:
    """中断時は partial へ history tail をつなげ、前 turn を再取り込みしない."""
    streamed = [
        SdkEvent("assistant.turn_start", event_id="turn-2"),
        SdkEvent(
            "assistant.message_delta",
            Data(delta_content="途中"),
            event_id="turn-2-d1",
        ),
    ]
    # history: 前 turn の event -> 現 turn の一部 (streamed) -> tail (未受信)
    history = [
        SdkEvent("user.message", Data(content="前回の質問"), event_id="turn-1-user"),
        SdkEvent(
            "assistant.message",
            Data(content="前回の回答"),
            event_id="turn-1-answer",
        ),
        SdkEvent(
            "assistant.message_delta",
            Data(delta_content="途中"),
            event_id="turn-2-d1",
        ),
        SdkEvent(
            "assistant.message_delta",
            Data(delta_content="続き"),
            event_id="turn-2-d2",
        ),
        SdkEvent("session.idle", event_id="turn-2-idle"),
    ]
    session, fake = await _open(tmp_path)
    fake.last_session.queue_turn(streamed, result=TURN_PENDING, history=history)
    events = [event async for event in session.stream("続きがある")]
    texts = [event.text for event in events if event.text]
    assert "途中" in texts and "続き" in texts  # streamed + history tail の順
    assert "前回の回答" not in texts  # 前 turn 相当を再度 turn 内 event にしない
    kinds = [event.type.value for event in events]
    assert kinds[-1] == "session_idle"  # history tail が terminal を含むなら合成しない
    assert fake.last_session.abort_calls == 1
    assert fake.last_session.get_events_calls == 1


async def test_history_without_boundary_is_not_trusted(tmp_path: Path) -> None:
    """streamed event と照合できない history は信用せず、streamed + 1 error にする."""
    streamed = [
        SdkEvent(
            "assistant.message_delta",
            Data(delta_content="途中"),
            event_id="fresh-1",
        ),
    ]
    unrelated = [
        SdkEvent("user.message", Data(content="別会话"), event_id="other-1"),
        SdkEvent("session.idle", event_id="other-2"),
    ]
    session, fake = await _open(tmp_path)
    fake.last_session.queue_turn(streamed, result=TURN_PENDING, history=unrelated)
    events = [event async for event in session.stream("id が合わない")]
    assert [event.type.value for event in events] == ["delta", "session_error"]
    assert events[-1].error_type == "turn_timeout"
    assert all((event.text or "") != "別会话" for event in events)


async def test_mid_turn_close_runs_recovery_without_hanging(tmp_path: Path) -> None:
    """stream 中に caller が離れても interrupt -> error -> history で回収."""
    session, fake = await _open(tmp_path)
    fake.last_session.queue_turn(
        [
            SdkEvent(
                "assistant.message_delta",
                Data(delta_content="途中"),
                event_id="mid-1",
            )
        ],
        result=TURN_PENDING,
        history=[],
    )
    stream = session.stream("途中で抜ける")
    first = await stream.__anext__()
    assert first.type.value == "delta"
    await asyncio.wait_for(stream.aclose(), timeout=5)  # hang しない
    await session.aclose()  # recovery 完了を待つ
    assert fake.last_session.abort_calls == 1
    assert fake.last_session.get_events_calls == 1
    assert fake.last_session.calls[:2] == ["send_and_wait", "abort"]


async def test_send_after_close_raises(tmp_path: Path) -> None:
    session, _fake = await _open(tmp_path)
    await session.aclose()
    try:
        await session.send("nope")
    except AgentSessionError as exc:
        assert "閉じたセッション" in str(exc)
    else:
        raise AssertionError("AgentSessionError を期待した")


# ---------------------------------------------------------------------------
# 終了・中断 (1.0.29 の CopilotSession に close() は無い)
# ---------------------------------------------------------------------------
async def test_close_session_calls_disconnect(tmp_path: Path) -> None:
    provider, fake = _provider(tmp_path)
    await provider.start()
    session = await provider.create_session(SessionRequest())
    await provider.close_session(session.session_id)
    assert fake.last_session.disconnect_calls == 1
    assert provider.session_ids() == []
    assert session.snapshot().session_id == "sdk-session"


async def test_stop_closes_sessions_then_client(tmp_path: Path) -> None:
    provider, fake = _provider(tmp_path)
    await provider.start()
    await provider.create_session(SessionRequest())
    await provider.stop()
    assert fake.last_session.disconnect_calls == 1
    assert fake.stop_calls == 1
    assert provider.session_ids() == []


async def test_unknown_session_raises_session_error(tmp_path: Path) -> None:
    provider, _ = _provider(tmp_path)
    await provider.start()
    try:
        await provider.close_session("nope")
    except AgentSessionError as exc:
        assert "登録されていない" in str(exc)
    else:
        raise AssertionError("AgentSessionError を期待した")


async def test_interrupt_abort_with_registered_tool_names(tmp_path: Path) -> None:
    provider, fake = _provider(tmp_path)
    await provider.start()
    session = await provider.create_session(SessionRequest(tools=[_declaration()]))
    await provider.interrupt_session(session.session_id)
    assert fake.last_session.abort_calls == 1
    assert session.snapshot().tool_names == ["list_household_skills"]


# ---------------------------------------------------------------------------
# Custom Tool 宣言 → SDK tool 変換 (README §2.2.1)
# ---------------------------------------------------------------------------
def test_coerce_custom_tools_are_declared_and_sent_as_sdk_tools(tmp_path: Path) -> None:
    handler = RecordingToolHandler()
    provided = _declaration(
        "search_memory",
        description="memory を参照する。",
        handler=handler,
        parameters={"type": "object", "properties": {"query": {"type": "string"}}},
    )

    bindings = coerce_custom_tools([provided], session_id="sess-1")

    assert len(bindings) == 1
    (binding,) = bindings
    sent = binding.sdk_tool
    assert binding.name == "search_memory"
    assert sent.name == "search_memory"
    assert sent.description == "memory を参照する。"
    assert sent.parameters == {
        "type": "object",
        "properties": {"query": {"type": "string"}},
    }
    assert callable(sent.handler)
    # ask  mode は SDK へ permission を要求する (auto だけが skip できる)
    assert sent.skip_permission is False
    # 実行 counter は binding 側で保持する (§2.3 監査ログ)
    assert binding.executions == 0


def test_coerce_custom_tools_auto_approval_skips_permission() -> None:
    (binding,) = coerce_custom_tools(
        [_declaration("read_rule", approval_mode="auto")], session_id="sess-1"
    )
    assert binding.sdk_tool.skip_permission is True


def test_coerce_custom_tools_rejects_name_only_tools() -> None:
    # 1.0.29 では tools=["name"] が session.create 自体を壊す (AttributeError)
    try:
        coerce_custom_tools(["search_memory"], session_id="sess-1")
    except AgentSessionError as exc:
        assert "Custom Tool" in str(exc)
    else:
        raise AssertionError("AgentSessionError を期待した")


def test_coerce_custom_tools_rejects_declaration_without_handler() -> None:
    # handler 欠如は session 作成失败 (§2.5.1 「apps apps 侧で捕获」ではなく登録拒绝)。
    broken = Data(name="search_memory", description="handler 無い宣言")
    try:
        coerce_custom_tools([broken], session_id="sess-1")
    except AgentSessionError as exc:
        assert "handler" in str(exc)
    else:
        raise AssertionError("AgentSessionError を期待した")


def test_coerce_custom_tools_rejects_declaration_without_description() -> None:
    try:
        coerce_custom_tools(
            [_declaration("search_memory", description="")], session_id="sess-1"
        )
    except AgentSessionError as exc:
        assert "description" in str(exc)
    else:
        raise AssertionError("AgentSessionError を期待した")


def test_coerce_custom_tools_rejects_duplicate_names() -> None:
    tools = [_declaration("search_memory"), _declaration("search_memory")]
    try:
        coerce_custom_tools(tools, session_id="sess-1")
    except AgentSessionError as exc:
        assert "search_memory" in str(exc)
    else:
        raise AssertionError("AgentSessionError を期待した")


async def test_binding_calls_registered_callback_with_tool_name_and_args() -> None:
    handler = RecordingToolHandler({"hits": 1})
    (binding,) = coerce_custom_tools(
        [_declaration("search_memory", handler=handler)], session_id="sess-1"
    )

    result = await binding.sdk_tool.handler(
        SdkToolInvocation(tool_name="search_memory", arguments={"query": "家事"})
    )

    assert handler.calls == [("search_memory", {"query": "家事"})]
    assert result.result_type == "success"
    assert json.loads(result.text_result_for_llm) == {"hits": 1}
    assert binding.executions == 1


async def test_binding_accepts_string_handler_result() -> None:
    async def handler(tool_name: str, arguments: Mapping[str, Any]) -> str:
        return f"{tool_name}:{arguments['query']}"

    (binding,) = coerce_custom_tools(
        [_declaration("search_memory", handler=handler)], session_id="sess-1"
    )
    result = await binding.sdk_tool.handler(
        SdkToolInvocation(tool_name="search_memory", arguments={"query": "ゴミ"})
    )
    assert result.text_result_for_llm == "search_memory:ゴミ"
    assert result.result_type == "success"


async def test_binding_forwards_sdk_tool_result_unchanged() -> None:
    prepared = ToolResult(text_result_for_llm="整形済み応答", result_type="success")

    async def handler(tool_name: str, arguments: Mapping[str, Any]) -> ToolResult:
        return prepared

    (binding,) = coerce_custom_tools(
        [_declaration("search_memory", handler=handler)], session_id="sess-1"
    )
    result = await binding.sdk_tool.handler(
        SdkToolInvocation(tool_name="search_memory")
    )
    assert result is prepared
    assert result.text_result_for_llm == "整形済み応答"


async def test_binding_failure_becomes_failure_result_without_raising() -> None:
    (binding,) = coerce_custom_tools(
        [
            _declaration(
                "search_memory",
                handler=RecordingToolHandler(raises=RuntimeError("DB 接続失敗")),
            )
        ],
        session_id="sess-1",
    )

    result = await binding.sdk_tool.handler(
        SdkToolInvocation(tool_name="search_memory")
    )

    # tool 内の例外を session 全体の失敗にしない (§2.2.1 例外 → failure)
    assert result.result_type == "failure"
    assert "DB 接続失敗" in result.text_result_for_llm
    # 失败しても呼び出し回数は数える (§2.5.1: tool 実行は開始している)。
    assert binding.executions == 1


def test_binding_handler_matches_sdk_dispatch_arity() -> None:
    """SDK の dispatch 形に対する回帰ガード (§2.5.1、§2.2 制約 6).

    1.0.29 は Tool handler を ``ToolInvocation`` 1 個で呼ぶ
    (``_execute_tool_and_respond`` → ``handler(invocation)``)。provider が 2 引数形
    (``(tool_name, arguments)``) で受けると呼出し自体が失敗し、CLI は
    「Tool execution failed」で完了する (apps 側 handler は実行されない)。実測
    (§15.3.3) で確認したため、呼べる形であることをここで固定する。
    """

    (binding,) = coerce_custom_tools(
        [_declaration("search_memory")], session_id="sess-1"
    )
    parameters = inspect.signature(binding.sdk_tool.handler).parameters
    positional = [
        name
        for name, parameter in parameters.items()
        if parameter.kind
        in (parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD)
        and parameter.default is parameter.empty
    ]
    assert len(positional) == 1

    # 1 個の invocation だけで dispatch できる (tool_name は invocation 側から取る)。
    result = asyncio.run(
        binding.sdk_tool.handler(
            SdkToolInvocation(tool_name="search_memory", arguments={"query": "家事"})
        )
    )
    assert result.result_type == "success"
    assert binding.executions == 1

    # invocation に tool_name がない形 (1.0.29 の実際の call 形) でも実行できる。
    result = asyncio.run(
        binding.sdk_tool.handler(SdkToolInvocation(arguments={"q": 1}))
    )
    assert result.result_type == "success"
    assert binding.executions == 2


def test_normalize_tool_arguments_falls_back_to_invocation() -> None:
    invocation = SdkToolInvocation(arguments={"query": "家事"})
    assert normalize_tool_arguments({"query": "直接"}, invocation) == {"query": "直接"}
    assert normalize_tool_arguments(None, invocation) == {"query": "家事"}
    assert normalize_tool_arguments("not-a-mapping", invocation) == {"query": "家事"}
    assert normalize_tool_arguments(None, None) == {}


async def test_open_sends_registered_names_available_policy(tmp_path: Path) -> None:
    """allow-list は registered 名の `custom:<name>` だけ (deny-by-default, §2.5.4)。"""
    _session, fake = await _open(
        tmp_path, tools=[_declaration("search_memory"), _declaration("read_rule")]
    )
    available = fake.create_kwargs[0]["available_tools"].to_list()
    assert available == ["custom:search_memory", "custom:read_rule", "builtin:skill"]
    non_custom = [name for name in available if not name.startswith("custom:")]
    assert non_custom == ["builtin:skill"]


async def test_open_registered_policy_は_wildcardを含まない(tmp_path: Path) -> None:
    """registered 名 session では wildcard (`custom:*`) を立てない。"""
    _session, fake = await _open(tmp_path, tools=[_declaration("search_memory")])
    assert "custom:*" not in fake.create_kwargs[0]["available_tools"].to_list()


async def test_open_tools無しは_wildcardへfallback(tmp_path: Path) -> None:
    """Custom Tool が 0 件の session は wildcard + skill 参照へ倒す。

    空 allow-list (空 ToolSet) を渡したときの SDK 側の扱いは未検証のため、skill 参照だけ
    許す形に倒す。
    """
    _session, fake = await _open(tmp_path)
    assert fake.create_kwargs[0]["available_tools"].to_list() == [
        "custom:*",
        "builtin:skill",
    ]


async def test_open_wildcard_switch(tmp_path: Path, monkeypatch) -> None:
    """registered 名 policy を wildcard 検証へ切り替える switch を確かめる。"""
    monkeypatch.setenv("HOUSEHOLD_TOOL_POLICY", "wildcard")
    _session, fake = await _open(tmp_path, tools=[_declaration("search_memory")])
    available = fake.create_kwargs[0]["available_tools"].to_list()
    assert "custom:*" in available
    assert "builtin:skill" in available


async def test_open_sends_sdk_tool_objects_not_declarations(tmp_path: Path) -> None:
    """`create_session(tools=)` へ渡るのは SDK の `Tool` (§6.2)。

    app 側の `CustomToolDeclaration` をそのまま渡すと SDK 内部で AttributeError になる
    (`_tool_to_wire_definition`、1.0.29 で実測)。
    """
    _session, fake = await _open(
        tmp_path, tools=[_declaration("search_memory"), _declaration("read_rule")]
    )
    sent_tools = fake.create_kwargs[0]["tools"]
    assert [tool.name for tool in sent_tools] == ["search_memory", "read_rule"]
    assert all(isinstance(tool, Tool) for tool in sent_tools)
    assert not [tool for tool in sent_tools if isinstance(tool, CustomToolDeclaration)]
    assert all(callable(tool.handler) for tool in sent_tools)


async def test_list_models_byokはsettings_modelへfallback(tmp_path: Path) -> None:
    """BYOK では models.list が認証エラーになるため settings.model を返す。

    1.0.29 で JsonRpcError(-32603, "Not authenticated") を実測した。
    """
    provider, _fake = _provider(
        tmp_path,
        list_models_error=RuntimeError('JsonRpcError(-32603, "Not authenticated")'),
    )
    await provider.start()
    assert await provider.list_models() == ["gemma4:e2b"]


async def test_list_models_byok以外はエラーへ変換(tmp_path: Path) -> None:
    """BYOK 以外の provider での models.list 失敗は黙って握りつぶさない。"""
    provider, _fake = _provider(
        tmp_path,
        _settings(tmp_path, provider="copilot"),
        list_models_error=RuntimeError("token 失効"),
    )
    await provider.start()
    try:
        await provider.list_models()
    except AgentProviderError as exc:
        assert "models.list" in str(exc)
    else:
        raise AssertionError("AgentProviderError を期待した")


# ---------------------------------------------------------------------------
# 許可判断 (README L244: custom しか承認しない)
# ---------------------------------------------------------------------------
async def test_permission_denies_non_custom_tool(tmp_path: Path) -> None:
    session, _fake = await _open(tmp_path)
    request = PermissionRequest("mcp:github:search")
    decision = await session._decide_permission(request, None)
    assert getattr(decision, "kind", None) == "reject"
    assert "Custom Tool" in (getattr(decision, "feedback", "") or "")


async def test_permission_approves_registered_custom_tool(tmp_path: Path) -> None:
    provider, fake = _provider(tmp_path)
    await provider.start()
    session = await provider.create_session(SessionRequest(tools=[_declaration()]))
    request = PermissionRequest("list_household_skills")
    decision = await session._decide_permission(request, None)
    assert getattr(decision, "kind", None) == "approve-once"
    assert fake.sessions  # session が開いている


# ---------------------------------------------------------------------------
# skills 一覧 (session.skills_loaded を capture)
# ---------------------------------------------------------------------------
async def test_skills_loaded_event_populates_snapshot(tmp_path: Path) -> None:
    session, fake = await _open(tmp_path)
    fake.last_session.emit(
        SdkEvent(
            "session.skills_loaded",
            Data(
                skills=[
                    Data(
                        name="builtin:household-advisor",
                        source="builtin",
                        enabled=True,
                        user_invocable=True,
                        path=str(Path("skills/household-advisor/SKILL.md")),
                    )
                ]
            ),
        )
    )
    skills = session.snapshot().skills
    assert [skill.name for skill in skills] == ["builtin:household-advisor"]
    assert skills[0].user_invocable is True
    assert skills[0].path is not None
