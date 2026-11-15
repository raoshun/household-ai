"""§6.2 Custom Tool 定義と §6.3 tool 権限 matrix の enforcement。

Phase 0 では in-memory の rule store へ作用する tool 群を提供する。README §6.3 の
「agent は rule を直接 active 化・削除できない」を tool 層で適用するため、rule を
変える tool は常に PermissionBroker の grant (家族が承認した 1 回分の grant) を要件に
する。grant が無ければ tool は実行されず、拒否は audit へ残る (§12.4 trace)。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from typing import Any

from app.agent.permissions import PermissionBroker
from app.agent.provider import CustomToolDeclaration
from app.services.audit import AuditTrail, JudgmentStore
from app.services.rules import Rule, RuleDraft, RuleStore

logger = logging.getLogger("household-ai.services.tools")

AGENT_SOURCE = "agent"
READ_TOOLS = ("read_rules", "detect_rule_conflicts")
WRITE_TOOLS = (
    "propose_rule",
    "update_rule_candidate",
    "delete_rule_candidate",
    "request_rule_approval",
)
RULE_TOOLS: tuple[str, ...] = READ_TOOLS + WRITE_TOOLS


@dataclass(slots=True)
class RuleToolContext:
    """1 会話分の tool 依存。conversation_id / session_id で audit を紐付ける。"""

    conversation_id: str
    store: RuleStore
    audit: AuditTrail
    judgments: JudgmentStore
    broker: PermissionBroker
    session_id: str | None = None

    def bind_session(self, session_id: str | None) -> None:
        self.session_id = session_id


def _deny(reason: str, *, detail: Mapping[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "granted": False,
        "message": reason,
        "status": "denied",
    }
    if detail:
        payload["detail"] = dict(detail)
    return payload


def _preview(text: str, limit: int = 120) -> str:
    compact = " ".join(str(text).split())
    if len(compact) <= limit:
        return compact
    return f"{compact[:limit]}…"


def _rule_draft(title: str, content: str) -> RuleDraft:
    return RuleDraft(
        content=str(content),
        kind="intent",
        rule_id=None,
        source=AGENT_SOURCE,
        title=str(title),
    )


def _rule_brief(rule: Rule) -> dict[str, Any]:
    return {
        "content": _preview(rule.content),
        "kind": rule.kind,
        "revision": rule.revision,
        "rule_id": rule.rule_id,
        "source": rule.source,
        "status": rule.status,
        "title": rule.title,
    }


def _audit(
    ctx: RuleToolContext,
    *,
    kind: str,
    actor: str,
    action: str,
    target: str,
    detail: dict[str, Any] | None = None,
) -> None:
    payload = dict(detail or {})
    payload.setdefault("conversation_id", ctx.conversation_id)
    payload.setdefault("session_id", ctx.session_id)
    ctx.audit.record(
        action=action, actor=actor, detail=payload, kind=kind, target=target
    )


def _require_grant(
    ctx: RuleToolContext,
    tool_name: str,
    arguments: Mapping[str, Any],
    *,
    target: str,
) -> bool:
    """§6.3 / §12.4: 承認済みの grant を 1 回だけ消費できるかを確認する。"""

    grant = ctx.broker.consume_grant(
        arguments=dict(arguments),
        session_id=ctx.session_id or "",
        tool_name=tool_name,
    )
    if grant is None:
        _audit(
            ctx,
            action="tool_denied_without_grant",
            actor="policy",
            detail={"reason": "no approved grant", "tool_name": tool_name},
            kind="rule_mutation",
            target=target,
        )
        logger.warning(
            "tool_denied_without_grant conversation=%s tool=%s",
            ctx.conversation_id,
            tool_name,
        )
        return False
    _audit(
        ctx,
        action="grant_consumed",
        actor="family",
        detail={
            "approval_id": grant.approval_id,
            "grant_sequence": grant.sequence,
            "tool_name": tool_name,
        },
        kind="approval",
        target=grant.approval_id,
    )
    return True


def _tool_read_rules(
    ctx: RuleToolContext,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    status = str(arguments.get("status") or "active")
    query = str(arguments.get("query") or "").strip()
    limit = int(arguments.get("limit") or 20)
    rules = ctx.store.list_rules(status=None if status == "all" else status)
    if query:
        needle = query.lower()
        rules = [
            rule for rule in rules if needle in f"{rule.title} {rule.content}".lower()
        ]
    selected = rules[: max(1, min(limit, 50))]
    _audit(
        ctx,
        action="read_rules",
        actor="agent",
        detail={"count": len(selected), "query": query, "status": status},
        kind="rule_mutation",
        target="rule-store",
    )
    return {
        "count": len(selected),
        "granted": True,
        "rules": [_rule_brief(rule) for rule in selected],
        "total": len(rules),
    }


def _tool_detect_rule_conflicts(
    ctx: RuleToolContext,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    draft = _rule_draft(
        str(arguments.get("title") or ""), str(arguments.get("content") or "")
    )
    conflicts = ctx.store.detect_conflicts(draft)
    _audit(
        ctx,
        action="detect_conflicts",
        actor="agent",
        detail={"conflict_count": len(conflicts), "title": draft.title},
        kind="rule_mutation",
        target="rule-store",
    )
    return {
        "conflicts": [conflict.to_dict() for conflict in conflicts],
        "count": len(conflicts),
        "granted": True,
        "hint": (
            "矛盾候補があります。提案前に家族へ確認するか、既存ルールとの"
            "統合を検討して下さい。"
            if conflicts
            else "既存ルールとの重複候補はありません。"
        ),
    }


def _tool_propose_rule(
    ctx: RuleToolContext,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    title = str(arguments.get("title") or "").strip()
    content = str(arguments.get("content") or "").strip()
    if not title or not content:
        return _deny("title と content の両方が必要です。")
    if not _require_grant(ctx, "propose_rule", arguments, target=f"rule:{title}"):
        return _deny(
            "家族の承認がないためルール候補を作成できません。"
            "判断根拠と提案内容を説明し、承認を依頼して下さい。"
        )
    rule = ctx.store.create_rule(
        content=content,
        created_by="agent",
        source=AGENT_SOURCE,
        status="pending",
        title=title,
    )
    conflicts = ctx.store.detect_conflicts(rule)
    judgment = ctx.judgments.record(
        approval_refs=[
            record.approval_id
            for record in ctx.broker.consumed_grants()
            if record.tool_name == "propose_rule"
        ],
        audit_refs=[entry.entry_id for entry in ctx.audit.entries("rule_mutation")],
        basis=[{"rule_id": None, "text": "agent proposal from conversation"}],
        created_at=rule.created_at,
        decision=f"created pending candidate {rule.rule_id} ({rule.title})",
        judgment_id=ctx.judgments.next_id(),
        kind="rule_proposal",
        notes=_preview(content),
        rule_refs=[_rule_brief(rule)],
        tool_call_refs=[f"{ctx.conversation_id}:{rule.rule_id}"],
    )
    _audit(
        ctx,
        action="rule_candidate_created",
        actor="agent",
        detail={"judgment_id": judgment.judgment_id, "rule_id": rule.rule_id},
        kind="rule_mutation",
        target=rule.rule_id,
    )
    return {
        "conflicts": [conflict.to_dict() for conflict in conflicts],
        "granted": True,
        "judgment_id": judgment.judgment_id,
        "message": (
            f"ルール候補 {rule.rule_id} を pending で作成しました。"
            "反映には家族の承認 (rules approve API) が必要です。"
        ),
        "rule": _rule_brief(rule),
        "status": rule.status,
    }


def _tool_update_rule_candidate(
    ctx: RuleToolContext,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    rule_id = str(arguments.get("rule_id") or "").strip()
    content = str(arguments.get("content") or "").strip()
    if not rule_id or not content:
        return _deny("rule_id と content の両方が必要です。")
    existing = ctx.store.get_rule(rule_id)
    if existing is None:
        return _deny(f"rule {rule_id} が見つかりません。", detail={"rule_id": rule_id})
    if not _require_grant(ctx, "update_rule_candidate", arguments, target=rule_id):
        return _deny(
            "家族の承認がないためルールを変更できません。"
            "変更内容と理由を説明し、承認依頼を先行させて下さい。"
        )
    rule = ctx.store.update_rule(
        rule_id,
        content=content,
        note="agent による候補更新 (要家族承認)",
        title=(str(arguments.get("title")).strip() if arguments.get("title") else None),
    )
    judgment = ctx.judgments.record(
        audit_refs=[entry.entry_id for entry in ctx.audit.entries("rule_mutation")],
        basis=[{"rule_id": rule_id, "text": "agent candidate update"}],
        created_at=rule.updated_at,
        decision=f"updated rule {rule.rule_id} to revision {rule.revision} (pending)",
        judgment_id=ctx.judgments.next_id(),
        kind="rule_update",
        notes=_preview(rule.content),
        rule_refs=[_rule_brief(rule)],
    )
    _audit(
        ctx,
        action="rule_candidate_updated",
        actor="agent",
        detail={"judgment_id": judgment.judgment_id, "revision": rule.revision},
        kind="rule_mutation",
        target=rule_id,
    )
    return {
        "granted": True,
        "judgment_id": judgment.judgment_id,
        "message": (
            f"rule {rule_id} を revision {rule.revision} で更新し pending にしました。"
        ),
        "rule": _rule_brief(rule),
        "status": rule.status,
    }


def _tool_delete_rule_candidate(
    ctx: RuleToolContext,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    rule_id = str(arguments.get("rule_id") or "").strip()
    reason = str(arguments.get("reason") or "").strip()
    if not rule_id:
        return _deny("rule_id が必要です。")
    if ctx.store.get_rule(rule_id) is None:
        return _deny(f"rule {rule_id} が見つかりません。", detail={"rule_id": rule_id})
    if not _require_grant(ctx, "delete_rule_candidate", arguments, target=rule_id):
        return _deny(
            "家族の承認がないためルールを削除できません。削除理由を説明し、"
            "承認依頼を先行させて下さい。"
        )
    rule = ctx.store.delete_rule(rule_id, reason=reason or "agent request")
    judgment = ctx.judgments.record(
        basis=[{"rule_id": rule_id, "text": "agent tombstone request"}],
        created_at=rule.updated_at,
        decision=f"tombstoned rule {rule_id}",
        judgment_id=ctx.judgments.next_id(),
        kind="rule_deletion",
        notes=reason,
        rule_refs=[_rule_brief(rule)],
    )
    _audit(
        ctx,
        action="rule_tombstoned",
        actor="agent",
        detail={"judgment_id": judgment.judgment_id, "reason": reason},
        kind="rule_mutation",
        target=rule_id,
    )
    return {
        "granted": True,
        "judgment_id": judgment.judgment_id,
        "message": f"rule {rule_id} を tombstone しました (復元は restore API)。",
        "rule": _rule_brief(rule),
        "status": rule.status,
    }


def _tool_request_rule_approval(
    ctx: RuleToolContext,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """agent は「承認依頼の記録」だけ作れる (§6.3 active 化禁止)。"""

    rule_id = str(arguments.get("rule_id") or "").strip()
    reason = str(arguments.get("reason") or "").strip()
    if not rule_id:
        return _deny("rule_id が必要です。")
    rule = ctx.store.get_rule(rule_id)
    if rule is None:
        return _deny(f"rule {rule_id} が見つかりません。", detail={"rule_id": rule_id})
    if rule.status != "pending":
        return _deny(f"rule {rule_id} は pending でありません ({rule.status})。")
    conflicts = ctx.store.detect_conflicts(rule)
    judgment = ctx.judgments.record(
        basis=[{"rule_id": rule_id, "text": reason or "agent approval request"}],
        created_at=rule.updated_at,
        decision=f"requested family approval for {rule_id}",
        human_confirmation={"state": "requested"},
        judgment_id=ctx.judgments.next_id(),
        kind="rule_approval_request",
        missing=[] if not conflicts else [c.kind for c in conflicts],
        notes=reason,
        rule_refs=[_rule_brief(rule)],
    )
    _audit(
        ctx,
        action="approval_requested_by_agent",
        actor="agent",
        detail={
            "conflict_count": len(conflicts),
            "judgment_id": judgment.judgment_id,
            "reason": reason,
        },
        kind="approval",
        target=rule_id,
    )
    return {
        "conflicts": [conflict.to_dict() for conflict in conflicts],
        "granted": True,
        "judgment_id": judgment.judgment_id,
        "message": (
            f"rule {rule_id} の承認依頼を記録しました。"
            "家族が UI / rules approve API で承認するまで active になりません。"
        ),
        "rule": _rule_brief(rule),
    }


def build_rule_tools(
    ctx: RuleToolContext,
    *,
    only: Sequence[str] | None = None,
) -> list[CustomToolDeclaration]:
    """§6.2 の signature に従い、conversation 専属の Custom Tool を作る。

    `approval_mode="ask"` の tool は SDK の permission gate ⇒ PermissionBroker で家族の
    承認を待ち、承認分だけ tool が実行される。read tool は `auto`。
    """

    allowed = set(only) if only is not None else set(RULE_TOOLS)

    def handler(name: str, run: Callable[..., dict[str, Any]]):
        def wrapped(tool_name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
            return run(dict(arguments))

        return wrapped

    declarations: list[CustomToolDeclaration] = []

    def add(
        *,
        name: str,
        description: str,
        parameters: dict[str, Any],
        approval_mode: str,
        run: Callable[..., dict[str, Any]],
    ) -> None:
        if name not in allowed:
            return
        declarations.append(
            CustomToolDeclaration(
                approval_mode=approval_mode,  # type: ignore[arg-type]
                description=description,
                handler=handler(name, run),
                name=name,
                parameters=parameters,
            )
        )

    add(
        approval_mode="auto",
        description=(
            "保存済み家族ルールを読みます。status=active|pending|tombstoned|all、"
            "query で語句検索、limit で件数上限 (既定 20)。"
        ),
        name="read_rules",
        parameters={
            "additionalProperties": False,
            "properties": {
                "limit": {"type": "integer"},
                "query": {"type": "string"},
                "status": {
                    "enum": ["active", "all", "pending", "tombstoned"],
                    "type": "string",
                },
            },
            "type": "object",
        },
        run=partial(_tool_read_rules, ctx),
    )
    add(
        approval_mode="auto",
        description=(
            "提案しようとするルールが既存ルールと重複・時刻衝突しないかを heuristic で"
            "検査します (提案前に行う)。"
        ),
        name="detect_rule_conflicts",
        parameters={
            "additionalProperties": False,
            "properties": {
                "content": {"type": "string"},
                "title": {"type": "string"},
            },
            "required": ["content", "title"],
            "type": "object",
        },
        run=partial(_tool_detect_rule_conflicts, ctx),
    )
    add(
        approval_mode="ask",
        description=(
            "新しいルール候補を pending で作成します。家族の承認なしには active になり"
            "ません。作成前に detect_rule_conflicts で確認して下さい。"
        ),
        name="propose_rule",
        parameters={
            "additionalProperties": False,
            "properties": {
                "content": {"type": "string"},
                "reason": {"type": "string"},
                "title": {"type": "string"},
            },
            "required": ["content", "title"],
            "type": "object",
        },
        run=partial(_tool_propose_rule, ctx),
    )
    add(
        approval_mode="ask",
        description="既存ルールの本文 (または title) を書き換え、pending へ戻します。",
        name="update_rule_candidate",
        parameters={
            "additionalProperties": False,
            "properties": {
                "content": {"type": "string"},
                "rule_id": {"type": "string"},
                "title": {"type:": None} if False else {"type": "string"},
            },
            "required": ["content", "rule_id"],
            "type": "object",
        },
        run=partial(_tool_update_rule_candidate, ctx),
    )
    add(
        approval_mode="ask",
        description="既存ルールを tombstone (削除) します。復元は家族の操作だけです。",
        name="delete_rule_candidate",
        parameters={
            "additionalProperties": False,
            "properties": {
                "reason": {"type": "string"},
                "rule_id": {"type": "string"},
            },
            "required": ["rule_id"],
            "type": "object",
        },
        run=partial(_tool_delete_rule_candidate, ctx),
    )
    add(
        approval_mode="auto",
        description=(
            "pending のルールについて家族への承認依頼を記録します。この tool 自身は"
            "ルールを active 化しません (§6.3)。"
        ),
        name="request_rule_approval",
        parameters={
            "additionalProperties": False,
            "properties": {
                "reason": {"type": "string"},
                "rule_id": {"type": "string"},
            },
            "required": ["rule_id"],
            "type": "object",
        },
        run=partial(_tool_request_rule_approval, ctx),
    )
    return declarations


__all__ = [
    "AGENT_SOURCE",
    "READ_TOOLS",
    "RULE_TOOLS",
    "WRITE_TOOLS",
    "RuleToolContext",
    "build_rule_tools",
]
