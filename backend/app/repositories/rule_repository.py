"""Rule Markdown file codec と version 記述層 (Phase 1)。

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

import hashlib
import re
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, cast

import yaml

from app.db.database import from_db_timestamp, to_db_timestamp
from app.models.rule import RuleBody, RuleCategory, RuleFrontMatter, RuleStatus
from app.services.search_service import refresh_rule_index, split_search_text

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
        return [f"{key}:", *(f"  - {_render_scalar(key, item)}" for item in value)]
    return [f"{key}: {_render_scalar(key, value)}"]


def _render_scalar(field: str, value: object) -> str:
    if value is None:
        # 承認されていない rule の approved_by は README に表記例が無い。13 attribute を
        # 常に書くため null で書き、load 時は model の default None に復元する。
        return "null"
    if isinstance(value, datetime):
        # timestamp は README L263-264 の unquoted ISO (`T` + 固定 offset)。PyYAML の
        # datetime dump は space 区切りになるため使わない (実測 §2.4.5 2)。
        return _require_single_line(_render_timestamp(field, value))
    if isinstance(value, Enum):
        # sample は enum を plain scalar (README L247-248) で書く。PyYAML に enum を
        # 渡すと `!!python/object/apply:` の python 専用 tag になり safe_load できない。
        value = value.value
    dumped = yaml.dump(
        value, default_flow_style=True, allow_unicode=True, sort_keys=False
    )
    return _require_single_line(dumped)


def _require_aware_timestamp(field: str, value: datetime) -> datetime:
    # README §4.2 sample L263-264 は offset 付き の 時刻 で、§17 L1415 は Asia/Tokyo を
    # 要求 し、契約 Q-11 も naive な 値 を 持た ない 方針。README の 表記 に 対応 しない
    # naive は render 側 で だけ reject し、load 側 は relaxed の まま (決定 1.18 と
    # 同じ 方針、1.19 で 登記)。
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        msg = f"front matter の {field} は timezone offset 付き datetime で書く"
        raise RuleMarkdownError(msg)
    return value


def _render_timestamp(field: str, value: datetime) -> str:
    # README L263-264 と 契約 Q-11 の Phase 1 方針どおり、正本 表記 を 秒 precision の
    # ISO text に 固定 し、micro second を 持つ value は 切捨て る (決定 1.19 で 登記)。
    # codec は source の offset を 変換 しない。
    return _require_aware_timestamp(field, value).isoformat(timespec="seconds")


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


# ---------------------------------------------------------------------------
# rule 記述層 (契約 §2.4。README §4.5 の version 管理の 4 操作)。
#
# public な書き込み関数 1 つが BEGIN IMMEDIATE の transaction を 1 つ所有する
# (契約 §1 決定 1.24)。pysqlite の legacy mode は非 DML 文の auto-commit を持つため
# SAVEPOINT も外側の BEGIN も使わず、失敗時は rollback だけ行う。commit 後に孤児
# として残った md は受け入れ、同じ id・version の書き直しだけを許す (§1 決定 1.24)。
# 時刻は呼び出し側が持っていて DB は clock を読まない (§1 決定 1.20) ため、front
# matter か datetime 引数から受け取る。呼び出し側は conn.row_factory =
# sqlite3.Row を設定する (rules の列 name で読む)。
# ---------------------------------------------------------------------------


class RuleRepositoryError(Exception):
    """rule 記述層の失敗で、Markdown の layout の壊れ以外を表す base error。"""


class RuleNotFoundError(RuleRepositoryError):
    """指定 id の rule row が DB に無い。"""


class RuleContentConflictError(RuleRepositoryError):
    """新しい version の content_hash が指し先 version と同じ (§1 決定 1.23)。"""


class RuleVersionStateError(RuleRepositoryError):
    """操作の前提 (指し先 version と status) を満たさない。"""


class RuleConsistencyError(RuleRepositoryError):
    """pointer の row・version row・md・hash の対応が壊れている (§1 決定 1.23)。"""


class RuleIdSequenceExhaustedError(RuleRepositoryError):
    """server 採番が rule id を作れない (§1 決定 1.30(2) の 500)。

    2 つの 経路: category 内の 連番 が 999 を 越える case と、UNIQUE
    collision の +1 retry が 上限 回数 で 解けない case。999 超過 の policy は
    契約 #2 の 後続課題で、Phase 1 は 500 にする。
    """


class ApprovalTargetNotFoundError(RuleRepositoryError):
    """審査対象 pending_approval の rule が無い (§1 決定 1.31(3) の 404)。

    approvals row ではなく rules.status が審査対象の持主 (§1 決定 1.28/1.25)。
    """


# approvals.status の値域は README・契約のどちらにも register が無く、0001 の column
# comment のとおり open なので、承認・却下操作で書く 2 value を module 側に置く。
_APPROVAL_STATUS_APPROVED = "approved"
_APPROVAL_STATUS_REJECTED = "rejected"

# rule id は README sample の parenting-001 (§4.2 L247) の形。category の slug だけへ
# 絞ることで、file_path の path component 2 つ (category・id) を escape 無く安全な
# 値に限定する (§1 決定 1.24)。
_RULE_ID_RE = re.compile(
    rf"^(?:{'|'.join(cat.value for cat in RuleCategory)})-[0-9]{{3,3}}$"
)

_Row = Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class RuleWriteResult:
    """書き込みの commit 後の結果。version は commit 時点の pointer の version。"""

    rule_id: str
    version: int
    file_path: str
    content_hash: str


@dataclass(frozen=True, slots=True)
class _RulePointer:
    """rules row と指し先 version の rule_versions row をセットにした snapshot。"""

    row: _Row
    version_row: _Row

    @property
    def current_version(self) -> int:
        return cast("int", self.row["current_version"])

    @property
    def status(self) -> str:
        return cast("str", self.row["status"])

    @property
    def content_hash(self) -> str:
        # content_hash は rule_versions の列で rules には無い (0001 DDL)。
        return cast("str", self.version_row["content_hash"])


def compute_content_hash(text: str) -> str:
    """md text の sha256 hex を返す (契約 §1 決定 1.23)。

    hash は md の byte そのものは判定へ使わず md から render した text から計算する。
    判定は呼び出し側が transaction 内で行う (決定 1.23)。
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def summarize_rule_body(body: RuleBody) -> str | None:
    """summary を本文の先頭の line から作る。

    summary は rule_versions.summary (NULL 可。README に列が無く契約 Q-06 の要求で
    migration 0002 が追加) で、basic_policy が空の本文からは作れないので None を
    返す。長さなど value domain の register は契約に無いので CHECK は無い。
    """
    stripped = body.basic_policy.strip()
    if not stripped:
        return None
    return stripped.splitlines()[0].strip()


