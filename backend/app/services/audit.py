"""§11 判定トレーサビリティ (ConversationLog / JudgmentStore / AuditTrail) を保持。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from app.clock import tokyo_iso
from app.services.exceptions import (
    InvalidRequestError,
    TurnIdConflictError,
    UnknownThreadError,
    UnknownTurnError,
)

AUDIT_ACTORS = ("family", "policy", "system", "agent")
AUDIT_KINDS = (
    "approval",
    "judgment",
    "provider",
    "rule_mutation",
    "session",
    "session_event",
    "tool_execution",
)

_KNOWN_STOP_REASONS = (
    "aborted",
    "end_turn",
    "interrupted",
    "permission_required",
    "send_failed",
    "tool_not_executed",
    "turn_timeout",
)

# turn の lifecycle state (§3 frame 系列と §4 busy/conflict 判定の正)。queued は
# 受付済みで pump 未開始、started は実行中。terminal 化は complete_turn 只在。
_ACTIVE_TURN_STATUSES = ("queued", "started")
_TERMINAL_TURN_STATUSES = (
    "completed",
    "cancelled",
    "interrupted",
    "failed",
    "timed_out",
)
_ALL_TURN_STATUSES = _ACTIVE_TURN_STATUSES + _TERMINAL_TURN_STATUSES
_TERMINAL_STATUS_BY_STOP_REASON = {
    "aborted": "cancelled",
    "end_turn": "completed",
    "interrupted": "interrupted",
    "permission_required": "interrupted",
    "send_failed": "failed",
    "tool_not_executed": "failed",
    "turn_timeout": "timed_out",
}


@dataclass(slots=True)
class ConversationTurn:
    """1 ターンの実行記録。§11 join (ConversationTurn.turn_id) の単位になる行。"""

    conversation_id: str
    turn_id: str
    user_text: str
    session_id: str | None = None
    message_id: str = ""
    assistant_text: str = ""
    tool_calls: dict[str, Any] = field(default_factory=dict)
    tool_call_refs: list[str] = field(default_factory=list)
    approval_refs: list[str] = field(default_factory=list)
    rules_proposed: list[str] = field(default_factory=list)
    rules_read: list[str] = field(default_factory=list)
    rules_touched: list[str] = field(default_factory=list)
    rule_references: list[dict[str, Any]] = field(default_factory=list)
    stop_reason: str = "end_turn"
    status: str = "started"
    error: dict[str, Any] | None = None
    judgment_id: str | None = None
    usage: dict[str, Any] | None = (
        None  # 未取得なら None (§9.4 追記)。Usage 推定値は置かない。
    )
    started_at: str = field(default_factory=tokyo_iso)
    completed_at: str | None = None
    processed_turn: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "approval_refs": list(self.approval_refs),
            "assistant_text": self.assistant_text,
            "completed_at": self.completed_at,
            "conversation_id": self.conversation_id,
            "error": self.error,
            "judgment_id": self.judgment_id,
            "message_id": self.message_id,
            "processed_turn": self.processed_turn,
            "rule_references": [dict(entry) for entry in self.rule_references],
            "rules_proposed": list(self.rules_proposed),
            "rules_read": list(self.rules_read),
            "rules_touched": list(self.rules_touched),
            "session_id": self.session_id,
            "started_at": self.started_at,
            "status": self.status,
            "stop_reason": self.stop_reason,
            "tool_call_refs": list(self.tool_call_refs),
            "tool_calls": dict(self.tool_calls),
            "turn_id": self.turn_id,
            "usage": self.usage,
            "user_text": self.user_text,
        }


class ConversationLog:
    """§9.3 / §9.4 / §11 会話履歴 (conversation_id keyed)。本文は apps 側で保持する。

    Provider history RPC (transcript) は Phase 0 で取得不能なため、§9.2 会話保持の
    family 操作 (list/read/interrupt/delete) は ConversationLog を正とする。turn の
    messages list を正とし、Assistant 本文は append_text() で逐次追記する。
    """

    def __init__(self) -> None:
        self._turns: dict[str, ConversationTurn] = {}
        self._messages: dict[str, list[dict[str, Any]]] = {}
        self._turn_order: list[str] = []
        self._turn_ids_by_conversation: dict[str, list[str]] = {}
        self._processed_turn_by_session: dict[str, int] = {}
        self._conversation_titles: dict[str, str] = {}

    # ------------------------------------------------------------------
    # §9.2 会話保持の family 操作 (list/read/rename/interrupt/delete)
    # ------------------------------------------------------------------

    def new_conversation_id(self) -> str:
        return f"conv-{uuid4().hex[:12]}"

    def start_conversation(
        self, conversation_id: str, *, title: str | None = None
    ) -> None:
        self._turn_ids_by_conversation.setdefault(conversation_id, [])
        if title:
            self._conversation_titles[conversation_id] = title

    def rename_conversation(self, conversation_id: str, title: str) -> None:
        if conversation_id not in self._turn_ids_by_conversation:
            raise UnknownThreadError(thread_id=conversation_id)
        self._conversation_titles[conversation_id] = title

    def conversation_title(self, conversation_id: str) -> str | None:
        return self._conversation_titles.get(conversation_id)

    def delete_conversation(self, conversation_id: str) -> None:
        for turn_id in self._turn_ids_by_conversation.pop(conversation_id, []):
            turn = self._turns.pop(turn_id, None)
            if turn is not None and turn.session_id:
                self._processed_turn_by_session.pop(turn.session_id, None)
        self._messages.pop(conversation_id, None)
        self._conversation_titles.pop(conversation_id, None)
        self._turn_order = [tid for tid in self._turn_order if tid in self._turns]

    def interrupt_turn(
        self, turn_id: str, *, reason: str | None = None
    ) -> ConversationTurn:
        """family 操作の interrupt (§9.2)。未完了 turn を interrupted 化する。"""
        turn = self._turns.get(turn_id)
        if turn is None:
            raise UnknownTurnError(turn_id=turn_id)
        if turn.status not in _ACTIVE_TURN_STATUSES:
            return turn
        return self.complete_turn(
            turn_id,
            stop_reason="interrupted",
            status="interrupted",
            error={"code": "interrupted", "message": reason or "family interrupt"},
        )

    def start_turn(
        self,
        *,
        conversation_id: str,
        user_text: str,
        session_id: str | None = None,
    ) -> ConversationTurn:
        turn_count = len(self._turn_ids_by_conversation.get(conversation_id, []))
        turn_id = f"{conversation_id}:t{turn_count}"
        processed = (
            self._processed_turn_by_session.get(session_id or conversation_id, 0) + 1
        )
        if session_id:
            self._processed_turn_by_session[session_id] = processed
        turn = ConversationTurn(
            conversation_id=conversation_id,
            processed_turn=processed,
            session_id=session_id,
            turn_id=turn_id,
            user_text=user_text,
        )
        self._turns[turn_id] = turn
        self._turn_order.append(turn_id)
        self._turn_ids_by_conversation.setdefault(conversation_id, []).append(turn_id)
        self._messages.setdefault(conversation_id, []).append(
            {"content": user_text, "created_at": turn.started_at, "role": "user"}
        )
        return turn

    def next_turn_id(self, conversation_id: str) -> str:
        """次の canonical turn id (`{conversation_id}:t{n}`、§4 conflict 判定用)。"""

        return (
            f"{conversation_id}:t"
            f"{len(self._turn_ids_by_conversation.get(conversation_id, []))}"
        )

    def queue_turn(
        self,
        *,
        conversation_id: str,
        user_text: str,
        turn_id: str | None = None,
        session_id: str | None = None,
    ) -> ConversationTurn:
        """turn を queued で受付る (§4 busy 409 / turn_id 422 の判定材料)。

        - 未指定なら canonical `{conversation_id}:t{n}` を採番する
        - client 指定 id が既存 turn と衝突なら `TurnIdConflictError` (422。active
          への再投話は呼び出し側で busy 409 に写す)
        - canonical 規則外の採番は拒否 (400)。client 発行 id は `agui` 接頭辞のみ
          受け付ける (§3.1 の client turnId 系列。他形式は validation 対象)
        """

        expected = self.next_turn_id(conversation_id)
        resolved_id = turn_id or expected
        existing = self._turns.get(resolved_id)
        if existing is not None:
            raise TurnIdConflictError(turn_id=resolved_id)
        if resolved_id != expected and not resolved_id.startswith("agui"):
            raise InvalidRequestError(
                f"turn_id が canonical 規則 ({expected}) と異なるか不正です: "
                f"{resolved_id}"
            )
        processed = (
            self._processed_turn_by_session.get(session_id or conversation_id, 0) + 1
        )
        if session_id:
            self._processed_turn_by_session[session_id] = processed
        turn = ConversationTurn(
            conversation_id=conversation_id,
            processed_turn=processed,
            session_id=session_id,
            status="queued",
            turn_id=resolved_id,
            user_text=user_text,
        )
        self._turns[resolved_id] = turn
        self._turn_order.append(resolved_id)
        self._turn_ids_by_conversation.setdefault(conversation_id, []).append(
            resolved_id
        )
        self._messages.setdefault(conversation_id, []).append(
            {"content": user_text, "created_at": turn.started_at, "role": "user"}
        )
        return turn

    def mark_started(self, turn_id: str) -> ConversationTurn:
        """queued -> started への遷移 (実行開始)。start 済みの再呼出しは無害。"""

        turn = self._turns.get(turn_id)
        if turn is None:
            raise UnknownTurnError(turn_id=turn_id)
        if turn.status == "queued":
            turn.status = "started"
            turn.started_at = tokyo_iso()
        if turn.completed_at is not None:
            raise TurnIdConflictError(turn_id=turn_id)
        return turn

    def active_turn(self, conversation_id: str) -> ConversationTurn | None:
        """queued / started の未完了 turn (1 thread に最大 1 つ)。"""

        for turn_id in reversed(
            self._turn_ids_by_conversation.get(conversation_id, [])
        ):
            turn = self._turns[turn_id]
            if turn.status in _ACTIVE_TURN_STATUSES and turn.completed_at is None:
                return turn
        return None

    def is_busy(self, conversation_id: str) -> bool:
        return self.active_turn(conversation_id) is not None

    def append_assistant_text(self, turn_id: str, text: str) -> None:
        turn = self._turns.get(turn_id)
        if turn is None:
            msg = f"turn が見つかりません: {turn_id}"
            raise LookupError(msg)
        turn.assistant_text += text

    def record_message(
        self,
        *,
        conversation_id: str,
        role: str,
        content: str,
        created_at: str | None = None,
        source: str = "chat",
        turn_id: str | None = None,
    ) -> dict[str, Any]:
        entry = {
            "content": content,
            "created_at": created_at or tokyo_iso(),
            "role": role,
            "source": source,
        }
        if turn_id is not None:
            # §11 join: message 行から ConversationTurn.turn_id へ遡れるようにする
            entry["turn_id"] = turn_id
        self._messages.setdefault(conversation_id, []).append(entry)
        return entry

    def complete_turn(
        self,
        turn_id: str,
        *,
        stop_reason: str = "end_turn",
        error: dict[str, Any] | None = None,
        judgment_id: str | None = None,
        usage: dict[str, Any] | None = None,
        status: str | None = None,
    ) -> ConversationTurn:
        turn = self._turns.get(turn_id)
        if turn is None:
            msg = f"turn が見つかりません: {turn_id}"
            raise LookupError(msg)
        if stop_reason not in _KNOWN_STOP_REASONS:
            msg = f"未知の stop_reason: {stop_reason}"
            raise ValueError(msg)
        turn.stop_reason = stop_reason
        turn.status = (
            status
            if status in _ALL_TURN_STATUSES and status != "queued"
            else _TERMINAL_STATUS_BY_STOP_REASON.get(stop_reason, "completed")
        )
        turn.error = error
        turn.judgment_id = judgment_id
        turn.usage = usage
        turn.completed_at = tokyo_iso()
        return turn

    def get_turn(self, turn_id: str) -> ConversationTurn | None:
        return self._turns.get(turn_id)

    def turns(self, conversation_id: str | None = None) -> list[ConversationTurn]:
        if conversation_id is None:
            return [self._turns[turn_id] for turn_id in self._turn_order]
        return [
            self._turns[turn_id]
            for turn_id in self._turn_ids_by_conversation.get(conversation_id, [])
        ]

    def conversation_ids(self) -> list[str]:
        return list(self._turn_ids_by_conversation)

    def messages(self, conversation_id: str) -> list[dict[str, Any]]:
        return [dict(entry) for entry in self._messages.get(conversation_id, [])]

    def conversations(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for conversation_id in self.conversation_ids():
            turns = self.turns(conversation_id)
            if not turns:
                continue
            last = turns[-1]
            rules_touched: list[str] = []
            for turn in turns:
                for rule_id in turn.rules_touched:
                    if rule_id not in rules_touched:
                        rules_touched.append(rule_id)
            out.append(
                {
                    "conversation_id": conversation_id,
                    "created_at": turns[0].started_at,
                    "last_stop_reason": last.stop_reason,
                    "last_updated_at": last.completed_at or last.started_at,
                    "message_count": len(self.messages(conversation_id)),
                    "rules_touched": rules_touched,
                    "session_id": last.session_id,
                    "turn_count": len(turns),
                }
            )
        return out


@dataclass(slots=True)
class JudgmentRecord:
    """§11 出力。§12 固定形式 (refs + basis + human_confirmation + missing)。"""

    judgment_id: str
    kind: str
    decision: str
    basis: list[dict[str, Any]]
    human_confirmation: dict[str, Any] | None
    created_at: str
    rule_refs: list[dict[str, Any]] = field(default_factory=list)
    approval_refs: list[str] = field(default_factory=list)
    audit_refs: list[str] = field(default_factory=list)
    tool_call_refs: list[str] = field(default_factory=list)
    session_event_refs: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "approval_refs": list(self.approval_refs),
            "audit_refs": list(self.audit_refs),
            "basis": [dict(entry) for entry in self.basis],
            "created_at": self.created_at,
            "decision": self.decision,
            "human_confirmation": self.human_confirmation,
            "judgment_id": self.judgment_id,
            "kind": self.kind,
            "missing": list(self.missing),
            "notes": self.notes,
            "rule_references": [dict(entry) for entry in self.rule_refs],
            "session_event_refs": list(self.session_event_refs),
            "tool_call_refs": list(self.tool_call_refs),
        }


class JudgmentStore:
    """§11 deterministic judgment (approval + tool + rule refs) を保存する。"""

    def __init__(self) -> None:
        self._records: dict[str, JudgmentRecord] = {}

    def next_id(self) -> str:
        return f"jgm-{len(self._records) + 1:04d}"

    def record(
        self,
        *,
        judgment_id: str,
        kind: str,
        decision: str,
        basis: list[dict[str, Any]] | None = None,
        human_confirmation: dict[str, Any] | None = None,
        rule_refs: list[dict[str, Any]] | None = None,
        approval_refs: list[str] | None = None,
        audit_refs: list[str] | None = None,
        tool_call_refs: list[str] | None = None,
        session_event_refs: list[str] | None = None,
        created_at: str | None = None,
        missing: list[str] | None = None,
        notes: str = "",
    ) -> JudgmentRecord:
        record = JudgmentRecord(
            approval_refs=list(approval_refs or []),
            audit_refs=list(audit_refs or []),
            basis=list(basis or []),
            created_at=created_at or tokyo_iso(),
            decision=decision,
            human_confirmation=human_confirmation,
            judgment_id=judgment_id,
            kind=kind,
            missing=list(missing or []),
            notes=notes,
            rule_refs=list(rule_refs or []),
            session_event_refs=list(session_event_refs or []),
            tool_call_refs=list(tool_call_refs or []),
        )
        self._records[record.judgment_id] = record
        return record

    def list(self) -> list[JudgmentRecord]:
        return list(self._records.values())

    def get(self, judgment_id: str) -> JudgmentRecord | None:
        return self._records.get(judgment_id)


@dataclass(slots=True)
class AuditEntry:
    entry_id: str
    kind: str
    actor: str
    action: str
    target: str
    detail: dict[str, Any]
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "actor": self.actor,
            "created_at": self.created_at,
            "detail": dict(self.detail),
            "entry_id": self.entry_id,
            "kind": self.kind,
            "target": self.target,
        }


class AuditTrail:
    """§9.6 / §11 監査 trace。detail に秘密情報 (token 等) は保存しない。"""

    def __init__(self, max_entries: int = 10_000) -> None:
        if max_entries <= 0:
            msg = "max_entries は正の整数で指定して下さい"
            raise ValueError(msg)
        self._entries: list[AuditEntry] = []
        self._max_entries = max_entries
        self._sequence = 0

    def record(
        self,
        *,
        kind: str,
        actor: str,
        action: str,
        target: str,
        detail: dict[str, Any] | None = None,
        created_at: str | None = None,
    ) -> AuditEntry:
        if kind not in AUDIT_KINDS:
            msg = f"未知の audit kind: {kind}"
            raise ValueError(msg)
        if actor not in AUDIT_ACTORS:
            msg = f"未知の audit actor: {actor}"
            raise ValueError(msg)
        entry = AuditEntry(
            action=action,
            actor=actor,
            created_at=created_at or tokyo_iso(),
            detail=dict(detail or {}),
            entry_id=self._next_entry_id(),
            kind=kind,
            target=target,
        )
        self._entries.append(entry)
        if len(self._entries) > self._max_entries:
            self._entries.pop(0)
        return entry

    def _next_entry_id(self) -> str:
        self._sequence += 1
        return f"aud-{self._sequence:06d}"

    def entries(self, kind: str | None = None) -> list[AuditEntry]:
        if kind is None:
            return list(self._entries)
        return [entry for entry in self._entries if entry.kind == kind]

    def timeline(self, session_id: str | None = None) -> list[dict[str, Any]]:
        """session_id 指定時は detail.session_id 完全一致のみ返す (§12.4 join 固定)。

        tool_call_id / request_id は join 条件に含めない。primary key は
        ConversationTurn.turn_id とし、detail.turn_id で追跡する。
        """

        if session_id is None:
            return [entry.to_dict() for entry in self._entries]
        return [
            entry.to_dict()
            for entry in self._entries
            if entry.detail.get("session_id") == session_id
        ]


__all__ = [
    "AUDIT_ACTORS",
    "AUDIT_KINDS",
    "AuditEntry",
    "AuditTrail",
    "ConversationLog",
    "ConversationTurn",
    "JudgmentRecord",
    "JudgmentStore",
]
