"""ConversationLog 会話 family 操作 (§9.2: list/rename/delete/interrupt) test。

- `new_conversation_id()` は `conv-` 採番で、create_thread の会話 id 供給点
- `start_conversation` は turn 無し会話でも listing 対象に登記し、title を保持する
- `rename_conversation` の不存在 id は `UnknownThreadError` -> 404
  unknown_error.thread_not_found (§4 thread_not_found 行)
- `delete_conversation` は turn / message / title / session 採番をまとめて除去し、
  不存在 id の delete は冪等 (README 13.4 の family delete 欠落を閉じる)
- `interrupt_turn` は queued/started を interrupted terminal 化し、完了済み turn
  には冪等。不存在 id は §4 `no_active_turn` 相当
"""

from __future__ import annotations

import pytest

from app.services.audit import ConversationLog
from app.services.exceptions import (
    ProblemEmitter,
    UnknownThreadError,
    UnknownTurnError,
)

EMITTER = ProblemEmitter.default()
CONV = "conv-family"


def _problem(exc: BaseException):
    return EMITTER.problem_for(exc)


def _log_with_turn(log: ConversationLog, *, conv: str = CONV) -> str:
    """queued -> assistant record まで置いた turn を作り、turn id を返す。"""

    turn = log.queue_turn(conversation_id=conv, user_text="こんにちは")
    log.record_message(
        conversation_id=conv,
        role="assistant",
        content="やあ",
        turn_id=turn.turn_id,
    )
    return turn.turn_id


def test_record_message_keeps_turn_id_join_key():
    log = ConversationLog()
    turn = log.queue_turn(conversation_id=CONV, user_text="こんにちは")

    record = log.record_message(
        conversation_id=CONV,
        role="assistant",
        content="やあ",
        turn_id=turn.turn_id,
    )
    plain = log.record_message(conversation_id=CONV, role="system", content="system")

    assistant = [entry for entry in log.messages(CONV) if entry["role"] == "assistant"]

    assert record["turn_id"] == turn.turn_id
    assert assistant[0]["turn_id"] == turn.turn_id
    assert "turn_id" not in plain


def test_new_conversation_id_is_conv_prefixed_and_unique():
    log = ConversationLog()
    first = log.new_conversation_id()
    second = log.new_conversation_id()
    assert first.startswith("conv-")
    assert second.startswith("conv-")
    assert first != second
    assert len(first) > len("conv-")


def test_start_conversation_registers_id_and_title():
    log = ConversationLog()
    log.start_conversation(CONV, title="台所の相談")
    assert CONV in log.conversation_ids()
    assert log.conversation_title(CONV) == "台所の相談"
    assert log.turns(CONV) == []
    assert log.conversations() == []


def test_rename_conversation_round_trip():
    log = ConversationLog()
    _log_with_turn(log)
    log.rename_conversation(CONV, "リビングの温度")
    assert log.conversation_title(CONV) == "リビングの温度"


def test_rename_unknown_conversation_maps_to_404_thread_not_found():
    log = ConversationLog()
    with pytest.raises(UnknownThreadError) as excinfo:
        log.rename_conversation("conv-missing", "存在しない")
    assert excinfo.value.thread_id == "conv-missing"
    problem = _problem(excinfo.value)
    assert problem.status_code == 404
    assert problem.code == "unknown_error.thread_not_found"


def test_delete_conversation_purges_turns_messages_and_title():
    log = ConversationLog()
    other_turn = _log_with_turn(log, conv="conv-other")
    log.rename_conversation("conv-other", "残す会話")
    turn_id = _log_with_turn(log)
    log.rename_conversation(CONV, "消す会話")

    log.delete_conversation(CONV)

    assert CONV not in log.conversation_ids()
    assert log.turns(CONV) == []
    assert log.messages(CONV) == []
    assert log.get_turn(turn_id) is None
    assert log.conversation_title(CONV) is None
    assert [t.turn_id for t in log.turns()] == [other_turn]
    assert log.conversation_title("conv-other") == "残す会話"
    assert [c["conversation_id"] for c in log.conversations()] == ["conv-other"]


def test_delete_unknown_conversation_is_noop():
    log = ConversationLog()
    log.delete_conversation("conv-missing")
    assert log.conversation_ids() == []


def test_interrupt_queued_turn_marks_it_interrupted():
    log = ConversationLog()
    turn_id = _log_with_turn(log)

    interrupted = log.interrupt_turn(turn_id, reason="UI cancel")

    assert interrupted.status == "interrupted"
    assert interrupted.stop_reason == "interrupted"
    assert interrupted.completed_at is not None
    assert interrupted.error == {"code": "interrupted", "message": "UI cancel"}
    # 2 回目の interrupt は terminal なので冪等 (status 据え置き)
    again = log.interrupt_turn(turn_id)
    assert again.status == "interrupted"
    assert again.error == {"code": "interrupted", "message": "UI cancel"}


def test_interrupt_started_turn_marks_it_interrupted():
    log = ConversationLog()
    turn_id = _log_with_turn(log)
    log.mark_started(turn_id)

    interrupted = log.interrupt_turn(turn_id)

    assert interrupted.status == "interrupted"
    assert interrupted.stop_reason == "interrupted"
    assert interrupted.error is not None
    assert interrupted.error["code"] == "interrupted"


def test_interrupt_canonical_active_turn_uses_interrupted_stop_reason():
    log = ConversationLog()
    first = log.queue_turn(conversation_id=CONV, user_text="実行中")
    log.mark_started(first.turn_id)

    interrupted = log.interrupt_turn(first.turn_id, reason="Runtime 側 interrupt")

    assert interrupted.turn_id == f"{CONV}:t0"
    assert interrupted.status == "interrupted"
    assert interrupted.stop_reason == "interrupted"


def test_interrupt_completed_turn_is_unchanged():
    log = ConversationLog()
    done_id = _log_with_turn(log)
    completed = log.complete_turn(done_id, stop_reason="end_turn")

    again = log.interrupt_turn(done_id, reason="遅延 cancel")

    assert again.status == "completed"
    assert again.stop_reason == "end_turn"
    assert again.completed_at == completed.completed_at


def test_interrupt_unknown_turn_maps_to_no_active_turn():
    log = ConversationLog()
    with pytest.raises(UnknownTurnError) as excinfo:
        log.interrupt_turn(f"{CONV}:t9")
    assert excinfo.value.turn_id == f"{CONV}:t9"
    problem = _problem(excinfo.value)
    assert problem.status_code == 404
    assert problem.code == "unknown_error.no_active_turn"


def test_complete_turn_status_override_honored_unless_queued():
    log = ConversationLog()
    cancel_id = _log_with_turn(log, conv="conv-cancel")
    keep_id = _log_with_turn(log, conv="conv-keep")

    cancelled = log.complete_turn(cancel_id, stop_reason="end_turn", status="cancelled")
    completed = log.complete_turn(keep_id, stop_reason="end_turn", status="queued")

    assert cancelled.status == "cancelled"
    assert cancelled.stop_reason == "end_turn"
    assert completed.status == "completed"
    assert completed.stop_reason == "end_turn"