def compute_rule_search_text(front_matter: RuleFrontMatter, body: RuleBody) -> str:
    """FTS 5 と 6 の検索対象 text を返す (契約 §1 決定 1.5)。

    title・本文 4 section・tags を search_text とみなす。applicable_to は決 1.5 の
    対象外で、search_service §2.9 の splitter と同じ text に揃えるため \n 区切りで
    連結する。
    """
    parts = [
        front_matter.title,
        body.basic_policy,
        body.applicable_conditions,
        body.exception_conditions,
        body.decision_criteria,
        *front_matter.tags,
    ]
    return "\n".join(part for part in parts if part.strip())


def build_rule_file_path(rule_id: str, category: str, version: int) -> str:
    """rules.file_path と data/rules/ 相対の md の相対 path を返す (§1 決定 1.24)。

    path の template (data/rules/<category>/<rule-id>/v<n>.md) は README に無いが
    hub の Markdown mirror と §4.5 の version 管理の要求から契約 §1 決定 1.24 で
    決めた。id は _RULE_ID_RE の validation を通った値だけを expected する。
    """
    if not _RULE_ID_RE.fullmatch(rule_id):
        msg = f"rule id の形ではない: {rule_id!r}"
        raise RuleRepositoryError(msg)
    return f"{category}/{rule_id}/v{version}.md"


def _as_row(row: object) -> _Row:
    if not hasattr(row, "keys"):
        msg = "row は conn.row_factory = sqlite3.Row で dict-like に読む"
        raise RuleRepositoryError(msg)
    return cast("_Row", row)


def _validate_rule_identity(front_matter: RuleFrontMatter) -> str:
    """id と category の整合を検証し、file_path へ使う category の value を返す。"""
    if not _RULE_ID_RE.fullmatch(front_matter.id):
        msg = f"rule id に受け付けられない form: {front_matter.id!r}"
        raise RuleRepositoryError(msg)
    category = front_matter.category.value
    if not front_matter.id.startswith(f"{category}-"):
        msg = f"rule id の prefix が category と不一致: {front_matter.id!r}"
        raise RuleRepositoryError(msg)
    return category


