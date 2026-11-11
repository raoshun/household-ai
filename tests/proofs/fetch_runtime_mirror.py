"""pinned manifest の runtime release asset を mirror へ staging する proof (§15).

network 制限環境では image build 用的な download が通らないため、manifest の tag /
asset 名 / digest を使って mirror 側 (静的 file server) に同一 layout を用意する。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import urllib.request
from pathlib import Path

DEFAULT_OUT = Path(".local/mirror")
CHUNK = 1024 * 1024


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


# host 側 log で識別できるよう UA を付与する (GitHub は UA なしでも 200 を返す)。
REQUEST = urllib.request.Request
USER_AGENT = "household-ai-runtime-mirror/1.0"


def fetch(url: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    request = REQUEST(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    with urllib.request.urlopen(request, timeout=600) as response:
        dest.write_bytes(response.read())
    return dest


def stage(manifest_path: Path, out_dir: Path, platform: str) -> tuple[Path, Path]:
    """manifest に記載の asset を `<out>/v<cli>/` へ download して digest 照合する.

    既に同名 file が manifest digest と一致する場合は再 download を skip する。
    """
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    cli_version = manifest["cli_version"]
    tag = manifest["download"]["tag"]
    if tag != f"v{cli_version}":
        raise SystemExit(f"manifest tag と cli_version が不整合: {tag} / {cli_version}")
    checksums_url = manifest["download"]["checksums_url"]
    if not checksums_url.endswith(f"/{tag}/SHA256SUMS.txt"):
        raise SystemExit(f"manifest checksums_url 不正: {checksums_url}")
    sums_url = checksums_url
    tag_dir = checksums_url.rsplit("/", 1)[0]
    asset_name = manifest["release_asset_names"][platform]
    asset_url = f"{tag_dir}/{asset_name}"

    sums_path = out_dir / tag / "SHA256SUMS.txt"
    if not sums_path.exists():
        fetch(sums_url, sums_path)
    sums: dict[str, str] = {}
    for line in sums_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            digest, name = line.split()
            sums[name.lstrip("*")] = digest
    expected = sums[asset_name]
    manifest_digest = manifest["runtime_platforms"][platform]
    assert manifest_digest == expected, (
        f"manifest digest mismatch for {asset_name}: {manifest_digest} != {expected}"
    )

    asset_path = out_dir / tag / asset_name
    if asset_path.exists() and sha256_of(asset_path) == expected:
        return sums_path, asset_path
    fetch(asset_url, asset_path)
    actual = sha256_of(asset_path)
    if actual != expected:
        asset_path.unlink()
        raise SystemExit(f"digest mismatch for {asset_name}: {actual} != {expected}")
    return sums_path, asset_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="runtime release asset mirror staging")
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path(__file__).resolve().parents[2]
        / "docs"
        / "pinned-runtime-versions.json",
    )
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument(
        "--platform", default=None, help="既定は build host の runtime platform"
    )
    parser.add_argument(
        "--clean", action="store_true", help="out dir を削除してから staging"
    )
    args = parser.parse_args(argv)

    if args.clean and args.out.exists():
        shutil.rmtree(args.out)

    if args.platform:
        platform = args.platform
    else:
        # host に SDK が無くても stage できるよう manifest の platform を既定にする
        platform = json.loads(args.manifest.read_text(encoding="utf-8"))[
            "runtime_platform"
        ]

    sums_path, asset_path = stage(args.manifest, args.out, platform)
    print(f"staged {sums_path} ({sums_path.stat().st_size} bytes)")
    print(f"staged {asset_path} ({asset_path.stat().st_size} bytes, sha256 verified)")
    print(
        "serve: make runtime-serve MIRROR_PORT=<port> (build host は docker bridge の "
        "172.17.0.1 経由でアクセスする)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
