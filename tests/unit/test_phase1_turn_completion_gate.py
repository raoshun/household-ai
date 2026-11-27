"""Issue #3 gate test: Chat 応答の完了条件を pinned SDK 1.0.29 実装へ固定する。

README §7.2 / Issue #3 で決めた完了条件を hermetic に lock する:

1. turn 完了判定は ``send_and_wait(prompt, timeout=turn_timeout_seconds)`` の
   解決。(1.0.29 に ``session.status`` の completed 相当は存在しない)
2. idle (wire 名 ``session.idle`` / ``assistant.idle``) は Apps へ正規化済み
   terminal event ``session_idle`` として公開するが、turn 完了の証明には
   使わない (Microsoft provider は turn 完了前にも idle を送出する)。
3. terminal event は turn あたり必ず 1 件末尾に置かれる。完了待ち timeout は
   ``session_error`` (``error_type="turn_timeout"``) として閉じる。
4. turn 完了 (terminal 済み) 後は同一 session で再送信できる
   (turn lock が解かれ、次ターンの完了待機が始められる)。
"""

from __future__ import annotations

from typing import Any

from fake import TURN_PENDING, TURN_TIMEOUT, Data, SdkEvent
from test_provider import _open

from app.agent.events import AgentEvent, AgentEventType, is_terminal_event


def _terminal_events(events: list[AgentEvent]) -> list[AgentEvent]:
    return [event for event in events if is_terminal_event(event)]


async def test_turn_completes_closed_by_single_idle_terminal(tmp_path: Any) -> None:
    """正常 turn: send_and_wait 解決で完了し、terminal は session_idle 1 件 (§7.2)."""
    session, fake = await _open(tmp_path)
    fake.last_session.queue_turn(
        [SdkEvent("assistant.message", data=Data(content="こんにちは"))]
    )

    result = await session.send("おはよう")

    assert result.text == "こんにちは"
    assert not result.aborted
    terminal = _terminal_events(result.events)
    assert len(terminal) == 1
    assert terminal[0].type is AgentEventType.SESSION_IDLE
    assert is_terminal_event(result.events[-1])
    # send が完了 (send_and_wait) してから terminal で閉じている
    assert fake.last_session.send_and_wait_calls == 1


async def test_idle_wire_names_normalize_to_session_idle(tmp_path: Any) -> None:
    """pinned 実装の idle wire 名を Apps 公開名 ``session_idle`` に lock する。"""
    for wire_name in ("session.idle", "assistant.idle"):
        session, fake = await _open(tmp_path)
        fake.last_session.queue_turn(
            [
                SdkEvent("assistant.message", data=Data(content="応答")),
                SdkEvent(wire_name, data=None),
            ]
        )

        events = [event async for event in session.stream("質問")]

        idles = [e for e in events if e.type is AgentEventType.SESSION_IDLE]
        assert len(idles) == 1, f"{wire_name}: idle terminal が 1 件ではない"
        assert is_terminal_event(events[-1])
        assert [e for e in events if e.type is AgentEventType.SESSION_ERROR] == []


async def test_idle_alone_does_not_complete_turn(tmp_path: Any) -> None:
    """idle が来ても send_and_wait が解決しなければ turn は完了しない。

    Microsoft provider 実測 (turn 完了前に idle) を模倣する: idle を送出後も
    send_and_wait が解決しないまま期限を迎えれば、完了ではなく timeout 中断に
    なること (§7.2: 完了条件は send_and_wait の解決のみ)。
    """
    session, fake = await _open(tmp_path)
    fake.last_session.queue_turn(
        [
            SdkEvent("assistant.message", data=Data(content="途中")),
            SdkEvent("session.idle", data=None),
        ],
        result=TURN_PENDING,
    )

    events = [event async for event in session.stream("長い作業をして")]
    result = session._last_accumulator.finalize()

    # idle は Apps 名 session_idle で公開されるが、それで turn は完了しない
    assert any(e.type is AgentEventType.SESSION_IDLE for e in events)
    assert result.aborted, "idle だけで completed 扱いになってはいけない"
    assert result.text == "途中"  # 途中まで (partial) は保持される
    terminal = _terminal_events(events)
    assert len(terminal) == 1
    # idle 有無に関係なく send_and_wait 中断 (abort) で turn を確定している
    assert fake.last_session.abort_calls >= 1


async def test_turn_timeout_closes_with_single_error_terminal(
    tmp_path: Any,
) -> None:
    """完了待ち timeout は turn を session_error (turn_timeout) 1 件で閉じる。"""
    session, fake = await _open(tmp_path)
    fake.last_session.queue_turn(
        [SdkEvent("assistant.message", data=Data(content=" partial"))],
        result=TURN_TIMEOUT,
    )

    events = [event async for event in session.stream("タイムアウトして")]

    terminal = _terminal_events(events)
    assert len(terminal) == 1
    assert terminal[0].type is AgentEventType.SESSION_ERROR
    assert terminal[0].error_type == "turn_timeout"
    assert fake.last_session.abort_calls == 1


async def test_next_send_completes_after_turn_terminal(tmp_path: Any) -> None:
    """gate 条件: turn 送信 -> 完了 -> idle 検知 -> 次の送信が可能になる。"""
    session, fake = await _open(tmp_path)
    fake.last_session.queue_turn(
        [SdkEvent("assistant.message", data=Data(content="1 回目"))]
    )

    first = await session.send("1 つ目")
    assert is_terminal_event(first.events[-1])

    # turn lock は terminal 後に解かれている: 同一 session で再送信が完了する
    fake.last_session.queue_turn(
        [SdkEvent("assistant.message", data=Data(content="2 回目"))]
    )
    second = await session.send("2 つ目")

    assert second.text == "2 回目"
    assert not second.aborted
    assert _terminal_events(second.events) == [second.events[-1]]
    assert fake.last_session.sent == ["1 つ目", "2 つ目"]
    assert fake.last_session.send_and_wait_calls == 2


async def test_send_possible_after_timeout_turn(tmp_path: Any) -> None:
    """timeout 中断後も session は再利用でき、次ターンは正常完了する。"""
    session, fake = await _open(tmp_path)
    fake.last_session.queue_turn([], result=TURN_TIMEOUT)
    first_events = [event async for event in session.stream("失敗ターン")]
    assert _terminal_events(first_events)[0].type is AgentEventType.SESSION_ERROR

    fake.last_session.queue_turn(
        [SdkEvent("assistant.message", data=Data(content="復活"))]
    )
    second = await session.send("再送信")

    assert second.text == "復活"
    assert not second.aborted
    assert is_terminal_event(second.events[-1])
