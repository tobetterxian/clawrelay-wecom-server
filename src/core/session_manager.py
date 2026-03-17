"""
会话管理器模块

管理 relay_session_id 的内存缓存：
- 存储和检索 relay_session_id（clawrelay-api 会话标识）
- 2小时超时自动过期（触发新会话）
- 纯内存实现，进程重启后自动创建新会话

会话历史由 clawrelay-api 通过 session_id 自行维护，本地只管 ID 映射。
"""

import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)


class SessionManager:
    """会话管理器 — 内存实现

    Attributes:
        SESSION_TIMEOUT_SECONDS: 会话超时时间（默认 2 小时）
    """

    SESSION_TIMEOUT_SECONDS = 2 * 3600  # 2 hours

    def __init__(self):
        # {session_key: {"relay_session_id": str, "last_active": float}}
        self._sessions: dict[str, dict] = {}

    async def get_relay_session_id(self, bot_key: str, user_id: str) -> str:
        """获取 relay_session_id，超时或不存在返回空字符串"""
        key = f"{bot_key}_{user_id}"
        entry = self._sessions.get(key)

        if not entry:
            return ""

        elapsed = time.monotonic() - entry["last_active"]
        if elapsed > self.SESSION_TIMEOUT_SECONDS:
            logger.info("会话已超时: %s (%.1f小时前)", key, elapsed / 3600)
            del self._sessions[key]
            return ""

        return entry.get("relay_session_id", "")

    async def save_relay_session_id(self, bot_key: str, user_id: str, relay_session_id: str):
        """保存 relay_session_id"""
        key = f"{bot_key}_{user_id}"
        self._sessions[key] = {
            "relay_session_id": relay_session_id,
            "last_active": time.monotonic(),
        }

    async def clear_session(self, bot_key: str, user_id: str):
        """清空会话"""
        key = f"{bot_key}_{user_id}"
        self._sessions.pop(key, None)
        logger.info("清空会话: %s", key)
