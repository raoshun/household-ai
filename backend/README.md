# backend 実行リファレンス（Phase 0）

SDK は pinned 固定: Python 3.12 / `github-copilot-sdk==1.0.29`（CLI runtime 1.0.107）。
pinned 版の実測 API 面は `docs/phase0-capability-notes.md`、実行証跡は
`docs/phase0-verification-report.md`（docs site の v1.2.0-beta API docs は pinned より新しいため名前が異なる）。

## host 実行（uv 固定）

uv で lock/venv を固定し、secret は git 管理外の `.env` か host env で渡す。

```bash
set -a; . ./.env; set +a                  # AI_provider, AI_model, AI_api_key など
uv sync --frozen --extra dev --no-install-project   # --frozen は lock 固定に必須
.venv/bin/python -m ruff check app ../tests
.venv/bin/python -m ruff format --check app ../tests
.venv/bin/ruff check --config pyproject.toml app ../tests
```

`--no-install-project` は backend 自体を editable install しないため、`app` は package として
import せず `.venv/bin/python` 実行時の `sys.path`（backend/）で解決する。uv cache/dir は host
home に作らない（repo 内 `.venv`、`.venv/bin/*`、lock file は git 管理、uv の cache dir は host 側で
`UV_CACHE_DIR` 等显式指定か repo 外へ）。

### pytest rootdir の落とし穴

| 設定 | 意味と注意点 |
| :--- | :--- |
| `pyproject.toml` | `[dependency-groups] dev` と `[tool.uv] package = false`。backend を pip install しない運用 |
| `testpaths = ["../tests"]` | **実行場所が rootdir になる**。`--no-install-project` で repo root 直下に `pyproject.toml` がないため、`backend/` から path 引数なしで実行する（repo root から `pytest tests/...` と渡すと `tests/unit/fake.py` 収集失敗） |
| `.venv/bin/python -m pytest` | `backend/` から path 引数なしで実行。`-p no:cacheprovider` を付けると cache dir を作らない |
| host の Ollama 到達 | host 実行では `OLLAMA_BASE_URL=http://127.0.0.1:11434/v1`（`host.docker.internal` は container 側の名前） |

`data/copilot-runtime` 等は repo 内既定。Proof 実行では消し込みしやすい `/tmp` の一時 dir を使う。

## host の live proof（Ollama gemma4:e2b）

```bash
AI_PROVIDER=ollama AI_MODEL=gemma4:e2b AI_API_KEY=local-dummy \
OLLAMA_BASE_URL=http://127.0.0.1:11434/v1 \
DATA_DIR=/tmp/household-proof-data COPILOT_RUNTIME_DIR=/tmp/household-proof-runtime \
HOME=/tmp/household-proof-home SESSION_IDLE_TIMEOUT_SECONDS=600 AGENT_TURN_TIMEOUT_SECONDS=240 \
timeout 900 backend/.venv/bin/python tests/proofs/phase0_live_proof.py host
```

driver は tag 引数（`sys.argv[1]`）のみ。tool 方針は `HOUSEHOLD_TOOL_POLICY`（既定 `registered`）で決める。
成功条件是 Custom Tool 実行 + built-in shell 不実行（`RESULT [<tag>] ok=True`、exit 0）。

## host の pinned API probe

```bash
.venv/bin/python tests/proofs/sdk_api_probe.py   # pinned 版の実在名前を JSON で出力
```

## 凭证（GitHub token）の扱い

- BYOK Chat と Custom Tool 実行に GitHub 凭证は**不要**（token 無しで host/Docker とも green。
  `list_models` だけで "Not authenticated" が出、BYOK では BYOK models を返す）。実測は `docs/phase0-verification-report.md`。
- 凭证が要る GitHub connector / Connect 系は Phase 2（Issue #4）で扱う。
- 1.0.29 が読む env 名は `COPILOT_GITHUB_TOKEN` / `GH_TOKEN` / `GITHUB_TOKEN`（client の `github_token` 引数でも可）。
  **`COPILOT_GITHUB_TOKEN_FILE` は存在しない**。token を渡すなら host env か project dir の `.env`（compose が読む）で注入する。
- base_directory（`COPILOT_RUNTIME_DIR`）に CLI の auth state は保存されない（token 無しで gate green。session resume は session id での resume）。

## CLI runtime のダウンロードと保存場所

`python -m copilot download-runtime`（1.0.29 のサブコマンドは `download-runtime`、`version`、`help` のみ。`cli-path` は無い）。
保存先は `XDG_CACHE_HOME`（無ければ `XDG_DATA_HOME`、`~/.local/share`）尊重:
`<cache>/github-copilot-sdk/cli/<sdk-ver>/<cli-ver>/prebuilds/<platform>/copilot`。
image 内では `XDG_CACHE_HOME=/tmp/xdg-cache` により `/tmp/xdg-cache/github-copilot-sdk/cli/1.0.107/...`。
`~/.copilot-cli` は存在しない。

## Docker host commands

`make` Target を使わない最小コマンド（compose file は repo 直下、context は repo root）:

```bash
docker compose build
docker compose --profile checks run --rm gate
docker compose --profile checks run --rm proof
docker compose up -d runtime
docker compose down
docker compose build --pull=no            # lock 保持で build
docker run --rm --entrypoint sh <image> -c 'python -c "import copilot; print(copilot.__version__)"'
docker run --rm household-ai-runtime-test:0.1.0 sh -c 'env | cut -d= -f1'   # env 名前だけ表示
```

Compose file は env var を host env から渡す。Compose project dir の `.env` も docker compose が読む。
Named volume `household-data` は `/app/data`（image 内 paths）に mount する。Repo 内 `.venv` や `data/`
は image build 対象外（`.dockerignore`）。

## make targets

| Target | 内容 |
| :--- | :--- |
| `make build` | image build |
| `make gate` | import 確認 → ruff check → ruff format --check → pytest（pinned image 内） |
| `make lint` / `make test` / `make fmt` | gate の各工程を単独実行 |
| `make probe` | pinned image 内で `tests/proofs/sdk_api_probe.py` を実行 |
| `make proof` | live proof。host env の `AI_api_key` 等を container へ渡す |
| `make up` / `logs` / `down` | runtime 起動・確認・停止 |

Proof は `HOME=/home/app` と `/tmp/xdg-*` に state を書く（container 内のみ、host 側へは書かない）。
Cloudflare 経由の本番運用は `docs/deployment-cloudflare.md`（Phase 2 以降）。