# --- public 関数 (契約 §2.2 の state machine の操作)。----------------------------


@contextmanager
def _rule_write_txn(conn: sqlite3.Connection) -> Iterator[None]:
    """public 書き込み関数を BEGIN IMMEDIATE の 1 transaction で serial 化する。

    契約 §1 決定 1.24 は nested な SAVEPOINT も追加の rollback も置かないと決めた。
    失敗時の rollback と成功時の commit だけ行う。pysqlite の legacy mode の
    auto-commit に任せないため、呼び出し側は transaction 中でない conn を渡す。
    """
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield
        conn.commit()
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise


def _load_rule_pointer(conn: sqlite3.Connection, rule_id: str) -> _RulePointer:
    """rules row と指し先の rule_versions row をセットで読む。

    current_version と指し先 version row は同じ transaction 内で作る (決定 1.25)。
    rules row が無いのは RuleNotFoundError、指し先 version row が無いのは row 間が
    壊れているので RuleConsistencyError にする。
    """
    raw_rule = conn.execute("SELECT * FROM rules WHERE id = ?", (rule_id,)).fetchone()
    if raw_rule is None:
        msg = f"rules に id {rule_id!r} の row が無い"
        raise RuleNotFoundError(msg)
    rule_row = _as_row(raw_rule)
    version = cast("int", rule_row["current_version"])
    raw_version = conn.execute(
        "SELECT * FROM rule_versions WHERE rule_id = ? AND version = ?",
        (rule_id, version),
    ).fetchone()
    if raw_version is None:
        msg = f"rule_versions に pointer ({rule_id!r}, {version}) の row が無い"
        raise RuleConsistencyError(msg)
    return _RulePointer(row=rule_row, version_row=_as_row(raw_version))


def _write_rule_markdown(rules_root: Path, relative_path: str, content: str) -> None:
    """render 済みの md text を rules_root 相対 path の file に書く。

    path の parent まで作る。commit 失敗後の孤児 md へ同じ id・version を書き直す
    場合だけ同じ path への上書きを許す (契約 §1 決定 1.24)。
    """
    target = rules_root / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def _read_rule_markdown(rules_root: Path, relative_path: str) -> str:
    """rules_root 相対 path の md text を読む。md が正本 (README §4.1)。"""
    try:
        return (rules_root / relative_path).read_text(encoding="utf-8")
    except OSError as err:
        msg = f"rule Markdown を読めない: {relative_path!r}"
        raise RuleConsistencyError(msg) from err


def _refresh_search_index(
    conn: sqlite3.Connection,
    front_matter: RuleFrontMatter,
    body: RuleBody,
) -> None:
    """決定 1.5 の対象 field から search index を作り直す (契約 §2.4)。

    rules row と同じ transaction 内で rule_search_index を置換する。search_service
    の refresh_rule_index が DELETE → INSERT を行う。
    """
    search_text = compute_rule_search_text(front_matter, body)
    refresh_rule_index(conn, front_matter.id, split_search_text(search_text))


