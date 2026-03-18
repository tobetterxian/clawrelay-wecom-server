"""
AI 模型编排器基类

定义统一的接口，供不同 AI 模型实现。
"""

from abc import ABC, abstractmethod
from typing import Awaitable, Callable, List, Optional

# 流式内容回调类型：(accumulated_text, is_finished) -> None
OnStreamDelta = Optional[Callable[[str, bool], Awaitable[None]]]


class BaseOrchestrator(ABC):
    """AI 模型编排器基类

    所有 AI 模型编排器必须继承此类并实现抽象方法。
    """

    @abstractmethod
    async def handle_text_message(
        self,
        user_id: str,
        message: str,
        stream_id: str,
        session_key: str = "",
        log_context: dict = None,
        on_stream_delta: OnStreamDelta = None,
    ) -> str:
        """处理文本消息

        Args:
            user_id: 企业微信用户ID
            message: 用户消息文本
            stream_id: 消息ID
            session_key: 会话key（群聊=chatid，单聊=user_id）
            log_context: 日志上下文
            on_stream_delta: 流式内容回调 (accumulated_text, finish) -> None

        Returns:
            最终累积文本
        """
        pass

    @abstractmethod
    async def handle_multimodal_message(
        self,
        user_id: str,
        content_blocks: List[dict],
        stream_id: str,
        session_key: str = "",
        log_context: dict = None,
        on_stream_delta: OnStreamDelta = None,
    ) -> str:
        """处理多模态消息（图片+文本）

        Args:
            user_id: 企业微信用户ID
            content_blocks: OpenAI 格式的内容数组
            stream_id: 消息ID
            session_key: 会话key
            log_context: 日志上下文
            on_stream_delta: 流式内容回调

        Returns:
            最终累积文本
        """
        pass

    async def handle_file_message(
        self,
        user_id: str,
        message: str,
        files: List[dict],
        stream_id: str,
        session_key: str = "",
        log_context: dict = None,
        on_stream_delta: OnStreamDelta = None,
    ) -> str:
        """处理文件消息（默认实现：转为多模态消息）

        子类可以覆盖此方法以提供自定义实现。
        """
        content_blocks = [{"type": "text", "text": message}] + list(files)
        return await self.handle_multimodal_message(
            user_id=user_id,
            content_blocks=content_blocks,
            stream_id=stream_id,
            session_key=session_key,
            log_context=log_context,
            on_stream_delta=on_stream_delta,
        )

    async def clear_session(self, session_key: str) -> None:
        """清空指定会话状态

        默认无状态实现，子类可按需覆盖。
        """
        return None

    def has_pending_interaction(self, session_key: str) -> bool:
        """当前会话是否存在待处理的人机交互"""
        return False

    async def handle_interaction_text(self, session_key: str, text: str) -> Optional[str]:
        """处理用户对待处理交互的文本回复"""
        return None

    async def handle_interaction_card(self, session_key: str, event: dict) -> Optional[str]:
        """处理模板卡片交互回调"""
        return None
