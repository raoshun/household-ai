"""ルール API の endpoint (読み取り・作成・改訂・復元。契約 §2.7・決定 1.30/1.31)。

- 全 endpoint で HTTP Basic 認証 を 要求 する (決定 1.27)。
- sqlite3 connection は request 単位 で app.state.rules_db_path から
  開き、response 出力 後に 閉じる。rules_root は app.state.rules_root の 値 を
  使う (app 起動 時 wiring は 後続 の 実装 単位)。
- response の 時刻 の み +09:00 の ms ISO 表示 (決定 1.31(4)。DB 保存 は
  UTC µs text の まま)。
- 一覧・version 一覧・search の wrapper key ("count" と "items"/"results") は
  contract が 項目 だけ 規定 の 実装 判断。
- 検索 の 結果 は FTS の 項目 の み 露出 し、limit 上限 20 等 は
  search_service (決定 1.6・1.7) の 規定 に 従う。
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, PlainSerializer

from app.api.auth import ApiUser, create_auth_dependency
from app.clock import tokyo_iso, tokyo_now
from app.db.database import connect
from app.models.rule import RuleBody, RuleCategory, RuleFrontMatter, RuleStatus
from app.repositories import rule_repository
from app.services import rules as services_rules
from app.services import search_service
from app.services.exceptions import ProblemEmitter, ProblemRule

# ---------------------------------------------------------------------------
# app.state 依存 (per-request wiring)
# ---------------------------------------------------------------------------


def get_rules_connection(request: Request) -> Iterator[sqlite3.Connection]:
    """rules DB の connection を request 毎 に 開き、応答 後に 閉じる。

    async endpoint と threadpool 実行 の 同期 dependency が 同じ request で
    connection を 共有 する ため、connect() 側 で thread 同一性 確認 を
    無効化 して ある (1 request 内 の 直列 利用 なので 安全)。finally で
    必ず close する。
    """

    conn = connect(request.app.state.rules_db_path)
    try:
        yield conn
    finally:
        conn.close()


def get_rules_root(request: Request) -> Path:
    """rule Markdown (正本) の root directory を 返す。"""

    return request.app.state.rules_root


def require_api_user(request: Request) -> ApiUser:
    """Basic 認証 を api.auth の dependency で 検査 する。

    app.state.authenticator の 遅延 参照 で wiring 順 の 制約 を 外す。
    失敗 は UnauthenticatedError → install_unauthenticated_handler の
    401 (決定 1.27)。
    """

    require = create_auth_dependency(request.app.state.authenticator)
    return require(request)


RulesConnectionDep = Annotated[sqlite3.Connection, Depends(get_rules_connection)]
RulesRootDep = Annotated[Path, Depends(get_rules_root)]
ApiUserDep = Annotated[ApiUser, Depends(require_api_user)]

# ---------------------------------------------------------------------------
# response model (決定 1.31(1)/(2))
# ---------------------------------------------------------------------------

#: response 限定 の datetime 表示 (+09:00 ms ISO)。
ResponseDatetime = Annotated[datetime, PlainSerializer(tokyo_iso, return_type=str)]


class RuleListItemResponse(BaseModel):
    """一覧 1 item の 8 key (決定 1.31(1))。"""

    id: str
    title: str
    category: str
    status: str
    rule_type: str
    priority: str
    current_version: int
    updated_at: ResponseDatetime


class RuleListResponse(BaseModel):
    count: int
    items: list[RuleListItemResponse]


class RuleVersionItemResponse(BaseModel):
    """version 一覧 1 item の 6 field (決定 1.31(2))。"""

    version: int
    summary: str | None
    change_reason: str | None
    created_by: str | None
    approved_by: str | None
    created_at: ResponseDatetime


class RuleVersionListResponse(BaseModel):
    count: int
    items: list[RuleVersionItemResponse]


class RuleDetailResponse(RuleFrontMatter):
    """詳細 = front matter 13 attr + 現 本文 + 一覧 meta (決定 1.31(2))。

    rules.current_version と 現 version の summary、version 一覧 を 添える。
    承認 履歴 は 審査 API が 担い、本 response は approved_by の み。
    """

    created_at: ResponseDatetime
    updated_at: ResponseDatetime
    current_version: int
    summary: str | None
    body: RuleBody
    versions: list[RuleVersionItemResponse]


class RuleSearchItemResponse(BaseModel):
    """search 1 hit の 6 key (決定 1.8・1.22)。"""

    id: str
    title: str
    category: str
    status: str
    summary: str | None
    current_version: int


class RuleSearchResponse(BaseModel):
    count: int
    results: list[RuleSearchItemResponse]


# ---------------------------------------------------------------------------
# problem JSON 登録 (決定 1.31(3))
# ---------------------------------------------------------------------------


def register_rule_problem_rules(emitter: ProblemEmitter) -> None:
    """rule repository の exception を problem JSON rule へ 登録 する。

    ProblemEmitter.default() が 登録 済み の services 系 RuleNotFoundError や
    RuleValidationError と は 別 class の ため、repository 系 は ここ で
    個別 に 追加 する (dict key が 別 class なので overwrite は 不要)。
    RuleConsistencyError・采番 枯渇・基底 class は 意図 的に 未登録 の
    まま catch-all 500 に 落 す (決定 1.31(3))。
    """

    emitter.register(
        rule_repository.RuleNotFoundError,
        ProblemRule(404, "unknown_error", "unknown_error.rule_not_found"),
    )
    emitter.register(
        rule_repository.RuleVersionStateError,
        ProblemRule(409, "unknown_error", "unknown_error.rule_version_state"),
    )
    emitter.register(
        rule_repository.RuleContentConflictError,
        ProblemRule(409, "unknown_error", "unknown_error.rule_content_conflict"),
    )


# ---------------------------------------------------------------------------
# 内部 helper
# ---------------------------------------------------------------------------


def to_list_item(item: rule_repository.RuleListItem) -> RuleListItemResponse:
    return RuleListItemResponse(
        id=item.id,
        title=item.title,
        category=item.category,
        status=item.status,
        rule_type=item.rule_type,
        priority=item.priority,
        current_version=item.current_version,
        updated_at=item.updated_at,
    )


def _to_version_item(
    item: rule_repository.RuleVersionListItem,
) -> RuleVersionItemResponse:
    return RuleVersionItemResponse(
        version=item.version,
        summary=item.summary,
        change_reason=item.change_reason,
        created_by=item.created_by,
        approved_by=item.approved_by,
        created_at=item.created_at,
    )


def build_rule_detail(
    conn: sqlite3.Connection, *, rules_root: Path, rule_id: str
) -> RuleDetailResponse:
    """md (正本) 基準 の 詳細 を 作る (決定 1.31(2))。

    summary は 余分 な query を 足さ ず list_rule_versions の 結果 から
    現 version の 行 を 引く。md と hash の 破損 は RuleConsistencyError とし
    未登録 の まま catch-all 500 に 落 とす (決定 1.31(3))。
    """

    front_matter, body = rule_repository.load_current_rule(
        conn, rules_root=rules_root, rule_id=rule_id
    )
    versions = rule_repository.list_rule_versions(conn, rule_id)
    summary = next(
        (item.summary for item in versions if item.version == front_matter.version),
        None,
    )
    return RuleDetailResponse(
        **front_matter.model_dump(),
        current_version=front_matter.version,
        summary=summary,
        body=body,
        versions=[_to_version_item(item) for item in versions],
    )


# ---------------------------------------------------------------------------
# router
# ---------------------------------------------------------------------------

router = APIRouter(tags=["rules"], dependencies=[Depends(require_api_user)])

#: 一覧・search は 静的 segment。search を {rule_id} より 手前 に 宣言 し
#: FastAPI の 登録 順 で detail route より 優先 させる (決定的 な 解決 順)。
_SEARCH_PATH = "/api/rules/search"


@router.get("/api/rules", response_model=RuleListResponse)
def list_rules(
    conn: RulesConnectionDep,
    status: RuleStatus | None = None,
    category: RuleCategory | None = None,
) -> RuleListResponse:
    """rules を filter 付き 一覧 (決定 1.30(4)。updated_at DESC・同値 id DESC)。"""

    items = rule_repository.list_rules(
        conn,
        status=status.value if status is not None else None,
        category=category.value if category is not None else None,
    )
    responses = [to_list_item(item) for item in items]
    return RuleListResponse(count=len(responses), items=responses)


@router.get(_SEARCH_PATH, response_model=RuleSearchResponse)
def search_rule_texts(
    conn: RulesConnectionDep,
    q: Annotated[str, Query(min_length=1)],
    status: RuleStatus = RuleStatus.ACTIVE,
    category: RuleCategory | None = None,
    limit: Annotated[int, Query(ge=1, le=search_service.MAX_SEARCH_LIMIT)] = (
        search_service.DEFAULT_SEARCH_LIMIT
    ),
) -> RuleSearchResponse:
    """FTS5 部分一致 検索 (決定 1.6・1.7・1.21。results は bm25 順)。

    status・category・limit の value 域 不正 は FastAPI が 400 に し、
    search_service の clamp より 先 に 弾く (契約 §2.7 の 400 規定)。
    """

    rows = search_service.search_rules(
        conn,
        q,
        status=status.value,
        category=category.value if category is not None else None,
        limit=limit,
    )
    return RuleSearchResponse(count=len(rows), results=rows)


@router.get("/api/rules/{rule_id}/versions", response_model=RuleVersionListResponse)
def list_rule_version_items(
    rule_id: str, conn: RulesConnectionDep
) -> RuleVersionListResponse:
    """1 rule の version 一覧 (新しい 順。決定 1.31(2))。"""

    versions = rule_repository.list_rule_versions(conn, rule_id)
    items = [_to_version_item(item) for item in versions]
    return RuleVersionListResponse(count=len(items), items=items)


@router.get("/api/rules/{rule_id}", response_model=RuleDetailResponse)
def get_rule(
    rule_id: str, conn: RulesConnectionDep, rules_root: RulesRootDep
) -> RuleDetailResponse:
    """rule 詳細 (md 正本 + hash 検証 決定 1.12/1.23・front matter 決定 1.19)。"""

    return build_rule_detail(conn, rules_root=rules_root, rule_id=rule_id)


# ---------------------------------------------------------------------------
# rule Markdown 送信 (作成・改訂 共通)
# ---------------------------------------------------------------------------

#: rule Markdown text (UTF-8) の 上限 byte 数。超過 は 400 (F-05)。
_RULE_TEXT_MAX_BYTES = 1_048_576


def _invalid_request(loc: tuple[str, ...], msg: str) -> RequestValidationError:
    """API 独自 check を install() の 400 invalid_request に 乗せる helper。

    契約 §1 決定 1.31(3)。RequestValidationError を 合成 する のが 一番
    简单的 (新規 exception class は 追加 しない。§1 決定 1.31)。
    """

    return RequestValidationError(
        [
            {
                "type": "value_error",
                "loc": loc,
                "msg": f"Value error, {msg}",
                "input": None,
            }
        ]
    )


async def _read_rule_text(request: Request) -> str:
    """body を rule Markdown text として 受け取る (不正 byte 列 は 400)。

    空・上限 超過・UTF-8 で decode できない 3 case は FastAPI の body
    validation 失敗 の 400 を 使う (install() の 400)。
    """

    raw = await request.body()
    if not raw:
        empty = "rule Markdown が 空 です"
        raise _invalid_request(("body",), empty)
    if len(raw) > _RULE_TEXT_MAX_BYTES:
        too_long = f"rule Markdown は {_RULE_TEXT_MAX_BYTES} bytes まで です"
        raise _invalid_request(("body",), too_long)
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        not_utf8 = "UTF-8 text では ありません"
        raise _invalid_request(("body",), not_utf8) from exc


def _parse_rule_submission(text: str) -> tuple[dict[str, Any], str]:
    """submission を front matter raw dict と 本文 text に 分解 する。

    markdown・YAML 不正 は RuleMarkdownError (ValueError subclass) なので、
    services の RuleValidationError に 読み替えて 同じ 422 に map する。
    raw dict を 返す のは、server 采番 や id 照合 で client 指定 の 有無 を
    見る ため (model 検証 は server 上書き 後、本文 検証 は 途中 check 後)。
    """

    try:
        front_matter_text, body_text = rule_repository.split_rule_markdown(text)
        raw = rule_repository.load_rule_front_matter_dict(front_matter_text)
    except ValueError as exc:
        raise services_rules.RuleValidationError(str(exc)) from exc
    return raw, body_text


#: fingerprint から 落とす front matter 行 頭 (server が 毎回 書き換える 値。
#: codec が 単一 行 に 正規化 する ので 行 頭 match で 除去 できる)。
#: status・approved_by は 落とさ ない (決定 1.30)。
_FINGERPRINT_IGNORED_LINE_PREFIXES = (
    "version:",
    "updated_at:",
    "created_at:",
    "created_by:",
)


def _content_fingerprint(front_matter: RuleFrontMatter, body: RuleBody) -> str:
    """md を 再 render し server 管理 行 を 除い た 内容 sha256 hex を 返す。

    server が updated_at を 毎回 更新 する ため、repository の 内容 一致
    guard は HTTP 経由 では 機能 し ない。同内容 拒否 (決定 1.30) を 比較
    可能 に する ため、render 済み front matter から version・updated_at・
    created_at・created_by 行 を 除い た 行 + 本文 を hash する。status・
    approved_by は その まま 含める ので、ACTIVE が pending_approval へ 戻る
    改訂 で も 再 render に 失敗 し ない (md へ の 反映 は repository が 行う)。
    """

    rendered = rule_repository.render_rule_markdown(front_matter, body)
    front_matter_text, body_text = rule_repository.split_rule_markdown(rendered)
    kept = [
        line
        for line in front_matter_text.splitlines()
        if not line.startswith(_FINGERPRINT_IGNORED_LINE_PREFIXES)
    ]
    digest_target = "".join(line + "\n" for line in kept) + body_text
    return hashlib.sha256(digest_target.encode("utf-8")).hexdigest()


@router.post("/api/rules", status_code=201, response_model=RuleDetailResponse)
async def create_rule(
    request: Request,
    conn: RulesConnectionDep,
    rules_root: RulesRootDep,
    user: ApiUserDep,
) -> RuleDetailResponse:
    """rule を 作成 する (契約 §2.7・決定 1.10)。201 で 詳細 を 返す。

    client は front matter で id を 指定 でき ない (指定 は 400。決定 1.31)。
    id は repository が category 最大 +1 で 采番 する。version・status・
    approved_by・created_at・updated_at・created_by は server が 上書き する
    (黙って 無視。新規 rule は version 1・pending_approval・承認者 なし)。
    """

    text = await _read_rule_text(request)
    raw, body_text = _parse_rule_submission(text)
    id_loc = ("body", "rule_text", "front_matter", "id")
    if "id" in raw:
        forbidden = "作成時に id は指定 できません"
        raise _invalid_request(id_loc, forbidden)
    now = tokyo_now()
    server_owned = {
        "id": "server-assigned",
        "version": 1,
        "status": RuleStatus.PENDING_APPROVAL,
        "approved_by": None,
        "created_at": now,
        "updated_at": now,
        "created_by": user.id,
    }
    try:
        front_matter = RuleFrontMatter.model_validate({**raw, **server_owned})
        body = rule_repository.parse_rule_body(body_text)
    except ValueError as exc:
        raise services_rules.RuleValidationError(str(exc)) from exc
    result = rule_repository.create_rule_with_new_id(
        conn,
        rules_root=rules_root,
        front_matter=front_matter,
        body=body,
        change_reason="作成",
    )
    return build_rule_detail(conn, rules_root=rules_root, rule_id=result.rule_id)


@router.put("/api/rules/{rule_id}", response_model=RuleDetailResponse)
async def revise_rule(
    request: Request,
    rule_id: str,
    conn: RulesConnectionDep,
    rules_root: RulesRootDep,
    user: ApiUserDep,
) -> RuleDetailResponse:
    """rule を 改訂 し、新 version を pending_approval に する (決定 1.30)。

    id は 省略 または rule_id と 一致 なら 黙って 上書き (不一致 のみ 400)。
    現 status が ACTIVE か PENDING_APPROVAL の 時 だけ 改訂 可能 (他 は 409)。
    version・status・approved_by・created_at・created_by は server が 上書き。
    category 変更 の み server が 拒否 する (400。repository の 同一 内容
    guard は server stamp で 無効 に なる ため、内容 一致 は fingerprint で
    409 に する)。変更 理由 は 「内容変更」 を 記録 する。
    """

    text = await _read_rule_text(request)
    raw, body_text = _parse_rule_submission(text)
    submitted_id = raw.get("id")
    if submitted_id is not None and submitted_id != rule_id:
        id_loc = ("body", "rule_text", "front_matter", "id")
        mismatch = "URL の rule id と 本文 の id が 不一致 です"
        raise _invalid_request(id_loc, mismatch)
    current_fm, current_body = rule_repository.load_current_rule(
        conn, rules_root=rules_root, rule_id=rule_id
    )
    if current_fm.status not in (RuleStatus.ACTIVE, RuleStatus.PENDING_APPROVAL):
        reject = "ACTIVE または PENDING_APPROVAL の rule のみ 改訂 できます"
        raise rule_repository.RuleVersionStateError(reject)
    server_owned = {
        "id": rule_id,
        "version": current_fm.version,
        "status": RuleStatus.PENDING_APPROVAL,
        "approved_by": None,
        "created_at": current_fm.created_at,
        "updated_at": tokyo_now(),
        "created_by": user.id,
    }
    try:
        front_matter = RuleFrontMatter.model_validate({**raw, **server_owned})
    except ValueError as exc:
        raise services_rules.RuleValidationError(str(exc)) from exc
    if front_matter.category != current_fm.category:
        immutable = "category は 変更 できません"
        category_loc = ("body", "rule_text", "front_matter", "category")
        raise _invalid_request(category_loc, immutable)
    try:
        body = rule_repository.parse_rule_body(body_text)
    except ValueError as exc:
        raise services_rules.RuleValidationError(str(exc)) from exc
    same_content = _content_fingerprint(front_matter, body) == _content_fingerprint(
        current_fm, current_body
    )
    if same_content:
        unchanged = "現在の 内容 と 同一 の ため 改訂 できません"
        raise rule_repository.RuleContentConflictError(unchanged)
    rule_repository.revise_rule(
        conn,
        rules_root=rules_root,
        rule_id=rule_id,
        front_matter=front_matter,
        body=body,
        expected_current_version=current_fm.version,
        expected_status=current_fm.status.value,
        change_reason="内容変更",
    )
    return build_rule_detail(conn, rules_root=rules_root, rule_id=rule_id)


@router.post("/api/rules/{rule_id}/restore", response_model=RuleDetailResponse)
def restore_rule_version(
    rule_id: str,
    version: Annotated[int, Query(ge=1)],
    conn: RulesConnectionDep,
    rules_root: RulesRootDep,
    user: ApiUserDep,
) -> RuleDetailResponse:
    """過去 version の 本文 で 新 version を 作リ 直 し、即 ACTIVE に する。

    決定 1.25: 復元 は 承認フロー を 経 ない。approvals row を 追加 せず、
    rules.approved_at も 変え ない。復元 後 の status は ACTIVE、created_by
    は 復元 操作 者、created_at は 現 version から 維持 (front matter は
    現 側 を 引き継ぎ、本文 だけ 過去 側。契約 §2.2 L143)。変更 理由 は
    「v{n} による復元」。現 status が ACTIVE か REJECTED の 時 だけ 復元
    可能 (他 は 409)。version 指定 不正 は 400、現 version と の 同 version・
    同一 内容 は 409。
    """

    current_fm, current_body = rule_repository.load_current_rule(
        conn, rules_root=rules_root, rule_id=rule_id
    )
    if current_fm.status not in (RuleStatus.ACTIVE, RuleStatus.REJECTED):
        reject = "ACTIVE または REJECTED の rule のみ 復元 できます"
        raise rule_repository.RuleVersionStateError(reject)
    if version == current_fm.version:
        unchanged = "現在の version と 同一 です"
        raise rule_repository.RuleContentConflictError(unchanged)
    _, old_body = rule_repository.load_rule_version(
        conn, rules_root=rules_root, rule_id=rule_id, version=version
    )
    restored_fm = current_fm.model_copy(
        update={
            "status": RuleStatus.ACTIVE,
            "approved_by": None,
            "created_by": user.id,
            "updated_at": tokyo_now(),
        }
    )
    same_content = _content_fingerprint(restored_fm, old_body) == (
        _content_fingerprint(current_fm, current_body)
    )
    if same_content:
        unchanged = "現在の 内容 と 同一 の ため 復元 できません"
        raise rule_repository.RuleContentConflictError(unchanged)
    rule_repository.revise_rule(
        conn,
        rules_root=rules_root,
        rule_id=rule_id,
        front_matter=restored_fm,
        body=old_body,
        expected_current_version=current_fm.version,
        expected_status=current_fm.status.value,
        change_reason=f"v{version} による復元",
    )
    return build_rule_detail(conn, rules_root=rules_root, rule_id=rule_id)
