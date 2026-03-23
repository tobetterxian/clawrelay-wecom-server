"""
画册源资料清单辅助工具

统一读写 `docs/brochure-source-materials.json`，用于记录用户上传的图片素材、
参数文档与其在项目工作区中的保存位置，便于后续画册生成和断点恢复复用。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

SOURCE_MATERIALS_VERSION = 1
DEFAULT_BROCHURE_SOURCE_MATERIALS_PATH = "docs/brochure-source-materials.json"


def source_materials_path_for_workspace(workspace_path: str) -> Path:
    workspace_root = Path(workspace_path).expanduser().resolve()
    return (workspace_root / DEFAULT_BROCHURE_SOURCE_MATERIALS_PATH).resolve()


def load_brochure_source_materials(workspace_path: str) -> Optional[Dict[str, Any]]:
    manifest_path = source_materials_path_for_workspace(workspace_path)
    if not manifest_path.exists():
        return None
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8") or "{}")
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def write_brochure_source_materials(workspace_path: str, payload: Dict[str, Any]) -> Path:
    manifest_path = source_materials_path_for_workspace(workspace_path)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = manifest_path.with_suffix(f"{manifest_path.suffix}.tmp")
    temp_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temp_path.replace(manifest_path)
    return manifest_path


def summarize_brochure_source_materials(payload: Optional[Dict[str, Any]]) -> str:
    data = payload if isinstance(payload, dict) else {}
    materials = list(data.get("materials") or [])
    image_count = sum(1 for item in materials if str((item or {}).get("kind") or "") == "image")
    document_count = sum(1 for item in materials if str((item or {}).get("kind") or "") == "document")
    generated_at = str(data.get("generated_at") or "-").strip() or "-"

    names: list[str] = []
    for item in materials[:3]:
        relative_path = str((item or {}).get("relative_path") or "").strip()
        if relative_path:
            names.append(relative_path)
    recent_names = "、".join(names) if names else "-"

    return (
        f"图片素材：{image_count}\n"
        f"参数文档：{document_count}\n"
        f"最近资料：{recent_names}\n"
        f"更新时间：{generated_at}"
    )
