"""対話型 permission 判断 (README 2.5.4 / 9.5 / 10.3 の Phase 0 実装)。

provider.py の `on_permission_request` handler から await される hook をここで定義し、
`approval_mode="ask"` の Custom Tool を family human の判断まで停止させる
(判断根拠と approval log を残すのが 2.5.3 / 2.5.4 の必須条件)。

この module は Copilot SDK 型を import しない (6.2 の layering contract)。
provider 側が SDK 型から抽出した素の値だけで作る prompt を受け取り、戻り値を
provider が SDK の PermissionDecision へ変換する。待機中の prompt は pending
registry として REST / SSE のどちらからも参照でき、§10.3 の承認 API で解決される。
期限までに解決がなければ policy 既定で拒否する (判断のない実行はしない)。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from ..clock import tokyo_iso, tokyo_now

logger = logging.getLogger(__name__)

DEFAULT_PERMISSION_TIMEOUT_SECONDS = 120.0
"""human 応答待ちの上限。超過は deny にする。"""

DENY_FEEDBACK_NO_RESPONSE = "承認が得られなかったため tool を実行しませんでした。"
DENY_FEEDBACK_REJECTED = "family human が tool の実行を却下しました。"


@dataclass(frozen=True, slots=True)
class PermissionPrompt:
    """SDK 非依存の permission 依頼 (provider が SDK 型から変換する)。

    `arguments` は SDK が渡した tool input をそのまま保持する (監査ログへ素の値で
    載せるためここで schema 検証はしない。引数検証は 6.2 の tool 契約側の仕事)。
    """

    prompt_id: str
    session_id: str
    tool_name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=tokyo_iso)
    tool_call_id: str | None = None
    """SDK の tool call 識別子 (監査 §11 と UI 突合用。判定には使わない)。"""

    def to_dict(self) -> dict[str, Any]:
        return {
            "prompt_id": self.prompt_id,
            "session_id": self.session_id,
            "tool_name": self.tool_name,
            "arguments": dict(self.arguments),
            "created_at": self.created_at,
            "tool_call_id": self.tool_call_id,
        }


@dataclass(frozen=True, slots=True)
class PermissionOutcome:
    """hook の戻り値。provider が SDK の approve / reject へ変換する。"""

    approved: bool
    decided_by: str = "family"
    feedback: str | None = None
    reason: str = ""


InteractivePermissionHook = Callable[[PermissionPrompt], Awaitable[PermissionOutcome]]
"""provider が session 単位で保持する hook (§2.5.4 の PermissionHandler port)。"""


@dataclass(slots=True)
class PermissionRecord:
    """pending / 解決済み判断の監査レコード (§11 approvals / audit_logs 相当)。"""

    prompt: PermissionPrompt
    status: str = "pending"
    """pending | approved | rejected | expired | cancelled。"""

    decided_by: str = ""
    feedback: str = ""
    reason: str = ""
    created_at: str = field(default_factory=tokyo_iso)
    updated_at: str = field(default_factory=tokyo_iso)
    resolved_at: str | None = None
    tool_call_id: str | None = None
    sequence: int = 0
    """session 内の判断順序 (§11 追跡用。判定材料には使わない)。"""

    timeout_seconds: float = DEFAULT_PERMISSION_TIMEOUT_SECONDS
    expires_at: str = ""
    """policy timeout での自動 reject 予測時刻 (§9.4「期限後に自動 reject」)。"""

    consumed_at: str | None = None
    """consumed_at が入ると grant は使い切り。同一判断の再実行は拒否される (§12.4)。"""

    def to_dict(self) -> dict[str, Any]:
        return {
            "approval_id": self.prompt.prompt_id,
            "session_id": self.prompt.session_id,
            "tool_name": self.prompt.tool_name,
            "arguments": dict(self.prompt.arguments),
            "status": self.status,
            "decided_by": self.decided_by,
            "feedback": self.feedback,
            "reason": self.reason,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "resolved_at": self.resolved_at,
            "tool_call_id": self.tool_call_id,
            "sequence": self.sequence,
            "timeout_seconds": self.timeout_seconds,
            "expires_at": self.expires_at,
            "consumed_at": self.consumed_at,
        }


def normalize_arguments(value: Any) -> dict[str, Any]:
    """判断リクエストと tool 実行の引数を突合可能な形へ正規化する。

    SDK の PermissionRequestCustomTool.args は JSON 文字列の可能性があるたため、
    dict / JSON object 文字列 / マッピングのいずれでも同一形へ寄せる。正規化は
    突合 (照合) 専用で、判定ロジックは使わない (§12.4 / 判定機構の固定)。
    """

    if isinstance(value, dict):
        candidate = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return {"arguments": text}
        if not isinstance(parsed, dict):
            return {"arguments": parsed}
        candidate = parsed
    elif hasattr(value, "items"):
        # Mapping → dict
        candidate = dict(value)
    elif value is None:
        return {}
    else:
        return {"arguments": value}
    normalized: dict[str, Any] = {}
    for key, item in candidate.items():
        normalized[str(key)] = _canonical(item)
    return normalized


def _canonical(value: Any) -> Any:
    if isinstance(value, str):
        text = value.strip()
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return text
        # 文字列 encoded JSON を避け、純粋な文字列はそのまま保つ
        if isinstance(parsed, (dict, list)):
            return parsed
        return text
    if isinstance(value, Mapping):
        return {str(key): _canonical(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    return value


def arguments_match(expected: Mapping[str, Any], actual: Any) -> bool:
    """approved grant と tool 実行引数が完全一致するか (§12.4 実行前検証)。"""

    return normalize_arguments(dict(expected)) == normalize_arguments(actual)


ApprovalPublishListener = Callable[[PermissionRecord], None]
ApprovalResolveListener = Callable[..., None]
"""hub へ approval を流す listener 型。publish / resolve の 2 系統がある。

