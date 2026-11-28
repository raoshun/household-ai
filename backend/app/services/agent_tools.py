"""Phase 2 永続ルール store 用の §6.2 Custom Tools."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable, Mapping
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.agent.permissions import PermissionBroker
from app.agent.provider import CustomToolDeclaration
from app.clock import tokyo_now
from app.db.database import connect
from app.models.rule import (
    RuleBody,
    RuleCategory,
    RuleFrontMatter,
    RuleStatus,
    RuleType,
)
from app.repositories import rule_repository
from app.services.audit import AuditTrail
from app.services.search_service import search_rules, split_search_text

logger = logging.getLogger("household-ai.services.agent_tools")

AGENT_RULE_TOOLS = (
    "search_rules",
    "get_rule",
    "propose_rule",
    "evaluate_rule",
)


@dataclass(slots=True)
class AgentRuleContext:
    """会話に bind された永続ルール tool の依存。"""

    conversation_id: str
    db_path: Path
    rules_root: Path
    broker: PermissionBroker
    audit: AuditTrail
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


def _audit(
    ctx: AgentRuleContext,
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
        action=action,
        actor=actor,
        detail=payload,
        kind=kind,
        target=target,
    )


def _require_grant(
    ctx: AgentRuleContext,
    tool_name: str,
    arguments: Mapping[str, Any],
    *,
    target: str,
) -> bool:
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
            "approval_id": grant.prompt.prompt_id,
            "grant_sequence": grant.sequence,
            "tool_name": tool_name,
        },
        kind="approval",
        target=grant.prompt.prompt_id,
    )
    return True


def _open_db(ctx: AgentRuleContext) -> sqlite3.Connection:
    return connect(ctx.db_path)


def _search(ctx: AgentRuleContext, arguments: dict[str, Any]) -> dict[str, Any]:
    query = str(arguments.get("query") or "").strip()
    status = str(arguments.get("status") or "active").strip()
    category = arguments.get("category")
    limit = arguments.get("limit", 10)
    if status == "pending_approval":
        return {
            "hint": (
                "pending_approval のルールは検索対象外です。"
                "active のルールを検索してください。"
            ),
            "results": [],
        }
    if status == "all":
        status = "active"
    if not isinstance(limit, int):
        limit = 10
    try:
        category_value = str(category).strip() if category is not None else None
        with closing(_open_db(ctx)) as conn:
            results = search_rules(
                conn,
                query,
                status=status,
                category=category_value,
                limit=limit,
            )
    except sqlite3.Error as exc:
        logger.exception("search_rules_failed")
        return {"error": "rule_search_failed", "results": [], "detail": str(exc)}
    return {"results": results}


def _get(ctx: AgentRuleContext, arguments: dict[str, Any]) -> dict[str, Any]:
    rule_id = str(arguments.get("rule_id") or "").strip()
    if not rule_id:
        return {"found": False, "error": "rule_id が必要です"}
    try:
        with closing(_open_db(ctx)) as conn:
            row = conn.execute(
                "SELECT current_version, status FROM rules WHERE id = ?",
                (rule_id,),
            ).fetchone()
            if row is None:
                return {"found": False, "rule_id": rule_id}
            current_version = int(row["current_version"])
            status = str(row["status"])
            front_matter, body = rule_repository.load_rule_version(
                conn,
                rules_root=ctx.rules_root,
                rule_id=rule_id,
                version=current_version,
            )
    except rule_repository.RuleRepositoryError as exc:
        return {"found": False, "rule_id": rule_id, "error": str(exc)}
    except (OSError, sqlite3.Error) as exc:
        return {"found": False, "rule_id": rule_id, "error": str(exc)}
    return {
        "found": True,
        "rule_id": rule_id,
        "title": front_matter.title,
        "category": front_matter.category.value,
        "status": status,
        "version": front_matter.version,
        "applicable_to": front_matter.applicable_to,
        "rule_type": front_matter.rule_type.value,
        "content": {
            "basic_rule": body.basic_policy,
            "applicable_conditions": body.applicable_conditions,
            "exception_conditions": body.exception_conditions,
            "decision_criteria": body.decision_criteria,
        },
    }


def _propose(ctx: AgentRuleContext, arguments: dict[str, Any]) -> dict[str, Any]:
    title = str(arguments.get("title") or "").strip()
    category_text = str(arguments.get("category") or "general").strip()
    rule_type_text = str(arguments.get("rule_type") or "principle").strip()
    applicable_to = arguments.get("applicable_to")
    content = arguments.get("content")
    reason = str(arguments.get("reason") or "agent 提案").strip()
    if not title:
        return _deny("title が必要です。")
    if not isinstance(applicable_to, list):
        applicable_to = []
    if not isinstance(content, dict):
        content = {}
    try:
        category = RuleCategory(category_text)
        rule_type = RuleType(rule_type_text)
    except ValueError as exc:
        return _deny(str(exc))
    exceptions = content.get("exceptions")
    if isinstance(exceptions, list):
        exception_text = "、".join(
            str(item).strip() for item in exceptions if str(item).strip()
        )
    else:
        exception_text = ""
    basic_rule = str(content.get("basic_rule") or "").strip()
    if not basic_rule:
        return _deny("content.basic_rule が必要です。")
    if not _require_grant(ctx, "propose_rule", arguments, target=title):
        return _deny("承認済みの propose_rule grant がありません。")
    applicable_text = "、".join(
        str(item).strip() for item in applicable_to if str(item).strip()
    )
    now = tokyo_now()
    front_matter = RuleFrontMatter(
        id="server-assigned",
        title=title,
        category=category,
        status=RuleStatus.PENDING_APPROVAL,
        version=1,
        rule_type=rule_type,
        applicable_to=[
            str(item).strip() for item in applicable_to if str(item).strip()
        ],
        tags=[],
        created_at=now,
        updated_at=now,
        created_by="agent",
        approved_by=None,
    )
    body = RuleBody(
        basic_policy=basic_rule,
        applicable_conditions=applicable_text or "全対象",
        exception_conditions=exception_text or "特記なし",
        decision_criteria="要家族確認",
    )
    try:
        with closing(_open_db(ctx)) as conn:
            result = rule_repository.create_rule_with_new_id(
                conn,
                rules_root=ctx.rules_root,
                front_matter=front_matter,
                body=body,
                change_reason=reason or "agent 提案",
            )
    except (
        rule_repository.RuleRepositoryError,
        sqlite3.Error,
        OSError,
        ValueError,
    ) as exc:
        return _deny("ルール候補を作成できませんでした。", detail={"error": str(exc)})
    _audit(
        ctx,
        action="rule_candidate_created",
        actor="agent",
        detail={"rule_id": result.rule_id, "reason": reason},
        kind="rule_mutation",
        target=result.rule_id,
    )
    return {
        "success": True,
        "rule_id": result.rule_id,
        "status": "pending_approval",
    }


def _normalize(text: str) -> str:
    compact = "".join(str(text).lower().split())
    return compact


def _evaluate(ctx: AgentRuleContext, arguments: dict[str, Any]) -> dict[str, Any]:
    candidate = arguments.get("rule_candidate")
    if not isinstance(candidate, dict):
        candidate = {}
    title = str(candidate.get("title") or "").strip()
    content = str(candidate.get("content") or "").strip()
    missing: list[str] = []
    if not title:
        missing.append("title")
    if not content:
        missing.append("content")

    related: list[dict[str, Any]] = []
    duplicate: list[dict[str, Any]] = []
    conflict: list[dict[str, Any]] = []
    if title or content:
        query = " ".join(part for part in (title, content) if part)
        try:
            with closing(_open_db(ctx)) as conn:
                matches = search_rules(
                    conn,
                    query,
                    status="active",
                    limit=20,
                )
        except sqlite3.Error as exc:
            matches = []
            logger.exception("evaluate_rule_search_failed")
            missing.append(f"検索失敗: {exc}")
        candidate_terms = set(split_search_text(query).cjk_terms)
        candidate_terms.update(split_search_text(query).ascii_terms)
        for match in matches:
            rule_id = str(match["id"])
            rule_title = str(match["title"])
            related.append(
                {
                    "id": rule_id,
                    "title": rule_title,
                    "version": match["current_version"],
                }
            )
            try:
                with closing(_open_db(ctx)) as conn:
                    _, body = rule_repository.load_rule_version(
                        conn,
                        rules_root=ctx.rules_root,
                        rule_id=rule_id,
                        version=int(match["current_version"]),
                    )
            except (rule_repository.RuleRepositoryError, sqlite3.Error, OSError):
                body = None
            existing_text = (
                body.basic_policy
                if body is not None
                else str(match.get("summary") or "")
            )
            terms = set(split_search_text(existing_text).cjk_terms)
            terms.update(split_search_text(existing_text).ascii_terms)
            jaccard = (
                len(candidate_terms & terms) / len(candidate_terms | terms)
                if candidate_terms and terms
                else 0.0
            )
            title_match = _normalize(title) == _normalize(rule_title)
            if title_match or jaccard >= 0.6:
                duplicate.append({"id": rule_id, "title": rule_title})
            if title_match and _normalize(content) != _normalize(existing_text):
                conflict.append(
                    {
                        "id": rule_id,
                        "title": rule_title,
                        "reason": "同名ルールで本文の表現が異なります",
                    }
                )
    confirmations = [
        "候補の適用対象を確認してください。",
        "例外条件と判断基準を確認してください。",
    ]
    return {
        "related_rules": related,
        "duplicate_possible": duplicate,
        "conflict_possible": conflict,
        "missing_information": missing,
        "confirmation_required": confirmations,
    }


def build_agent_rule_tools(
    ctx: AgentRuleContext,
) -> list[CustomToolDeclaration]:
    """§6.2 の 4 tool を Custom Tool Declaration として生成する。"""

    declarations: list[CustomToolDeclaration] = []

    def wrapped(
        name: str,
        run: Callable[[dict[str, Any]], dict[str, Any]],
    ) -> Callable[..., dict[str, Any]]:
        del name

        def execute(tool_name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
            del tool_name
            return run(dict(arguments))

        return execute

    def add(
        *,
        name: str,
        description: str,
        parameters: dict[str, Any],
        approval_mode: str,
        run: Callable[[dict[str, Any]], dict[str, Any]],
    ) -> None:
        declarations.append(
            CustomToolDeclaration(
                approval_mode=approval_mode,
                description=description,
                handler=wrapped(name, run),
                name=name,
                parameters=parameters,
            )
        )

    add(
        name="search_rules",
        description="永続化された家庭内ルールを検索します。",
        approval_mode="auto",
        parameters={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "query": {"type": "string"},
                "category": {"type": "string"},
                "status": {
                    "type": "string",
                    "enum": ["active", "pending_approval", "all"],
                },
                "limit": {"type": "integer"},
            },
            "required": ["query"],
        },
        run=lambda arguments: _search(ctx, arguments),
    )
    add(
        name="get_rule",
        description="ルールの現在版と本文を取得します。",
        approval_mode="auto",
        parameters={
            "type": "object",
            "additionalProperties": False,
            "properties": {"rule_id": {"type": "string"}},
            "required": ["rule_id"],
        },
        run=lambda arguments: _get(ctx, arguments),
    )
    add(
        name="propose_rule",
        description="承認待ちの新しいルール候補を作成します。",
        approval_mode="ask",
        parameters={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "title": {"type": "string"},
                "category": {"type": "string"},
                "rule_type": {"type": "string"},
                "applicable_to": {"type": "array", "items": {"type": "string"}},
                "content": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "basic_rule": {"type": "string"},
                        "exceptions": {"type": "array", "items": {"type": "string"}},
                    },
                },
                "reason": {"type": "string"},
            },
            "required": ["title", "content"],
        },
        run=lambda arguments: _propose(ctx, arguments),
    )
    add(
        name="evaluate_rule",
        description="既存ルールと新しい候補の重複・矛盾・不足情報を決定論的に比較します。",
        approval_mode="auto",
        parameters={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "rule_candidate": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "title": {"type": "string"},
                        "content": {"type": "string"},
                    },
                    "required": ["title", "content"],
                }
            },
            "required": ["rule_candidate"],
        },
        run=lambda arguments: _evaluate(ctx, arguments),
    )
    return declarations
