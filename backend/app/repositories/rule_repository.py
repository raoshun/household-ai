"""Rule Markdown file codec (Phase 1).

README §4.1 L232-240 は rule の正本を Markdown file とし、§4.2 L244-285 の sample で
front matter 13 attribute と level 1 見出し 4 節の本文の layout を示す。contract 決定
1.17 (§2.4.5) は literal layout を application 側 manual writer で保つことを決めた:
pinned image の PyYAML 6.0.3 は block sequence の dash を parent key と同じ column へ
出し、datetime を space 区切りの `2026-11-07 10:00:00+09:00` で dump するため sample と
byte 一致しない。この module は quoting だけ 1 値ずつ yaml.dump へ委譲し、delimiter と
group 空行と indent は手で書く。Parser は closing `---` を含んだ text を yaml.safe_load
へ渡すと ComposerError になる (実測 §2.4.5 3) ので `---` を hand-slice で切る。

Pydantic の ValidationError は model が知っている field 情報としてそのまま上げ、Markdown
text の layout として壊れている場合だけ RuleMarkdownError を使う。
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

import yaml

from app.models.rule import RuleBody, RuleFrontMatter

FRONT_MATTER_DELIMITER = "---"
# Pydantic field 順が README §4.3 一覧 L291-303 の順 (tests/unit で assert 済み)。
FRONT_MATTER_KEY_ORDER: tuple[str, ...] = tuple(RuleFrontMatter.model_fields)
# README §4.2 sample L245-268 の group 空行 L249・L252・L255・L258・L262・L265 相当。
# 13 attribute を 1 始まり index で数えて key group が閉じる位置 (contract §2.4.5)。
FRONT_MATTER_GROUP_END_INDEXES: frozenset[int] = frozenset({3, 5, 7, 8, 9, 11})
BODY_SECTION_ORDER: tuple[str, ...] = RuleBody.SECTION_HEADINGS


class RuleMarkdownError(ValueError):
    """README §4.2 の layout に従えない rule Markdown text の error."""


def render_rule_markdown(front_matter: RuleFrontMatter, body: RuleBody) -> str:
    """README §4.2 sample と同じ layout の rule Markdown text を返す。

    front matter の value として 1 line に render できない object (multi-line scalar) を
    持つ model は rejected する (決定 1.17)。
    """
    lines = [
        FRONT_MATTER_DELIMITER,
        *_render_front_matter_lines(front_matter),
        FRONT_MATTER_DELIMITER,
        "",
        *_render_body_lines(body),
    ]
    return "\n".join(lines) + "\n"


def parse_rule_markdown(text: str) -> tuple[RuleFrontMatter, RuleBody]:
    """rule Markdown text を front matter と body へ分離する。

    delimiter が無い、本文の 4 見出しが欠落・重複・順不同な text は rejected される。
    """
    lines = text.splitlines()
    if not lines or lines[0] != FRONT_MATTER_DELIMITER:
        msg = "rule Markdown text の 1 行目が opening `---` でない"
        raise RuleMarkdownError(msg)
    closing = next(
        (
            index
            for index in range(1, len(lines))
            if lines[index] == FRONT_MATTER_DELIMITER
        ),
        None,
    )
    if closing is None:
        msg = "rule Markdown text に closing `---` が無い"
        raise RuleMarkdownError(msg)
    front_matter = _parse_front_matter("\n".join(lines[1:closing]))
    body = _parse_body(lines[closing + 1 :])
    return front_matter, body


def _render_front_matter_lines(front_matter: RuleFrontMatter) -> list[str]:
    values = front_matter.model_dump(mode="python")
    lines: list[str] = []
    for index, key in enumerate(FRONT_MATTER_KEY_ORDER, start=1):
        lines.extend(_render_front_matter_entry(key, values[key]))
        if index in FRONT_MATTER_GROUP_END_INDEXES:
            lines.append("")
    return lines


def _render_front_matter_entry(key: str, value: object) -> list[str]:
    if isinstance(value, list):
        if not value:
            # 実測 §2.4.5 4: 空 list は `key: []` の flow style で render される。
            return [f"{key}: []"]
        return [f"{key}:", *(f"  - {_render_scalar(item)}" for item in value)]
    return [f"{key}: {_render_scalar(value)}"]


def _render_scalar(value: object) -> str:
    if value is None:
        # 承認されていない rule の approved_by は README に表記例が無い。13 attribute を
        # 常に書くため null で書き、load 時は model の default None に復元する。
        return "null"
    if isinstance(value, datetime):
        # timestamp は README L263-264 の unquoted ISO (`T` + 固定 offset)。PyYAML の
        # datetime dump は space 区切りになるため使わない (実測 §2.4.5 2)。
        return _require_single_line(value.isoformat())
    if isinstance(value, Enum):
        # sample は enum を plain scalar (README L247-248) で書く。PyYAML に enum を
        # 渡すと `!!python/object/apply:` の python 専用 tag になり safe_load できない。
        value = value.value
    dumped = yaml.dump(
        value, default_flow_style=True, allow_unicode=True, sort_keys=False
    )
    return _require_single_line(dumped)


def _require_single_line(rendered: str) -> str:
    # plain scalar の output 末尾には document end を示す `...` の line が付く (実測)。
    lines = [line for line in rendered.splitlines() if line != "..."]
    if len(lines) != 1:
        msg = "front matter の value を 1 line の scalar として render できない"
        raise RuleMarkdownError(msg)
    return lines[0]


def _render_body_lines(body: RuleBody) -> list[str]:
    sections = [getattr(body, field) for field in RuleBody.model_fields]
    lines: list[str] = []
    for heading, section_text in zip(BODY_SECTION_ORDER, sections, strict=True):
        if lines:
            lines.append("")
        lines.append(f"# {heading}")
        lines.append("")
        lines.extend(section_text.splitlines())
    return lines


def _parse_front_matter(front_matter_text: str) -> RuleFrontMatter:
    try:
        loaded = yaml.safe_load(front_matter_text)
    except yaml.YAMLError as exc:
        msg = "rule Markdown の front matter が YAML として load できない"
        raise RuleMarkdownError(msg) from exc
    if not isinstance(loaded, dict):
        msg = "rule Markdown の front matter が mapping でない"
        raise RuleMarkdownError(msg)
    return RuleFrontMatter.model_validate(loaded)


def _parse_body(body_lines: list[str]) -> RuleBody:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in body_lines:
        if current is None and not line.strip():
            continue  # closing delimiter と 1 番目の見出しの間の空行 (README L269)
        if line.startswith("# "):
            heading = line[2:]
            if heading not in BODY_SECTION_ORDER or heading in sections:
                msg = f"rule Markdown 本文の 4 見出し以外か重複した見出し: {heading}"
                raise RuleMarkdownError(msg)
            sections[heading] = []
            current = heading
            continue
        if current is None:
            msg = "rule Markdown 本文に見出しより前の text がある"
            raise RuleMarkdownError(msg)
        sections[current].append(line)
    if tuple(sections) != BODY_SECTION_ORDER:
        msg = "rule Markdown 本文の 4 見出しが欠落しているか順不同"
        raise RuleMarkdownError(msg)
    values = [
        "\n".join(sections[heading]).strip("\n") for heading in BODY_SECTION_ORDER
    ]
    return RuleBody.model_validate(
        dict(zip(RuleBody.model_fields, values, strict=True))
    )
