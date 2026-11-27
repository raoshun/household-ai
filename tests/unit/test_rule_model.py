"""Rule model の domain 検証 (README §4.2 / §4.3 / §4.4 / §4.5).

README の rule sample (L246-267) が model で受理され、README が定義しない追加属性
と空 text は reject されることを検証する。status / rule_type / category の value
domain も README の列挙と完全一致することを見る。
"""

from __future__ import annotations

from datetime import datetime

import pytest
from pydantic import ValidationError

from app.models.rule import (
    RuleBody,
    RuleCategory,
    RuleFrontMatter,
    RuleStatus,
    RuleType,
)


def _readme_sample_front_matter() -> dict[str, object]:
    """README §4.2 L246-267 の sample front matter をそのまま返す。"""
    return {
        "id": "parenting-001",
        "title": "お菓子の与え方",
        "category": "parenting",
        "status": "active",
        "version": 1,
        "rule_type": "principle",
        "priority": "normal",
        "applicable_to": ["children"],
        "tags": ["food", "parenting"],
        "created_at": "2026-11-07T10:00:00+09:00",
        "updated_at": "2026-11-07T10:00:00+09:00",
        "created_by": "user",
        "approved_by": "user",
    }


def test_rule_status_enum_matches_readme_list() -> None:
    # README §4.5 L325-329
    assert [status.value for status in RuleStatus] == [
        "draft",
        "pending_approval",
        "active",
        "rejected",
        "archived",
    ]


def test_rule_type_enum_matches_readme_list() -> None:
    # README §4.4 L311-315
    assert [rule_type.value for rule_type in RuleType] == [
        "strict",
        "principle",
        "preference",
        "guideline",
        "exception",
    ]


def test_rule_category_enum_matches_readme_dirs() -> None:
    # README §3 L199-204 (data/rules dir 名 6 値) と §11.3 L970 の rules.category
    assert [category.value for category in RuleCategory] == [
        "parenting",
        "education",
        "health",
        "lifestyle",
        "finance",
        "general",
    ]


def test_readme_sample_front_matter_is_accepted() -> None:
    front_matter = RuleFrontMatter.model_validate(_readme_sample_front_matter())

    assert front_matter.id == "parenting-001"
    assert front_matter.category is RuleCategory.PARENTING
    assert front_matter.status is RuleStatus.ACTIVE
    assert front_matter.rule_type is RuleType.PRINCIPLE
    assert front_matter.version == 1
    assert front_matter.applicable_to == ["children"]
    readme_created_at = datetime.fromisoformat("2026-11-07T10:00:00+09:00")
    assert front_matter.created_at == readme_created_at
    assert front_matter.approved_by == "user"


def test_front_matter_has_exactly_readme_13_attributes() -> None:
    # README §4.3 L291-303 の 13 属性以外を model が持たないこと
    assert list(RuleFrontMatter.model_fields) == [
        "id",
        "title",
        "category",
        "status",
        "version",
        "rule_type",
        "priority",
        "applicable_to",
        "tags",
        "created_at",
        "updated_at",
        "created_by",
        "approved_by",
    ]


def test_front_matter_rejects_attribute_not_in_readme() -> None:
    # README §4.6 L337「既存 version を上書きしない。」で version 管理は file 名前で行う
    payload = _readme_sample_front_matter() | {
        "summary": "お菓子は食事の前には与えない",
    }

    with pytest.raises(ValidationError) as excinfo:
        RuleFrontMatter.model_validate(payload)

    assert "summary" in str(excinfo.value)


def test_front_matter_rejects_unknown_status_and_category() -> None:
    for key, value in (("status", "disabled"), ("category", "cooking")):
        payload = _readme_sample_front_matter() | {key: value}

        with pytest.raises(ValidationError):
            RuleFrontMatter.model_validate(payload)


def test_front_matter_requires_version_at_least_1() -> None:
    # README §4.6 L341-347 の version は v1 から始まる (bug fix version のみ採用)
    for version in (0, None):
        payload = _readme_sample_front_matter() | {"version": version}

        with pytest.raises(ValidationError):
            RuleFrontMatter.model_validate(payload)


def test_front_matter_rejects_required_attribute_missing_or_blank() -> None:
    # README §4.3 L291-303 で required と定めた属性が缺けると reject される。
    # approved_by は §4.5 L333 で未定を許すため required にしない。
    for key in ("id", "title", "created_by", "status", "category", "rule_type"):
        payload = {k: v for k, v in _readme_sample_front_matter().items() if k != key}
        with pytest.raises(ValidationError):
            RuleFrontMatter.model_validate(payload)

    blank_payload = _readme_sample_front_matter() | {"title": "   "}
    with pytest.raises(ValidationError):
        RuleFrontMatter.model_validate(blank_payload)


def test_approved_by_may_be_uncommitted() -> None:
    # README §4.5 L331「AI が生成した案は pending_approval として扱う。」と §4.5 L333
    payload = _readme_sample_front_matter() | {"status": "pending_approval"}
    del payload["approved_by"]

    front_matter = RuleFrontMatter.model_validate(payload)

    assert front_matter.status is RuleStatus.PENDING_APPROVAL
    assert front_matter.approved_by is None


def test_body_sections_use_readme_headings() -> None:
    # README §4.2 L270 / L274 / L278 / L282 の level 1 見出し 4 個 (差異台帳 D-02)
    assert list(RuleBody.SECTION_HEADINGS) == [
        "基本方針",
        "適用条件",
        "例外条件",
        "判断基準",
    ]

    body = RuleBody(
        basic_policy="食事の前にお菓子を与えない。",
        applicable_conditions="通常の家庭内でのおやつ。",
        exception_conditions="外出時など、特別な事情がある場合は柔軟に対応する。",
        decision_criteria="食事への影響を優先して考える。",
    )
    assert body.exception_conditions.startswith("外出時")


def test_body_rejects_empty_section() -> None:
    with pytest.raises(ValidationError):
        RuleBody(
            basic_policy="食事の前にお菓子を与えない。",
            applicable_conditions="通常の家庭内でのおやつ。",
            exception_conditions="   ",
            decision_criteria="食事への影響を優先して考える。",
        )
