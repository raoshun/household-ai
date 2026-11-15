"""PermissionBroker 用の adapter (§4/§5 approvals API が呼ぶ語彙の補完)。

失われた ChatService glue (chat.py tail) は `get_approval` / `resolve_permission`
/ `resolve_many` / `approvals(session_id=...)` を呼ぶが、`app.agent.permissions`
の public API にはこれらの名前がない (`approvals()` は session 絞り込み引数を
取らない)。本 adapter は broker を継承し names だけを追加する (§5 の broker 自体
は壊さない)。`resolve` の折れ方 (pending 以外 -> LookupError) はそのまま使い、
HTTP 問題への写し方だけを §4 表に揃える:

- 「見つかりません」            -> 404 unknown_error.approval_not_found
- 解決済み (approved/rejected)  -> 409 unknown_error.approval_not_resolvable
- cancelled/expired (stale 系)  -> 503 agent_error.permission_stale
  (§7 upstream vocabulary。session 失効で grant が解決不能になった場合のみ)
- pending 一覧/決着 payload は §5 permission metadata の `to_dict()` を正とする
"""

from __future__ import annotations

from collections.abc import Iterable

from app.agent.permissions import (
    PermissionBroker,
    PermissionPrompt,
    PermissionRecord,
)
from app.services.exceptions import (
    PermissionStaleError,
    UnknownApprovalError,
    UnknownResolverError,
)

__all__ = ["PermissionBrokerFacade"]

_STATUS_IN_LOOKUP_MARK = " は既に "
_NOT_FOUND_MARK = "見つかりません"
_STALE_STATUSES = frozenset({"cancelled", "expired"})


class PermissionBrokerFacade(PermissionBroker):
    """chat glue / §4 approvals route が呼ぶ名前を提供する broker adapter。"""

    def approval_by_turn(self, turn_id: str) -> PermissionRecord | None:
        """turn と紐付く approval (tool_call_id に turn_id を載せる運用用)。"""

        for record in reversed(list(self._records.values())):
            if record.tool_call_id == turn_id or record.prompt.prompt_id == turn_id:
                return record
        return None

    def approvals(
        self,
        session_id: str | None = None,
        *,
        status: str | None = None,
    ) -> list[PermissionRecord]:
        """broker approvals() へ session/status 絞り込みを生やした override。"""

        records = list(self._records.values())
        if session_id is not None:
            records = [
                record for record in records if record.prompt.session_id == session_id
            ]
        if status is not None:
            records = [record for record in records if record.status == status]
        return records

    def get_approval(self, approval_id: str) -> PermissionRecord | None:
        return self.get(approval_id)

    async def resolve_permission(
        self,
        approval_id: str,
        *,
        approved: bool,
        decided_by: str = "family",
        feedback: str | None = None,
        reason: str = "",
    ) -> PermissionRecord:
        """approval を決着する (§5)。broker resolve の折れ方を §4 表へ写す。"""

        record = self._records.get(approval_id)
        if record is None:
            raise UnknownApprovalError(approval_id=approval_id)
        if record.status != "pending":
            raise _resolved_problem(record)
        try:
            return await self.resolve(
                approval_id,
                approved=approved,
                decided_by=decided_by,
                feedback=feedback,
                reason=reason,
                resolved_by=decided_by,
            )
        except LookupError as exc:
            raise _lookup_problem(approval_id, exc) from exc

    async def resolve_many(
        self,
        approval_ids: Iterable[str],
        *,
        approved: bool,
        decided_by: str = "family",
        feedback: str | None = None,
        reason: str = "",
    ) -> list[PermissionRecord]:
        """resolve_permission の一括版 (§5 bulk 決着)。折れ方は fail-fast。"""

        return [
            await self.resolve_permission(
                approval_id,
                approved=approved,
                decided_by=decided_by,
                feedback=feedback,
                reason=reason,
            )
            for approval_id in approval_ids
        ]

    def request_permission(
        self,
        prompt: PermissionPrompt,
        tool_call_id: str | None = None,
    ):
        """broker 継承の薄い override (chat glue の positional 呼び出し対応)。"""

        return super().request_permission(prompt, tool_call_id)


def _resolved_problem(record: PermissionRecord) -> Exception:
    if record.status in _STALE_STATUSES:
        return PermissionStaleError(
            f"approval {record.prompt.prompt_id} は {record.status} で解決できません",
            approval_ids=[record.prompt.prompt_id],
        )
    return UnknownResolverError(
        f"approval {record.prompt.prompt_id} は既に {record.status} 済みです",
        approval_id=record.prompt.prompt_id,
    )


def _lookup_problem(approval_id: str, exc: LookupError) -> Exception:
    text = str(exc)
    if _NOT_FOUND_MARK in text:
        return UnknownApprovalError(text, approval_id=approval_id)
    if _STATUS_IN_LOOKUP_MARK in text:
        status = text.split(_STATUS_IN_LOOKUP_MARK, 1)[1].removesuffix(" 済みです")
        if status in _STALE_STATUSES:
            return PermissionStaleError(text, approval_ids=[approval_id])
        return UnknownResolverError(text, approval_id=approval_id)
    return UnknownApprovalError(text, approval_id=approval_id)
