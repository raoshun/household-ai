# Phase 0 の検証 / 運用コマンド (§15 Phase 0 完了条件の "lint と unit test は Docker
# 環境内で実行する" に対応)。host へ pip install せず、image 内の .venv を使う。
COMPOSE ?= docker compose
DOCKER ?= docker
IMAGE ?= household-ai-runtime-test:0.1.0
MIRROR_DIR ?= .local/mirror
MIRROR_PORT ?= 8099
MIRROR_BIND ?= 172.17.0.1

.PHONY: help build build-offline up down logs gate proof probe lint test fmt runtime-mirror runtime-serve verify-pinned

help: ## 使い方の一覧
	@printf '%s\n' "make build | build-offline | up | down | logs | gate | proof | probe | lint | test | fmt"
	@printf '%s\n' "make verify-pinned | runtime-mirror | runtime-serve (network 制限環境向け)"

build: ## Docker image を build する
	$(COMPOSE) build

verify-pinned: ## pinned manifest と SDK 実測版を host venv で照合 (§15)
	.venv/bin/python tests/proofs/verify_pinned_manifest.py

runtime-mirror: ## pinned manifest の runtime asset を mirror dir へ staging
	.venv/bin/python tests/proofs/fetch_runtime_mirror.py --out $(MIRROR_DIR)

runtime-serve: ## staging 済み mirror を docker bridge 向けに serve (build と別 shell)
	.venv/bin/python -m http.server $(MIRROR_PORT) --bind $(MIRROR_BIND) --directory $(MIRROR_DIR)

build-offline: ## staging 済み mirror 経由で image を build (network 制限環境向け)
	$(DOCKER) build -f backend/Dockerfile --network=host \
	  --build-arg COPILOT_CLI_DOWNLOAD_BASE_URL=http://172.17.0.1:$(MIRROR_PORT) -t $(IMAGE) .

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
	@docker run --rm $(IMAGE) python /app/tests/proofs/sdk_api_probe.py

lint: ## gate の lint 工程だけを実行
	docker run --rm $(IMAGE) sh -c "ruff check --config pyproject.toml app ../tests --output-format=concise && ruff format --check --config pyproject.toml app ../tests"

test: ## gate の unit test 工程だけを実行
	docker run --rm $(IMAGE) sh -c "python -m pytest -q -p no:cacheprovider --tb=short -rf"

fmt: ## host の git 管理 source を image 内の ruff で format (repo を /mnt/repo へ mount)
	docker run --rm -e RUFF_CACHE_DIR=/tmp/ruff-cache -v $$PWD:/mnt/repo -w /mnt/repo/backend $(IMAGE) sh -c "ruff format app ../tests"