def create_rule(
    conn: sqlite3.Connection,
    *,
    rules_root: Path,
    front_matter: RuleFrontMatter,
    body: RuleBody,
    change_reason: str | None = None,
) -> RuleWriteResult:
    """新しい rule を version 1・status pending_approval の作成する (契約 §2.2)。

    id は呼び出し側 (service・API) が決める (0001 DDL L「値域の生成はサービス層」)。
    この layer は id と category の整合だけ検証する。version・status・approved_by は
    §2.2 の state machine のとおり layer 側で 1・pending_approval・NULL へ揃える。
    rules row・rule_versions row・md・search index を 1 transaction で書く (§1 決定
    1.26: INSERT 後の pointer 追加 UPDATE は無い)。commit 失敗で孤児になった md は
    受け入れる (§1 決定 1.24)。id の repeat 呼び出しは sqlite3.IntegrityError が
    そのまま上がる (API layer で 409 へ対応付ける)。
    """
    category = _validate_rule_identity(front_matter)
    fm = front_matter.model_copy(
        update={
            "version": 1,
            "status": RuleStatus.PENDING_APPROVAL,
            "approved_by": None,
        }
    )
    content = render_rule_markdown(fm, body)
    content_hash = compute_content_hash(content)
    rule_id = fm.id
    relative_path = build_rule_file_path(rule_id, category, 1)
    with _rule_write_txn(conn):
        conn.execute(
            "INSERT INTO rules (id, title, category, status, rule_type, priority,"
            " current_version, file_path, created_at, updated_at, approved_at,"
            " archived_at)"
            " VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, NULL, NULL)",
            (
                rule_id,
                fm.title,
                category,
                fm.status.value,
                fm.rule_type.value,
                fm.priority,
                relative_path,
                to_db_timestamp(fm.created_at),
                to_db_timestamp(fm.updated_at),
            ),
        )
        conn.execute(
            "INSERT INTO rule_versions (rule_id, version, content_hash,"
            " change_reason, created_by, approved_by, created_at, summary)"
            " VALUES (?, 1, ?, ?, ?, NULL, ?, ?)",
            (
                rule_id,
                content_hash,
                change_reason,
                fm.created_by,
                to_db_timestamp(fm.created_at),
                summarize_rule_body(body),
            ),
        )
        _write_rule_markdown(rules_root, relative_path, content)
        _refresh_search_index(conn, fm, body)
    return RuleWriteResult(rule_id, 1, relative_path, content_hash)


def revise_rule(
    conn: sqlite3.Connection,
    *,
    rules_root: Path,
    rule_id: str,
    front_matter: RuleFrontMatter,
    body: RuleBody,
    expected_current_version: int | None = None,
    expected_status: str | None = None,
    change_reason: str | None = None,
) -> RuleWriteResult:
    """rule の属性と本文を新しい version へ書き直す (契約 §2.2 の改訂)。

    status と approved_by の state machine の値は service layer が渡す値を front
    matter のまま通す (§2.2。active からの改訂を pending_approval にする必然性は
    service の policy)。この layer は version だけ pointer.current_version + 1 に
    揃える (§1 決定 1.25)。新しい md の content_hash が指し先 version と同じ場合は
    version を作らず断る (§1 決定 1.23)。rules.approved_at と created_at は update
    しない (0001 DDL D-13: approved_at は承認操作だけの writer)。UPDATE の WHERE に
    current_version と現行 status を条件へ入れる (§1 決定 1.26)。
    """
    category = _validate_rule_identity(front_matter)
    if front_matter.id != rule_id:
        msg = f"front matter id {front_matter.id!r} と rule_id {rule_id!r} が不一致"
        raise RuleRepositoryError(msg)
    with _rule_write_txn(conn):
        pointer = _load_rule_pointer(conn, rule_id)
        if (
            expected_current_version is not None
            and pointer.current_version != expected_current_version
        ):
            msg = f"current_version が {expected_current_version!r} でない"
            raise RuleVersionStateError(msg)
        if expected_status is not None and pointer.status != expected_status:
            msg = f"status が {expected_status!r} でない"
            raise RuleVersionStateError(msg)
        if category != pointer.row["category"]:
            msg = f"category {category!r} は既存 rule と変更できない"
            raise RuleRepositoryError(msg)
        old_version = pointer.current_version
        new_version = old_version + 1
        fm = front_matter.model_copy(update={"version": new_version})
        content = render_rule_markdown(fm, body)
        content_hash = compute_content_hash(content)
        if content_hash == pointer.content_hash:
            msg = "新しい version の content_hash が指し先 version と同じ"
            raise RuleContentConflictError(msg)
        relative_path = build_rule_file_path(rule_id, category, new_version)
        conn.execute(
            "INSERT INTO rule_versions (rule_id, version, content_hash,"
            " change_reason, created_by, approved_by, created_at, summary)"
            " VALUES (?, ?, ?, ?, ?, NULL, ?, ?)",
            (
                rule_id,
                new_version,
                content_hash,
                change_reason,
                fm.created_by,
                to_db_timestamp(fm.updated_at),
                summarize_rule_body(body),
            ),
        )
        updated = conn.execute(
            "UPDATE rules SET title = ?, status = ?, rule_type = ?, priority = ?,"
            " current_version = ?, file_path = ?, updated_at = ?"
            " WHERE id = ? AND current_version = ? AND status = ?",
            (
                fm.title,
                fm.status.value,
                fm.rule_type.value,
                fm.priority,
                new_version,
                relative_path,
                to_db_timestamp(fm.updated_at),
                rule_id,
                old_version,
                pointer.status,
            ),
        )
        if updated.rowcount != 1:
            msg = "rules pointer の UPDATE が 1 件も該当しない"
            raise RuleConsistencyError(msg)
        _write_rule_markdown(rules_root, relative_path, content)
        _refresh_search_index(conn, fm, body)
    return RuleWriteResult(rule_id, new_version, relative_path, content_hash)


