"""AgentEvent 群の意味論テスト (仕様 §7.1.5 / §7.2)。

mixin enum を StrEnum へ移行した結果、外部へ出す文字列が素朴な値になることを固定する。
"""

from __future__ import annotations

from dataclasses import fields

from app.agent.events import (
    AgentEvent,
    AgentEventType,
    TurnResult,
    to_agent_event,
    wire_type,
)


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


class _EnumMember:
    """SDK の SessionEventType member 模し (value が wire 名, name は素の名前)."""

    def __init__(self, name: str, value: str) -> None:
        self.name = name
        self.value = value


class _OpaqueMember:
    """value も name も持たない SDK 便宜 object."""


def test_wire_type_prefers_str_then_value_then_name_then_raw_type() -> None:
    assert wire_type("assistant.message") == "assistant.message"
    assert wire_type(_EnumMember("ASSISTANT_MESSAGE", "assistant.message")) == (
        "assistant.message"
    )
    opaque_named = _OpaqueMember()
    opaque_named.name = "ASSISTANT_MESSAGE"
    assert wire_type(opaque_named) == "assistant_message"
    assert wire_type(_OpaqueMember(), "model.turn_started") == "model.turn_started"
    assert wire_type(_OpaqueMember()) == "unknown"


def test_wire_type_maps_unknown_member_to_unknown_not_raw_type() -> None:
    # pinned 1.0.29 では未知名 member は UNKNOWN (value "unknown") 解決で、素の名前は
    # SessionEvent.raw_type へ残る。value 優先なので raw_type は使わない。
    unknown_member = _EnumMember("UNKNOWN", "unknown")
    assert wire_type(unknown_member, "model.messages_snapshot") == "unknown"


def test_to_agent_event_drops_wire_names_without_mapping() -> None:
    # 実測した model.* 系 (UNKNOWN 経由) と未 mapping wire 名は AgentEvent にしない。
    assert to_agent_event(_EnumMember("UNKNOWN", "unknown")) is None
    assert to_agent_event("model.turn_started") is None
    assert to_agent_event(_OpaqueMember(), raw_type="model.messages_snapshot") is None


def test_to_agent_event_records_wire_name_in_raw() -> None:
    event = to_agent_event(_EnumMember("SESSION_IDLE", "session.idle"))
    assert event is not None
    assert event.type is AgentEventType.SESSION_IDLE
    assert event.raw["sdk_type"] == "session.idle"
    assert event.raw["sdk_event_name"] == "SESSION_IDLE"
