# Phase 0 検証用イメージ (§15).
# ビルド文脈はリポジトリルート (docker build -f backend/Dockerfile .)。
# API サーバー (app/main.py) は Phase 1 のため、イメージの既定コマンドは import 確認のみ。
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS runtime

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1 \
    HOME=/home/app

# pyproject と uv.lock を先に入れて依存レイヤーをキャッシュ化する (§18.12 再現性)。
WORKDIR /app/backend
COPY backend/pyproject.toml backend/uv.lock ./
COPY backend/README.md ./README.md
RUN adduser --disabled-password --gecos "" --uid 10001 app \
    && uv sync --project /app/backend --extra dev --no-install-project --frozen \
    && mkdir -p /app/data/conversations /app/data/rules /app/data/copilot-runtime \
    && chown -R app:app /app

COPY --chown=app:app backend/app ./app
COPY --chown=app:app tests /app/tests
COPY --chown=app:app skills /app/skills

USER app
WORKDIR /app/backend
ENV VIRTUAL_ENV=/app/backend/.venv \
    PATH=/app/backend/.venv/bin:/usr/local/bin:/usr/bin:/bin \
    SKILLS_DIR=/app/skills \
    DATA_DIR=/app/data \
    COPILOT_RUNTIME_DIR=/app/data/copilot-runtime \
    XDG_CONFIG_HOME=/tmp/xdg-config \
    XDG_STATE_HOME=/tmp/xdg-state \
    XDG_CACHE_HOME=/tmp/xdg-cache \
    XDG_DATA_HOME=/tmp/xdg-data \
    CLAUDE_CODE_EXTENSIONS_CONFIG_DIR=/tmp/xdg-config/claude-code \
    CLAUDE_CODE_EXTENSIONS_DATA_DIR=/tmp/xdg-data/claude-code \
    CLAUDE_CODE_EXTENSIONS_CACHE_DIR=/tmp/xdg-cache/claude-code \
    CLAUDE_CODE_EXTENSIONS_STATE_DIR=/tmp/xdg-state/claude-code

# pinned 版 manifest と SDK 実測版を照合してから、CLI runtime を build cache に配置する (§15)。
# base URL は mirror へ差し替え可能 (capability matrix §9)。既定値は SDK 既定と同一。
COPY --chown=app:app docs/pinned-runtime-versions.json /app/docs/pinned-runtime-versions.json
ARG COPILOT_CLI_DOWNLOAD_BASE_URL=https://github.com/github/copilot-cli/releases/download
ENV COPILOT_CLI_DOWNLOAD_BASE_URL=${COPILOT_CLI_DOWNLOAD_BASE_URL}
RUN python /app/tests/proofs/verify_pinned_manifest.py \
    && python -m copilot download-runtime \
    && python -c "from copilot import CopilotClient; print('copilot import ok after CLI setup')"

# 既定コマンドは import ゲート (§15)。API サーバー起動は Phase 1。
CMD ["python", "-c", "import app.agent; print('import app.agent ok')"]