def _decide_pending_rule_version(
    conn: sqlite3.Connection,
    *,
    rules_root: Path,
    rule_id: str,
    is_approve: bool,
    reviewed_by: str,
    reviewed_at: datetime,
    review_comment: str | None = None,
) -> RuleWriteResult:
    """pending_approval の rule を active (承認) か rejected (却下) へ進める。

    切替前に md (正本) と rule_versions.content_hash の一致を検証し (契約 §1 決定
    1.23)、切替後の front matter を同じ version の md へ書き直して hash を計算し
    直す。決定 1.18 は rejected の md の approved_by を null にするので、approve だ
    け詰める。rule_versions.approved_by も approve だけで入り、却下は NULL の残る。
    rules.approved_at は承認瞬間の時刻だけ、却下ではそのまま (0001 DDL D-13)。
    approvals row は審査操作のこの場で作る (status の値域は 0001 comment の open
    なので、この layer が approved・rejected を register する)。search index は
    決定 1.5 の対象 field (title・本文 4 section・tags) が変わらないので refresh
    しない (§1 決定 1.5)。Q-10 は解決済みで却下は rejected とし active へ昇格させ
    ず、本文を削除しない。updated_at は reviewed_at へ進める (§1 決定 1.19)。
    """
    with _rule_write_txn(conn):
        pointer = _load_rule_pointer(conn, rule_id)
        if pointer.status != RuleStatus.PENDING_APPROVAL.value:
            msg = f"rule {rule_id!r} は pending_approval でない: {pointer.status!r}"
            raise RuleVersionStateError(msg)
        version_row = pointer.version_row
        version = pointer.current_version
        if version_row["approved_by"] is not None:
            msg = f"version ({rule_id!r}, {version}) は審査済み"
            raise RuleVersionStateError(msg)
        relative_path = cast("str", pointer.row["file_path"])
        content = _read_rule_markdown(rules_root, relative_path)
        if compute_content_hash(content) != pointer.content_hash:
            msg = f"md の hash が content_hash と不一致: {relative_path!r}"
            raise RuleConsistencyError(msg)
        front_matter, body = parse_rule_markdown(content)
        flipped = front_matter.model_copy(
            update={
                "status": RuleStatus.ACTIVE if is_approve else RuleStatus.REJECTED,
                "updated_at": reviewed_at,
                "approved_by": reviewed_by if is_approve else None,
            }
        )
        new_content = render_rule_markdown(flipped, body)
        new_hash = compute_content_hash(new_content)
        reviewed = to_db_timestamp(reviewed_at)
        versioned = conn.execute(
            "UPDATE rule_versions SET content_hash = ?, approved_by = ?"
            " WHERE rule_id = ? AND version = ? AND approved_by IS NULL",
            (new_hash, reviewed_by if is_approve else None, rule_id, version),
        )
        if versioned.rowcount != 1:
            msg = "審査前の version row を UPDATE できない"
            raise RuleVersionStateError(msg)
        if is_approve:
            rules_updated = conn.execute(
                "UPDATE rules SET status = ?, updated_at = ?, approved_at = ?"
                " WHERE id = ? AND current_version = ? AND status = ?",
                (
                    flipped.status.value,
                    reviewed,
                    reviewed,
                    rule_id,
                    version,
                    RuleStatus.PENDING_APPROVAL.value,
                ),
            )
        else:
            rules_updated = conn.execute(
                "UPDATE rules SET status = ?, updated_at = ?"
                " WHERE id = ? AND current_version = ? AND status = ?",
                (
                    flipped.status.value,
                    reviewed,
                    rule_id,
                    version,
                    RuleStatus.PENDING_APPROVAL.value,
                ),
            )
        if rules_updated.rowcount != 1:
            msg = "rules pointer の UPDATE が 1 件も該当しない"
            raise RuleConsistencyError(msg)
        approval_status = (
            _APPROVAL_STATUS_APPROVED if is_approve else _APPROVAL_STATUS_REJECTED
        )
        conn.execute(
            "INSERT INTO approvals (rule_id, status, proposed_by, reviewed_by,"
            " review_comment, created_at, reviewed_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                rule_id,
                approval_status,
                version_row["created_by"],
                reviewed_by,
                review_comment,
                reviewed,
                reviewed,
            ),
        )
        _write_rule_markdown(rules_root, relative_path, new_content)
    return RuleWriteResult(rule_id, version, relative_path, new_hash)


