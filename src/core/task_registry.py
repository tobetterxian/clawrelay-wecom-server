"""
全局任务注册表

管理 registry_key → asyncio.Task 映射，支持取消正在运行的 Agent 任务。
registry_key 格式: "{bot_key}:{session_key}"
"""

import asyncio
import logging
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)
RECENT_TASK_TTL_SECONDS = 3600


class TaskRegistry:
    """全局任务注册表，管理正在运行的 Agent 任务"""

    def __init__(self):
        self._tasks: dict[str, asyncio.Task] = {}
        self._stream_ids: dict[str, str] = {}
        self._extra: dict[str, dict] = {}  # 额外元数据（如 req_id、reply_state）
        self._recent: dict[str, dict] = {}  # 最近结束的任务终态，便于后续查询
        self._lock = threading.Lock()
        logger.info("[TaskRegistry] 初始化完成")

    def register(self, key: str, task: asyncio.Task, stream_id: str, **extra):
        """注册任务

        若已有旧任务（已完成），直接覆盖。
        通过 done_callback 自动清理完成的任务。

        Args:
            extra: 额外元数据，如 req_id（WebSocket 模式需要用旧 req_id 更新消息气泡）
        """
        with self._lock:
            old = self._tasks.get(key)
            if old and not old.done():
                logger.warning("[TaskRegistry] key=%s 已有运行中任务，覆盖注册", key)
            self._recent.pop(key, None)

            now = time.time()
            now_monotonic = time.monotonic()
            metadata = dict(extra or {})
            metadata.setdefault("started_at", now)
            metadata.setdefault("started_at_monotonic", now_monotonic)
            metadata.setdefault("last_activity_at", now)
            metadata.setdefault("last_activity_at_monotonic", now_monotonic)
            metadata.setdefault("last_status_render_at", now)
            metadata.setdefault("last_status_render_at_monotonic", now_monotonic)
            metadata.setdefault("last_preview", "")

            self._tasks[key] = task
            self._stream_ids[key] = stream_id
            self._extra[key] = metadata

        def _cleanup(t: asyncio.Task, _key=key):
            with self._lock:
                if self._tasks.get(_key) is t:
                    metadata = dict(self._extra.get(_key, {}) or {})
                    finished_at = time.time()
                    metadata["finished_at"] = finished_at
                    if t.cancelled():
                        metadata["terminal_status"] = "cancelled"
                    else:
                        try:
                            error = t.exception()
                        except asyncio.CancelledError:
                            metadata["terminal_status"] = "cancelled"
                        else:
                            if error is None:
                                metadata["terminal_status"] = "completed"
                            else:
                                metadata["terminal_status"] = "error"
                                metadata["terminal_error"] = str(error)
                    self._recent[_key] = metadata
                    self._prune_recent_locked(now=finished_at)
                    del self._tasks[_key]
                    self._stream_ids.pop(_key, None)
                    self._extra.pop(_key, None)
                    logger.debug("[TaskRegistry] 自动清理完成任务: key=%s", _key)

        task.add_done_callback(_cleanup)
        logger.info("[TaskRegistry] 注册任务: key=%s, stream_id=%s", key, stream_id)

    def get(self, key: str) -> tuple[Optional[asyncio.Task], Optional[str], dict]:
        """获取任务、stream_id 与扩展上下文"""
        with self._lock:
            return self._tasks.get(key), self._stream_ids.get(key), self._extra.get(key, {})

    def get_recent(self, key: str) -> dict:
        """获取最近结束任务的终态"""
        with self._lock:
            self._prune_recent_locked()
            return dict(self._recent.get(key, {}) or {})

    def update_stream(self, key: str, stream_id: str, **extra) -> bool:
        """更新运行中任务的回复通道信息"""
        with self._lock:
            task = self._tasks.get(key)
            if not task or task.done():
                return False
            self._stream_ids[key] = stream_id
            current = self._extra.setdefault(key, {})
            current.update(extra)
            return True

    def touch(self, key: str, **extra) -> bool:
        """更新任务活动时间与附加状态"""
        with self._lock:
            task = self._tasks.get(key)
            if not task or task.done():
                return False
            current = self._extra.setdefault(key, {})
            now = time.time()
            now_monotonic = time.monotonic()
            current["last_activity_at"] = now
            current["last_activity_at_monotonic"] = now_monotonic
            current["last_status_render_at"] = now
            current["last_status_render_at_monotonic"] = now_monotonic
            current.update(extra)
            return True

    def mark_rendered(self, key: str, **extra) -> bool:
        """更新最近一次状态渲染时间，不改变真实活动时间。"""
        with self._lock:
            task = self._tasks.get(key)
            if not task or task.done():
                return False
            current = self._extra.setdefault(key, {})
            now = time.time()
            now_monotonic = time.monotonic()
            current["last_status_render_at"] = now
            current["last_status_render_at_monotonic"] = now_monotonic
            current.update(extra)
            return True

    def annotate(self, key: str, **extra) -> bool:
        """更新运行中或最近结束任务的附加状态，不改动活动时间"""
        with self._lock:
            if key in self._extra:
                current = self._extra.setdefault(key, {})
                current.update(extra)
                return True
            if key in self._recent:
                current = self._recent.setdefault(key, {})
                current.update(extra)
                return True
            return False

    def cancel(self, key: str) -> tuple[bool, Optional[str], dict]:
        """取消任务

        Returns:
            (是否成功取消, 对应的 stream_id, 额外元数据)
        """
        with self._lock:
            task = self._tasks.get(key)
            stream_id = self._stream_ids.get(key)
            extra = self._extra.get(key, {})

            if not task or task.done():
                return False, None, {}

            task.cancel()
            logger.info("[TaskRegistry] 取消任务: key=%s, stream_id=%s", key, stream_id)
            return True, stream_id, extra

    def is_running(self, key: str) -> bool:
        """检查是否有运行中任务"""
        with self._lock:
            task = self._tasks.get(key)
            return task is not None and not task.done()

    def forget(self, key: str) -> None:
        """清理最近终态缓存（不影响运行中任务）"""
        with self._lock:
            self._recent.pop(key, None)

    def _prune_recent_locked(self, now: Optional[float] = None) -> None:
        cutoff = float(now or time.time()) - RECENT_TASK_TTL_SECONDS
        expired = [
            key
            for key, value in self._recent.items()
            if float((value or {}).get("finished_at") or 0) < cutoff
        ]
        for key in expired:
            self._recent.pop(key, None)


_global_task_registry: Optional[TaskRegistry] = None
_registry_lock = threading.Lock()


def get_task_registry() -> TaskRegistry:
    """获取全局任务注册表单例"""
    global _global_task_registry

    if _global_task_registry is None:
        with _registry_lock:
            if _global_task_registry is None:
                _global_task_registry = TaskRegistry()

    return _global_task_registry
