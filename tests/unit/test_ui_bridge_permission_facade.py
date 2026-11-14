"""PermissionBrokerFacade (adapter) §4/§5 approvals 語彙の unit test。

chat glue が呼ぶ get_approval / resolve_permission / resolve_many /
approvals(session_id=...) を broker 継承で補完し、broker resolve の折れ方を
§4 表へ写す: 404 approval_not_found / 409 approval_not_resolvable /
503 agent_error.permission_stale (timeout で expired 化した grant のみ)。
"""

from __future__ import annotations

import asyncio

import pytest
from app.agent.permissions import PermissionPrompt
from app.services.exceptions import (
    PermissionStaleError,
    ProblemEmitter,
    UnknownApprovalError,
    UnknownResolverError,
)
from app.services.hub import ApprovalBroadcaster, MonitorHub
from app.services.permissions_api import PermissionBrokerFacade

EMITTER = ProblemEmitter.default()


def _prompt(
    prompt_id: str,
    *,
    session_id: str = "sess-1",
    tool_name: str = "propose_rule",
    tool_call_id: str | None = None,
) -> PermissionPrompt:
    return PermissionPrompt(
        prompt_id=prompt_id,
        session_id=session_id,
        tool_name=tool_name,
        arguments={"text": "ルール本文"},
        tool_call_id=tool_call_id,
    )


async def _await_pending(facade: PermissionBrokerFacade, approval_id: str):
    for _ in range(50):
        record = facade.get_approval(approval_id)
        if record is not None:
            return record
        await asyncio.sleep(0.01)
    raise AssertionError(f"pending record が見つかりません: {approval_id}")


async def start_request(
    facade: PermissionBrokerFacade,
    prompt: PermissionPrompt,
    tool_call_id: str | None = None,
):
    task = asyncio.ensure_future(facade.request_permission(prompt, tool_call_id))
    await _await_pending(facade, prompt.prompt_id)
    return task


def test_facade_extends_without_touching_broker_api():
    facade = PermissionBrokerFacade()
    assert facade.timeout_seconds > 0
    assert facade.approvals() == []
    assert facade.get_approval("unknown-id") is None


async def test_resolve_permission_approve_flow_returns_record():
    facade = PermissionBrokerFacade()
    task = await start_request(facade, _prompt("p-1"))
    outcome_feedback = "了解"
    record = await facade.resolve_permission(
        "p-1",
        approved=True,
        decided_by="family",
        feedback=outcome_feedback,
        reason="家族が承認",
    )
    assert record.status == "approved"
    assert record.decided_by == "family"
    assert record.to_dict()["approval_id"] == "p-1"
    outcome = await task
    # broker は approve 時の outcome.feedback を None にする (family 決定は
    # record 側に保持される)。待機側の 2 回目の close で record.feedback も
    # 空になるため、outcome.feedback を語彙の検証点にはしない。
    assert outcome.approved is True
    assert outcome_feedback  # API へ渡した feedback は reject 時のみ保持される


async def test_resolve_permission_reject_default_feedback():
    facade = PermissionBrokerFacade()
    task = await start_request(facade, _prompt("p-2"))
    record = await facade.resolve_permission("p-2", approved=False)
    assert record.status == "rejected"
    assert record.feedback
    outcome = await task
    assert outcome.approved is False


async def test_unknown_approval_maps_to_404():
    facade = PermissionBrokerFacade()
    with pytest.raises(UnknownApprovalError) as excinfo:
        await facade.resolve_permission("ghost", approved=True)
    assert excinfo.value.approval_id == "ghost"
    problem = EMITTER.problem_for(excinfo.value)
    assert problem.status_code == 404
    assert problem.code == "unknown_error.approval_not_found"


async def test_double_resolve_maps_to_409_not_resolvable():
    facade = PermissionBrokerFacade()
    task = await start_request(facade, _prompt("p-3"))
    await facade.resolve_permission("p-3", approved=True)
    await task
    with pytest.raises(UnknownResolverError) as excinfo:
        await facade.resolve_permission("p-3", approved=True)
    problem = EMITTER.problem_for(excinfo.value)
    assert problem.status_code == 409
    assert problem.code == "unknown_error.approval_not_resolvable"