def approve_pending_rule_version(
    conn: sqlite3.Connection,
    *,
    rules_root: Path,
    rule_id: str,
    reviewed_by: str,
    reviewed_at: datetime,
    review_comment: str | None = None,
) -> RuleWriteResult:
    """pending_approval の rule の現行 version を承認して active へ進める (§2.2)。

    時刻は呼び出し側が渡す aware な datetime (DB は clock を読まない、§1 決定
    1.20)。naive は to_db_timestamp の ValueError がそのまま上がる。
    """
    return _decide_pending_rule_version(
        conn,
        rules_root=rules_root,
        rule_id=rule_id,
        is_approve=True,
        reviewed_by=reviewed_by,
        reviewed_at=reviewed_at,
        review_comment=review_comment,
    )


def reject_pending_rule_version(
    conn: sqlite3.Connection,
    *,
    rules_root: Path,
    rule_id: str,
    reviewed_by: str,
    reviewed_at: datetime,
    review_comment: str | None = None,
) -> RuleWriteResult:
    """pending_approval の rule の現行 version を却下して rejected にする (§2.2)。

    Q-10 は解決済みで却下は rejected とし、active へ昇格させず、本文を削除しない。
    rules.approved_at と rule_versions.approved_by は却下では変更しない (D-13)。
    """
    return _decide_pending_rule_version(
        conn,
        rules_root=rules_root,
        rule_id=rule_id,
        is_approve=False,
        reviewed_by=reviewed_by,
        reviewed_at=reviewed_at,
        review_comment=review_comment,
    )


def load_current_rule(
    conn: sqlite3.Connection,
    *,
    rules_root: Path,
    rule_id: str,
) -> tuple[RuleFrontMatter, RuleBody]:
    """pointer の指す md (正本) を読んで返す (契約 §2.4)。

    md と rule_versions.content_hash が違うのは row・md の対応が壊れているので
    RuleConsistencyError。古い version の restore は load_rule_version と
    revise_rule の呼び出し合わせで service が行う (契約 §1 決定 1.10)。
    """
    pointer = _load_rule_pointer(conn, rule_id)
    relative_path = cast("str", pointer.row["file_path"])
    content = _read_rule_markdown(rules_root, relative_path)
    if compute_content_hash(content) != pointer.content_hash:
        msg = f"md の hash が content_hash と不一致: {relative_path!r}"
        raise RuleConsistencyError(msg)
    return parse_rule_markdown(content)


def load_rule_version(
    conn: sqlite3.Connection,
    *,
    rules_root: Path,
    rule_id: str,
    version: int,
) -> tuple[RuleFrontMatter, RuleBody]:
    """任意 (古い含む) version の md を固定 path template から読んで返す。

    rules.file_path は現行 version だけなので、path は build_rule_file_path の
    template (§1 決定 1.24) で再構築する。hash の検証は決定 1.23 で同じ。
    """
    raw_rule = conn.execute(
        "SELECT category FROM rules WHERE id = ?", (rule_id,)
    ).fetchone()
    if raw_rule is None:
        msg = f"rules に id {rule_id!r} の row が無い"
        raise RuleNotFoundError(msg)
    category = cast("str", _as_row(raw_rule)["category"])
    raw_version = conn.execute(
        "SELECT content_hash FROM rule_versions WHERE rule_id = ? AND version = ?",
        (rule_id, version),
    ).fetchone()
    if raw_version is None:
        msg = f"rule_versions に ({rule_id!r}, {version}) の row が無い"
        raise RuleNotFoundError(msg)
    content_hash = cast("str", _as_row(raw_version)["content_hash"])
    relative_path = build_rule_file_path(rule_id, category, version)
    content = _read_rule_markdown(rules_root, relative_path)
    if compute_content_hash(content) != content_hash:
        msg = f"md の hash が content_hash と不一致: {relative_path!r}"
        raise RuleConsistencyError(msg)
    return parse_rule_markdown(content)


