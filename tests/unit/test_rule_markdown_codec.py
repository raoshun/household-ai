"""rule Markdown codec の render / parse 検証 (決定 1.17 / contract §2.4.5)。

README §4.2 L245-284 の sample を rule 正本 の byte 列とみなす golden fixture を使う。
gate container へ README.md は mount されない (Makefile L63) ため sample はこの file へ
inline する。render が canonical form を再現し、parse が同一 model を返し、render →
parse → render が不変であることを検証し、layout 破綻と model 検証の reject を見る。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from pydantic import ValidationError

from app.models.rule import RuleBody, RuleFrontMatter
from app.repositories.rule_repository import (
    FRONT_MATTER_DELIMITER,
    RuleMarkdownError,
    parse_rule_body,
    parse_rule_front_matter,
    parse_rule_markdown,
    render_rule_markdown,
    split_rule_markdown,
)

README_SAMPLE_RULE = """\
---
id: parenting-001
title: お菓子の与え方
category: parenting

status: active
version: 1

rule_type: principle
priority: normal

applicable_to:
  - children

tags:
  - food
  - parenting

created_at: 2026-11-07T10:00:00+09:00
updated_at: 2026-11-07T10:00:00+09:00

created_by: user
approved_by: user
---

# 基本方針

食事の前にお菓子を与えない。

# 適用条件

通常の家庭内でのおやつ。

# 例外条件

外出時など、特別な事情がある場合は柔軟に対応する。

# 判断基準

