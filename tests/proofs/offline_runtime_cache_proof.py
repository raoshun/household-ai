"""bake 済み runtime cache が network 遮断環境で効くことを実証する proof (§15).

- home PC build で image layer に bake した cache を、container を network 遮断したまま
  SDK の download 経路で再利用できること (cache hit) を確認する。
- 空 dir を extract dir に指定した場合は network 取得を試みて failure すること
  (= cache hit が silent fallback ではなく、実際に download 経路が動いていること)
  を確認する。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

PROBE_HOST = "172.17.0.1"
PROBE_PORT = "8098"


def network_reachable() -> tuple[bool, str]:
    try:
        with socket.create_connection((PROBE_HOST, int(PROBE_PORT)), timeout=3):
            return True, f"{PROBE_HOST}:{PROBE_PORT} reachable"
    except OSError as exc:
        return False, f"{PROBE_HOST}:{PROBE_PORT} unreachable ({type(exc).__name__})"


_FAILURE_PATTERN = re.compile(
    r"(failed|error|fetch|connection|enotfound|eai_again|EHOSTUNREACH|network)",
    re.IGNORECASE,
)


def run_download(*, extract_dir: Path | None, force: bool) -> tuple[int, str]:
    cmd = [sys.executable, "-m", "copilot", "download-runtime", "--in-process"]
    if force:
        cmd.append("--force")
    env_extra = {}
    if extract_dir is not None:
        env_extra["COPILOT_CLI_EXTRACT_DIR"] = str(extract_dir)
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=600,
        env={**os.environ, **env_extra},
        check=False,
    )
    return proc.returncode, proc.stdout + proc.stderr


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="offline runtime cache proof")
    parser.add_argument(
        "--mode",
        choices=("cache-hit", "force-download-failure"),
        required=True,
    )
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    reachable, probe_note = network_reachable()
    if args.mode == "cache-hit" and reachable:
        raise SystemExit(f"cache-hit proof には network 遮断が必要: {probe_note}")

    record: dict[str, object] = {"mode": args.mode, "network_probe": probe_note}

    if args.mode == "cache-hit":
        code, output = run_download(extract_dir=None, force=False)
        cached = [line for line in output.splitlines() if "cached at" in line]
        record.update(
            {
                "download_exit_code": code,
                "cache_hit_lines": cached,
                "cache_home": os.environ.get("XDG_CACHE_HOME"),
            }
        )
        print(json.dumps(record, ensure_ascii=False, indent=2), flush=True)
        if code != 0 or not cached:
            print(output[-2000:], file=sys.stderr, flush=True)
            return 1
        return 0

    tmp = Path(tempfile.mkdtemp(prefix="empty-cache-"))
    try:
        code, output = run_download(extract_dir=tmp, force=True)
        failures = [
            line for line in output.splitlines() if _FAILURE_PATTERN.search(line)
        ]
        record.update(
            {
                "download_exit_code": code,
                "failure_lines": failures[:4],
                "extract_dir": str(tmp),
                "output_tail": output[-600:],
                "extract_dir_files": sorted(p.name for p in tmp.iterdir()),
            }
        )
        print(json.dumps(record, ensure_ascii=False, indent=2), flush=True)
        if code == 0 or not failures:
            print("force download failure が観測できない", file=sys.stderr, flush=True)
            return 1
        if list(tmp.iterdir()):
            print("失敗後に partial cache が残った", file=sys.stderr, flush=True)
            return 1
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