# ---------------------------------------------------------------------------
# API 用の 読み出し query と server 採番 (契約 §1 決定 1.28/1.30/1.31)。
# ---------------------------------------------------------------------------

# 一覧 item の projection (決定 1.31(1))。SQL と dataclass の順を揃える。
_RULE_LIST_COLUMNS = (
    "id, title, category, status, rule_type, priority, current_version, updated_at"
)
_RULE_LIST_SQL_PREFIX = f"SELECT {_RULE_LIST_COLUMNS} FROM rules"
_RULE_LIST_SQL_ORDER = " ORDER BY updated_at DESC, id DESC"


@dataclass(frozen=True, slots=True)
class RuleListItem:
    """rules 一覧 の 1 item (決定 1.31(1) の 8 key)。"""

    id: str
    title: str
    category: str
    status: str
    rule_type: str
    priority: str
    current_version: int
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class RuleVersionListItem:
    """rule_versions 1 row の version 一覧 item。

    契約の version 一覧 の key (version と summary) に 作成・承認 の meta を
    含む (決定 1.31(2))。content_hash は内部検証用で API に晒さない (§1 決定 1.23)。
    """

    version: int
    summary: str | None
    change_reason: str | None
    created_by: str | None
    approved_by: str | None
    created_at: datetime


def list_rules(
    conn: sqlite3.Connection,
    *,
    status: str | None = None,
    category: str | None = None,
) -> list[RuleListItem]:
    """rules を filter 付きの一覧で返す (契約 §1 決定 1.30(4)/1.31(1))。

    status・category は省略可で、渡した value だけの filter になる。value 域の
    validation は API layer (Pydantic enum) が持ち、この layer は未知の value を
    空の一覧に落とす。並びは updated_at DESC で、同値の tiebreak の id DESC は
    contract 規定の無い impl 判断 (決定的な順が一覧に必要なため)。
    """
    clauses: list[str] = []
    args: list[Any] = []
    if status is not None:
        clauses.append("status = ?")
        args.append(status)
    if category is not None:
        clauses.append("category = ?")
        args.append(category)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = conn.execute(_RULE_LIST_SQL_PREFIX + where + _RULE_LIST_SQL_ORDER, args)
    return [_rule_list_item(_as_row(row)) for row in rows]


def _rule_list_item(row: _Row) -> RuleListItem:
    return RuleListItem(
        id=cast("str", row["id"]),
        title=cast("str", row["title"]),
        category=cast("str", row["category"]),
        status=cast("str", row["status"]),
        rule_type=cast("str", row["rule_type"]),
        priority=cast("str", row["priority"]),
        current_version=cast("int", row["current_version"]),
        updated_at=from_db_timestamp(cast("str", row["updated_at"])),
    )


def list_rule_versions(
    conn: sqlite3.Connection, rule_id: str
) -> list[RuleVersionListItem]:
    """rule の version 一覧を新しい順で返す (契約 §1 決定 1.31(2))。

    rules の row が無いのは 404 対象なので RuleNotFoundError。並びの新しい順は
    contract に規定の無い impl 判断 (一覧と同じ「新しい順」に揃える)。
    """
    raw_rule = conn.execute("SELECT 1 FROM rules WHERE id = ?", (rule_id,)).fetchone()
    if raw_rule is None:
        msg = f"rules に id {rule_id!r} の row が無い"
        raise RuleNotFoundError(msg)
    rows = conn.execute(
        "SELECT version, summary, change_reason, created_by, approved_by,"
        " created_at"
        " FROM rule_versions WHERE rule_id = ? ORDER BY version DESC",
        (rule_id,),
    )
    return [
        RuleVersionListItem(
            version=cast("int", row["version"]),
            summary=row["summary"],
            change_reason=row["change_reason"],
            created_by=row["created_by"],
            approved_by=row["approved_by"],
            created_at=from_db_timestamp(cast("str", row["created_at"])),
        )
        for row in rows
    ]


