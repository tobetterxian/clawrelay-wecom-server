"""
JSON 状态文件存储辅助模块

为项目 / 工作区 / 会话绑定等轻量持久化数据提供原子读写能力。
"""

import json
import threading
from pathlib import Path
from typing import Any, List


class JsonStateStore:
    """基于 JSON 文件的轻量状态存储"""

    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def read_list(self) -> List[dict]:
        with self._lock:
            return self.read_list_unlocked()

    def write_list(self, rows: List[dict]) -> None:
        with self._lock:
            self._write_atomic(rows)

    def update_list(self, updater) -> Any:
        with self._lock:
            rows = self.read_list_unlocked()
            result = updater(rows)
            self._write_atomic(rows)
            return result

    def read_list_unlocked(self) -> List[dict]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding='utf-8') or '[]')
        except Exception:
            return []
        return data if isinstance(data, list) else []

    def _write_atomic(self, rows: List[dict]) -> None:
        temp_path = self.path.with_suffix(self.path.suffix + '.tmp')
        temp_path.write_text(
            json.dumps(rows, ensure_ascii=False, indent=2),
            encoding='utf-8',
        )
        temp_path.replace(self.path)
