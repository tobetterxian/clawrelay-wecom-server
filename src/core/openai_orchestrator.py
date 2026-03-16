"""
OpenAI 编排器模块

通过 OpenAI API 处理企业微信消息。
"""

import asyncio
import logging
import time
from datetime import datetime
from typing import Dict, List, Optional

try:
    from openai import AsyncOpenAI
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False

from .base_orchestrator import BaseOrchestrator, OnStreamDelta
from .session_manager import SessionManager
from .chat_logger import get_chat_logger
from .openai_model_selector import OpenAIModelSelector

logger = logging.getLogger(__name__)


class OpenAIOrchestrator(BaseOrchestrator):
    """OpenAI 编排器

    通过 OpenAI API 处理企业微信消息。
    """

    def __init__(
        self,
        bot_key: str,
        api_key: str,
        model: str = "",
        system_prompt: str = "",
        base_url: str = None,
    ):
        if not OPENAI_AVAILABLE:
            raise ImportError(
                "openai 未安装，请运行: pip install openai"
            )

        logger.info(
            f"开始初始化 OpenAI 编排器: bot_key={bot_key}, model={model or 'auto'}, base_url={base_url or 'default'}"
        )

        self.bot_key = bot_key
        self.system_prompt = system_prompt

        # 如果未指定模型，自动选择最佳模型
        if not model:
            logger.info("[OpenAI] 未指定模型，自动选择最佳可用模型...")
            model = OpenAIModelSelector.select_best_model(
                api_key=api_key,
                base_url=base_url or "https://api.openai.com/v1"
            )
            logger.info(f"[OpenAI] 自动选择模型: {model}")

        self.model = model

        # 创建 OpenAI 客户端
        client_kwargs = {"api_key": api_key}
        if base_url:
            client_kwargs["base_url"] = base_url
        self.client = AsyncOpenAI(**client_kwargs)

        # 会话管理
        self.session_manager = SessionManager()
        self.chat_histories: Dict[str, List[dict]] = {}

        logger.info(f"OpenAI 编排器初始化完成: bot_key={bot_key}, model={self.model}")

    def _get_or_create_history(self, session_key: str) -> List[dict]:
        """获取或创建会话历史"""
        if session_key not in self.chat_histories:
            history = []
            if self.system_prompt:
                history.append({"role": "system", "content": self.system_prompt})
            self.chat_histories[session_key] = history
            logger.info(f"[OpenAI] 创建新会话: session_key={session_key}")
        return self.chat_histories[session_key]

    async def handle_text_message(
        self,
        user_id: str,
        message: str,
        stream_id: str,
        session_key: str = "",
        log_context: dict = None,
        on_stream_delta: OnStreamDelta = None,
    ) -> str:
        """处理文本消息"""
        start_time = time.time()
        request_at = datetime.now()
        chat_logger = get_chat_logger()
        log_context = log_context or {}

        effective_key = session_key or user_id

        try:
            logger.info(
                f"[OpenAI] 处理消息: bot={self.bot_key}, user={user_id}, "
                f"session_key={effective_key}, message={message[:50]}"
            )

            # 获取会话历史
            messages = self._get_or_create_history(effective_key)
            messages.append({"role": "user", "content": message})

            # 流式调用 OpenAI API
            accumulated_text = ""
            stream = await self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                stream=True,
            )

            async for chunk in stream:
                if chunk.choices and chunk.choices[0].delta.content:
                    accumulated_text += chunk.choices[0].delta.content
                    if on_stream_delta:
                        await on_stream_delta(accumulated_text, False)

            if not accumulated_text or not accumulated_text.strip():
                logger.warning("[OpenAI] 返回空回复，使用默认文本")
                accumulated_text = "AI 已完成处理，但未生成文本回复。请尝试换个方式描述您的需求。"

            # 保存助手回复到历史
            messages.append({"role": "assistant", "content": accumulated_text})

            logger.info(
                f"[OpenAI] 流式完成: text_len={len(accumulated_text)}"
            )

            # 完成回调
            if on_stream_delta:
                await on_stream_delta(accumulated_text, True)

            # 记录日志
            latency_ms = int((time.time() - start_time) * 1000)
            log_context['session_key'] = effective_key
            chat_logger.log(
                bot_key=self.bot_key,
                user_id=user_id,
                stream_id=stream_id,
                message_content=message,
                response_content=accumulated_text,
                status="success",
                latency_ms=latency_ms,
                request_at=request_at,
                log_context=log_context,
            )

            return accumulated_text

        except asyncio.CancelledError:
            logger.warning(f"[OpenAI] 任务被取消: bot={self.bot_key}, user={user_id}")

            latency_ms = int((time.time() - start_time) * 1000)
            log_context['session_key'] = effective_key
            chat_logger.log(
                bot_key=self.bot_key,
                user_id=user_id,
                stream_id=stream_id,
                message_content=message,
                response_content="",
                status="timeout",
                error_message="任务被取消（超时）",
                latency_ms=latency_ms,
                request_at=request_at,
                log_context=log_context,
            )
            raise

        except Exception as e:
            logger.error(f"[OpenAI] 处理消息失败: {e}", exc_info=True)

            latency_ms = int((time.time() - start_time) * 1000)
            log_context['session_key'] = effective_key
            chat_logger.log(
                bot_key=self.bot_key,
                user_id=user_id,
                stream_id=stream_id,
                message_content=message,
                response_content="",
                status="error",
                error_message=str(e),
                latency_ms=latency_ms,
                request_at=request_at,
                log_context=log_context,
            )
            raise

    async def handle_multimodal_message(
        self,
        user_id: str,
        content_blocks: List[dict],
        stream_id: str,
        session_key: str = "",
        log_context: dict = None,
        on_stream_delta: OnStreamDelta = None,
    ) -> str:
        """处理多模态消息（图片+文本）"""
        start_time = time.time()
        request_at = datetime.now()
        chat_logger = get_chat_logger()
        log_context = log_context or {}

        effective_key = session_key or user_id

        try:
            text_summary = self._extract_text_from_blocks(content_blocks)
            logger.info(
                f"[OpenAI] 处理多模态消息: bot={self.bot_key}, user={user_id}, "
                f"session_key={effective_key}, blocks={len(content_blocks)}"
            )

            # 获取会话历史
            messages = self._get_or_create_history(effective_key)
            messages.append({"role": "user", "content": content_blocks})

            # 流式调用 OpenAI API
            accumulated_text = ""
            stream = await self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                stream=True,
            )

            async for chunk in stream:
                if chunk.choices and chunk.choices[0].delta.content:
                    accumulated_text += chunk.choices[0].delta.content
                    if on_stream_delta:
                        await on_stream_delta(accumulated_text, False)

            if not accumulated_text or not accumulated_text.strip():
                logger.warning("[OpenAI] 多模态返回空回复，使用默认文本")
                accumulated_text = "AI 已完成处理，但未生成文本回复。请尝试换个方式描述您的需求。"

            # 保存助手回复到历史
            messages.append({"role": "assistant", "content": accumulated_text})

            logger.info(
                f"[OpenAI] 多模态流式完成: text_len={len(accumulated_text)}"
            )

            # 完成回调
            if on_stream_delta:
                await on_stream_delta(accumulated_text, True)

            # 记录日志
            latency_ms = int((time.time() - start_time) * 1000)
            log_context['session_key'] = effective_key
            chat_logger.log(
                bot_key=self.bot_key,
                user_id=user_id,
                stream_id=stream_id,
                message_content=text_summary,
                response_content=accumulated_text,
                status="success",
                latency_ms=latency_ms,
                request_at=request_at,
                log_context=log_context,
            )

            return accumulated_text

        except asyncio.CancelledError:
            logger.warning(f"[OpenAI] 多模态任务被取消: bot={self.bot_key}, user={user_id}")

            latency_ms = int((time.time() - start_time) * 1000)
            log_context['session_key'] = effective_key
            chat_logger.log(
                bot_key=self.bot_key,
                user_id=user_id,
                stream_id=stream_id,
                message_content=self._extract_text_from_blocks(content_blocks),
                response_content="",
                status="timeout",
                error_message="多模态任务被取消（超时）",
                latency_ms=latency_ms,
                request_at=request_at,
                log_context=log_context,
            )
            raise

        except Exception as e:
            logger.error(f"[OpenAI] 处理多模态消息失败: {e}", exc_info=True)

            latency_ms = int((time.time() - start_time) * 1000)
            log_context['session_key'] = effective_key
            chat_logger.log(
                bot_key=self.bot_key,
                user_id=user_id,
                stream_id=stream_id,
                message_content=self._extract_text_from_blocks(content_blocks),
                response_content="",
                status="error",
                error_message=str(e),
                latency_ms=latency_ms,
                request_at=request_at,
                log_context=log_context,
            )
            raise

    @staticmethod
    def _extract_text_from_blocks(content_blocks: List[dict]) -> str:
        """从 content blocks 提取文本摘要"""
        texts = []
        for block in content_blocks:
            if block.get("type") == "text":
                texts.append(block.get("text", ""))
            elif block.get("type") == "image_url":
                texts.append("[图片]")
        return " ".join(texts)
