"""セッション／ターン系の正規 monitor frame 用語と session event 集約.

README §9.6（実行監視: tool execution / 実行結果 / 承認 / session lifecycle /
session error / token・cost）と §15 Phase 1 要求（session event に tool calls /
approvals / session lifecycle / errors / context usage を含める）を満たす
single vocabulary を定義する。

Frame 語彙（chat SSE ``event: turn`` の data 兼 monitor frame、README §9.6 準拠）:
``agent_run_started``, ``run_queued``, ``run_cancelled``, ``run_finished``,
``message_complete``, ``assistant_text_delta``, ``thinking_delta``,
``tool_call_started``, ``tool_call_args``, ``tool_call_complete``,
``permission_requested``, ``permission_resolved``, ``approval_requested``,
``approval_resolved``, ``pending_approvals_updated``, ``error``,
``heartbeat``, ``context_usage``, ``usage_warning``,
``session_created``, ``session_closed``, ``session_forked``,
``session_reaped``, ``session_idle``, ``session_event_recorded``,
``judgment_created``, ``rule_changed``

tool frame は ``tool_call_id`` と ``parentMessageId`` を持ち、AG-UI
``TOOL_CALL_START.toolCallId`` / ``parentMessageId`` と同一値を使う。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

__all__ = [
    "APPROVAL_FRAME_TYPES",
    "CONTEXT_USAGE_FRAME_TYPES",
    "ERROR_FRAME_TYPES",
    "SESSION_EVENT_FRAME_TYPES",
    "SESSION_LIFECYCLE_FRAME_TYPES",
    "TOOL_FRAME_TYPES",
    "TURN_OUTCOME_FRAME_TYPES",
    "MonitorEventType",
    "TurnOutcome",
    "approval_payloads",
    "attach_approvals",
    "dedupe_frames",
    "error_type_for",
    "flatten_human_confirmation",
    "format_context_usage",
    "format_cost",
    "frame",
    "group_by_conversation",
    "group_by_turn",
    "is_tool_frame",
    "latest_status_by_turn",
    "merge_frames",
    "monitor_run_started",
    "pending_turn_outcome",
    "sum_total_cost",
    "sum_total_usage",
    "usage_report_reason",
    "usage_warning_for",
]


class MonitorEventType(StrEnum):
    AGENT_RUN_STARTED = "agent_run_started"
    RUN_QUEUED = "run_queued"
    RUN_CANCELLED = "run_cancelled"
    RUN_FINISHED = "run_finished"
    MESSAGE_COMPLETE = "message_complete"
    ASSISTANT_TEXT_DELTA = "assistant_text_delta"
    THINKING_DELTA = "thinking_delta"
    TOOL_CALL_STARTED = "tool_call_started"
    TOOL_CALL_ARGS = "tool_call_args"
    TOOL_CALL_COMPLETE = "tool_call_complete"
    PERMISSION_REQUESTED = "permission_requested"
    PERMISSION_RESOLVED = "permission_resolved"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_RESOLVED = "approval_resolved"
    PENDING_APPROVALS_UPDATED = "pending_approvals_updated"
    ERROR = "error"
    SESSION_CREATED = "session_created"
    SESSION_CLOSED = "session_closed"
    SESSION_FORKED = "session_forked"
    SESSION_REAPED = "session_reaped"
    SESSION_IDLE = "session_idle"
    SESSION_EVENT_RECORDED = "session_event_recorded"
    JUDGMENT_CREATED = "judgment_created"
    RULE_CHANGED = "rule_changed"
    HEARTBEAT = "heartbeat"
    CONTEXT_USAGE = "context_usage"
    USAGE_WARNING = "usage_warning"


TURN_OUTCOME_FRAME_TYPES: frozenset[str] = frozenset(
    {
        MonitorEventType.RUN_FINISHED,
        MonitorEventType.RUN_CANCELLED,
        MonitorEventType.ERROR,
    }
)

TOOL_FRAME_TYPES: frozenset[str] = frozenset(
    {
        MonitorEventType.TOOL_CALL_STARTED,
        MonitorEventType.TOOL_CALL_ARGS,
        MonitorEventType.TOOL_CALL_COMPLETE,
    }
)

APPROVAL_FRAME_TYPES: frozenset[str] = frozenset(
    {
        MonitorEventType.PERMISSION_REQUESTED,
        MonitorEventType.PERMISSION_RESOLVED,
        MonitorEventType.APPROVAL_REQUESTED,
        MonitorEventType.APPROVAL_RESOLVED,
        MonitorEventType.PENDING_APPROVALS_UPDATED,
    }
)

ERROR_FRAME_TYPES: frozenset[str] = frozenset({MonitorEventType.ERROR})

#: UI の session event timeline が受け取る hub 由来 monitor frame types.
SESSION_EVENT_FRAME_TYPES: frozenset[str] = frozenset(
    {
        MonitorEventType.SESSION_CREATED,
        MonitorEventType.SESSION_CLOSED,
        MonitorEventType.SESSION_FORKED,
        MonitorEventType.SESSION_REAPED,
        MonitorEventType.SESSION_IDLE,
        MonitorEventType.SESSION_EVENT_RECORDED,
        MonitorEventType.APPROVAL_REQUESTED,
        MonitorEventType.APPROVAL_RESOLVED,
        MonitorEventType.PENDING_APPROVALS_UPDATED,
        MonitorEventType.JUDGMENT_CREATED,
        MonitorEventType.RULE_CHANGED,
        MonitorEventType.ERROR,
    }
)

SESSION_LIFECYCLE_FRAME_TYPES: frozenset[str] = frozenset(
    {
        MonitorEventType.SESSION_CREATED,
        MonitorEventType.SESSION_CLOSED,
        MonitorEventType.SESSION_FORKED,
        MonitorEventType.SESSION_REAPED,
        MonitorEventType.SESSION_IDLE,
        MonitorEventType.SESSION_EVENT_RECORDED,
    }
)

CONTEXT_USAGE_FRAME_TYPES: frozenset[str] = frozenset(
    {
        MonitorEventType.RUN_FINISHED,
        MonitorEventType.SESSION_EVENT_RECORDED,
        MonitorEventType.CONTEXT_USAGE,
        MonitorEventType.USAGE_WARNING,
    }
)

#: 1 turn 中の event type 昇格順（turn 毎の latest status join 用）。
_EVENT_RANK: dict[str, int] = {
    MonitorEventType.AGENT_RUN_STARTED: 0,
    MonitorEventType.RUN_QUEUED: 0,
    MonitorEventType.ASSISTANT_TEXT_DELTA: 1,
    MonitorEventType.THINKING_DELTA: 2,
    MonitorEventType.PERMISSION_REQUESTED: 3,
    MonitorEventType.TOOL_CALL_STARTED: 3,
    MonitorEventType.TOOL_CALL_ARGS: 4,
    MonitorEventType.TOOL_CALL_COMPLETE: 5,
    MonitorEventType.PERMISSION_RESOLVED: 6,
    MonitorEventType.APPROVAL_REQUESTED: 6,
    MonitorEventType.APPROVAL_RESOLVED: 7,
    MonitorEventType.PENDING_APPROVALS_UPDATED: 7,
    MonitorEventType.ERROR: 8,
    MonitorEventType.RUN_CANCELLED: 8,
    MonitorEventType.MESSAGE_COMPLETE: 9,
    MonitorEventType.RUN_FINISHED: 9,
}

_COST_CURRENCY = "JPY"


def frame(
    event_type: str,
    *,
    seq: int,
    conversation_id: str,
    turn_id: str | None = None,
    created_at: str,
    payload: Mapping[str, Any] | None = None,
    event_id: str | None = None,
    session_id: str | None = None,
    thread_id: str | None = None,
    nested: bool = True,
) -> dict[str, Any]:
    """chat SSE / monitor 共有の dict frame を作る (契約 §2 の key 集合).

    ``nested=True`` (既定) は payload を ``payload`` key へ入れ、frame 側 keys は
    ``{event_id, seq, event_type, created_at, turn_id, thread_id, session_id,
    payload}`` に固定する。join するため ``conversation_id`` は thread_id と同値で
    残す。``nested=False`` は従来 form (payload を frame 直下へ merge)。
    """
    data: dict[str, Any] = {
        "conversation_id": conversation_id,
        "created_at": created_at,
        "event_id": event_id,
        "event_type": event_type,
        "seq": seq,
        "session_id": session_id,
        "thread_id": thread_id if thread_id is not None else conversation_id,
        "turn_id": turn_id,
    }
    if nested:
        data["payload"] = dict(payload or {})
    elif payload:
        data.update(payload)
    return data


def _seq_of(frame_dict: Mapping[str, Any]) -> int:
    try:
        return int(frame_dict.get("seq") or 0)
    except (TypeError, ValueError):
        return 0


def _rank(event_type: str) -> int:
    return _EVENT_RANK.get(event_type, -1)


def dedupe_frames(frames: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """``(seq, event_type)`` の keep-first dedupe（subscribe+replay 重複対策）."""
    seen: set[tuple[Any, str]] = set()
    out: list[dict[str, Any]] = []
    for raw in frames:
        key = (raw.get("seq"), str(raw.get("event_type") or ""))
        if key in seen:
            continue
        seen.add(key)
        out.append(dict(raw))
    return out


def merge_frames(*groups: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """複数 series（turn frames + approval frames）を seq 昇順の 1 series へ."""
    merged = dedupe_frames([item for group in groups for item in group])
    return sorted(merged, key=lambda f: (_seq_of(f), str(f.get("created_at") or "")))


def latest_status_by_turn(
    frames: Iterable[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    """turn 毎に「latest seq + 上位順」の 1 frame を選ぶ (join 規則の正本)."""
    best: dict[str, dict[str, Any]] = {}
    best_rank: dict[str, int] = {}
    for raw in frames:
        turn_id = raw.get("turn_id")
        if not isinstance(turn_id, str) or not turn_id:
            continue
        frame_dict = dict(raw)
        rank = _rank(str(frame_dict.get("event_type") or ""))
        current = best.get(turn_id)
        current_seq = _seq_of(current) if current is not None else -1
        better_seq = _seq_of(frame_dict) > current_seq
        same_seq = _seq_of(frame_dict) == current_seq and rank > best_rank.get(
            turn_id, -1
        )
        if current is None or better_seq or same_seq:
            best[turn_id] = frame_dict
            best_rank[turn_id] = rank
    return best


def group_by_turn(
    frames: Iterable[Mapping[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for raw in frames:
        turn_id = raw.get("turn_id")
        if not isinstance(turn_id, str) or not turn_id:
            continue
        grouped.setdefault(turn_id, []).append(dict(raw))
    for items in grouped.values():
        items.sort(key=lambda f: (_seq_of(f), str(f.get("created_at") or "")))
    return grouped


def group_by_conversation(
    frames: Iterable[Mapping[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for raw in frames:
        conversation_id = raw.get("conversation_id")
        if not isinstance(conversation_id, str) or not conversation_id:
            continue
        grouped.setdefault(conversation_id, []).append(dict(raw))
    for items in grouped.values():
        items.sort(key=lambda f: (_seq_of(f), str(f.get("created_at") or "")))
    return grouped


def is_tool_frame(frame_dict: Mapping[str, Any]) -> bool:
    return str(frame_dict.get("event_type") or "") in TOOL_FRAME_TYPES


def error_type_for(frame_dict: Mapping[str, Any]) -> str:
    """frame の error_type を §9.6 分類へ寄せる（未知は ``runtime_error``）."""
    raw = str(frame_dict.get("error_type") or "")
    if raw:
        return raw
    event_type = str(frame_dict.get("event_type") or "")
    if (
        event_type == MonitorEventType.TOOL_CALL_COMPLETE
        and str((frame_dict.get("payload") or {}).get("status") or "") == "denied"
    ):
        return "no_matching_grant"
    if event_type == MonitorEventType.RUN_CANCELLED:
        return "turn_cancelled"
    if event_type == MonitorEventType.RUN_FINISHED:
        return "turn_finished"
    return "runtime_error"


@dataclass(slots=True)
class TurnOutcome:
    """未終了 turn の stop_reason 推定結果（review 要求の区別を満たす）."""

    stop_reason: str | None
    source: str

    def to_dict(self) -> dict[str, Any]:
        return {"source": self.source, "stop_reason": self.stop_reason}


def pending_turn_outcome(
    turn_id: str,
    frames: Sequence[Mapping[str, Any]],
) -> TurnOutcome:
    """terminal frame のない turn の stop_reason を frame から推定する."""
    statuses = {
        str(f.get("event_type") or "") for f in frames if f.get("turn_id") == turn_id
    }
    if MonitorEventType.ERROR in statuses:
        return TurnOutcome(stop_reason="send_failed", source="frame")
    if MonitorEventType.RUN_CANCELLED in statuses:
        return TurnOutcome(stop_reason="aborted", source="frame")
    tool_seen = statuses & {
        MonitorEventType.TOOL_CALL_STARTED,
        MonitorEventType.TOOL_CALL_COMPLETE,
    }
    if MonitorEventType.PERMISSION_REQUESTED in statuses and not tool_seen:
        return TurnOutcome(stop_reason="permission_required", source="frame")
    if MonitorEventType.RUN_QUEUED in statuses:
        return TurnOutcome(stop_reason=None, source="queued")
    if monitor_run_started(statuses):
        return TurnOutcome(stop_reason=None, source="frame")
    return TurnOutcome(stop_reason=None, source="absent")


def monitor_run_started(statuses: set[str]) -> bool:
    return MonitorEventType.AGENT_RUN_STARTED in statuses


def approval_payloads(
    records: Iterable[Mapping[str, Any]],
    *,
    resolved_by: str | None = None,
) -> list[dict[str, Any]]:
    """ApprovalRecord dict を nested payload へ変換（hub merge 対策）.

    hub ``to_frame()`` は payload keys を frame 直下へ merge するため、record map を
    そのまま渡すと record が serialize される（review 要求）。必ず nest する。
    """
    payloads: list[dict[str, Any]] = []
    for record in records:
        payload: dict[str, Any] = {"approval": dict(record)}
        if resolved_by is not None:
            payload["action"] = resolved_by
        payloads.append(payload)
    return payloads


def _approval_record_of(
    frame_dict: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    """nested / merged どちらの frame からも approval record を取り出す."""
    for key in ("approval", "record"):
        record = frame_dict.get(key)
        if isinstance(record, Mapping):
            return record
    payload = frame_dict.get("payload")
    if isinstance(payload, Mapping):
        nested = payload.get("approval") or payload.get("record")
        if isinstance(nested, Mapping):
            return nested
    return None


def attach_approvals(
    approval_frames: Sequence[Mapping[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """``turn_id`` を持つ approval frame を turn 単位で group する (chat 表示用)."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for raw in approval_frames:
        turn_id = raw.get("turn_id")
        if not isinstance(turn_id, str) or not turn_id:
            continue
        record = _approval_record_of(raw)
        if record is None:
            continue
        grouped.setdefault(turn_id, []).append(dict(record))
    return grouped


