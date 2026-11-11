# Phase 0 の検証 / 運用コマンド (§15 Phase 0 完了条件の "lint と unit test は Docker
# 環境内で実行する" に対応)。host へ pip install せず、image 内の .venv を使う。
COMPOSE ?= docker compose
DOCKER ?= docker
IMAGE ?= household-ai-runtime-test:0.1.0
MIRROR_DIR ?= .local/mirror
MIRROR_PORT ?= 8099
MIRROR_BIND ?= 172.17.0.1
EVIDENCE ?= docs/evidence

.PHONY: help build build-offline up down logs gate proof probe lint test fmt runtime-mirror
.PHONY: runtime-serve verify-pinned prove-offline prove-offline-negative prove-offline-runtime
.PHONY: prove-manifest-version-tamper prove-manifest-digest-tamper

help: ## 使い方の一覧
	@printf '%s\n' "make build | build-offline | up | down | logs | gate | proof | probe | lint | test | fmt"
	@printf '%s\n' "make verify-pinned | runtime-mirror | runtime-serve (network 制限環境向け)"
	@printf '%s\n' "make prove-offline | prove-offline-negative | prove-offline-runtime (§15 network 遮断 proof)"
	@printf '%s\n' "make prove-manifest-version-tamper | prove-manifest-digest-tamper | prove-negative-gates (integrity negative gate)"

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

# 検証 target は image へ COPY 済みの code ではなく working tree を見る (stale image で
# gate が通るのを防ぐ)。source だけ read-only で mount し、.venv と data volume は隠さない。
SRC_MOUNTS = -v $(CURDIR)/backend/app:/app/backend/app:ro -v $(CURDIR)/tests:/app/tests:ro

lint: ## gate の lint 工程だけを実行 (working tree を mount して実行)
	docker run --rm $(SRC_MOUNTS) $(IMAGE) sh -c "ruff check --config pyproject.toml app ../tests --output-format=concise && ruff format --check --config pyproject.toml app ../tests"

test: ## gate の unit test 工程だけを実行 (working tree を mount して実行)
	docker run --rm $(SRC_MOUNTS) $(IMAGE) sh -c "python -m pytest -q -p no:cacheprovider --tb=short -rf"

fmt: ## host の git 管理 source を image 内の ruff で format (repo を /mnt/repo へ mount)
	docker run --rm -e RUFF_CACHE_DIR=/tmp/ruff-cache -v $$PWD:/mnt/repo -w /mnt/repo/backend $(IMAGE) sh -c "ruff format app ../tests"

# --- §15 network 遮断環境の proof -----------------------------------------------
# bake とは image build 内の `python -m copilot download-runtime` が download までを
# 完了させ、image layer に cache を残すこと (§9 の mirror base URL を使う)。
# proof 側は network 遮断 container で download 経路を再実行し、cache 再利用だけを観測する。

prove-offline: ## network 遮断 container で bake 済み runtime の cache hit を実証
	@mkdir -p $(EVIDENCE)
	$(DOCKER) run --rm --network none $(IMAGE) \
	  python /app/tests/proofs/offline_runtime_cache_proof.py --mode cache-hit \
	  > $(EVIDENCE)/offline-cache-hit.json
	@echo "evidence: $(EVIDENCE)/offline-cache-hit.json"

prove-offline-negative: ## network 遮断で force download が失敗することを実証 (negative gate)
	@mkdir -p $(EVIDENCE)
	$(DOCKER) run --rm --network none $(IMAGE) \
	  python /app/tests/proofs/offline_runtime_cache_proof.py --mode force-download-failure \
	  > $(EVIDENCE)/offline-force-download-failure.json
	@echo "evidence: $(EVIDENCE)/offline-force-download-failure.json"

prove-offline-runtime: ## network 遮断 container で runtime 起動と Custom Tool 登録を実証
	@mkdir -p .local/evidence $(EVIDENCE) && chmod 0777 .local/evidence
	$(DOCKER) run --rm --network none -v $$PWD/.local/evidence:/tmp/evidence $(IMAGE) \
	  python /app/tests/proofs/phase0_offline_runtime_proof.py \
	  --out /tmp/evidence/offline-runtime-proof.json
	@cp .local/evidence/offline-runtime-proof.json $(EVIDENCE)/offline-runtime-proof.json
	@echo "evidence: $(EVIDENCE)/offline-runtime-proof.json"

# pinned manifest を script 側が信用しているかを問う negative gate。manifest の写しを
# 改ざんして実行し、guard が reject すること (exit code != 0) を期待値とする。
# 通過した場合は target 自身が exit 1 して fail する。
GATE_TMP ?= .local/negative-gates
GATE_LOG ?= docs/evidence/negative-gates.md

prove-manifest-version-tamper: ## manifest の SDK 版改ざんが integrity check で failure になることを実証
	@mkdir -p $(GATE_TMP)
	@.venv/bin/python -c "import json,pathlib;p=pathlib.Path('$(GATE_TMP)/manifest-version-tamper.json');d=json.load(open('docs/pinned-runtime-versions.json'));d['sdk_version']='9.9.9';p.write_text(json.dumps(d,ensure_ascii=False,indent=2)+chr(10))"
	@COPILOT_SDK_PKG=github-copilot .venv/bin/python tests/proofs/verify_pinned_manifest.py --manifest $(GATE_TMP)/manifest-version-tamper.json >$(GATE_TMP)/version-tamper.out 2>&1; rc=$$?; \
	if [ $$rc -eq 0 ]; then echo "FAILED: 改ざん manifest が integrity check を通过"; exit 1; fi; \
	echo "negative gate OK: verify_pinned_manifest.py は exit $$rc で reject した"; tail -n 3 $(GATE_TMP)/version-tamper.out

prove-manifest-digest-tamper: ## manifest の runtime digest 改ざんが staging 照合で failure になることを実証
	@mkdir -p $(GATE_TMP)/mirror
	@.venv/bin/python -c "import json,pathlib,shutil;src=pathlib.Path('docs/pinned-runtime-versions.json');d=json.loads(src.read_text(encoding='utf-8'));tag=d['download']['tag'];dst=pathlib.Path('$(GATE_TMP)/mirror');dst.mkdir(parents=True,exist_ok=True);shutil.copytree(pathlib.Path('$(MIRROR_DIR)')/tag,dst/tag,dirs_exist_ok=True);d['runtime_platforms'][d['runtime_platform']]='0'*64;pathlib.Path('$(GATE_TMP)/manifest-digest-tamper.json').write_text(json.dumps(d,ensure_ascii=False,indent=2)+chr(10))"
	@.venv/bin/python tests/proofs/fetch_runtime_mirror.py --manifest $(GATE_TMP)/manifest-digest-tamper.json --out $(GATE_TMP)/mirror >$(GATE_TMP)/digest-tamper.out 2>&1; rc=$$?; \
	if [ $$rc -eq 0 ]; then echo "FAILED: digest 改ざんが staging 照合を通过"; exit 1; fi; \
	echo "negative gate OK: fetch_runtime_mirror.py は exit $$rc で reject した"; tail -n 3 $(GATE_TMP)/digest-tamper.out

prove-negative-gates: ## 4 本の negative gate を順に実行し docs/evidence へ保存
	@bash tests/proofs/collect_negative_gates.sh $(GATE_TMP) $(GATE_LOG)
