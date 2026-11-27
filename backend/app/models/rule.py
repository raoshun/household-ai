"""Rule model (Phase 1).

README §4.3 L291-303 の 13 属性、§4.4 L311-315 の rule_type 5 値、§4.5 L325-329 の
status 5 値、§3 L199-204 の category 6 値を value domain として保持する。本文の 4
見出しは README §4.2 L269-284 の見出し名を使う。Markdown file への読み書きはこの
model ではなく `app.repositories.rule_repository` が持つ (README §4.2)。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field, field_validator


class RuleStatus(StrEnum):
    """README §4.5 L325-329 の status 5 値。"""

    DRAFT = "draft"
    PENDING_APPROVAL = "pending_approval"
    ACTIVE = "active"
    REJECTED = "rejected"
    ARCHIVED = "archived"


class RuleType(StrEnum):
    """README §4.4 L311-315 の rule_type 5 値。AI は種類を区別して回答する (L317)。"""

    STRICT = "strict"
    PRINCIPLE = "principle"
    PREFERENCE = "preference"
    GUIDELINE = "guideline"
    EXCEPTION = "exception"


class RuleCategory(StrEnum):
    """README §3 L199-204 の data/rules dir 名 6 値。

    rules.category の value domain もこの 6 値を使う (README §11.3 L970)。
    """

    PARENTING = "parenting"
    EDUCATION = "education"
    HEALTH = "health"
    LIFESTYLE = "lifestyle"
    FINANCE = "finance"
    GENERAL = "general"


class RuleBody(BaseModel):
    """README §4.2 L269-284 の本文 4 見出し。level 1 見出し名を §4.2 の表記で持つ。"""

    SECTION_HEADINGS: ClassVar[tuple[str, ...]] = (
        "基本方針",
        "適用条件",
        "例外条件",
        "判断基準",
    )

    basic_policy: str
    applicable_conditions: str
    exception_conditions: str
    decision_criteria: str

    @field_validator(
        "basic_policy",
        "applicable_conditions",
        "exception_conditions",
        "decision_criteria",
    )
    @classmethod
    def _reject_empty_section(cls, value: str) -> str:
        text = value.strip()
        if not text:
            msg = "rule Markdown の本文 section が空"
            raise ValueError(msg)
        return text


class RuleFrontMatter(BaseModel):
    """README §4.3 L291-303 の 13 属性。13 属性以外は front matter に書かない。

    README は priority の値域を列挙しない (§4.3 L297) ため model では絞らない。
    value domain が未確定な件は contract の差異台帳 D-03 へ記録した。
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    category: RuleCategory
    status: RuleStatus
    version: int = Field(ge=1)
    rule_type: RuleType
    # README §4.2 の例 L254 は priority: normal を使う。§4.3 L297 と §11.3 L973 は
    # 名前のみで値域を列挙しないため、既定のみ normal とし model で絞らない。
    priority: str = "normal"
    applicable_to: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
    created_by: str
    # README §4.5 L333「activeへの変更は人間の承認操作のみで実行する。」により
    # pending_approval の rule は承認者が未定。approved_by は承認操作で入る。
    approved_by: str | None = None

    @field_validator("id", "title", "priority", "created_by")
    @classmethod
    def _reject_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            msg = "rule front matter の text 属性が空"
            raise ValueError(msg)
        return text
