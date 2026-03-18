"""
全局任务注册表

管理 registry_key → asyncio.Task 映射，支持取消正在运行的 Agent 任务。
registry_key 格式: "{bot_key}:{session_key}"
"""

import asyncio
import logging
import threading
from typing import Optional

logger = logging.getLogger(__name__)


class TaskRegistry:
    """全局任务注册表，管理正在运行的 Agent 任务"""

    def __init__(self):
        self._tasks: dict[str, asyncio.Task] = {}
        self._stream_ids: dict[str, str] = {}
        self._extra: dict[str, dict] = {}  # 额外元数据（如 req_id、reply_state）
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

            self._tasks[key] = task
            self._stream_ids[key] = stream_id
            self._extra[key] = extra

        def _cleanup(t: asyncio.Task, _key=key):
            with self._lock:
                if self._tasks.get(_key) is t:
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