def fetch_pending_approval(conn: sqlite3.Connection, rule_id: str) -> RuleListItem:
    """審査対象 1 件を pending_approval の限りで返す (§1 決定 1.28(1))。

    承認待ち state は rules.status が持ち approvals table は読まない (§1 決定
    1.25: approvals row は承認操作だけの writer)。rule が無いのも審査対象で無い
    のも 404 approval_not_found に同じなので区別しない (§1 決定 1.31(3))。
    """
    row = conn.execute(
        "SELECT " + _RULE_LIST_COLUMNS + " FROM rules WHERE id = ? AND status = ?",
        (rule_id, RuleStatus.PENDING_APPROVAL.value),
    ).fetchone()
    if row is None:
        msg = f"pending_approval の rule が無い: {rule_id!r}"
        raise ApprovalTargetNotFoundError(msg)
    return _rule_list_item(_as_row(row))


# rules.id の PK collision text。sqlite の message は "UNIQUE constraint
# failed: rules.id" の form (§1 決定 1.30(2) の retry 条件)。
_UNIQUE_RULE_ID_TEXT = "UNIQUE constraint failed: rules.id"
# 連続 UNIQUE の retry 上限は contract 規定の無い impl 判断。他 client との
# 競走が続く場合は raise して 500 に落とす (§1 決定 1.31(3) の catch-all)。
_NEW_ID_MAX_ATTEMPTS = 3


def next_rule_id(conn: sqlite3.Connection, category: RuleCategory | str) -> str:
    """category 毎の 3 桁 suffix の 既存 max + 1 の rule id を返す。

    采番は §1 決定 1.30(2) の max + 1 零埋め 3 桁。999 が在れば
    RuleIdSequenceExhaustedError (500 相当)。UNIQUE collision の +1 retry は
    create_rule_with_new_id が担うので、この function 単体は rule id を作るだけ。
    """
    try:
        prefix = RuleCategory(category).value
    except ValueError as err:
        msg = f"unknown category: {category!r}"
        raise RuleRepositoryError(msg) from err
    # GLOB の [0-9] 3 桁だけへ絞り、suffix 位置の substr で数値の max を取る。
    # substr の 開始位置 len(prefix) + 2 は dash の straight (parenting-001 なら 11)。
    max_suffix = conn.execute(
        "SELECT MAX(CAST(substr(id, ?) AS INTEGER)) FROM rules WHERE id GLOB ?",
        (len(prefix) + 2, f"{prefix}-[0-9][0-9][0-9]"),
    ).fetchone()[0]
    if max_suffix is None:
        candidate = 1
    else:
        if max_suffix >= 999:
            msg = f"rule id の 採番 が 3 桁の上限超过ぎ: {prefix!r}"
            raise RuleIdSequenceExhaustedError(msg)
        candidate = max_suffix + 1
    return f"{prefix}-{candidate:03d}"


def create_rule_with_new_id(
    conn: sqlite3.Connection,
    *,
    rules_root: Path,
    front_matter: RuleFrontMatter,
    body: RuleBody,
    change_reason: str | None = None,
) -> RuleWriteResult:
    """server 採番で id を上書きして create_rule する (§1 決定 1.30(2))。

    front_matter.id は使わず category の 既存 max + 1 を毎回作り直す。採番の
    straight 後に別の writer が同じ id を commit すると rules.id の UNIQUE に
    当たるので、新しい max で 計算し直す = 結果として +1 の retry (§1 決定
    1.30(2))。 UNIQUE 以外の sqlite3.IntegrityError はそのまま raise する。
    front_matter.status や version は create_rule が §2.2 の値へ揃える。
    """
    last_error: sqlite3.IntegrityError | None = None
    for _ in range(_NEW_ID_MAX_ATTEMPTS):
        rule_id = next_rule_id(conn, front_matter.category)
        numbered = front_matter.model_copy(update={"id": rule_id})
        try:
            return create_rule(
                conn,
                rules_root=rules_root,
                front_matter=numbered,
                body=body,
                change_reason=change_reason,
            )
        except sqlite3.IntegrityError as err:
            if _UNIQUE_RULE_ID_TEXT not in str(err):
                raise
            last_error = err
    msg = (
        f"rule id 採番 の UNIQUE collision が retry {_NEW_ID_MAX_ATTEMPTS} 回"
        " 解消しない"
    )
    raise RuleIdSequenceExhaustedError(msg) from last_error