def sum_total_cost(usages: Iterable[Mapping[str, Any] | None]) -> dict[str, Any]:
    """context usage の cost を 1 つの total_cost へ集約（unit を明記）."""
    total = 0.0
    currency: str | None = None
    got = False
    for usage in usages:
        if usage is None:
            continue
        cost = usage.get("total_cost")
        if cost is None:
            continue
        total += float(cost)
        got = True
        unit = usage.get("cost_unit") or usage.get("currency")
        if unit:
            currency = currency or str(unit)
    final = currency or _COST_CURRENCY
    return {"currency": final, "unit": final, "value": round(total, 10) if got else 0.0}


def format_cost(total: Mapping[str, Any]) -> str:
    value = float(total.get("value") or 0.0)
    currency = str(total.get("currency") or _COST_CURRENCY)
    if currency == "JPY":
        return f"¥{value:,.0f}"
    return f"{value:.6f} {currency}"


_USAGE_META_KEYS = frozenset(
    {
        "created_at",
        "cost_unit",
        "currency",
        "seq",
        "stop_reason",
        "total_cost",
        "turn_id",
    }
)


def sum_total_usage(
    usages: Iterable[Mapping[str, Any] | None],
) -> dict[str, float] | None:
    """数値 usage field を key ごとに合計する (cost 系 key は除外)."""
    totals: dict[str, float] = {}
    for usage in usages:
        if not isinstance(usage, Mapping):
            continue
        for key, value in usage.items():
            if key in _USAGE_META_KEYS or isinstance(value, bool):
                continue
            if not isinstance(value, (int, float)):
                continue
            totals[str(key)] = totals.get(str(key), 0.0) + float(value)
    if not totals:
        return None
    return {key: round(value, 6) for key, value in sorted(totals.items())}