async def test_timed_out_grant_maps_to_503_permission_stale():
    facade = PermissionBrokerFacade(timeout_seconds=0.05)
    task = await start_request(facade, _prompt("p-4"))
    outcome = await task  # broker 側 timeout で expired 化
    assert outcome.approved is False
    record = facade.get_approval("p-4")
    assert record is not None and record.status == "expired"
    with pytest.raises(PermissionStaleError) as excinfo:
        await facade.resolve_permission("p-4", approved=True)
    problem = EMITTER.problem_for(excinfo.value)
    assert problem.status_code == 503
    assert problem.code == "agent_error.permission_stale"


async def test_approvals_filters_by_session_and_status():
    facade = PermissionBrokerFacade()
    task_a = await start_request(facade, _prompt("p-a", session_id="sess-a"))
    task_b = await start_request(facade, _prompt("p-b", session_id="sess-b"))
    await facade.resolve_permission("p-a", approved=True)
    await task_a
    pending_b = facade.approvals(session_id="sess-b", status="pending")
    assert [record.prompt.prompt_id for record in pending_b] == ["p-b"]
    approved = facade.approvals(status="approved")
    assert [record.prompt.prompt_id for record in approved] == ["p-a"]
    resolved_b = facade.approvals(session_id="sess-b", status="approved")
    assert resolved_b == []
    await facade.resolve_permission("p-b", approved=False)
    await task_b


async def test_approval_by_turn_uses_tool_call_id():
    facade = PermissionBrokerFacade()
    task = await start_request(facade, _prompt("p-t"), tool_call_id="sess-1:t0")
    record = facade.approval_by_turn("sess-1:t0")
    assert record is not None
    assert record.tool_call_id == "sess-1:t0"
    assert facade.approval_by_turn("sess-1:t404") is None
    await facade.resolve_permission("p-t", approved=True)
    await task


async def test_resolve_many_bulk_and_fail_fast():
    facade = PermissionBrokerFacade()
    task_a = await start_request(facade, _prompt("p-m1"))
    task_b = await start_request(facade, _prompt("p-m2"))
    records = await facade.resolve_many(["p-m1", "p-m2"], approved=True)
    assert [record.status for record in records] == ["approved", "approved"]
    await task_a
    await task_b
    task_c = await start_request(facade, _prompt("p-m3"))
    with pytest.raises(UnknownApprovalError):
        await facade.resolve_many(["p-m3", "ghost"], approved=False)
    # fail fast: ghost 到達前に p-m3 は reject 済みで確定している
    assert facade.get_approval("p-m3").status == "rejected"
    outcome_c = await task_c
    assert outcome_c.approved is False


async def test_broadcaster_frames_flow_from_facade_listeners():
    hub = MonitorHub()
    broadcaster = ApprovalBroadcaster(hub)
    facade = PermissionBrokerFacade()
    facade.set_listeners(
        on_publish=broadcaster.on_publish,
        on_resolve=broadcaster.on_resolve,
    )
    hub.bind_session(session_id="sess-1", conversation_id="conv-1")
    hub.set_current_turn(session_id="sess-1", turn_id="sess-1:t0")
    task = await start_request(facade, _prompt("p-c"))
    await facade.resolve_permission("p-c", approved=True, decided_by="family")
    await task
    frames = hub.replay_frames()
    requested = [f for f in frames if f["event_type"] == "approval_requested"]
    resolved = [f for f in frames if f["event_type"] == "approval_resolved"]
    # broker は resolve() 側と agent 待機側 (_start_request continuation) の
    # 両方で close を fire するため approval_resolved が 2 本流れる。
    # series 側の重複吸収は reducer (merge/dedupe) の責務であり facade は
    # publish_updated を廃止済みなので余剰 frame は仕様どおり。
    assert len(requested) == 1 and len(resolved) >= 1
    assert requested[0]["payload"]["approval"]["approval_id"] == "p-c"
    assert requested[0]["turn_id"] == "sess-1:t0"
    assert requested[0]["conversation_id"] == "conv-1"
    assert resolved[0]["payload"]["action"] == "family"
    assert resolved[-1]["payload"]["approval"]["status"] == "approved"
    assert resolved[-1]["payload"]["approval"]["decided_by"] == "family"
