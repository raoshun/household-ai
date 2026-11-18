"""monitor 承認 frame 名(unit test / contract §2)。

`ApprovalBroadcaster` が発火する approval 系 frame 名を README 準拠語彙で固定する:
`approval_requested` / `approval_resolved` / `pending_approvals_updated`。
`approval_updated` は廃止済みなので混在させない。
"""

from __future__ import annotations

from app.services.hub import ApprovalBroadcaster, MonitorHub


class _StubPrompt:
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id


class _StubRecord:
    """ApprovalBroadcaster が読む属性だけを持つ承認レコード風の stub。

    参照するのは ``record.to_dict()`` と ``record.prompt.session_id`` のみ
    (hub の publish_for_session 呼び出しで検証済み)。
    """

    def __init__(self, session_id: str = "sess-1") -> None:
        self.prompt = _StubPrompt(session_id)

    def to_dict(self) -> dict[str, object]:
        return {"approval_id": "apr-1", "session_id": self.prompt.session_id}


def _frame_types(hub: MonitorHub) -> list[str]:
    return [str(frame["event_type"]) for frame in hub.replay_frames()]


def test_publish_updated_emits_pending_approvals_updated() -> None:
    hub = MonitorHub()
    broadcaster = ApprovalBroadcaster(hub)

    broadcaster.publish_updated(_StubRecord())

    assert _frame_types(hub) == ["pending_approvals_updated"]
    frame = hub.replay_frames()[-1]
    approval = {"approval_id": "apr-1", "session_id": "sess-1"}
    assert frame["payload"] == {"approval": approval}


def test_approval_lifecycle_frame_names() -> None:
    hub = MonitorHub()
    broadcaster = ApprovalBroadcaster(hub)
    record = _StubRecord()

    broadcaster.on_publish(record)
    broadcaster.on_resolve(record, resolved_by="user")
    broadcaster.publish_updated(record)

    assert _frame_types(hub) == [
        "approval_requested",
        "approval_resolved",
        "pending_approvals_updated",
    ]


def test_obsolete_approval_updated_frame_name_absent() -> None:
    hub = MonitorHub()
    broadcaster = ApprovalBroadcaster(hub)
    record = _StubRecord()

    broadcaster.on_publish(record)
    broadcaster.on_resolve(record, resolved_by="family")
    broadcaster.publish_updated(record)

    assert "approval_updated" not in _frame_types(hub)
