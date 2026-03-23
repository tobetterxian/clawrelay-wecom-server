"""
Canva 画册状态辅助工具

统一读写 `docs/canva-brochure.json`，用于记录 Canva 精修版设计状态与导出结果。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

CANVA_BROCHURE_STATE_VERSION = 1
DEFAULT_CANVA_BROCHURE_STATE_PATH = "docs/canva-brochure.json"


def canva_state_path_for_workspace(workspace_path: str) -> Path:
    workspace_root = Path(workspace_path).expanduser().resolve()
    return (workspace_root / DEFAULT_CANVA_BROCHURE_STATE_PATH).resolve()


def load_canva_brochure_state(workspace_path: str) -> Optional[Dict[str, Any]]:
    state_path = canva_state_path_for_workspace(workspace_path)
    if not state_path.exists():
        return None
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8") or "{}")
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def write_canva_brochure_state(workspace_path: str, payload: Dict[str, Any]) -> Path:
    state_path = canva_state_path_for_workspace(workspace_path)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = state_path.with_suffix(f"{state_path.suffix}.tmp")
    temp_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temp_path.replace(state_path)
    return state_path


def summarize_canva_brochure_state(payload: Optional[Dict[str, Any]]) -> str:
    data = payload if isinstance(payload, dict) else {}
    design_title = str(data.get("design_title") or data.get("title") or "-").strip() or "-"
    design_id = str(data.get("design_id") or "-").strip() or "-"
    page_count = int(data.get("page_count") or 0)
    generated_at = str(data.get("generated_at") or "-").strip() or "-"
    return (
        f"设计标题：{design_title}\n"
        f"设计ID：{design_id}\n"
        f"页数：{page_count}\n"
        f"生成时间：{generated_at}"
    )
