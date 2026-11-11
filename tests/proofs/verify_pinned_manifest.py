"""pinned runtime manifest と SDK 実測版の一致を検証する proof (§15).

Docker build 時に実行し、manifest に記録した pinned 版ずらしを build failure として
可視化する。SDK 側の API には CLI 版 pin が無いため (capability matrix §9)、この照合が
pinned pair を維持する唯一の gate である。
"""

from __future__ import annotations

import argparse
import importlib.metadata as md
import json
import os
from pathlib import Path

DEFAULT_MANIFEST = (
    Path(__file__).resolve().parents[2] / "docs" / "pinned-runtime-versions.json"
)


def verify(manifest_path: Path) -> list[str]:
    """manifest と pinned SDK 実測値を照合し、一致項目の一覧を返す.

    不一致は AssertionError を raise する。
    """
    from copilot import _cli_version as pinned

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    checks: list[str] = []

    sdk_version = md.version("github-copilot-sdk")
    assert sdk_version == manifest["sdk_version"], (
        f"SDK 版 mismatch: 実測 {sdk_version} != manifest {manifest['sdk_version']}"
    )
    checks.append(f"sdk_version={sdk_version}")

    cli_version = pinned.CLI_VERSION
    assert cli_version == manifest["cli_version"], (
        f"CLI 版 mismatch: 実測 {cli_version} != manifest {manifest['cli_version']}"
    )
    checks.append(f"cli_version={cli_version}")

    platform = pinned.get_runtime_platform()
    runtime_platforms = manifest["runtime_platforms"]
    if platform not in runtime_platforms:
        known = ", ".join(sorted(runtime_platforms))
        raise AssertionError(f"platform {platform} が manifest に無い: {known}")
    checks.append(f"runtime_platform={platform}")

    asset = pinned.get_release_asset_name(cli_version)
    expected_asset = manifest.get("release_asset_names", {}).get(platform)
    assert expected_asset is None or asset == expected_asset, (
        f"release asset mismatch: 実測 {asset} != manifest {expected_asset}"
    )
    checks.append(f"release_asset={asset}")

    # base override があれば mirror URL、無ければ release URL を期待する。
    download = manifest["download"]
    default_base = download["default_base_url"].rstrip("/")
    env_name = download["base_url_override_env"]
    expected_env = "COPILOT_CLI_DOWNLOAD_BASE_URL"
    if env_name != expected_env:
        raise AssertionError(f"env 名 mismatch: {env_name} != {expected_env}")
    override = os.environ.get(env_name)
    resolved_base = override.rstrip("/") if override else default_base
    expected_url = f"{resolved_base}/{download['release_path']}"
    checksums_url = pinned.get_checksums_url(cli_version)
    assert checksums_url == expected_url, (
        f"checksums url mismatch: 実測 {checksums_url} != expect {expected_url}"
    )
    checks.append(f"checksums_base={resolved_base}")

    digest = runtime_platforms[platform]
    assert len(digest) == 64, f"digest 長不正: {digest!r}"
    checks.append(f"sha256={digest[:12]}...")

    marker = manifest["cache_layout"]["bundle_complete_marker"]
    from copilot._cli_download import _HOSTLESS_ASSETS_MARKER

    assert marker == _HOSTLESS_ASSETS_MARKER, (
        f"marker mismatch: 実測 {_HOSTLESS_ASSETS_MARKER} != manifest {marker}"
    )
    checks.append(f"marker={marker}")
    return checks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="pinned runtime manifest 照合")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    args = parser.parse_args(argv)
    checks = verify(args.manifest)
    print("[pinned-check] " + " ".join(checks))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
