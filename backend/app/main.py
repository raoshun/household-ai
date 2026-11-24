"""Phase 1 HTTP API の 起動 組立 (契約 §2.7・決定 1.32)。

`uvicorn app.main:app` (README §13.4 L1122) で 起動 する app を 組み立てる。
組立 内容 は 次 に 限定 する:

- mount する の は 契約 §2.7.1 の 11 endpoint (rules 7 + approvals 4) だけ。
  Phase 0 hub 本来 の chat・HITL・tools router と history API は 載せ ない
  (決定 1.28(2)・1.29・1.32)。
- CORS は config.py 既存 の `api_cors_origins` を 再利用 する (決定 1.32)。
  Basic 認証 を browser から 送る ため credential を 許可 し、header は
  `Authorization` と `Content-Type` に 絞る。
- startup で (1) 認証 users の fail-closed 検査 (決定 1.27)、(2) SQLite の
  migration 適用 を行う。検査 を startup に 置く のは `import app.main` だけ で
  環境 変数 依存 の exception を 出さ ない ため。users 未設定・空 list は
  AuthConfigError で 起動 に 失敗 する。
- address・port の bind は uvicorn の 指定 に まかせる (README §13.4 の 運用 で は
  `127.0.0.1:8000` だけ へ publish。repo コード で bind を 固定 し ない = 決定 1.32)。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.approvals import router as approvals_router
from app.api.auth import build_authenticator, install_unauthenticated_handler
from app.api.rules import register_rule_problem_rules
from app.api.rules import router as rules_router
from app.config import Settings, load_settings
from app.db.database import init_database
from app.services.exceptions import ProblemEmitter


def create_app(settings: Settings | None = None) -> FastAPI:
    """Phase 1 endpoint を 載せ た FastAPI instance を 組み立てる。

    `settings` を 渡さ ない と 環境 変数・`.env` から 読む (uvicorn 起動 経路)。
    dependency の 値 は `app.state.rules_db_path`・`app.state.rules_root`・
    `app.state.authenticator` に 置き、rules API の per-request wiring が 読む。
    authenticator だ け startup で 埋める (fail-closed 検査 を起動 時 に 残す ため)。
    """

    cfg = load_settings() if settings is None else settings

    # lifespan は async generator 関数 を 渡す (decorator を 付け る と 二重 wrap)。
    async def lifespan(startup_app: FastAPI) -> AsyncIterator[None]:
        startup_app.state.authenticator = build_authenticator(cfg)
        init_database(cfg.database_path).close()
        cfg.rules_dir.mkdir(parents=True, exist_ok=True)
        yield None

    app = FastAPI(title="household-ai Phase 1 API", lifespan=lifespan)
    app.state.settings = cfg
    app.state.rules_db_path = cfg.database_path
    app.state.rules_root = cfg.rules_dir
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(cfg.api_cors_origins),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )
    emitter: ProblemEmitter = ProblemEmitter.default()
    register_rule_problem_rules(emitter)
    emitter.install(app)
    install_unauthenticated_handler(app)
    app.include_router(rules_router)
    app.include_router(approvals_router)
    return app


app = create_app()
