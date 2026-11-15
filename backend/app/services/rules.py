"""§9.1 family rule (§10.1 `/api/rules`) と §9.4 pending → active 判定を保持する。"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.clock import tokyo_iso

RuleKind = str  # Phase 0 は "intent" のみ (§15)。
RuleStatus = str  # "pending" | "active" | "tombstoned"
RuleSource = str  # "agent" | "family_ui" | "migration"

RULE_STATUSES = ("pending", "active", "tombstoned")
RULE_SOURCES = ("agent", "family_ui", "migration")
CONFLICT_KINDS = ("intent_duplicate", "time_overlap")

_TIME_PATTERN = re.compile(r"([01]?\d|2[0-3]):([0-5]\d)")
_WEEKDAY_PATTERN = re.compile(
    r"(月曜|火曜|水曜|木曜|金曜|土曜|日曜|毎日|平日|土日|毎週[0-9]+回?)"
)
_STOPWORDS = {
    "して",
    "する",
    "する時は",
    "しない",
    "です",
    "ます",
    "以降",
    "までに",
    "まで",
    "の",
    "を",
    "は",
    "と",
    "で",
    "に",
    "から",
}


@dataclass(slots=True)
class Rule:
    """§9.1 rule 1 件。`status` 以外の変更は revision を上げる。"""

    rule_id: str
    title: str
    content: str
    status: RuleStatus = "pending"
    kind: RuleKind = "intent"
    source: RuleSource = "agent"
    created_by: str = "family"
    created_at: str = field(default_factory=tokyo_iso)
    updated_at: str = field(default_factory=tokyo_iso)
    pending_at: str | None = None
    approved_at: str | None = None
    tombstoned_at: str | None = None
    revision: int = 1
    notes: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.status not in RULE_STATUSES:
            msg = f"未知の rule status: {self.status}"
            raise ValueError(msg)
        if self.source not in RULE_SOURCES:
            msg = f"未知の rule source: {self.source}"
            raise ValueError(msg)

    def to_dict(self) -> dict[str, Any]:
        return {
            "approved_at": self.approved_at,
            "content": self.content,
            "created_at": self.created_at,
            "created_by": self.created_by,
            "kind": self.kind,
            "notes": [dict(entry) for entry in self.notes],
            "pending_at": self.pending_at,
            "revision": self.revision,
            "rule_id": self.rule_id,
            "source": self.source,
            "status": self.status,
            "title": self.title,
            "tombstoned_at": self.tombstoned_at,
            "updated_at": self.updated_at,
        }


@dataclass(slots=True)
class RuleConflict:
    """§9.4 conflicts heuristic 結果 (§9.4 判定)。UI 表示専用。"""

    conflict_id: str
    kind: str
    rule_ids: list[str]
    detail: str
    created_at: str = field(default_factory=tokyo_iso)

    def __post_init__(self) -> None:
        if self.kind not in CONFLICT_KINDS:
            msg = f"未知の rule conflict kind: {self.kind}"
            raise ValueError(msg)

    def to_dict(self) -> dict[str, Any]:
        return {
            "conflict_id": self.conflict_id,
            "created_at": self.created_at,
            "detail": self.detail,
            "kind": self.kind,
            "rule_ids": list(self.rule_ids),
        }


@dataclass(slots=True)
class RuleDraft:
    """§9.4 conflict 判定に使う temporary representation (active rule にはしない)。"""

    title: str
    content: str
    rule_id: str | None
    kind: RuleKind = "intent"
    source: RuleSource = "agent"

    @property
    def text(self) -> str:
        return f"{self.title} {self.content}".strip()


def _overlap_ratio(a: str, b: str) -> float:
    """heuristic な text overlap (日本語 token + 文字 bigram, §9.4)。"""

    def tokenize(text: str) -> set[str]:
        words = {
            token
            for token in re.split(r"[\s、。，．!?！？:：]+", text)
            if len(token) >= 2
        }
        words.update({token for token in words if token not in _STOPWORDS})
        compact = re.sub(r"[\s、。，．!?！？:：]", "", text)
        words.update({compact[i : i + 2] for i in range(len(compact) - 1)})
        return words

    left = tokenize(a)
    right = tokenize(b)
    if not left or not right:
        return 0.0
    return len(left & right) / max(len(left), len(right))


def _time_tokens(text: str) -> set[str]:
    out: set[str] = set()
    for hour, minute in _TIME_PATTERN.findall(text):
        out.add(f"{int(hour):02d}:{minute}")
    return out


def _weekday_tokens(text: str) -> set[str]:
    return {token for token in _WEEKDAY_PATTERN.findall(text) if token}


def detect_rule_conflicts(
    draft: RuleDraft | Mapping[str, Any],
    existing: Iterable[Mapping[str, Any]],
    *,
    overlap_threshold: float = 0.5,
) -> list[RuleConflict]:
    """§9.4 heuristic: 意図重複 + 同一開始時刻の重複 (UI 表示用)。

    Strict conflict resolution はしない (§13 の gate には含めない)。
    """

    candidate = _as_draft(draft)
    conflicts: list[RuleConflict] = []
    sequence = 0
    candidate_time = _time_tokens(candidate.text)
    candidate_weekday = _weekday_tokens(candidate.text)
    for raw in existing:
        other = _as_draft(raw)
        if other.rule_id and other.rule_id == candidate.rule_id:
            continue
        ratio = _overlap_ratio(candidate.text, other.text)
        if ratio >= overlap_threshold:
            sequence += 1
            conflicts.append(
                RuleConflict(
                    conflict_id=f"con-{sequence:04d}",
                    detail=(
                        f"意図が類似 (consistency ratio {ratio:.2f}): "
                        f"{_preview(candidate.text)} / {_preview(other.text)}"
                    ),
                    kind="intent_duplicate",
                    rule_ids=[
                        candidate.rule_id or "candidate",
                        other.rule_id or "existing",
                    ],
                )
            )
        shared_time = candidate_time & _time_tokens(other.text)
        if shared_time:
            shared_weekday = candidate_weekday & _weekday_tokens(other.text)
            sequence += 1
            conflicts.append(
                RuleConflict(
                    conflict_id=f"con-{sequence:04d}",
                    detail=(
                        "同一開始時刻: "
                        + ", ".join(sorted(shared_time))
                        + (
                            " (同一曜日指定 " + ", ".join(sorted(shared_weekday)) + ")"
                            if shared_weekday
                            else ""
                        )
                    ),
                    kind="time_overlap",
                    rule_ids=[
                        candidate.rule_id or "candidate",
                        other.rule_id or "existing",
                    ],
                )
            )
    return conflicts


def _as_draft(value: RuleDraft | Mapping[str, Any]) -> RuleDraft:
    if isinstance(value, RuleDraft):
        return value
    return RuleDraft(
        content=str(value.get("content", "")),
        kind=str(value.get("kind", "intent")),
        rule_id=value.get("rule_id"),
        source=str(value.get("source", "agent")),  # type: ignore[arg-type]
        title=str(value.get("title", "")),
    )


def _preview(text: str, limit: int = 40) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return f"{compact[:limit]}…"


class RuleValidationError(ValueError):
    """§9.1 REST 400/404/409 の base validation。"""


class RuleStore:
    """§9.1 rule storage + §9.4 pending → active + §9.1 tombstone。"""

    def __init__(self, rules: Sequence[Mapping[str, Any]] | None = None) -> None:
        self._rules: dict[str, Rule] = {}
        self._sequence = 0
        if rules:
            for raw in rules:
                self._append(Rule(**dict(raw)))

    def _append(self, rule: Rule) -> Rule:
        self._rules[rule.rule_id] = rule
        self._sequence = max(
            self._sequence, _numeric_suffix(rule.rule_id, prefix="rule") or 0
        )
        return rule

    def new_rule_id(self) -> str:
        self._sequence += 1
        return f"rule-{self._sequence:04d}"

    def list_rules(self, *, status: str | None = None) -> list[Rule]:
        rules = list(self._rules.values())
        if status is None:
            return rules
        if status not in RULE_STATUSES:
            msg = f"未知の rule status: {status}"
            raise RuleValidationError(msg)
        return [rule for rule in rules if rule.status == status]

    def get_rule(self, rule_id: str) -> Rule | None:
        return self._rules.get(rule_id)

    def require_rule(self, rule_id: str) -> Rule:
        rule = self._rules.get(rule_id)
        if rule is None:
            msg = f"rule が見つかりません: {rule_id}"
            raise LookupError(msg)
        return rule

    def create_rule(
        self,
        *,
        title: str,
        content: str,
        source: RuleSource = "family_ui",
        kind: RuleKind = "intent",
        created_by: str = "family",
        status: RuleStatus = "pending",
        rule_id: str | None = None,
        notes: Sequence[Mapping[str, Any]] | None = None,
    ) -> Rule:
        normalized_title = title.strip()
        normalized_content = content.strip()
        if not normalized_title or not normalized_content:
            msg = "title と content は空でない文字列で指定して下さい"
            raise RuleValidationError(msg)
        if status not in RULE_STATUSES:
            msg = f"未知の rule status: {status}"
            raise RuleValidationError(msg)
        if source not in RULE_SOURCES:
            msg = f"未知の rule source: {source}"
            raise RuleValidationError(msg)
        resolved_id = rule_id or self.new_rule_id()
        if resolved_id in self._rules:
            msg = f"rule_id は既に存在します: {resolved_id}"
            raise RuleValidationError(msg)
        now = tokyo_iso()
        rule = Rule(
            approved_at=now if status == "active" else None,
            content=normalized_content,
            created_at=now,
            created_by=created_by,
            kind=kind,
            notes=[dict(entry) for entry in notes or []],
            pending_at=now if status == "pending" else None,
            revision=1,
            rule_id=resolved_id,
            source=source,
            status=status,
            title=normalized_title,
            tombstoned_at=None,
            updated_at=now,
        )
        self._append(rule)
        return rule

    def update_rule(
        self,
        rule_id: str,
        *,
        title: str | None = None,
        content: str | None = None,
        note: str | None = None,
    ) -> Rule:
        """§9.4 edit (active → pending の再判定待ち) (§13 heuristic なし)。"""

        rule = self.require_rule(rule_id)
        if rule.status == "tombstoned":
            msg = f"tombstoned rule は編集できません: {rule_id}"
            raise RuleValidationError(msg)
        changed = False
        if title is not None and title.strip() != rule.title:
            rule.title = title.strip()
            changed = True
        if content is not None and content.strip() != rule.content:
            rule.content = content.strip()
            changed = True
        if not changed:
            return rule
        now = tokyo_iso()
        rule.revision += 1
        rule.status = "pending"
        rule.approved_at = None
        rule.pending_at = now
        rule.updated_at = now
        if note:
            rule.notes.append({"at": now, "text": note})
        return rule

    def approve_rule(self, rule_id: str, *, decided_by: str = "family") -> Rule:
        """§9.4 pending → active。§10.1 rules approval API の approve 相当。"""

        rule = self._transition(
            rule_id, target="active", decided_by=decided_by, note_prefix="承認"
        )
        rule.approved_at = tokyo_iso()
        return rule

    def reject_rule(
        self, rule_id: str, *, decided_by: str = "family", reason: str | None = None
    ) -> Rule:
        """§9.4 pending → pending (tombstone しない) + §11 record。"""

        rule = self._transition(
            rule_id,
            target="pending",
            decided_by=decided_by,
            note_prefix="差し戻し",
        )
        if reason:
            rule.notes.append({"at": rule.updated_at, "text": f"却下理由: {reason}"})
        return rule

    def delete_rule(self, rule_id: str, *, reason: str | None = None) -> Rule:
        """§9.1 delete = tombstone (active/pending から tombstoned)。"""

        rule = self.require_rule(rule_id)
        if rule.status == "tombstoned":
            return rule
        now = tokyo_iso()
        rule.status = "tombstoned"
        rule.tombstoned_at = now
        rule.updated_at = now
        rule.approved_at = None
        rule.notes.append(
            {"at": now, "text": f"削除 (tombstone): {reason or 'family_ui'}"}
        )
        return rule

    def _transition(
        self,
        rule_id: str,
        *,
        target: RuleStatus,
        decided_by: str,
        note_prefix: str,
    ) -> Rule:
        rule = self.require_rule(rule_id)
        if rule.status == "tombstoned":
            msg = f"tombstoned rule は状態遷移できません: {rule_id}"
            raise RuleValidationError(msg)
        if target == "active" and rule.status != "pending":
            msg = f"pending 以外 (§9.4) を §9.4 承認できません: {rule_id}"
            raise RuleValidationError(msg)
        now = tokyo_iso()
        rule.status = target
        rule.updated_at = now
        if target == "pending" and rule.pending_at is None:
            rule.pending_at = now
        rule.notes.append({"at": now, "text": f"{note_prefix}: {decided_by}"})
        return rule

    def detect_conflicts(
        self, draft: RuleDraft | Mapping[str, Any]
    ) -> list[RuleConflict]:
        """§9.4 提案 draft 対 existing active (§11/§9.4 conflict heuristic)。"""

        compare = [
            *self.list_rules(status="active"),
            *self.list_rules(status="pending"),
        ]
        return detect_rule_conflicts(draft, compare)


def _numeric_suffix(value: str, *, prefix: str) -> int | None:
    suffix = value.removeprefix(f"{prefix}-")
    if suffix == value:
        return None
    try:
        return int(suffix)
    except ValueError:
        return None


__all__ = [
    "CONFLICT_KINDS",
    "RULE_SOURCES",
    "RULE_STATUSES",
    "Rule",
    "RuleConflict",
    "RuleDraft",
    "RuleKind",
    "RuleSource",
    "RuleStatus",
    "RuleStore",
    "RuleValidationError",
    "detect_rule_conflicts",
]
