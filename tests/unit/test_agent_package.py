"""app.agent の import 境界テスト (仕様 §2.3).

`import app.agent` が成立することと、Copilot SDK の import が `app/agent/` 配下に
閉じ込められていることを検証する。
"""

from __future__ import annotations

import ast
from pathlib import Path

import app.agent as agent


def test_agent_package_exposes_declared_names() -> None:
    for name in agent.__all__:
        assert getattr(agent, name) is not None


def test_copilot_import_is_confined_to_agent_package() -> None:
    # SDK の import は app.agent 配下に閉じ込める (§2.3)
    app_dir = Path(agent.__file__).parent.parent
    offenders = [
        str(path.relative_to(app_dir))
        for path in sorted(app_dir.rglob("*.py"))
        if path.relative_to(app_dir).parts[0] != "agent" and _imports_copilot(path)
    ]
    assert offenders == []


def _imports_copilot(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
    return any(root == "copilot" for root in (name.split(".")[0] for name in imported))
