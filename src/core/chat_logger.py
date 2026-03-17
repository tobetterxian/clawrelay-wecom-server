"""
对话日志记录器

异步记录用户与AI的对话日志到 JSONL 文件。
使用 fire-and-forget 模式，不阻塞主流程。

日志文件: logs/chat.jsonl（每行一条 JSON 记录）
"""

import asyncio
import json
import logging
import os
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

LOG_DIR = Path(os.getenv("CHAT_LOG_DIR", "logs"))
LOG_FILE = LOG_DIR / "chat.jsonl"


class ChatLogger:
    """对话日志记录器 — JSONL 文件实现"""

    def __init__(self):
        LOG_DIR.mkdir(parents=True, exist_ok=True)

    def log(
        self,
        bot_key: str,
        user_id: str,
        stream_id: str,
        message_content: str,
        response_content: str,
        status: str = "success",
        error_message: str = "",
        latency_ms: int = 0,
        request_at: datetime = None,
        relay_session_id: str = "",
        tools_used: list = None,
        log_context: dict = None,
    ):
        """启动异步日志写入（fire-and-forget）"""
        record = {
            "timestamp": datetime.now().isoformat(),
            "bot_key": bot_key,
            "user_id": user_id,
            "stream_id": stream_id,
            "relay_session_id": relay_session_id or None,
            "chat_type": (log_context or {}).get("chat_type", "single"),
            "session_key": (log_context or {}).get("session_key", ""),
            "message_type": (log_context or {}).get("message_type", "text"),
            "message": message_content[:5000] if message_content else "",
            "response": response_content[:10000] if response_content else "",
            "tools_used": tools_used,
            "status": status,
            "error": error_message or None,
            "latency_ms": latency_ms,
            "request_at": request_at.isoformat() if request_at else None,
        }
        asyncio.create_task(self._write(record))

    async def _write(self, record: dict):
        try:
            line = json.dumps(record, ensure_ascii=False, default=str)
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._append_line, line)
        except Exception as e:
            logger.error("[ChatLogger] 日志写入失败: %s", e)

    @staticmethod
    def _append_line(line: str):
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")


# 单例
_chat_logger = ChatLogger()


def get_chat_logger() -> ChatLogger:
    return _chat_logger
