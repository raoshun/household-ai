"""ルール API の 読み出し endpoint (契約 §2.7・決定 1.30/1.31)。

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

import sqlite3
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, PlainSerializer

from app.api.auth import ApiUser, create_auth_dependency
from app.clock import tokyo_iso
from app.db.database import connect
from app.models.rule import RuleBody, RuleCategory, RuleFrontMatter, RuleStatus
from app.repositories import rule_repository
from app.services import search_service
from app.services.exceptions import ProblemEmitter, ProblemRule

# ---------------------------------------------------------------------------
# app.state 依存 (per-request wiring)
# ---------------------------------------------------------------------------


def get_rules_connection(request: Request) -> Iterator[sqlite3.Connection]:
    """rules DB の connection を request 毎 に 開き、応答 後に 閉じる。

    endpoint は 同期 定義 で threadpool 実行 に 乗る ため connection の
    thread 跨ぎ 共有 は 起き ない。未使用 に も ならない ので finally で
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


def _to_list_item(item: rule_repository.RuleListItem) -> RuleListItemResponse:
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


def _build_rule_detail(
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
    responses = [_to_list_item(item) for item in items]
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

    return _build_rule_detail(conn, rules_root=rules_root, rule_id=rule_id)
