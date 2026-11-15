"""ConversationLog turn state 拡張 (§3/§4: queued/started + canonical 採番) test。

- queue_turn は受付を queued で表し、canonical `{session_id}:t{n}` を採番する
- 既存 id への再投話 (active 含む) は TurnIdConflictError -> 422
  unknown_error.turn_id_conflict (§4 turn_id_conflict 行)
- canonical 規則外かつ client 系 (`agui` 接頭辞) でない id は 400
- complete_turn の stop_reason -> terminal status 写像 (§3 terminal 系列)
"""

from __future__ import annotations

import pytest

from app.services.audit import ConversationLog
from app.services.exceptions import (
    InvalidRequestError,
    ProblemEmitter,
    TurnIdConflictError,
    UnknownTurnError,
)

EMITTER = ProblemEmitter.default()
CONV = "sess-bridge"


def _problem(exc: BaseException):
    return EMITTER.problem_for(exc)


def test_queue_turn_canonical_numbering_and_queued_state():
    log = ConversationLog()
    first = log.queue_turn(conversation_id=CONV, user_text="こんにちは")
    assert first.turn_id == f"{CONV}:t0"
    assert first.status == "queued"
    assert first.completed_at is None
    assert log.messages(CONV) == [
        {
            "content": "こんにちは",
            "created_at": first.started_at,
            "role": "user",
        }
    ]
    log.mark_started(first.turn_id)
    log.complete_turn(first.turn_id, stop_reason="end_turn")
    second = log.queue_turn(conversation_id=CONV, user_text="次")
    assert second.turn_id == f"{CONV}:t1"
    assert log.next_turn_id(CONV) == f"{CONV}:t2"


def test_queue_turn_conflict_maps_to_422_turn_id_conflict():
    log = ConversationLog()
    log.queue_turn(conversation_id=CONV, user_text="一つ目")
    with pytest.raises(TurnIdConflictError) as excinfo:
        log.queue_turn(conversation_id=CONV, user_text="重複", turn_id=f"{CONV}:t0")
    assert excinfo.value.turn_id == f"{CONV}:t0"
    problem = _problem(excinfo.value)
    assert problem.status_code == 422
    assert problem.code == "unknown_error.turn_id_conflict"


def test_queue_turn_rejects_non_canonical_client_id():
    log = ConversationLog()
    with pytest.raises(InvalidRequestError):
        log.queue_turn(
            conversation_id=CONV,
            turn_id="sess-bridge:t9",
            user_text="不正な採番",
        )
    with pytest.raises(InvalidRequestError):
        log.queue_turn(conversation_id=CONV, turn_id="乱数id", user_text="不正")
    problem = _problem(
        InvalidRequestError("body が不正"),
    )
    assert problem.status_code == 400
    assert problem.code == "unknown_error.invalid_request"


def test_queue_turn_accepts_client_agui_turn_ids():
    log = ConversationLog()
    turn = log.queue_turn(
        conversation_id=CONV,
        turn_id="agui-req-1",
        user_text="client 採番",
    )
    assert turn.turn_id == "agui-req-1"
    assert [entry.turn_id for entry in log.turns(CONV)] == ["agui-req-1"]


def test_mark_started_transition_and_errors():
    log = ConversationLog()
    turn = log.queue_turn(conversation_id=CONV, user_text="実行")
    started = log.mark_started(turn.turn_id)
    assert started.status == "started"
    assert log.mark_started(turn.turn_id).status == "started"
    log.complete_turn(turn.turn_id, stop_reason="end_turn")
    with pytest.raises(TurnIdConflictError):
        log.mark_started(turn.turn_id)
    with pytest.raises(UnknownTurnError) as excinfo:
        log.mark_started(f"{CONV}:t404")
    problem = _problem(excinfo.value)
    assert problem.status_code == 404
    assert problem.code == "unknown_error.no_active_turn"


def test_active_turn_and_is_busy_lifecycle():
    log = ConversationLog()
    assert not log.is_busy(CONV)
    queued = log.queue_turn(conversation_id=CONV, user_text="queued")
    assert log.is_busy(CONV)
    assert log.active_turn(CONV) is queued
    log.mark_started(queued.turn_id)
    assert log.active_turn(CONV) is queued
    log.complete_turn(queued.turn_id, stop_reason="aborted")
    assert not log.is_busy(CONV)
    assert log.active_turn(CONV) is None


@pytest.mark.parametrize(
    ("stop_reason", "expected_status"),
    [
        ("end_turn", "completed"),
        ("aborted", "cancelled"),
        ("permission_required", "interrupted"),
        ("send_failed", "failed"),
        ("tool_not_executed", "failed"),
        ("turn_timeout", "timed_out"),
    ],
)
def test_complete_turn_maps_stop_reason_to_terminal_status(
    stop_reason: str,
    expected_status: str,
):
    log = ConversationLog()
    turn = log.start_turn(conversation_id=CONV, user_text="本文")
    completed = log.complete_turn(turn.turn_id, stop_reason=stop_reason)
    assert completed.status == expected_status
    assert completed.to_dict()["status"] == expected_status


def test_queued_turn_completes_without_mark_started():
    log = ConversationLog()
    turn = log.queue_turn(conversation_id=CONV, session_id=CONV, user_text="queued")
    completed = log.complete_turn(turn.turn_id, stop_reason="turn_timeout")
    assert completed.status == "timed_out"
    assert log.turns(CONV)[0].status == "timed_out"


def test_start_turn_stays_compatible_with_queue_turn_numbering():
    log = ConversationLog()
    queued = log.queue_turn(conversation_id=CONV, user_text="q")
    legacy = log.start_turn(conversation_id=CONV, user_text="legacy")
    assert legacy.turn_id == f"{CONV}:t1"
    assert queued.turn_id == f"{CONV}:t0"
    assert legacy.status == "started"
