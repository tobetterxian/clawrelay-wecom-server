"""
画册素材清单辅助工具

统一读写 `docs/brochure-assets.json`，用于记录 Cloudinary 等外部素材处理结果。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

MANIFEST_VERSION = 1
DEFAULT_BROCHURE_ASSET_MANIFEST_PATH = "docs/brochure-assets.json"


def manifest_path_for_workspace(workspace_path: str) -> Path:
    workspace_root = Path(workspace_path).expanduser().resolve()
    return (workspace_root / DEFAULT_BROCHURE_ASSET_MANIFEST_PATH).resolve()


def load_brochure_asset_manifest(workspace_path: str) -> Optional[Dict[str, Any]]:
    manifest_path = manifest_path_for_workspace(workspace_path)
    if not manifest_path.exists():
        return None
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8") or "{}")
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def write_brochure_asset_manifest(workspace_path: str, payload: Dict[str, Any]) -> Path:
    manifest_path = manifest_path_for_workspace(workspace_path)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = manifest_path.with_suffix(f"{manifest_path.suffix}.tmp")
    temp_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temp_path.replace(manifest_path)
    return manifest_path


def summarize_brochure_asset_manifest(payload: Optional[Dict[str, Any]]) -> str:
    data = payload if isinstance(payload, dict) else {}
    provider = str(data.get("provider") or "-").strip() or "-"
    asset_count = int(data.get("asset_count") or len(data.get("assets") or []) or 0)
    generated_at = str(data.get("generated_at") or "-").strip() or "-"
    return (
        f"素材来源：{provider}\n"
        f"素材数量：{asset_count}\n"
        f"生成时间：{generated_at}"
    )
