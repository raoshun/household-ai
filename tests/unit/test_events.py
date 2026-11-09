"""AgentEvent 群の意味論テスト (仕様 §7.1.5 / §7.2)。

mixin enum を StrEnum へ移行した結果、外部へ出す文字列が素朴な値になることを固定する。
"""

from __future__ import annotations

from dataclasses import fields

from app.agent.events import AgentEvent, AgentEventType, TurnResult


def test_agent_event_type_is_str_enumeration() -> None:
    assert [str(event_type) for event_type in AgentEventType] == [
        "delta",
        "assistant_message",
        "reasoning",
        "tool_call",
        "tool_result",
        "permission_request",
        "session_error",
        "session_idle",
    ]
    assert f"{AgentEventType.DELTA}" == "delta"
    assert AgentEventType.TOOL_CALL == "tool_call"
    assert AgentEventType("session_idle") is AgentEventType.SESSION_IDLE


def test_agent_event_has_expected_fields() -> None:
    names = {field.name for field in fields(AgentEvent)}
    assert names == {
        "type",
        "text",
        "tool_name",
        "tool_arguments",
        "error_type",
        "error_message",
        "aborted",
        "raw",
    }
    assert fields(TurnResult)[0].name == "text"


def test_turn_result_defaults_are_isolated_between_instances() -> None:
    first = TurnResult(text="a", aborted=False)
    second = TurnResult(text="b", aborted=False)
    first.events.append(AgentEvent(type=AgentEventType.DELTA, text="x"))
    first.tool_calls.append("search_rules")
    assert second.events == []
    assert second.tool_calls == []


def test_agent_event_raw_defaults_to_empty_dict() -> None:
    event = AgentEvent(type=AgentEventType.ASSISTANT_MESSAGE, text="hello")
    assert event.raw == {}
    assert event.aborted is None