resolve 側は `resolved_by` (family | policy-timeout | turn-cancelled |
session-closed) を keyword で受け取る。listener 内で await してはいけない
(broker の await の途中で呼ばれるため)。
"""


class PermissionBroker:
    """pending 判断を保持し、REST / SSE 経由の解決を待つ registry。

    `request_permission` は turn 実行中に await される。turn lock は session 単位
    なので、他の session の turn と同時進行できる (1 turn で 1 判断)。
    """

    def __init__(
        self,
        *,
        timeout_seconds: float = DEFAULT_PERMISSION_TIMEOUT_SECONDS,
        on_publish: ApprovalPublishListener | None = None,
        on_resolve: ApprovalResolveListener | None = None,
    ) -> None:
        self._records: dict[str, PermissionRecord] = {}
        self._futures: dict[str, asyncio.Future[PermissionOutcome]] = {}
        self._timeout_seconds = timeout_seconds
        self._on_publish = on_publish
        self._on_resolve = on_resolve
        self._sequence_by_session: dict[str, int] = {}
        self._consumed_grants: list[PermissionRecord] = []

    @property
    def timeout_seconds(self) -> float:
        return self._timeout_seconds

    def set_listeners(
        self,
        *,
        on_publish: Callable[[PermissionRecord], None] | None = None,
        on_resolve: Callable[[PermissionRecord], None] | None = None,
    ) -> None:
        """hub 構築後にも listener を結線できるようにする (startup 順序の制約回避)。

        値が渡されたときだけ更新するので、既存 listener をうっかり消さない。
        """

        if on_publish is not None:
            self._on_publish = on_publish
        if on_resolve is not None:
            self._on_resolve = on_resolve

    async def request_permission(
        self,
        prompt: PermissionPrompt,
        tool_call_id: str | None = None,
    ) -> PermissionOutcome:
        """provider から呼ばれる hook。human 判断まで await して待機する。

        第 2 引数の `tool_call_id` は SDK の permission リクエストから来る値で、
        監査 (§11) と UI の突合にだけ使う。判定材料にはしない。
        """

        loop = asyncio.get_running_loop()
        future: asyncio.Future[PermissionOutcome] = loop.create_future()
        record = PermissionRecord(
            prompt=prompt,
            reason="awaiting family decision",
            tool_call_id=tool_call_id
            if tool_call_id is not None
            else prompt.tool_call_id,
            sequence=self._sequence_for(prompt.session_id),
            timeout_seconds=self._timeout_seconds,
            created_at=prompt.created_at,
            updated_at=prompt.created_at,
            expires_at=self._expires_at(prompt),
        )
        self._records[prompt.prompt_id] = record
        self._futures[prompt.prompt_id] = future
        logger.info(
            "permission_requested approval_id=%s session=%s tool=%s timeout=%.1f",
            prompt.prompt_id,
            prompt.session_id,
            prompt.tool_name,
            self._timeout_seconds,
        )
        self._fire_publish(record)
        try:
            outcome = await asyncio.wait_for(future, timeout=self._timeout_seconds)
        except TimeoutError:
            self._close_record(
                record,
                "expired",
                decided_by="policy-timeout",
                resolved_by="policy-timeout",
                reason="timeout",
            )
            logger.info(
                "permission_expired approval_id=%s session=%s tool=%s timeout=%.1f",
                prompt.prompt_id,
                prompt.session_id,
                prompt.tool_name,
                self._timeout_seconds,
            )
            return PermissionOutcome(
                approved=False,
                decided_by="policy-timeout",
                feedback=DENY_FEEDBACK_NO_RESPONSE,
                reason="timeout",
            )
        except asyncio.CancelledError:
            self._close_record(
                record,
                "cancelled",
                decided_by="turn-cancelled",
                resolved_by="turn-cancelled",
                feedback=DENY_FEEDBACK_NO_RESPONSE,
                reason="turn cancelled",
            )
            logger.info(
                "permission_cancelled approval_id=%s session=%s tool=%s",
                prompt.prompt_id,
                prompt.session_id,
                prompt.tool_name,
            )
            raise
        self._close_record(
            record,
            "approved" if outcome.approved else "rejected",
            decided_by=outcome.decided_by,
            resolved_by="family",
            feedback=outcome.feedback or "",
            reason=outcome.reason,
        )
        logger.info(
            "permission_resolved approval_id=%s session=%s tool=%s status=%s by=%s",
            prompt.prompt_id,
            prompt.session_id,
            prompt.tool_name,
            record.status,
            outcome.decided_by,
        )
        return outcome

    def _close_record(
        self,
        record: PermissionRecord,
        status: str,
        *,
        decided_by: str,
        resolved_by: str,
        feedback: str = "",
        reason: str = "",
    ) -> None:
        record.status = status
        record.decided_by = decided_by
        record.feedback = feedback
        record.reason = reason
        record.updated_at = tokyo_iso()
        record.resolved_at = record.updated_at
        self._fire_resolve(record, resolved_by=resolved_by)

    def _sequence_for(self, session_id: str) -> int:
        sequence = self._sequence_by_session.get(session_id, 0) + 1
        self._sequence_by_session[session_id] = sequence
        return sequence

    def _expires_at(self, prompt: PermissionPrompt) -> str:
        """policy timeout による自動 reject 予測時刻 (UI の countdown 表示用)。"""
        base = tokyo_now()
        with contextlib.suppress(ValueError):
            base = datetime.fromisoformat(prompt.created_at)
        return tokyo_iso(base + timedelta(seconds=self._timeout_seconds))

    def _fire_publish(self, record: PermissionRecord) -> None:
        if self._on_publish is None:
            return
        try:
            self._on_publish(record)
        except Exception:  # listener 例外で turn を止めない
            logger.exception(
                "approval_publish_failed approval_id=%s", record.prompt.prompt_id
            )

    def _fire_resolve(self, record: PermissionRecord, *, resolved_by: str) -> None:
        if self._on_resolve is None:
            return
        try:
            self._on_resolve(record, resolved_by=resolved_by)
        except Exception:  # pragma: no cover - listener 実装の保護
            logger.debug(
                "approval_resolve_publish_failed approval_id=%s",
                record.prompt.prompt_id,
                exc_info=True,
            )

    def _resolve_future(self, prompt_id: str, outcome: PermissionOutcome) -> bool:
        """待機中の future へ結果を渡す。待機が無ければ False を返す。"""
        future = self._futures.pop(prompt_id, None)
        if future is None or future.done():
            return False
        future.set_result(outcome)
        return True

    async def resolve(
        self,
        prompt_id: str,
        *,
        approved: bool,
        decided_by: str = "family",
        feedback: str | None = None,
        reason: str = "",
        resolved_by: str = "family",
    ) -> PermissionRecord:
        """§10.3 承認 API から呼ぶ解決。pending でなければ LookupError。"""
        record = self._records.get(prompt_id)
        if record is None:
            raise LookupError(f"approval {prompt_id} は見つかりません")
        if record.status != "pending":
            raise LookupError(f"approval {prompt_id} は既に {record.status} 済みです")
        outcome = PermissionOutcome(
            approved=approved,
            decided_by=decided_by,
            feedback=None if approved else (feedback or DENY_FEEDBACK_REJECTED),
            reason=reason or ("approved via API" if approved else "rejected via API"),
        )
        resolved = self._resolve_future(prompt_id, outcome)
        self._close_record(
            record,
            "approved" if approved else "rejected",
            decided_by=decided_by,
            resolved_by=resolved_by,
            feedback=outcome.feedback or "",
            reason=outcome.reason,
        )
        if not resolved:
            # turn が既に終わっている場合は再試行できない (UI には resolved 状態で
            # 見える)。判断は監査ログとして残す。
            logger.info(
                "permission_resolve_orphan approval_id=%s status=%s",
                prompt_id,
                record.status,
            )
        return record

    def mark_stale(self, session_id: str) -> list[PermissionRecord]:
        """session 終了時に pending 判断をすべて却下して返す。"""
        stale: list[PermissionRecord] = []
        for record in list(self._records.values()):
            if record.prompt.session_id != session_id or record.status != "pending":
                continue
            outcome = PermissionOutcome(
                approved=False,
                decided_by="session-closed",
                feedback=DENY_FEEDBACK_NO_RESPONSE,
                reason="session closed",
            )
            self._resolve_future(record.prompt.prompt_id, outcome)
            self._close_record(
                record,
                "rejected",
                decided_by="session-closed",
                resolved_by="session-closed",
                feedback=DENY_FEEDBACK_NO_RESPONSE,
                reason="session closed",
            )
            stale.append(record)
        return stale

    def pending(self, session_id: str | None = None) -> list[PermissionRecord]:
        return [
            record
            for record in self._records.values()
            if record.status == "pending"
            and (session_id is None or record.prompt.session_id == session_id)
        ]

    def resolved(self, session_id: str | None = None) -> list[PermissionRecord]:
        return [
            record
            for record in self._records.values()
            if record.status != "pending"
            and (session_id is None or record.prompt.session_id == session_id)
        ]

    def records(self) -> list[PermissionRecord]:
        return list(self._records.values())

    def get(self, prompt_id: str) -> PermissionRecord | None:
        return self._records.get(prompt_id)

    def pending_approvals(
        self, session_id: str | None = None
    ) -> list[PermissionRecord]:
        """UI / API 向けの pending 一覧 (§10.3 approvals list)。"""
        return self.pending(session_id)

    def resolved_approvals(
        self, session_id: str | None = None
    ) -> list[PermissionRecord]:
        return self.resolved(session_id)

    def approvals(self) -> list[PermissionRecord]:
        return list(self._records.values())

    def _newest_approved_first(self) -> list[PermissionRecord]:
        # dict_values は reversed() 非対応なので list 化してから新着順に走査する。
        return list(reversed(list(self._records.values())))

    def consume_grant(
        self,
        *,
        session_id: str,
        tool_name: str,
        arguments: Any,
    ) -> PermissionRecord | None:
        """approved 判断を 1 回だけ実行権へ変換する (使い切り)。

        一致する grant が無ければ None。§12.4 の「AI が self-approve しない /
        実行前 approval 必須」を tool 実行側でも担保するためのもので、判定の
        分岐 (ルール内容・model 出力) は一切行わない。
        """

        for record in self._newest_approved_first():
            if record.status != "approved":
                continue
            if (
                record.prompt.session_id != session_id
                or record.prompt.tool_name != tool_name
            ):
                continue
            if record.consumed_at is not None:
                continue
            if not arguments_match(record.prompt.arguments, arguments):
                continue
            record.consumed_at = tokyo_iso()
            self._consumed_grants.append(record)
            return record
        return None

    def record_execution(
        self,
        *,
        session_id: str,
        tool_name: str,
        arguments: Any,
        auto_approved: bool = False,
        outcome: str = "success",
    ) -> PermissionRecord:
        """tool 実行を grant レコードとして保持する (§11 tool_executions 相当)。

        permission 判断が要らない tool (read 系 auto tool) も実行記録は残す。
        approved レコードが残っていればそれを grant として流用し、一致が無い
        場合は新しい grant レコードを作る (§10.3 の human 判断一覧には出さず、
        consumed_grants() 側にだけ載せる)。
        """

        normalized = normalize_arguments(arguments)
        approved = self.consume_grant(
            session_id=session_id,
            tool_name=tool_name,
            arguments=normalized,
        )
        if approved is not None:
            self._consumed_grants.append(approved)
            return approved
        record = PermissionRecord(
            prompt=PermissionPrompt(
                prompt_id=new_prompt_id(),
                session_id=session_id,
                tool_name=tool_name,
                arguments=normalized,
            ),
            status="approved" if auto_approved else "rejected",
            decided_by="policy-auto" if auto_approved else "policy-no-grant",
            reason="auto-approved tool" if auto_approved else "no matching grant",
            expires_at="",
            consumed_at=tokyo_iso() if outcome == "success" else None,
        )
        self._consumed_grants.append(record)
        return record

    def consumed_grants(self) -> list[PermissionRecord]:
        """実行に使われた grant (§11 tool_executions と突合できる形)。"""
        return list(self._consumed_grants)


def new_prompt_id() -> str:
    """判断レコードの識別子 (§11 approvals.id 相当)。"""
    return f"appr-{uuid.uuid4().hex[:12]}"


__all__ = [
    "DEFAULT_PERMISSION_TIMEOUT_SECONDS",
    "DENY_FEEDBACK_NO_RESPONSE",
    "DENY_FEEDBACK_REJECTED",
    "ApprovalPublishListener",
    "ApprovalResolveListener",
    "InteractivePermissionHook",
    "PermissionBroker",
    "PermissionOutcome",
    "PermissionPrompt",
    "PermissionRecord",
    "arguments_match",
    "new_prompt_id",
    "normalize_arguments",
]
