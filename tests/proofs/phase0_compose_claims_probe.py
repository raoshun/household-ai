#!/usr/bin/env python3
"""Phase 0 gate: compose 構成の claims を展開値から機械検証する。

README §13.1-§13.3 と docs/capability-matrix.md §8 が書く compose 設計の
主張 (profile 分離 / 1 pinned image / 秘密を bake しない / data volume /
host gateway / gate の ro mount / proof state の /tmp) を claims として検査し、
gate target と残存局限の記録は docs/evidence/offline-runtime-proof-notes.md §6。

gate 用 image に docker CLI が無いため、展開 JSON 生成は host の
`docker compose ... config --format json` が担い、claims 検査の code は
pinned image 内で実行する。expand は --env-file /dev/null を使い、host env
の影響を排除して compose 側の既定値だけを検証する。

検証 claims (code の検査順 = README §13 の claims 番号):
  1. 5 service (runtime / gate / proof / api / ui-bridge) で gate / proof /
     ui-bridge だけ profile `checks` を持ち、api は profile `ui`、default up
     service は runtime だけ。
  2. gate / proof / ui-bridge / runtime / api が同一 image reference を指す。
  3. 認証 env は host 補間で渡り、compose file にも展開後も literal secret
     を書かない (展開値は empty か `local-dummy` に限制)。
  4. runtime / api の data mount は named volume `household-data` を /app/data
     へ mount する (host dir bind mount を混ぜない)。
  5. runtime / proof / api / ui-bridge に host.docker.internal:host-gateway を
     設定し、gate は host 出向きの extra_hosts を持たない。
  6. gate の source mount は working tree の read-only mount である。
  7. proof の session store / runtime cache は /tmp 退避で named volume 無し。
  8. .dockerignore の .env / .env.* / data / .venv 除外と Dockerfile の COPY
     一覧から秘密 / 実行時 data の焼付けがない。
  9. ui-bridge (UI ブリッジ live proof) の session store / runtime cache も
     /tmp 退避で、mount は working tree ro 2 か所と証跡用 `/out` だけ。
 10. api (UI 向け API サーバー) の published port は loopback (127.0.0.1) 限定で、
     他の service は port を公開しない (UI frontend の Node image は compose に
     入れない: README §6.4 / §13.4)。

`--tamper <mode>` は展開値 / Dockerfile を壊した copy へ同じ検査を実行し、
claims 検査が defect を拒否することを実証する (negative gate)。実配置不変。
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Any

HOST_PATH_MASK = "<repo>"
COMPOSE_BASENAME = "docker-compose.yml"
DOCKERFILE_BASENAME = "backend/Dockerfile"
DOCKERIGNORE_BASENAME = ".dockerignore"

GATE_SERVICE_NAMES: tuple[str, ...] = ("gate", "proof")
RUNTIME_SERVICE_NAME = "runtime"
UI_SERVICE_NAME = "api"
UI_BRIDGE_SERVICE_NAME = "ui-bridge"
UI_PROFILE = "ui"
CHECKED_GATE_SERVICE_NAMES: tuple[str, ...] = ("gate", "proof", "ui-bridge")
HOST_GATEWAY_SERVICE_NAMES: tuple[str, ...] = ("runtime", "proof", "api", "ui-bridge")
COMPOSE_SERVICE_NAMES: frozenset[str] = frozenset(
    {"gate", "proof", "runtime", UI_SERVICE_NAME, UI_BRIDGE_SERVICE_NAME}
)
UI_PUBLISHED_PORT = 8000
UI_LOOPBACK_BINDS: frozenset[str] = frozenset({"127.0.0.1", "localhost", "::1"})
UI_BRIDGE_EVIDENCE_TARGET = "/out"
UI_BRIDGE_EVIDENCE_SOURCE_SEGMENT = ".local/ui/out"
UI_BRIDGE_MOUNT_TARGETS: frozenset[str] = frozenset(
    {"/app/backend/app", "/app/tests", UI_BRIDGE_EVIDENCE_TARGET}
)
CHECKS_PROFILE = "checks"
DEFAULT_IMAGE = "household-ai-runtime-test:0.1.0"
DATA_MOUNT_TARGET = "/app/data"
NAMED_DATA_VOLUME = "household-data"
HOST_GATEWAY = "host.docker.internal=host-gateway"
PROOF_STATE_ENV_KEYS: tuple[str, ...] = ("DATA_DIR", "COPILOT_RUNTIME_DIR")
PROOF_TMP_PREFIX = "/tmp/"
GATE_MOUNT_TARGETS: tuple[str, ...] = ("/app/backend/app", "/app/tests")
CREDENTIAL_ENV_KEYS: tuple[str, ...] = (
    "AI_API_KEY",
    "COPILOT_GITHUB_TOKEN",
    "GH_TOKEN",
    "GITHUB_TOKEN",
)
NON_SECRET_PLACEHOLDER_VALUES: frozenset[str] = frozenset({"", "local-dummy"})
IMAGE_EXCLUDED_CONTEXT_NAMES: tuple[str, ...] = (".env", ".env.*", "data/", ".venv/")

# compose text の env 行で credential らしい name (API_KEY 等) に literal 値が直接
# 書かれている場合を検出する。補間参照 (${VAR} / ${VAR:-dummy}) は除外し、quoted 値も
# 捕捉する。name が credential 由来でも value が数値 / boolean / URL / path / 補間参照
# なら credential literal ではない (例: CONTEXT_TOKEN_WARNING_RATIO: "0.8")。
LITERAL_CREDENTIAL_RE = re.compile(
    r"^[ \t]*[A-Z0-9_]*(?:API_KEY|TOKEN|SECRET|PASSWORD)[A-Z0-9_]*"
    r"[ \t]*:[ \t]*(?!\$\{)(?P<value>[^\s}#]\S*)",
    re.MULTILINE,
)
NON_CREDENTIAL_LITERAL_RE = re.compile(
    r"(?:[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?"
    r"|true|false|null|none"
    r"|[a-z][a-z0-9+.-]*://\S*"
    r"|/\S*"
    r"|.*?\$\{.*)",
    re.IGNORECASE,
)
TAMPER_MODES: tuple[str, ...] = (
    "literal-credential",
    "bind-data-dir",
    "unprofiled-gate",
    "baked-secret-in-image",
    "ui-bridge-state-mounted",
    "ui-port-wildcard",
)


class ComposeClaimsError(AssertionError):
    """compose claims と measured が食い違ったことを示す。"""


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ComposeClaimsError(f"必需 file が無い: {path}") from exc


def _services(config: dict[str, Any]) -> dict[str, Any]:
    services = config.get("services")
    if not isinstance(services, dict) or not services:
        raise ComposeClaimsError("compose config に services が無い")
    return services


def _env(service: dict[str, Any]) -> dict[str, str]:
    raw = service.get("environment")
    if isinstance(raw, dict):
        return {
            str(key): ("" if value is None else str(value))
            for key, value in raw.items()
        }
    if isinstance(raw, list):
        env: dict[str, str] = {}
        for item in raw:
            if isinstance(item, str) and "=" in item:
                key, _, value = item.partition("=")
                env[key] = value
        return env
    return {}


def _volumes(service: dict[str, Any]) -> list[dict[str, Any]]:
    raw = service.get("volumes")
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _volume_mode(item: dict[str, Any]) -> str:
    """compose v5 の config JSON は read_only flag で ro を表す (mode key も許可)。"""

    if item.get("read_only") is True or str(item.get("mode", "")).lower() in {
        "ro",
        "readonly",
        "read_only",
        "z",
        "Z",
    }:
        return "ro"
    return "rw"


def _volume_strings(service: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for item in _volumes(service):
        out.append(
            f"{item.get('source', '')}:{item.get('target', '')}:{_volume_mode(item)}"
        )
    return sorted(out)


def _named_volumes(config: dict[str, Any]) -> list[str]:
    raw = config.get("volumes")
    if isinstance(raw, dict):
        return sorted(str(name) for name in raw)
    if isinstance(raw, list):
        return sorted(
            str(item.get("name", "")) for item in raw if isinstance(item, dict)
        )
    return []


def _dockerignore_names(text: str) -> list[str]:
    return sorted(
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    )


def _dockerfile_copy_sources(text: str) -> list[str]:
    sources: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("COPY "):
            continue
        parts = stripped.split()
        sources.extend(
            part
            for part in parts[1:]
            if not part.startswith("--") and part not in {"--chown=app:app"}
        )
    return sorted(sources)


def _published_port_strings(svc: dict[str, Any]) -> list[str]:
    """ports を `host_ip:published->target/proto` 形へ正規化する。

    string / dict 両対応。
    """

    entries: list[str] = []
    for item in svc.get("ports") or []:
        if isinstance(item, dict):
            host_ip = str(item.get("host_ip") or "0.0.0.0")
            published = str(item.get("published") or "")
            target = str(item.get("target") or "")
            protocol = str(item.get("protocol") or "tcp")
            mode = str(item.get("mode") or "")
            entries.append(
                f"{host_ip}:{published or '->'}->{target}/{protocol}"
                + (f" mode={mode}" if mode else "")
            )
        else:
            entries.append(str(item))
    return sorted(entries)


def _is_credential_literal(match: re.Match[str]) -> bool:
    """LITERAL_CREDENTIAL_RE の match が実際に credential literal 値か。"""

    value = (match.group("value") or "").strip("\"'")
    if not value:
        return False
    return NON_CREDENTIAL_LITERAL_RE.fullmatch(value) is None


def _credential_compose_literals(compose_text: str) -> list[str]:
    """compose text から credential literal として扱われる行を列挙する。"""

    return sorted(
        line.strip()
        for line in (
            match.group(0)
            for match in LITERAL_CREDENTIAL_RE.finditer(compose_text)
            if _is_credential_literal(match)
        )
    )


def _baked_source_is_secret(source: str) -> bool:
    """COPY 元 (または DEST 混在 list) から秘密 / data 焼付けを検出するか。"""

    cleaned = source.removeprefix("./")
    for part in PurePosixPath(cleaned).parts:
        if part == "data" or part == ".env" or part.startswith(".env."):
            return True
    return False


def _credential_value_class(value: str) -> str:
    return "placeholder" if value in NON_SECRET_PLACEHOLDER_VALUES else "literal_secret"


def collect_measured(
    config: dict[str, Any],
    compose_text: str,
    dockerfile_text: str,
    dockerignore_text: str,
) -> dict[str, Any]:
    services = _services(config)
    named_volumes = _named_volumes(config)
    return {
        "services": sorted(services),
        "default_up_services": sorted(
            name for name, svc in services.items() if not svc.get("profiles")
        ),
        "profiled_services": {
            name: sorted(str(profile) for profile in svc.get("profiles") or [])
            for name, svc in sorted(services.items())
        },
        "service_images": {
            name: svc.get("image") for name, svc in sorted(services.items())
        },
        "service_volumes": {
            name: _volume_strings(svc) for name, svc in sorted(services.items())
        },
        "named_volumes": named_volumes,
        "named_volume_mounts": {
            name: sorted(
                f"{item.get('source')}:{item.get('target')}"
                for item in _volumes(svc)
                if str(item.get("source", "")) in named_volumes
            )
            for name, svc in sorted(services.items())
        },
        "extra_hosts": {
            name: sorted(
                str(item).replace(":", "=", 1)
                if isinstance(item, str) and ":" in item
                else str(item)
                for item in svc.get("extra_hosts") or []
            )
            for name, svc in sorted(services.items())
        },
        "credential_env": {
            name: {
                key: {
                    "present": key in _env(svc),
                    "value_class": _credential_value_class(_env(svc).get(key, "")),
                }
                for key in CREDENTIAL_ENV_KEYS
            }
            for name, svc in sorted(services.items())
        },
        "proof_state_dirs": {
            key: value
            for key, value in sorted(_env(services.get("proof", {})).items())
            if key in PROOF_STATE_ENV_KEYS
        },
        "runtime_dir_env": {
            name: value
            for name, svc in sorted(services.items())
            for key, value in sorted(_env(svc).items())
            if "RUNTIME_DIR" in key
        },
        "ui_bridge_mounts": {
            str(item.get("target", "")): {
                "source": str(item.get("source", "")),
                "mode": _volume_mode(item),
                "named": str(item.get("source", "")) in named_volumes,
            }
            for item in _volumes(services.get(UI_BRIDGE_SERVICE_NAME, {}))
        },
        "published_ports": {
            name: _published_port_strings(svc) for name, svc in sorted(services.items())
        },
        "ui_bridge_state_dirs": {
            key: value
            for key, value in sorted(
                _env(services.get(UI_BRIDGE_SERVICE_NAME, {})).items()
            )
            if key in PROOF_STATE_ENV_KEYS
        },
        "ui_bridge_evidence_dir": _env(services.get(UI_BRIDGE_SERVICE_NAME, {})).get(
            "UI_BRIDGE_EVIDENCE_DIR", ""
        ),
        "ui_bridge_command": " ".join(
            str(part)
            for part in (services.get(UI_BRIDGE_SERVICE_NAME, {}).get("command") or [])
        ),
        "gate_python_path": _env(services.get("gate", {})).get("PYTHONPATH", ""),
        "compose_credential_literals": _credential_compose_literals(compose_text),
        "dockerfile_copy_sources": _dockerfile_copy_sources(dockerfile_text),
        "dockerignore_excluded": _dockerignore_names(dockerignore_text),
    }


def verify_claims(measured: dict[str, Any]) -> list[tuple[str, str]]:
    """claims を検証し claim id と summary を返す (不合格なら ComposeClaimsError)。"""

    results: list[tuple[str, str]] = []

    def check(claim: str, ok: bool, detail: str) -> None:
        if not ok:
            raise ComposeClaimsError(f"{claim} 不合格: {detail}")
        results.append((claim, detail))

    check(
        "phase0_compose_service_set",
        set(measured["services"]) == COMPOSE_SERVICE_NAMES
        and measured["default_up_services"] == [RUNTIME_SERVICE_NAME]
        and all(
            measured["profiled_services"].get(name) == [CHECKS_PROFILE]
            for name in CHECKED_GATE_SERVICE_NAMES
        )
        and measured["profiled_services"].get(UI_SERVICE_NAME) == [UI_PROFILE],
        "services="
        + json.dumps(measured["services"], ensure_ascii=False)
        + " default_up="
        + json.dumps(measured["default_up_services"], ensure_ascii=False)
        + " profiles="
        + json.dumps(measured["profiled_services"], ensure_ascii=False),
    )
    check(
        "single_pinned_image",
        all(image == DEFAULT_IMAGE for image in measured["service_images"].values()),
        json.dumps(measured["service_images"], ensure_ascii=False),
    )
    check(
        "credentials_not_baked",
        not measured["compose_credential_literals"]
        and all(
            (not entry["present"]) or entry["value_class"] == "placeholder"
            for service_env in measured["credential_env"].values()
            for entry in service_env.values()
        ),
        "compose literals="
        + json.dumps(measured["compose_credential_literals"], ensure_ascii=False)
        + " expanded="
        + json.dumps(measured["credential_env"], ensure_ascii=False),
    )
    runtime_volumes = measured["service_volumes"].get(RUNTIME_SERVICE_NAME, [])
    ui_volumes = measured["service_volumes"].get(UI_SERVICE_NAME, [])
    check(
        "data_volume_mount",
        NAMED_DATA_VOLUME in measured["named_volumes"]
        and any(
            f"{NAMED_DATA_VOLUME}:{DATA_MOUNT_TARGET}:" in entry
            for entry in runtime_volumes
        )
        # datavolume への mount は named volume だけ (host dir bind mount を混ぜない)
        and not any(
            entry.startswith("./") or entry.startswith("/") for entry in runtime_volumes
        )
        and not any(
            entry.startswith("./") or entry.startswith("/") for entry in ui_volumes
        ),
        "named_volumes="
        + json.dumps(measured["named_volumes"], ensure_ascii=False)
        + " runtime="
        + json.dumps(runtime_volumes, ensure_ascii=False)
        + f" {UI_SERVICE_NAME}="
        + json.dumps(ui_volumes, ensure_ascii=False),
    )
    check(
        "host_ollama_gateway",
        all(
            HOST_GATEWAY in measured["extra_hosts"].get(name, [])
            for name in HOST_GATEWAY_SERVICE_NAMES
        )
        # gate は host へ outbound な extra_hosts を持たない
        and not measured["extra_hosts"].get("gate"),
        json.dumps(measured["extra_hosts"], ensure_ascii=False),
    )
    gate_volumes = measured["service_volumes"].get("gate", [])
    check(
        "gate_uses_working_tree_ro",
        all(
            any(entry.endswith(f":{target}:ro") for entry in gate_volumes)
            for target in GATE_MOUNT_TARGETS
        )
        and all(
            entry.split(":")[0] != target
            for entry in gate_volumes
            for target in GATE_MOUNT_TARGETS
        ),
        json.dumps(gate_volumes, ensure_ascii=False),
    )
    proof_state = measured["proof_state_dirs"]
    check(
        "proof_state_in_container_tmp",
        all(
            key in proof_state and proof_state[key].startswith(PROOF_TMP_PREFIX)
            for key in PROOF_STATE_ENV_KEYS
        )
        and not measured["service_volumes"].get("proof"),
        "state="
        + json.dumps(proof_state, ensure_ascii=False)
        + " proof_volumes="
        + json.dumps(measured["service_volumes"].get("proof", []), ensure_ascii=False),
    )
    check(
        "secrets_and_data_excluded_from_image",
        all(
            name in measured["dockerignore_excluded"]
            for name in IMAGE_EXCLUDED_CONTEXT_NAMES
        )
        and not [
            src
            for src in measured["dockerfile_copy_sources"]
            if _baked_source_is_secret(src)
        ],
        "excluded="
        + json.dumps(measured["dockerignore_excluded"], ensure_ascii=False)
        + " copy_sources="
        + json.dumps(measured["dockerfile_copy_sources"], ensure_ascii=False),
    )
    ui_bridge_mounts = measured["ui_bridge_mounts"]
    ui_bridge_state = measured["ui_bridge_state_dirs"]
    check(
        "ui_bridge_state_and_mounts",
        all(
            key in ui_bridge_state and ui_bridge_state[key].startswith(PROOF_TMP_PREFIX)
            for key in PROOF_STATE_ENV_KEYS
        )
        # mount は working tree ro 2 か所 + 証跡 /out (host mount rw) だけ
        and sorted(ui_bridge_mounts) == sorted(UI_BRIDGE_MOUNT_TARGETS)
        and all(
            ui_bridge_mounts.get(target, {}).get("mode") == "ro"
            for target in GATE_MOUNT_TARGETS
        )
        and ui_bridge_mounts.get(UI_BRIDGE_EVIDENCE_TARGET, {}).get("mode") == "rw"
        and UI_BRIDGE_EVIDENCE_SOURCE_SEGMENT
        in ui_bridge_mounts.get(UI_BRIDGE_EVIDENCE_TARGET, {}).get("source", "")
        and not any(mount["named"] for mount in ui_bridge_mounts.values())
        and "tests/live" in measured["ui_bridge_command"],
        "state="
        + json.dumps(ui_bridge_state, ensure_ascii=False)
        + " mounts="
        + json.dumps(ui_bridge_mounts, ensure_ascii=False)
        + " command="
        + measured["ui_bridge_command"],
    )
    ui_ports = measured["published_ports"].get(UI_SERVICE_NAME, [])
    check(
        "ui_published_ports_loopback_only",
        bool(ui_ports)
        and all(
            any(entry.startswith(f"{bind}:") for bind in UI_LOOPBACK_BINDS)
            and f"->{UI_PUBLISHED_PORT}/" in entry
            for entry in ui_ports
        )
        # api 以外の service は host 側へ published port を公開しない
        and not any(
            measured["published_ports"].get(name)
            for name in (
                RUNTIME_SERVICE_NAME,
                "gate",
                "proof",
                UI_BRIDGE_SERVICE_NAME,
            )
        ),
        f"{UI_SERVICE_NAME} ports="
        + json.dumps(ui_ports, ensure_ascii=False)
        + " all_ports="
        + json.dumps(measured["published_ports"], ensure_ascii=False),
    )
    return results


def load_compose_config(repo_root: Path, config_json: Path | None) -> dict[str, Any]:
    if config_json is not None:
        return json.loads(_read(config_json))
    proc = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            COMPOSE_BASENAME,
            "--profile",
            CHECKS_PROFILE,
            "--profile",
            UI_PROFILE,
            "--env-file",
            "/dev/null",
            "config",
            "--format",
            "json",
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise ComposeClaimsError(
            f"docker compose config 失敗 rc={proc.returncode}: "
            f"{proc.stderr.strip()[:400]}"
        )
    return json.loads(proc.stdout)


def tamper_config(config: dict[str, Any], mode: str) -> tuple[dict[str, Any], str]:
    """展開済み compose config を壊した copy を返す (実配置は変更しない)。"""

    tampered = copy.deepcopy(config)
    services = _services(tampered)
    if mode == "literal-credential":
        env = services["runtime"].setdefault("environment", {})
        if isinstance(env, list):
            env.append("AI_API_KEY=sk-FAKELITERALVALUE123")
        else:
            env["AI_API_KEY"] = "sk-FAKELITERALVALUE123"
        marker = "runtime AI_API_KEY=sk-FAKELITERALVALUE123"
    elif mode == "bind-data-dir":
        for volume in services["runtime"].setdefault("volumes", []):
            if (
                isinstance(volume, dict)
                and str(volume.get("source")) == NAMED_DATA_VOLUME
            ):
                volume["source"] = "./data"
                volume["type"] = "bind"
        marker = f"runtime mount ./data:{DATA_MOUNT_TARGET}"
    elif mode == "unprofiled-gate":
        services["gate"]["profiles"] = []
        marker = "gate profiles=[]"
    elif mode == "baked-secret-in-image":
        marker = "Dockerfile COPY data/ (dockerfile_text tamper)"
    elif mode == "ui-bridge-state-mounted":
        services[UI_BRIDGE_SERVICE_NAME].setdefault("volumes", []).append(
            {
                "type": "bind",
                "source": "./data/ui-bridge-state",
                "target": DATA_MOUNT_TARGET,
                "bind": {},
            }
        )
        marker = f"ui-bridge mount ./data/ui-bridge-state:{DATA_MOUNT_TARGET}"
    elif mode == "ui-port-wildcard":
        for port in services[UI_SERVICE_NAME].get("ports") or []:
            if isinstance(port, dict):
                port.pop("host_ip", None)
        marker = f"{UI_SERVICE_NAME} published port から host_ip 削除 (0.0.0.0 扱い)"
    else:
        raise ComposeClaimsError(f"未知 tamper mode: {mode}")
    return tampered, marker


def tamper_dockerfile(dockerfile_text: str) -> str:
    """Dockerfile へ秘密 / 実行時 data を焼付けるときの inspection 用 patch。"""

    return dockerfile_text + "\nCOPY .env /app/.env\nCOPY data /app/baked-data\n"


def compose_text_with_literal_credential(compose_text: str) -> str:
    tampered = compose_text.replace(
        "${AI_API_KEY:-local-dummy}", "sk-FAKELITERALVALUE123"
    )
    if tampered == compose_text:
        raise ComposeClaimsError(
            "compose text 内へ placeholder を発見できず tamper 適用できなかった"
        )
    return tampered


_TAMPER_CLAIMS: dict[str, str] = {
    "literal-credential": "credentials_not_baked",
    "bind-data-dir": "data_volume_mount",
    "unprofiled-gate": "phase0_compose_service_set",
    "baked-secret-in-image": "secrets_and_data_excluded_from_image",
    "ui-bridge-state-mounted": "ui_bridge_state_and_mounts",
    "ui-port-wildcard": "ui_published_ports_loopback_only",
}


def run(args: argparse.Namespace) -> int:
    repo_root = args.repo_root.resolve()
    compose_text = _read(repo_root / COMPOSE_BASENAME)
    dockerfile_text = _read(repo_root / DOCKERFILE_BASENAME)
    dockerignore_text = _read(repo_root / DOCKERIGNORE_BASENAME)
    config = load_compose_config(repo_root, args.config_json)

    if args.tamper:
        mode = args.tamper
        tampered_config, marker = tamper_config(config, mode)
        tampered_compose_text = (
            compose_text_with_literal_credential(compose_text)
            if mode == "literal-credential"
            else compose_text
        )
        tampered_dockerfile = (
            tamper_dockerfile(dockerfile_text)
            if mode == "baked-secret-in-image"
            else dockerfile_text
        )
        claims = _TAMPER_CLAIMS[mode]
        rejection: str | None = None
        try:
            verify_claims(
                collect_measured(
                    tampered_config,
                    tampered_compose_text,
                    tampered_dockerfile,
                    dockerignore_text,
                )
            )
        except ComposeClaimsError as exc:
            rejection = f"{type(exc).__name__}: {exc}"
        evidence = {
            "gate": "compose-claims-tamper",
            "tamper_mode": mode,
            "tamper_marker": marker,
            "target_claim": claims,
            "reject_reason": rejection,
            "guard_detected": rejection is not None,
        }
        write_evidence(args.output, evidence, args.host_repo_root)
        print(
            f"[compose-claims] tamper={mode} "
            f"guard_detected={evidence['guard_detected']} claim={claims}"
        )
        if rejection:
            print(f"  reject_reason: {rejection}")
        return 0 if evidence["guard_detected"] else 1

    measured = collect_measured(
        config, compose_text, dockerfile_text, dockerignore_text
    )
    claims = verify_claims(measured)
    evidence = {
        "gate": "compose-claims",
        "config_source": "docker compose config --format json",
        "claims": [{"claim": claim, "measured": detail} for claim, detail in claims],
        "measured": measured,
    }
    write_evidence(args.output, evidence, args.host_repo_root)
    print(f"[compose-claims] OK: {len(claims)} claims (config=docker compose config)")
    for claim, detail in claims:
        print(f"  - {claim}: {detail}")
    return 0


def redact_host_paths(value: Any, repo_root: Path) -> Any:
    """Track する evidence へ host absolute path を残さない (repo root を mask する)."""
    if isinstance(value, str):
        return value.replace(str(repo_root), HOST_PATH_MASK)
    if isinstance(value, (list, tuple)):
        return [redact_host_paths(item, repo_root) for item in value]
    if isinstance(value, dict):
        return {key: redact_host_paths(item, repo_root) for key, item in value.items()}
    return value


def write_evidence(
    output: Path | None,
    evidence: dict[str, Any],
    host_repo_root: Path | None = None,
) -> None:
    payload_dict: dict[str, Any] = evidence
    if host_repo_root is not None:
        payload_dict = redact_host_paths(evidence, host_repo_root)
        payload_dict["host_path_redaction"] = (
            f"host repo root 前方は {HOST_PATH_MASK} へ mask 済み (--host-repo-root)"
        )
    payload = json.dumps(payload_dict, ensure_ascii=False, indent=2)
    if host_repo_root is not None and str(host_repo_root) in payload:
        raise ComposeClaimsError(
            "host repo root が mask 後の evidence に残っている "
            "(redact_host_paths が扱わない container 型がないか確認)"
        )
    if output is None:
        print(payload)
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(payload + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="compose 構成 claims の実測検証 (README §13 / docs §5-§8)"
    )
    parser.add_argument(
        "--repo-root", type=Path, default=Path(__file__).resolve().parents[2]
    )
    parser.add_argument(
        "--config-json",
        type=Path,
        default=None,
        help="docker compose config --format json 出力",
    )
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument(
        "--host-repo-root",
        type=Path,
        default=None,
        help="track する evidence 内 host path を mask する host 側 repo root",
    )
    parser.add_argument("--tamper", choices=TAMPER_MODES, default=None)
    args = parser.parse_args(argv)
    try:
        return run(args)
    except (ComposeClaimsError, OSError, json.JSONDecodeError) as exc:
        print(
            f"[compose-claims] REJECTED: {type(exc).__name__}: {exc}", file=sys.stderr
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
