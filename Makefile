# Phase 0 の検証 / 運用コマンド (§15 Phase 0 完了条件の "lint と unit test は Docker
# 環境内で実行する" に対応)。host へ pip install せず、image 内の .venv を使う。
COMPOSE ?= docker compose
IMAGE ?= household-ai-runtime-test:0.1.0

.PHONY: help build up down logs gate proof probe lint test fmt

help: ## 使い方の一覧
	@printf '%s\n' "make build | up | down | logs | gate | proof | probe | lint | test | fmt"

build: ## Docker image を build する
	$(COMPOSE) build

up: ## 常駐 container を起動 (gate / proof は profile 分離で起動しない)
	$(COMPOSE) up -d runtime

down: ## container を停止
	$(COMPOSE) down

logs: ## runtime container の logs を tail
	$(COMPOSE) logs -f runtime

gate: ## import 確認 + ruff + unit test を Docker で実行 (既定の検証)
	$(COMPOSE) --profile checks run --rm gate

proof: ## Custom Tool / permission の live proof を Docker で実行
	$(COMPOSE) --profile checks run --rm proof

probe: ## pinned SDK の API 面を実測 (pinned image 内で実行)
	docker run --rm $(IMAGE) python /app/tests/proofs/sdk_api_probe.py

lint: ## gate の lint 工程だけを実行
	docker run --rm $(IMAGE) sh -c "ruff check --config pyproject.toml app ../tests --output-format=concise && ruff format --check --config pyproject.toml app ../tests"

test: ## gate の unit test 工程だけを実行
	docker run --rm $(IMAGE) sh -c "python -m pytest -q -p no:cacheprovider --tb=short -rf"

fmt: ## host の git 管理 source を image 内の ruff で format (repo を /mnt/repo へ mount)
	docker run --rm -e RUFF_CACHE_DIR=/tmp/ruff-cache -v $$PWD:/mnt/repo -w /mnt/repo/backend $(IMAGE) sh -c "ruff format app ../tests"
