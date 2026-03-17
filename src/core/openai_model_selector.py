"""
OpenAI 兼容 API 模型选择器

自动获取可用模型列表并选择最佳模型。
"""

import logging
from typing import Optional, List

logger = logging.getLogger(__name__)


class OpenAIModelSelector:
    """OpenAI 兼容 API 模型选择器

    自动获取可用模型并选择最佳默认模型。
    """

    # 模型优先级（从高到低）
    # 按照性能和发布时间排序
    MODEL_PRIORITY = [
        # Claude 4.x 系列
        "claude-opus-4-6",
        "claude-opus-4-5",
        "claude-sonnet-4-6",
        "claude-sonnet-4-5",
        "claude-haiku-4-5",

        # Claude 3.x 系列
        "claude-3-opus-20240229",
        "claude-3-5-sonnet-20241022",
        "claude-3-5-sonnet-20240620",
        "claude-3-sonnet-20240229",
        "claude-3-haiku-20240307",

        # GPT-4 系列
        "gpt-4o",
        "gpt-4-turbo",
        "gpt-4-turbo-preview",
        "gpt-4-1106-preview",
        "gpt-4",

        # GPT-3.5 系列
        "gpt-3.5-turbo",
        "gpt-3.5-turbo-16k",

        # Gemini 系列
        "gemini-2.5-pro",
        "gemini-2.5-flash",
        "gemini-2.0-flash",
        "gemini-1.5-pro",
        "gemini-1.5-flash",
    ]

    @staticmethod
    def list_models(api_key: str, base_url: str) -> List[str]:
        """列出可用模型

        Args:
            api_key: API Key
            base_url: API 基础 URL

        Returns:
            模型名称列表
        """
        try:
            from openai import OpenAI

            client = OpenAI(api_key=api_key, base_url=base_url)
            response = client.models.list()

            models = [model.id for model in response.data]
            logger.info(f"[OpenAIModelSelector] 找到 {len(models)} 个可用模型")

            return models

        except Exception as e:
            logger.error(f"[OpenAIModelSelector] 获取模型列表失败: {e}")
            return []

    @staticmethod
    def select_best_model(api_key: str, base_url: str) -> str:
        """选择最佳可用模型

        Args:
            api_key: API Key
            base_url: API 基础 URL

        Returns:
            最佳模型名称
        """
        models = OpenAIModelSelector.list_models(api_key, base_url)

        if not models:
            logger.warning("[OpenAIModelSelector] 无法获取模型列表，使用默认模型")
            return "gpt-4o"

        logger.info(f"[OpenAIModelSelector] 可用模型: {', '.join(models[:5])}...")

        # 按优先级选择
        for preferred in OpenAIModelSelector.MODEL_PRIORITY:
            if preferred in models:
                logger.info(f"[OpenAIModelSelector] 选择模型: {preferred}")
                return preferred

        # 如果没有匹配的优先模型，尝试智能选择
        # 优先选择包含 opus/sonnet/gpt-4 的模型
        for model in models:
            model_lower = model.lower()
            if any(keyword in model_lower for keyword in ["opus", "sonnet", "gpt-4"]):
                logger.info(f"[OpenAIModelSelector] 智能选择模型: {model}")
                return model

        # 选择第一个可用的
        if models:
            selected = models[0]
            logger.info(f"[OpenAIModelSelector] 使用第一个可用模型: {selected}")
            return selected

        # 兜底
        logger.warning("[OpenAIModelSelector] 未找到合适模型，使用默认")
        return "gpt-4o"