食事への影響を優先して考える。
"""

# README §4.3 L291-303 の 13 attribute の一覧順
README_ATTRIBUTES = (
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
)

JST = timezone(timedelta(hours=9))
CREATED_AT = datetime(2026, 11, 7, 10, 0, 0, tzinfo=JST)
BODY_HEADINGS = ("# 基本方針", "# 適用条件", "# 例外条件", "# 判断基準")


def _front_matter(**overrides: Any) -> RuleFrontMatter:
    """README §4.2 sample と §4.3 一覧の 13 attribute から front matter を作る。"""
    values: dict[str, Any] = {
        "id": "parenting-001",
        "title": "お菓子の与え方",
        "category": "parenting",
        "status": "active",
        "version": 1,
        "rule_type": "principle",
        "priority": "normal",
        "applicable_to": ["children"],
        "tags": ["food", "parenting"],
        "created_at": CREATED_AT,
        "updated_at": CREATED_AT,
        "created_by": "user",
        "approved_by": "user",
    }
    values.update(overrides)
    return RuleFrontMatter.model_validate(values)


def _body(**overrides: str) -> RuleBody:
    """README §4.2 sample L269-284 の 4 section 本文を作る。"""
    sections: dict[str, str] = {
        "basic_policy": "食事の前にお菓子を与えない。",
        "applicable_conditions": "通常の家庭内でのおやつ。",
        "exception_conditions": "外出時など、特別な事情がある場合は柔軟に対応する。",
        "decision_criteria": "食事への影響を優先して考える。",
    }
    sections.update(overrides)
    return RuleBody.model_validate(sections)


def _front_matter_keys(text: str) -> list[str]:
    """render された front matter の key 名を出現順に返す (group 空行は除く)。"""
    closing = text.index("\n---\n")
    return [
        line.split(":", 1)[0]
        for line in text[:closing].split("\n")[1:]
        if line and not line.startswith(" ")
    ]


def _rotate_headings(text: str) -> str:
    """本文 4 見出しの名前を 1 つずつ前に rotate した text を返す。"""
    placeholders = ("<<1>>", "<<2>>", "<<3>>", "<<4>>")
    marked = text
    for heading, placeholder in zip(BODY_HEADINGS, placeholders, strict=True):
        marked = marked.replace(heading, placeholder, 1)
    rotated = marked
    shifted = BODY_HEADINGS[1:] + BODY_HEADINGS[:1]
    for placeholder, heading in zip(placeholders, shifted, strict=True):
        rotated = rotated.replace(placeholder, heading, 1)
    return rotated


def test_render_uses_readme_attribute_order_and_body_headings() -> None:
    rendered = render_rule_markdown(_front_matter(), _body())

    assert _front_matter_keys(rendered) == list(README_ATTRIBUTES)
    assert [line for line in rendered.splitlines() if line.startswith("# ")] == list(
        BODY_HEADINGS
    )


def test_render_reproduces_readme_sample_bytes() -> None:
    assert render_rule_markdown(_front_matter(), _body()) == README_SAMPLE_RULE


def test_parse_of_readme_sample_yields_same_models() -> None:
    front_matter, body = parse_rule_markdown(README_SAMPLE_RULE)

    assert front_matter == _front_matter()
    assert body == _body()
    # timestamp は aware datetime + offset のまま復元される (README §4.3 L298-299)
    assert front_matter.created_at == CREATED_AT
    assert front_matter.updated_at == CREATED_AT
    assert front_matter.created_at.utcoffset() == timedelta(hours=9)


def test_render_then_parse_keeps_model_values() -> None:
    front_matter = _front_matter(status="pending_approval", approved_by=None)
    body = _body(decision_criteria="最初の段落。\n\n二番目の段落。")

    assert parse_rule_markdown(render_rule_markdown(front_matter, body)) == (
        front_matter,
        body,
    )


def test_render_is_stable_after_parse() -> None:
    cases: list[tuple[str, dict[str, Any], dict[str, str]]] = [
        ("未承認", {"status": "pending_approval", "approved_by": None}, {}),
        ("空 list", {"applicable_to": [], "tags": []}, {}),
        (
            "複数 paragraph の section",
            {},
            {"decision_criteria": "最初の段落。\n\n二番目の段落。"},
        ),
        (
            "level 2 見出しを含む section",
            {},
            {"basic_policy": "基本方針。\n\n## 補足"},
        ),
        (
            "YAML として曖昧な scalar",
            {"tags": ["2026", "true", "null", "-1", "1.5", "a: b"], "priority": "-001"},
            {},
        ),
        ("長い title", {"title": "対応方針" * 20}, {}),
        ("1 text のみの list", {"applicable_to": ["長めの 文字列"]}, {}),
    ]
    for label, front_overrides, body_overrides in cases:
        front_matter = _front_matter(**front_overrides)
        body = _body(**body_overrides)
        rendered = render_rule_markdown(front_matter, body)
        reparsed = parse_rule_markdown(rendered)

        assert render_rule_markdown(*reparsed) == rendered, label
        assert reparsed == (front_matter, body), label


def test_uncommitted_rule_renders_approved_by_null() -> None:
    # README は未承認 の approved_by の表記を決めていない (差異台帳 D-01)。13
    # attribute を常に書くため null で書き、parse 側で model の default None に戻す。
    rendered = render_rule_markdown(
        _front_matter(status="pending_approval", approved_by=None),
        _body(),
    )

    assert "approved_by: null\n" in rendered
    assert parse_rule_markdown(rendered)[0].approved_by is None


def test_empty_list_renders_as_flow_style_empty_sequence() -> None:
    rendered = render_rule_markdown(_front_matter(tags=[]), _body())

    assert "tags: []\n" in rendered
    assert parse_rule_markdown(rendered)[0].tags == []


def test_parse_canonicalizes_relaxed_layout() -> None:
    # group 空行を削り、list を flow style で書いても受理し、render は canonical
    # form (README sample の byte 列) へ戻す
    relaxed = (
        README_SAMPLE_RULE.replace(
            "category: parenting\n\nstatus:",
            "category: parenting\nstatus:",
        )
        .replace("applicable_to:\n  - children", "applicable_to: [children]")
        .replace("tags:\n  - food\n  - parenting", "tags: ['food', 'parenting']")
    )
    front_matter, body = parse_rule_markdown(relaxed)

    assert (front_matter, body) == (_front_matter(), _body())
    assert render_rule_markdown(front_matter, body) == README_SAMPLE_RULE


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("# 基本方針\n\n本文。\n", id="front matter 区切りが無い"),
        pytest.param(README_SAMPLE_RULE.rsplit("\n---\n", 1)[0], id="closing が無い"),
        pytest.param(
            README_SAMPLE_RULE.replace("---\nid:", "YAML\nid:", 1),
            id="opening delimiter が別の文字",
        ),
        pytest.param(
            "---\n- id: parenting-001\n---\n", id="front matter が mapping でない"
        ),
        pytest.param("---\n\n---\n", id="front matter が空"),
    ],
)
def test_parse_rejects_front_matter_layout_breaks(text: str) -> None:
    with pytest.raises(RuleMarkdownError):
        parse_rule_markdown(text)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param(
            _rotate_headings(README_SAMPLE_RULE),
            RuleMarkdownError,
            id="4 見出しが順不同",
        ),
        pytest.param(
            README_SAMPLE_RULE.replace("# 判断基準", "# 判断根拠", 1),
            RuleMarkdownError,
            id="未知の見出し",
        ),
        pytest.param(
            README_SAMPLE_RULE.replace("# 例外条件\n\n外出時など", "外出時など", 1),
            RuleMarkdownError,
            id="見出しが欠けて本文が先頭",
        ),
        pytest.param(
            README_SAMPLE_RULE.replace("# 判断基準", "# 基本方針", 1),
            RuleMarkdownError,
            id="同じ見出しが 2 度出る",
        ),
        pytest.param(
            README_SAMPLE_RULE.replace("# 基本方針", "## 基本方針", 1),
            RuleMarkdownError,
            id="level 1 でない見出し",
        ),
    ],
)
def test_parse_rejects_body_section_layout_breaks(
    text: str,
    expected: type[Exception],
) -> None:
    with pytest.raises(expected):
        parse_rule_markdown(text)


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        pytest.param(
            "created_by: user",
            "created_by: user\nsummary: 補足",
            ValidationError,
            id="README 一覧に無い attribute",
        ),
        pytest.param(
            "created_by: user\n",
            "",
            ValidationError,
            id="required attribute が無い",
        ),
        pytest.param(
            "version: 1", "version: 0", ValidationError, id="version が 1 未満"
        ),
        pytest.param(
            "status: active",
            "status: approved",
            ValidationError,
            id="未知の status",
        ),
        pytest.param(
            "category: parenting",
            "category: food",
            ValidationError,
            id="未知の category",
        ),
        pytest.param(
            "created_at: 2026-11-07T10:00:00+09:00",
            "created_at: 無日付",
            ValidationError,
            id="timestamp が datetime でない",
        ),
        pytest.param(
            "tags:\n  - food",
            "tags: food",
            ValidationError,
            id="list 属性に scalar",
        ),
        pytest.param(
            "id: parenting-001",
            "id:   ",
            ValidationError,
            id="空 text の attribute",
        ),
    ],
)
def test_parse_rejects_front_matter_value_breaks(
    old: str,
    new: str,
    expected: type[Exception],
) -> None:
    with pytest.raises(expected):
        parse_rule_markdown(README_SAMPLE_RULE.replace(old, new, 1))


def test_render_rejects_value_that_breaks_front_matter_line() -> None:
    # 実測: PyYAML は multi-line scalar を folding して 2 line 以上にする
    with pytest.raises(RuleMarkdownError):
        render_rule_markdown(_front_matter(title="一行目\n二行目"), _body())
    with pytest.raises(RuleMarkdownError):
        render_rule_markdown(
            _front_matter(tags=["段落1\n段落2"]),
            _body(),
        )


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        pytest.param(
            "created_at",
            "created_at: 2026-11-07T10:00:00+09:00",
            id="created_at",
        ),
        pytest.param(
            "updated_at",
            "updated_at: 2026-11-07T10:00:00+09:00",
            id="updated_at",
        ),
    ],
)
def test_render_writes_timestamp_as_readme_style_iso_text(
    field: str, expected: str
) -> None:
    # README L263-264 は `T` 区切り・秒 precision・固定 offset の unquoted ISO text。
    # Pydantic dump の `2026-11-07 10:00:00+09:00` や PyYAML dump の space 区切りでは
    # ない 表記 を file へ 書く (決定 1.19)。
    rendered = render_rule_markdown(_front_matter(), _body())

    assert f"{expected}\n" in rendered
    assert field in _front_matter_keys(rendered)


def test_render_truncates_sub_second_timestamp_and_stays_stable() -> None:
    # 模型 は micro second まで 持てる が README の 表記 は 秒 precision。切捨てた
    # text を parse し直すと 同一 byte 列 に render する こと を 実測 で 確認 した。
    precise = datetime(2026, 11, 7, 10, 0, 0, 123456, tzinfo=JST)

    rendered = render_rule_markdown(_front_matter(created_at=precise), _body())

    assert "created_at: 2026-11-07T10:00:00+09:00\n" in rendered
    reparsed = parse_rule_markdown(rendered)
    assert reparsed[0].created_at == CREATED_AT
    assert render_rule_markdown(*reparsed) == rendered


def test_render_keeps_source_offset_without_conversion() -> None:
    # codec は source の offset を Asia/Tokyo へ 変換 しない (実測)。README §17 L1415 の
    # Asia/Tokyo 要求 を どこ で 満たす か は generation 側 の 論点 で Q-11 に のこる。
    utc_morning = datetime(2026, 11, 7, 1, 0, 0, tzinfo=UTC)

    rendered = render_rule_markdown(_front_matter(created_at=utc_morning), _body())

    assert "created_at: 2026-11-07T01:00:00+00:00\n" in rendered


@pytest.mark.parametrize("field", ["created_at", "updated_at"])
def test_render_rejects_naive_timestamp(field: str) -> None:
    # README sample L263-264 には offset なしの 表記 が 無い ため naive は reject する。
    # load 側 は relaxed の まま (決定 1.18 と 同じ 方針)。
    naive = datetime(2026, 11, 7, 10, 0, 0)

    with pytest.raises(RuleMarkdownError, match="timezone offset 付き"):
        render_rule_markdown(_front_matter(**{field: naive}), _body())


def test_parse_accepts_naive_timestamp_but_render_rejects_it() -> None:
    # offset の 無い ISO text は 受理 し naive datetime に 復元 する が、その model は
    # render できない (決定 1.19 の 非対称 な 受け 付け 方)。
    relaxed = README_SAMPLE_RULE.replace(
        "created_at: 2026-11-07T10:00:00+09:00",
        "created_at: 2026-11-07 10:00:00",
        1,
    )

    front_matter, body = parse_rule_markdown(relaxed)

    assert front_matter.created_at == datetime(2026, 11, 7, 10, 0, 0)
    assert front_matter.created_at.tzinfo is None
    with pytest.raises(RuleMarkdownError):
        render_rule_markdown(front_matter, body)


# --- public な split / 個別 parse (API 層の front matter 上書きのための入口) ---


def test_split_rule_markdown_separates_texts_without_delimiters() -> None:
    front_matter_text, body_text = split_rule_markdown(README_SAMPLE_RULE)

    assert front_matter_text.startswith("id: ")
    assert FRONT_MATTER_DELIMITER not in front_matter_text.splitlines()
    assert body_text.lstrip("\n").startswith("# 基本方針")
    # delimiter を戻して組み直すと元の byte 列に一致する (情報を失わない)。
    assert f"---\n{front_matter_text}\n---\n{body_text}\n" == README_SAMPLE_RULE


def test_split_rule_markdown_rejects_missing_delimiters() -> None:
    with pytest.raises(RuleMarkdownError):
        split_rule_markdown("# 基本方針\n\n本文。\n")
    with pytest.raises(RuleMarkdownError):
        split_rule_markdown(README_SAMPLE_RULE.rsplit("\n---\n", 1)[0])


def test_split_rule_markdown_passes_through_broken_body() -> None:
    # 4 見出しの検証は parse_rule_body の役割。split は layout を検査しない。
    _front_matter_text, _body_text = split_rule_markdown(
        README_SAMPLE_RULE.replace("# 判断基準", "# 判断根拠", 1)
    )


def test_public_parse_entries_match_parse_rule_markdown() -> None:
    fm_flat, body_flat = parse_rule_markdown(README_SAMPLE_RULE)
    front_matter_text, body_text = split_rule_markdown(README_SAMPLE_RULE)

    assert parse_rule_front_matter(front_matter_text) == fm_flat
    assert parse_rule_body(body_text) == body_flat


def test_public_parse_entries_reject_own_breaks() -> None:
    with pytest.raises(RuleMarkdownError, match="mapping"):
        parse_rule_front_matter("- 1\n- 2")
    with pytest.raises(ValidationError):
        parse_rule_front_matter("id: parenting-001\ncategory: 存在しない")
    with pytest.raises(RuleMarkdownError, match="見出し"):
        parse_rule_body("# 基本方針\n\n本文のみ。")
