"""
Gemini 编排器模块

通过 Google Gemini API 处理企业微信消息。
使用 requests 库（在 asyncio 中运行）以确保兼容性。
"""

import asyncio
import json
import logging
import time
from datetime import datetime
from typing import Dict, List, Optional

import requests

from .base_orchestrator import BaseOrchestrator, OnStreamDelta
from .session_manager import SessionManager
from .chat_logger import get_chat_logger
from .gemini_model_selector import GeminiModelSelector

logger = logging.getLogger(__name__)

GEMINI_AVAILABLE = True  # 使用 REST API，无需额外依赖


class GeminiOrchestrator(BaseOrchestrator):
    """Gemini 编排器

    通过 Google Gemini REST API 处理企业微信消息。
    """

    def __init__(
        self,
        bot_key: str,
        api_key: str,
        model: str = "",
        system_prompt: str = "",
        enable_search: bool = False,
    ):
        logger.info(
            f"开始初始化 Gemini 编排器: bot_key={bot_key}, model={model or 'auto'}, enable_search={enable_search}"
        )

        self.bot_key = bot_key
        self.api_key = api_key
        self.system_prompt = system_prompt
        self.enable_search = enable_search

        # 如果未指定模型，自动选择最佳模型
        if not model:
            logger.info("[Gemini] 未指定模型，自动选择最佳可用模型...")
            model = GeminiModelSelector.select_best_model(api_key, enable_search)
            logger.info(f"[Gemini] 自动选择模型: {model}")

        self.model_name = model

        # 根据模型选择 API 版本
        # 如果启用搜索，必须使用 v1beta（v1 不支持 tools）
        # 3.x 和 2.0 实验版模型也需要 v1beta
        if enable_search or "3.0" in model or "3.1" in model or "2.0" in model or "exp" in model:
            self.api_version = "v1beta"
        else:
            self.api_version = "v1"

        self.base_url = f"https://generativelanguage.googleapis.com/{self.api_version}/models"

        # 会话管理
        self.session_manager = SessionManager()
        self.chat_histories: Dict[str, List[dict]] = {}

        logger.info(f"Gemini 编排器初始化完成: bot_key={bot_key}, model={self.model_name}, api_version={self.api_version}")

    def _get_or_create_history(self, session_key: str) -> List[dict]:
        """获取或创建会话历史"""
        if session_key not in self.chat_histories:
            self.chat_histories[session_key] = []
            logger.info(f"[Gemini] 创建新会话: session_key={session_key}")
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
                f"[Gemini] 处理消息: bot={self.bot_key}, user={user_id}, "
                f"session_key={effective_key}, message={message[:50]}"
            )

            # 获取会话历史
            history = self._get_or_create_history(effective_key)

            # 构建请求内容
            contents = list(history)

            # 如果有系统提示词且是新会话，将其作为第一条消息
            if self.system_prompt and not contents:
                contents.append({
                    "role": "user",
                    "parts": [{"text": f"System: {self.system_prompt}"}]
                })
                contents.append({
                    "role": "model",
                    "parts": [{"text": "我明白了，我会遵循这些指示。"}]
                })

            contents.append({
                "role": "user",
                "parts": [{"text": message}]
            })

            # 构建请求体（v1 API 不支持 systemInstruction）
            request_body = {
                "contents": contents,
                "generationConfig": {
                    "temperature": 1.0,
                }
            }

            # 如果启用搜索，添加 google_search 工具
            if self.enable_search:
                request_body["tools"] = [{"google_search": {}}]

            # 流式调用 Gemini API（使用 requests 在线程池中运行）
            url = f"{self.base_url}/{self.model_name}:streamGenerateContent?key={self.api_key}&alt=sse"

            accumulated_text = ""

            # 在线程池中运行同步的 requests 调用
            loop = asyncio.get_event_loop()

            def _stream_request():
                """同步的流式请求"""
                nonlocal accumulated_text
                try:
                    with requests.post(url, json=request_body, stream=True, timeout=3600) as response:
                        if response.status_code != 200:
                            raise Exception(f"Gemini API error: HTTP {response.status_code}, {response.text[:500]}")

                        # 解析 SSE 流
                        for line in response.iter_lines():
                            if not line:
                                continue

                            line_text = line.decode("utf-8", errors="replace").strip()

                            if not line_text or line_text.startswith(":"):
                                continue

                            if line_text.startswith("data: "):
                                data = line_text[6:]

                                try:
                                    chunk = json.loads(data)
                                    candidates = chunk.get("candidates", [])
                                    if candidates:
                                        content = candidates[0].get("content", {})
                                        parts = content.get("parts", [])
                                        for part in parts:
                                            text = part.get("text", "")
                                            if text:
                                                accumulated_text += text
                                                # 在事件循环中调用回调
                                                if on_stream_delta:
                                                    asyncio.run_coroutine_threadsafe(
                                                        on_stream_delta(accumulated_text, False),
                                                        loop
                                                    ).result()
                                except json.JSONDecodeError:
                                    continue

                except Exception as e:
                    raise e

            # 在线程池中运行
            await loop.run_in_executor(None, _stream_request)

            if not accumulated_text or not accumulated_text.strip():
                logger.warning("[Gemini] 返回空回复，使用默认文本")
                accumulated_text = "AI 已完成处理，但未生成文本回复。请尝试换个方式描述您的需求。"

            # 保存到历史
            history.append({"role": "user", "parts": [{"text": message}]})
            history.append({"role": "model", "parts": [{"text": accumulated_text}]})

            logger.info(
                f"[Gemini] 流式完成: text_len={len(accumulated_text)}"
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
            logger.warning(f"[Gemini] 任务被取消: bot={self.bot_key}, user={user_id}")

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
            logger.error(f"[Gemini] 处理消息失败: {e}", exc_info=True)

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
                f"[Gemini] 处理多模态消息: bot={self.bot_key}, user={user_id}, "
                f"session_key={effective_key}, blocks={len(content_blocks)}"
            )

            # 获取会话历史
            history = self._get_or_create_history(effective_key)

            # 构建请求内容（支持图片）
            contents = list(history)

            # 如果有系统提示词且是新会话，将其作为第一条消息
            if self.system_prompt and not contents:
                contents.append({
                    "role": "user",
                    "parts": [{"text": f"System: {self.system_prompt}"}]
                })
                contents.append({
                    "role": "model",
                    "parts": [{"text": "我明白了，我会遵循这些指示。"}]
                })

            # 转换 content_blocks 为 Gemini 格式
            parts = []
            for block in content_blocks:
                if block.get("type") == "text":
                    parts.append({"text": block.get("text", "")})
                elif block.get("type") == "image_url":
                    # Gemini 支持图片 URL（base64 或 http/https）
                    image_url = block.get("image_url", {}).get("url", "")
                    if image_url:
                        if image_url.startswith("data:"):
                            # Base64 图片
                            # 格式: data:image/jpeg;base64,/9j/4AAQ...
                            parts_split = image_url.split(",", 1)
                            if len(parts_split) == 2:
                                mime_type = parts_split[0].split(":")[1].split(";")[0]
                                base64_data = parts_split[1]
                                parts.append({
                                    "inline_data": {
                                        "mime_type": mime_type,
                                        "data": base64_data
                                    }
                                })
                        else:
                            # HTTP/HTTPS URL - Gemini 不直接支持，需要下载
                            logger.warning(f"[Gemini] 暂不支持 HTTP 图片 URL: {image_url[:50]}")
                            parts.append({"text": "[图片URL暂不支持]"})

            contents.append({
                "role": "user",
                "parts": parts
            })

            # 构建请求体
            request_body = {
                "contents": contents,
                "generationConfig": {
                    "temperature": 1.0,
                }
            }

            # 如果启用搜索，添加 google_search 工具
            if self.enable_search:
                request_body["tools"] = [{"google_search": {}}]

            # 流式调用 Gemini API
            url = f"{self.base_url}/{self.model_name}:streamGenerateContent?key={self.api_key}&alt=sse"

            accumulated_text = ""
            loop = asyncio.get_event_loop()

            def _stream_request():
                """同步的流式请求"""
                nonlocal accumulated_text
                try:
                    with requests.post(url, json=request_body, stream=True, timeout=3600) as response:
                        if response.status_code != 200:
                            raise Exception(f"Gemini API error: HTTP {response.status_code}, {response.text[:500]}")

                        # 解析 SSE 流
                        for line in response.iter_lines():
                            if not line:
                                continue

                            line_text = line.decode("utf-8", errors="replace").strip()

                            if not line_text or line_text.startswith(":"):
                                continue

                            if line_text.startswith("data: "):
                                data = line_text[6:]

                                try:
                                    chunk = json.loads(data)
                                    candidates = chunk.get("candidates", [])
                                    if candidates:
                                        content = candidates[0].get("content", {})
                                        parts_result = content.get("parts", [])
                                        for part in parts_result:
                                            text = part.get("text", "")
                                            if text:
                                                accumulated_text += text
                                                if on_stream_delta:
                                                    asyncio.run_coroutine_threadsafe(
                                                        on_stream_delta(accumulated_text, False),
                                                        loop
                                                    ).result()
                                except json.JSONDecodeError:
                                    continue

                except Exception as e:
                    raise e

            # 在线程池中运行
            await loop.run_in_executor(None, _stream_request)

            if not accumulated_text or not accumulated_text.strip():
                logger.warning("[Gemini] 多模态返回空回复，使用默认文本")
                accumulated_text = "AI 已完成处理，但未生成文本回复。请尝试换个方式描述您的需求。"

            # 保存到历史
            history.append({"role": "user", "parts": parts})
            history.append({"role": "model", "parts": [{"text": accumulated_text}]})

            logger.info(
                f"[Gemini] 多模态流式完成: text_len={len(accumulated_text)}"
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
            logger.warning(f"[Gemini] 多模态任务被取消: bot={self.bot_key}, user={user_id}")

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
            logger.error(f"[Gemini] 处理多模态消息失败: {e}", exc_info=True)

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
