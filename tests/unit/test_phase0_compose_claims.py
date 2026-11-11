"""compose 構成 claims probe の hermetic unit test.

`make prove-compose-claims` は host の `docker compose config` 実展開で claims を
検証するが、gate container には docker CLI が無い。この test は claims 検査だけを、
実 expand 出力の shape を再現した fixture と、defect を検出できる tamper case について
検証する (§15「lint と unit test は Docker 環境内で実行」)。

fixture の shape は Docker 28.5.1 / compose v5 の `config --format json` 実測に対応:
- volumes は dict entry ({type, source, target, read_only}) で read-only は flag
- extra_hosts は "name=ip" の normalized string
- profiles 未指定 service は None / profiles 指定 service は ["checks"]
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "tests" / "proofs"))

from phase0_compose_claims_probe import (  # noqa: E402
    _TAMPER_CLAIMS,
    CHECKS_PROFILE,
    COMPOSE_SERVICE_NAMES,
    DEFAULT_IMAGE,
    GATE_MOUNT_TARGETS,
    HOST_GATEWAY,
    HOST_PATH_MASK,
    IMAGE_EXCLUDED_CONTEXT_NAMES,
    LITERAL_CREDENTIAL_RE,
    NAMED_DATA_VOLUME,
    PROOF_STATE_ENV_KEYS,
    TAMPER_MODES,
    ComposeClaimsError,
    collect_measured,
    compose_text_with_literal_credential,
    redact_host_paths,
    tamper_config,
    tamper_dockerfile,
    verify_claims,
    write_evidence,
)

EXPECTED_PROOF_STATE_ENV: dict[str, str] = {
    "DATA_DIR": "/tmp/proof-data",
    "COPILOT_RUNTIME_DIR": "/tmp/proof-runtime",
}

EXPECTED_CLAIM_IDS: tuple[str, ...] = (
    "phase0_compose_service_set",
    "single_pinned_image",
    "credentials_not_baked",
    "data_volume_mount",
    "host_ollama_gateway",
    "gate_uses_working_tree_ro",
    "proof_state_in_container_tmp",
    "secrets_and_data_excluded_from_image",
)

COMPOSE_TEXT = """
services:
  runtime:
    image: household-ai-runtime-test:0.1.0
    volumes:
      - household-data:/app/data
    environment:
      AI_PROVIDER: ollama
      AI_MODEL: ${AI_MODEL:-gemma4:e2b}
      AI_API_KEY: ${AI_API_KEY:-local-dummy}
      OLLAMA_BASE_URL: ${OLLAMA_BASE_URL:-http://host.docker.internal:11434/v1}
      COPILOT_GITHUB_TOKEN: ${COPILOT_GITHUB_TOKEN:-}
    extra_hosts:
      - host.docker.internal:host-gateway
  gate:
    profiles: ["checks"]
    image: household-ai-runtime-test:0.1.0
    volumes:
      - ./backend/app:/app/backend/app:ro
      - ./tests:/app/tests:ro
  proof:
    profiles: ["checks"]
    image: household-ai-runtime-test:0.1.0
    environment:
      DATA_DIR: ${PROOF_DATA_DIR:-/tmp/proof-data}
      COPILOT_RUNTIME_DIR: ${PROOF_RUNTIME_DIR:-/tmp/proof-runtime}
      AI_API_KEY: ${AI_API_KEY:-local-dummy}
    extra_hosts:
      - host.docker.internal:host-gateway
volumes:
  household-data:
"""

DOCKERFILE_TEXT = """
FROM python:3.12-slim-bookworm
WORKDIR /app/backend
COPY backend/pyproject.toml backend/uv.lock ./
RUN mkdir -p /app/data/conversations /app/data/rules
COPY --chown=app:app backend/app ./app
COPY --chown=app:app tests /app/tests
COPY docs/pinned-runtime-versions.json /app/docs/pinned-runtime-versions.json
USER app
"""

DOCKERIGNORE_TEXT = "\n".join(IMAGE_EXCLUDED_CONTEXT_NAMES) + "\n.git\n"

EXPANDED_CONFIG: dict[str, Any] = {
    "name": "household-ai",
    "services": {
        "runtime": {
            "image": DEFAULT_IMAGE,
            "profiles": None,
            "working_dir": "/app/backend",
            "volumes": [
                {
                    "type": "volume",
                    "source": NAMED_DATA_VOLUME,
                    "target": "/app/data",
                    "volume": {},
                },
            ],
            "environment": {
                "AI_PROVIDER": "ollama",
                "AI_MODEL": "gemma4:e2b",
                "AI_API_KEY": "local-dummy",
                "OLLAMA_BASE_URL": "http://host.docker.internal:11434/v1",
                "COPILOT_GITHUB_TOKEN": "",
            },
            "extra_hosts": [HOST_GATEWAY],
        },
        "gate": {
            "image": DEFAULT_IMAGE,
            "profiles": [CHECKS_PROFILE],
            "working_dir": "/app/backend",
            "volumes": [
                {
                    "type": "bind",
                    "source": "/host/worktree/backend/app",
                    "target": GATE_MOUNT_TARGETS[0],
                    "read_only": True,
                    "bind": {},
                },
                {
                    "type": "bind",
                    "source": "/host/worktree/tests",
                    "target": GATE_MOUNT_TARGETS[1],
                    "read_only": True,
                    "bind": {},
                },
            ],
        },
        "proof": {
            "image": DEFAULT_IMAGE,
            "profiles": [CHECKS_PROFILE],
            "working_dir": "/app/backend",
            "environment": {
                "DATA_DIR": "/tmp/proof-data",
                "COPILOT_RUNTIME_DIR": "/tmp/proof-runtime",
                "AI_API_KEY": "local-dummy",
            },
            "extra_hosts": [HOST_GATEWAY],
        },
    },
    "volumes": {NAMED_DATA_VOLUME: {"name": f"household-ai_{NAMED_DATA_VOLUME}"}},
}


def _measured(
    config: dict[str, Any] | None = None,
    compose_text: str = COMPOSE_TEXT,
    dockerfile_text: str = DOCKERFILE_TEXT,
    dockerignore_text: str = DOCKERIGNORE_TEXT,
) -> dict[str, Any]:
    return collect_measured(
        copy.deepcopy(EXPANDED_CONFIG if config is None else config),
        compose_text,
        dockerfile_text,
        dockerignore_text,
    )


def test_claims_are_green_for_expand_shape_fixture() -> None:
    results = verify_claims(_measured())
    assert [claim for claim, _ in results] == list(EXPECTED_CLAIM_IDS)
    assert set(EXPANDED_CONFIG["services"]) == set(COMPOSE_SERVICE_NAMES)


def test_measured_records_compose_facts_used_by_claims() -> None:
    measured = _measured()
    assert measured["default_up_services"] == ["runtime"]
    assert measured["profiled_services"]["gate"] == [CHECKS_PROFILE]
    assert measured["named_volumes"] == [NAMED_DATA_VOLUME]
    assert measured["named_volume_mounts"] == [f"{NAMED_DATA_VOLUME}:/app/data"]
    assert all(
        HOST_GATEWAY in measured["extra_hosts"][name] for name in ("runtime", "proof")
    )
    assert measured["extra_hosts"]["gate"] == []
    assert measured["proof_state_dirs"] == EXPECTED_PROOF_STATE_ENV
    assert set(measured["proof_state_dirs"]) == set(PROOF_STATE_ENV_KEYS)
    assert all(v.startswith("/tmp/") for v in measured["proof_state_dirs"].values())
    assert measured["compose_credential_literals"] == []
    assert all(
        name in measured["dockerignore_excluded"]
        for name in IMAGE_EXCLUDED_CONTEXT_NAMES
    )


@pytest.mark.parametrize(("mode", "expected_claim"), sorted(_TAMPER_CLAIMS.items()))
def test_claims_reject_compose_defects(mode: str, expected_claim: str) -> None:
    assert mode in TAMPER_MODES
    config, _marker = tamper_config(copy.deepcopy(EXPANDED_CONFIG), mode)
    compose_text = COMPOSE_TEXT
    dockerfile_text = DOCKERFILE_TEXT
    if mode == "literal-credential":
        compose_text = compose_text_with_literal_credential(COMPOSE_TEXT)
    if mode == "baked-secret-in-image":
        dockerfile_text = tamper_dockerfile(DOCKERFILE_TEXT)

    with pytest.raises(ComposeClaimsError) as excinfo:
        verify_claims(_measured(config, compose_text, dockerfile_text))
    assert expected_claim in str(excinfo.value)


def test_gate_read_only_mount_defect_is_rejected() -> None:
    config = copy.deepcopy(EXPANDED_CONFIG)
    config["services"]["gate"]["volumes"][1]["read_only"] = False
    with pytest.raises(ComposeClaimsError, match="gate_uses_working_tree_ro"):
        verify_claims(_measured(config))


def test_host_gateway_claim_rejects_missing_mapping() -> None:
    config = copy.deepcopy(EXPANDED_CONFIG)
    config["services"]["runtime"]["extra_hosts"] = []
    with pytest.raises(ComposeClaimsError, match="host_ollama_gateway"):
        verify_claims(_measured(config))


def test_dockerignore_missing_exclusion_is_rejected() -> None:
    dockerignore = "\n".join(
        line for line in DOCKERIGNORE_TEXT.splitlines() if line.strip() != ".env.*"
    )
    with pytest.raises(
        ComposeClaimsError, match="secrets_and_data_excluded_from_image"
    ):
        verify_claims(_measured(dockerignore_text=dockerignore))


def test_tamper_helpers_do_not_mutate_inputs() -> None:
    config = copy.deepcopy(EXPANDED_CONFIG)
    _tampered, _marker = tamper_config(config, "bind-data-dir")
    assert config == EXPANDED_CONFIG
    tampered_compose = compose_text_with_literal_credential(COMPOSE_TEXT)
    assert tampered_compose != COMPOSE_TEXT
    assert any(
        LITERAL_CREDENTIAL_RE.search(line) for line in tampered_compose.splitlines()
    )

    tampered_dockerfile = tamper_dockerfile(DOCKERFILE_TEXT)
    assert tampered_dockerfile != DOCKERFILE_TEXT
    assert DOCKERFILE_TEXT in tampered_dockerfile


COMPOSE_EVIDENCE_NAMES: tuple[str, ...] = (
    "compose-claims.json",
    *(f"compose-claims-tamper-{mode}.json" for mode in TAMPER_MODES),
)

FAKE_HOST_REPO_ROOT = "/host/repo-root"


def _evidence_with_host_paths() -> dict[str, Any]:
    """track 対象 evidence の shape で host absolute path を混入させた sample."""
    root = FAKE_HOST_REPO_ROOT
    return {
        "gate": "compose-claims",
        "claims": [
            {
                "claim": "gate_uses_working_tree_ro",
                "measured": {
                    "config_mounts": [f"{root}/.local:/gate:ro"],
                    "mounts": [f"{root}/backend/app:/app/backend/app:ro"],
                },
            },
            {
                "claim": "single_pinned_image",
                "measured": {"images": ["/home/other/build:/app/build"]},
            },
        ],
        "measured": {
            "gate_config_mounts": [f"{root}/.local/compose-claims"],
            "services": ["gate", "proof", "runtime"],
            "counts": {"claims": 8, "green": True},
        },
    }


def test_redact_host_paths_masks_nested_strings_only() -> None:
    original = _evidence_with_host_paths()
    host_root = FAKE_HOST_REPO_ROOT
    redacted = redact_host_paths(original, Path(host_root))

    claims = redacted["claims"]
    mounts = claims[0]["measured"]
    masked_config_mount = f"{HOST_PATH_MASK}/.local:/gate:ro"
    masked_ro_mount = f"{HOST_PATH_MASK}/backend/app:/app/backend/app:ro"
    assert mounts["config_mounts"] == [masked_config_mount]
    assert mounts["mounts"] == [masked_ro_mount]
    # container 内 path はホスト mask の対象ではない
    assert claims[1]["measured"]["images"] == ["/home/other/build:/app/build"]
    assert redacted["measured"]["services"] == ["gate", "proof", "runtime"]
    assert redacted["measured"]["counts"] == {"claims": 8, "green": True}
    # 入力は書き換えない (tamper 実測で inspection 中の copy だけが変わる)
    assert original == _evidence_with_host_paths()


def test_write_evidence_masks_host_root_before_writing(tmp_path: Path) -> None:
    output = tmp_path / "nested" / "compose-claims.json"
    evidence = _evidence_with_host_paths()

    write_evidence(output, evidence, host_repo_root=Path(FAKE_HOST_REPO_ROOT))

    written = json.loads(output.read_text(encoding="utf-8"))
    text = output.read_text(encoding="utf-8")
    assert FAKE_HOST_REPO_ROOT not in text
    masked_mounts = written["measured"]["gate_config_mounts"]
    assert masked_mounts == [f"{HOST_PATH_MASK}/.local/compose-claims"]

    assert HOST_PATH_MASK in text
    assert "mask" in written["host_path_redaction"]
    assert evidence["measured"]["gate_config_mounts"] == [
        f"{FAKE_HOST_REPO_ROOT}/.local/compose-claims"
    ]


def test_write_evidence_without_host_root_keeps_payload(tmp_path: Path) -> None:
    output = tmp_path / "compose-claims.json"
    evidence = _evidence_with_host_paths()

    write_evidence(output, evidence)

    written = json.loads(output.read_text(encoding="utf-8"))
    assert written == evidence
    assert "host_path_redaction" not in written


def test_masked_evidence_written_for_every_gate_artifact(tmp_path: Path) -> None:
    """gate の evidence は host path を含まないことを書く側で保証する."""
    evidence_dir = tmp_path / "docs" / "evidence"
    evidence = _evidence_with_host_paths()
    for name in COMPOSE_EVIDENCE_NAMES:
        write_evidence(
            evidence_dir / name,
            evidence,
            host_repo_root=Path(FAKE_HOST_REPO_ROOT),
        )
    for name in COMPOSE_EVIDENCE_NAMES:
        text = (evidence_dir / name).read_text(encoding="utf-8")
        assert FAKE_HOST_REPO_ROOT not in text
        assert HOST_PATH_MASK in text


def test_redact_host_paths_masks_tuple_containers() -> None:
    """tuple は JSON 化で array になるため mask で list へ正規化する."""
    evidence = {
        "mounts": [(f"{FAKE_HOST_REPO_ROOT}/data:/app/data", "/home/other/x:/app/x")]
    }

    redacted = redact_host_paths(evidence, Path(FAKE_HOST_REPO_ROOT))

    masked_mount = f"{HOST_PATH_MASK}/data:/app/data"
    assert redacted["mounts"] == [[masked_mount, "/home/other/x:/app/x"]]


def test_write_evidence_rejects_payload_with_unmasked_host_root(
    tmp_path: Path,
) -> None:
    """mask が:host key など mask 対象外の箇所へ残ったまま書くことを拒否する."""
    evidence = {f"{FAKE_HOST_REPO_ROOT}/leaked-key": {"mounts": ["/app/backend/app"]}}
    output = tmp_path / "compose-claims.json"

    with pytest.raises(ComposeClaimsError):
        write_evidence(output, evidence, host_repo_root=Path(FAKE_HOST_REPO_ROOT))

    assert not output.exists()
