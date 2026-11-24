"""承認 API の endpoint (契約 §2.7・決定 1.28/1.30/1.31)。

README §10.3 の 4 endpoint だけ を 実装 する (決定 1.28(2)。hub HITL の MCP 中継 用
3 endpoint と history API は 作ら ない)。

- 全 endpoint で HTTP Basic 認証 を 要求 する (決定 1.27。README §10.3 L932 で
  人間 の operation と 名指し さ れる)。
- 一覧・1 件 取得 は rules の pending_approval を 投影 する (決定 1.28(1))。専用 の
  response model は 置か ず、rules の 一覧・詳細 を その まま 返す。
- 承認・却下 の body は option の 審査 comment の み (決定 1.30(4))。version・id など
  を 送り たい ま は 400 invalid_request に する。reviewed_by は 認証 済み user id、
  reviewed_at は server clock で 決める (決定 1.20・1.30(4))。
- path の {id} は rules.id (決定 1.28)。審査対象 が 存在 し ない か pending_approval
  で は ない 場合 は 審査対象 不在 = 404 unknown_error.approval_not_found
  (決定 1.31(3))。同じ version が 既に 審査 済み など repository が 見つける 状態
  不整 は 409 unknown_error.rule_version_state、md hash 不整合 は catch-all 500 に
  落 とす。
- repository exception の problem JSON 登録 は rules と 共通 なので、app 組立 時 に
  app.api.rules.register_rule_problem_rules を 呼ぶ。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from app.api.rules import (
    ApiUserDep,
    RuleDetailResponse,
    RuleListResponse,
    RulesConnectionDep,
    RulesRootDep,
    build_rule_detail,
    require_api_user,
    to_list_item,
)
from app.clock import tokyo_now
from app.models.rule import RuleStatus
from app.repositories import rule_repository
from app.services.exceptions import UnknownApprovalError

router = APIRouter(tags=["approvals"], dependencies=[Depends(require_api_user)])


class ReviewSubmission(BaseModel):
    """承認・却下 が 受け取る 唯一 の body (決定 1.30(4))。

    審査 comment は option。version・id・reviewed_by など の 指定 は 受け付 け ない
    ので extra を 拒否 する。
    """

    model_config = ConfigDict(extra="forbid")

    review_comment: str | None = None


def _require_pending_target(
    conn: sqlite3.Connection, *, rules_root: Path, rule_id: str
) -> None:
    """path の id が 審査対象 (pending_approval の rule) か を 検査 する。

    承認 id = rules.id (決定 1.28) なので、rule が 無い 場合 も pending_approval で
    は ない 場合 も 審査対象 不在 と し て 同 じ 404 に 落 とす。status だけ を 見る
    repository public API が 無い ため、md 正本 検 証 を 兼ね て load_current_rule を
    使う (1 request で 最 大 2 回 本 文 を 読 む)。
    """

    try:
        front_matter, _ = rule_repository.load_current_rule(
            conn, rules_root=rules_root, rule_id=rule_id
        )
    except rule_repository.RuleNotFoundError as exc:
        raise UnknownApprovalError(approval_id=rule_id) from exc
    if front_matter.status is not RuleStatus.PENDING_APPROVAL:
        raise UnknownApprovalError(approval_id=rule_id)


@router.get("/api/approvals", response_model=RuleListResponse)
def list_approvals(conn: RulesConnectionDep) -> RuleListResponse:
    """承認待ち 一覧 = rules の pending_approval 投影 (決定 1.28(1))。

    param は 受け取ら ない (決定 1.30(5))。並び は rules 一覧 と 同じ updated_at DESC
    (同値 は id DESC)。
    """

    items = rule_repository.list_rules(conn, status=RuleStatus.PENDING_APPROVAL.value)
    responses = [to_list_item(item) for item in items]
    return RuleListResponse(count=len(responses), items=responses)


@router.get("/api/approvals/{rule_id}", response_model=RuleDetailResponse)
def get_approval(
    rule_id: str, conn: RulesConnectionDep, rules_root: RulesRootDep
) -> RuleDetailResponse:
    """承認対象 詳細 = pending_approval の rule 詳細 (決定 1.28(1))。"""

    _require_pending_target(conn, rules_root=rules_root, rule_id=rule_id)
    return build_rule_detail(conn, rules_root=rules_root, rule_id=rule_id)


def _decide(
    rule_id: str,
    submission: ReviewSubmission | None,
    conn: sqlite3.Connection,
    rules_root: Path,
    reviewed_by: str,
    *,
    is_approve: bool,
) -> RuleDetailResponse:
    """審査 comment を 受け取っ て 承認・却下 を 反映 し、審査 後 の 詳細 を 返す。

    成功 status は 200 (決定 1.30(3) が 201 を 決める のは rule 作成 だけ で、審査 は
    既存 resource の 状態 変更)。状態 遷移・approvals row・md 書 き 直 し は
    repository の 単一 transaction に 委 ねる (決定 1.11・1.19・1.23)。
    """

    _require_pending_target(conn, rules_root=rules_root, rule_id=rule_id)
    decide = (
        rule_repository.approve_pending_rule_version
        if is_approve
        else rule_repository.reject_pending_rule_version
    )
    decide(
        conn,
        rules_root=rules_root,
        rule_id=rule_id,
        reviewed_by=reviewed_by,
        reviewed_at=tokyo_now(),
        review_comment=None if submission is None else submission.review_comment,
    )
    return build_rule_detail(conn, rules_root=rules_root, rule_id=rule_id)


@router.post("/api/approvals/{rule_id}/approve", response_model=RuleDetailResponse)
def approve_rule(
    rule_id: str,
    conn: RulesConnectionDep,
    rules_root: RulesRootDep,
    user: ApiUserDep,
    submission: ReviewSubmission | None = None,
) -> RuleDetailResponse:
    """pending_approval を ACTIVE へ 昇格 さ せる (決定 1.11・1.28(1))。"""

    return _decide(rule_id, submission, conn, rules_root, user.id, is_approve=True)


@router.post("/api/approvals/{rule_id}/reject", response_model=RuleDetailResponse)
def reject_rule(
    rule_id: str,
    conn: RulesConnectionDep,
    rules_root: RulesRootDep,
    user: ApiUserDep,
    submission: ReviewSubmission | None = None,
) -> RuleDetailResponse:
    """pending_approval を REJECTED に する (決定 1.11、本文 は 保持)。"""

    return _decide(rule_id, submission, conn, rules_root, user.id, is_approve=False)