def usage_report_reason(usages: Iterable[Mapping[str, Any] | None]) -> str | None:
    """usage が 1 つも無いか / usage はあるが cost が無いかを判別する (§6)."""
    items = [usage for usage in usages if isinstance(usage, Mapping)]
    if not items or not any(sum_total_usage([item]) for item in items):
        return "no_usage_reported"
    if sum_total_cost(items).get("value") in (None, 0.0):
        return "no_cost_reported"
    return None


def _context_turn_row(turn: Mapping[str, Any]) -> dict[str, Any]:
    usage = turn.get("usage")
    seq = turn.get("seq")
    return {
        "turn_id": turn.get("turn_id"),
        "seq": turn.get("processed_turn") if seq is None else seq,
        "created_at": turn.get("created_at") or turn.get("started_at"),
        "stop_reason": turn.get("stop_reason"),
        "usage": dict(usage) if isinstance(usage, Mapping) else None,
    }


def format_context_usage(
    *,
    session_id: str,
    turns: Sequence[Mapping[str, Any]] = (),
    usages: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """§6 の context usage payload を組み立てる (frame と REST で同一 form)."""
    turn_rows = [_context_turn_row(turn) for turn in turns]
    usage_items: list[Mapping[str, Any]] = [
        *[row["usage"] for row in turn_rows if isinstance(row.get("usage"), Mapping)],
        *[usage for usage in usages if isinstance(usage, Mapping)],
    ]
    total_cost = sum_total_cost(usage_items)
    total_usage = sum_total_usage(usage_items)
    reason = usage_report_reason(usage_items)
    cost_available = reason != "no_cost_reported" and reason != "no_usage_reported"
    total_costs = [
        {
            "turn_id": row.get("turn_id"),
            "cost": sum_total_cost([row.get("usage")]),
            "usage": row.get("usage"),
        }
        for row in turn_rows
    ]
    return {
        "session_id": session_id,
        "turn_count": len(turn_rows) or len(usage_items),
        "cost_available": cost_available,
        "usage_unavailable_reason": reason,
        "total_cost": total_cost if cost_available else None,
        "total_cost_formatted": format_cost(total_cost),
        "total_usage": total_usage,
        "total_costs": total_costs,
        "turns": turn_rows,
    }


_CONTEXT_RATIO_KEYS = ("context_ratio", "context_used_ratio", "token_ratio")


def _context_ratio(usage: Mapping[str, Any]) -> float | None:
    """usage から context 使用率らしき数値を探す (未報告なら None)."""
    for key in _CONTEXT_RATIO_KEYS:
        value = usage.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    return None


def usage_warning_for(
    usage: Mapping[str, Any] | None,
    *,
    cost_threshold_jpy: float,
    token_threshold: float | None = None,
    turn_id: str | None = None,
) -> dict[str, Any] | None:
    """閾値超過時の `usage_warning` payload を作る (超過無ければ None, §9.6)."""
    if not isinstance(usage, Mapping):
        return None
    total = usage.get("total_cost")
    total_value = (
        float(total.get("value") or 0.0) if isinstance(total, Mapping) else 0.0
    )
    threshold: float | None = None
    kind = "cost"
    if 0.0 <= cost_threshold_jpy < total_value:
        threshold = cost_threshold_jpy
    else:
        ratio = _context_ratio(usage)
        if (
            token_threshold is not None
            and ratio is not None
            and ratio >= token_threshold
        ):
            threshold = token_threshold
            kind = "context_ratio"
    if threshold is None:
        return None
    return {
        "kind": kind,
        "threshold": threshold,
        "total_cost": total_value,
        "turn_id": turn_id if turn_id is not None else usage.get("session_id"),
    }


def flatten_human_confirmation(payload: Mapping[str, Any] | None) -> bool:
    """§9.4 / §12.4 の human confirmation を boolean へ平坦化する（review 要求）.

    - 家族／UI による承認のみ True（auto policy・allow-list・timeout は False）
    - 未承認・grant 無し・期限切れ・取消・pending は False
    """
    if payload is None:
        return False
    state = str(payload.get("state") or payload.get("status") or "")
    if state not in {"approved", "auto_executed", "allow_listed"}:
        return False
    decided_by = str(payload.get("decided_by") or "")
    auto_deciders = {
        "",
        "agent",
        "policy",
        "policy-timeout",
        "session-closed",
        "system",
    }
    return decided_by not in auto_deciders
