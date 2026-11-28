from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.agent.permissions import PermissionBroker, PermissionPrompt
from app.db.database import init_database
from app.models.rule import (
    RuleBody,
    RuleCategory,
    RuleFrontMatter,
    RuleStatus,
    RuleType,
)
from app.repositories import rule_repository
from app.services.agent_tools import AgentRuleContext, build_agent_rule_tools
from app.services.audit import AuditTrail


@pytest.fixture
def db_fixture(tmp_path: Path):
    data_dir = tmp_path / "data"
    db_path = data_dir / "household.db"
    rules_root = data_dir / "rules"
    conn = init_database(db_path)
    yield db_path, rules_root, conn
    conn.close()


def _context(
    db_path: Path, rules_root: Path
) -> tuple[AgentRuleContext, PermissionBroker, AuditTrail]:
    broker = PermissionBroker(timeout_seconds=1)
    audit = AuditTrail()
    ctx = AgentRuleContext(
        conversation_id="conversation-1",
        db_path=db_path,
        rules_root=rules_root,
        broker=broker,
        audit=audit,
    )
    ctx.bind_session("session-1")
    return ctx, broker, audit


def _declarations(ctx: AgentRuleContext):
    return {tool.name: tool for tool in build_agent_rule_tools(ctx)}


def test_agent_rule_tools_schema_and_permission_modes(db_fixture) -> None:
    db_path, rules_root, _ = db_fixture
    ctx, _, _ = _context(db_path, rules_root)
    declarations = _declarations(ctx)

    assert set(declarations) == {
        "search_rules",
        "get_rule",
        "propose_rule",
        "evaluate_rule",
    }
    assert declarations["search_rules"].approval_mode == "auto"
    assert declarations["get_rule"].approval_mode == "auto"
    assert declarations["evaluate_rule"].approval_mode == "auto"
    assert declarations["propose_rule"].approval_mode == "ask"


def test_search_and_get_rule_from_persistent_db(db_fixture) -> None:
    db_path, rules_root, conn = db_fixture
    from app.clock import tokyo_now

    now = tokyo_now()
    front_matter = RuleFrontMatter(
        id="server-assigned",
        title="お菓子の与え方",
        category=RuleCategory.PARENTING,
        status=RuleStatus.PENDING_APPROVAL,
        version=1,
        rule_type=RuleType.PRINCIPLE,
        applicable_to=["children"],
        tags=[],
        created_at=now,
        updated_at=now,
        created_by="test",
        approved_by=None,
    )
    body = RuleBody(
        basic_policy="食事前にお菓子を与えない",
        applicable_conditions="子ども全員",
        exception_conditions="体調不良の場合",
        decision_criteria="夕食前かどうか",
    )
    result = rule_repository.create_rule_with_new_id(
        conn,
        rules_root=rules_root,
        front_matter=front_matter,
        body=body,
        change_reason="test",
    )
    conn.execute("UPDATE rules SET status = 'active' WHERE id = ?", (result.rule_id,))
    conn.commit()

    ctx, _, _ = _context(db_path, rules_root)
    declarations = _declarations(ctx)
    search = declarations["search_rules"].handler(
        "search_rules",
        {"query": "お菓子", "status": "active", "limit": 10},
    )
    assert search["results"]
    assert search["results"][0]["id"] == result.rule_id

    detail = declarations["get_rule"].handler(
        "get_rule",
        {"rule_id": result.rule_id},
    )
    assert detail["found"] is True
    assert detail["content"]["basic_rule"] == "食事前にお菓子を与えない"
    assert detail["status"] == "active"


def test_propose_rule_requires_exact_approved_grant(db_fixture) -> None:
    db_path, rules_root, _ = db_fixture
    ctx, _, audit = _context(db_path, rules_root)
    declarations = _declarations(ctx)
    arguments = {
        "title": "夕食を残した場合のデザート",
        "category": "parenting",
        "rule_type": "principle",
        "applicable_to": ["children"],
        "content": {
            "basic_rule": "夕食を残した場合はデザートを与えない",
            "exceptions": ["体調不良の場合"],
        },
        "reason": "会話から抽出",
    }

    denied = declarations["propose_rule"].handler("propose_rule", arguments)
    assert denied["status"] == "denied"
    assert any(entry.action == "tool_denied_without_grant" for entry in audit.entries())


async def test_propose_rule_consumes_approved_grant(db_fixture) -> None:
    db_path, rules_root, _ = db_fixture
    ctx, broker, _ = _context(db_path, rules_root)
    declarations = _declarations(ctx)
    arguments = {
        "title": "夕食を残した場合のデザート",
        "category": "parenting",
        "rule_type": "principle",
        "applicable_to": ["children"],
        "content": {
            "basic_rule": "夕食を残した場合はデザートを与えない",
            "exceptions": ["体調不良の場合"],
        },
        "reason": "会話から抽出",
    }
    prompt = PermissionPrompt(
        prompt_id="approval-1",
        session_id="session-1",
        tool_name="propose_rule",
        arguments=arguments,
    )
    task = asyncio.create_task(broker.request_permission(prompt))
    for _ in range(50):
        if broker.get(prompt.prompt_id) is not None:
            break
        await asyncio.sleep(0.01)
    await broker.resolve(prompt.prompt_id, approved=True)
    await task

    created = declarations["propose_rule"].handler("propose_rule", arguments)
    assert created["success"] is True
    assert created["status"] == "pending_approval"
    assert created["rule_id"].startswith("parenting-")

    second = declarations["propose_rule"].handler("propose_rule", arguments)
    assert second["status"] == "denied"


def test_evaluate_rule_is_deterministic(db_fixture) -> None:
    db_path, rules_root, _ = db_fixture
    ctx, _, _ = _context(db_path, rules_root)
    declarations = _declarations(ctx)
    result = declarations["evaluate_rule"].handler(
        "evaluate_rule",
        {
            "rule_candidate": {
                "title": "夕食を残した場合のデザート",
                "content": "夕食を残した場合はデザートを与えない",
            }
        },
    )
    assert set(result) == {
        "related_rules",
        "duplicate_possible",
        "conflict_possible",
        "missing_information",
        "confirmation_required",
    }
