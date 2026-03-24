"""
路径归一化工具

兼容在 Linux/WSL 进程中读取 Windows 风格绝对路径（例如 `C:/next`）。
"""

import json
import os
import re
from pathlib import Path
from typing import Tuple

_WINDOWS_ABSOLUTE_PATH_RE = re.compile(r"^(?P<drive>[A-Za-z]):[\\/]*(?P<rest>.*)$")


def resolve_local_path(path_value: str) -> Path:
    """解析本地路径，兼容 WSL/Linux 下的 Windows 绝对路径。"""
    raw_value = str(path_value or "").strip()
    if not raw_value:
        return Path()

    expanded = os.path.expanduser(raw_value)
    if os.name != "nt":
        match = _WINDOWS_ABSOLUTE_PATH_RE.match(expanded)
        if match:
            drive = match.group("drive").lower()
            rest = str(match.group("rest") or "").replace("\\", "/").strip("/")
            candidate = Path("/mnt") / drive
            if rest:
                candidate = candidate.joinpath(*[part for part in rest.split("/") if part])
            return candidate.resolve()

    return Path(expanded).resolve()


def workspace_root_has_state(workspace_root: str | Path) -> bool:
    root_path = resolve_local_path(str(workspace_root or ""))
    if not str(root_path):
        return False
    state_dir = root_path / "state"
    for filename in ("projects.json", "workspaces.json", "sessions.json"):
        path = state_dir / filename
        if _state_file_has_rows(path):
            return True
    return False


def resolve_workspace_root_with_legacy_fallback(
    working_dir: str,
    configured_root: str = "",
    default_root_name: str = ".codex_data",
) -> Tuple[Path, str]:
    working_path = resolve_local_path(working_dir)
    legacy_root = (working_path / default_root_name).resolve() if str(working_path) else Path()

    configured_root_value = str(configured_root or "").strip()
    if not configured_root_value:
        return legacy_root, "legacy_default"

    configured_path = resolve_local_path(configured_root_value)
    if configured_path == legacy_root:
        return configured_path, "configured"
    if workspace_root_has_state(configured_path):
        return configured_path, "configured"
    if str(legacy_root) and workspace_root_has_state(legacy_root):
        return legacy_root, "legacy_fallback"
    return configured_path, "configured"


def _state_file_has_rows(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8") or "[]")
    except Exception:
        return False
    return isinstance(payload, list) and len(payload) > 0
