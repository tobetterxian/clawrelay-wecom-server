"""
Gemini 模型选择器

自动获取可用模型列表并选择最佳模型。
"""

import logging
import requests
from typing import Optional, List, Dict

logger = logging.getLogger(__name__)


class GeminiModelSelector:
    """Gemini 模型选择器

    自动获取可用模型并选择最佳默认模型。
    """

    # 模型优先级（从高到低）
    MODEL_PRIORITY = [
        "gemini-3.1-pro",      # 最新最强
        "gemini-3.1-flash",    # 最新快速版
        "gemini-3.0-pro",      # 3.0 强大版
        "gemini-3.0-flash",    # 3.0 快速版
        "gemini-2.5-pro",      # 2.5 强大版
        "gemini-2.5-flash",    # 2.5 快速版
        "gemini-2.0-flash",    # 2.0 稳定快速版
        "gemini-1.5-pro",      # 1.5 强大版
        "gemini-1.5-flash",    # 1.5 快速版
    ]

    @staticmethod
    def list_models(api_key: str, api_version: str = "v1") -> List[Dict]:
        """列出可用模型

        Args:
            api_key: Gemini API Key
            api_version: API 版本 (v1 或 v1beta)

        Returns:
            模型列表
        """
        try:
            url = f"https://generativelanguage.googleapis.com/{api_version}/models?key={api_key}"
            response = requests.get(url, timeout=10)

            if response.status_code == 200:
                data = response.json()
                models = data.get("models", [])

                # 过滤出支持 generateContent 的模型
                generate_models = [
                    m for m in models
                    if "generateContent" in m.get("supportedGenerationMethods", [])
                ]

                logger.info(f"[GeminiModelSelector] 找到 {len(generate_models)} 个可用模型 (API {api_version})")
                return generate_models
            else:
                logger.warning(f"[GeminiModelSelector] 获取模型列表失败: HTTP {response.status_code}")
                return []

        except Exception as e:
            logger.error(f"[GeminiModelSelector] 获取模型列表异常: {e}")
            return []

    @staticmethod
    def select_best_model(api_key: str, enable_search: bool = False) -> str:
        """选择最佳可用模型

        Args:
            api_key: Gemini API Key
            enable_search: 是否需要搜索功能（需要 v1beta）

        Returns:
            最佳模型名称
        """
        # 如果需要搜索，使用 v1beta
        api_version = "v1beta" if enable_search else "v1"

        models = GeminiModelSelector.list_models(api_key, api_version)

        if not models:
            logger.warning("[GeminiModelSelector] 无法获取模型列表，使用默认模型")
            return "gemini-2.5-flash"

        # 提取模型名称（去掉 "models/" 前缀）
        available_names = []
        for model in models:
            name = model.get("name", "")
            if name.startswith("models/"):
                name = name[7:]  # 去掉 "models/" 前缀
            available_names.append(name)

        logger.info(f"[GeminiModelSelector] 可用模型: {', '.join(available_names[:5])}...")

        # 按优先级选择
        for preferred in GeminiModelSelector.MODEL_PRIORITY:
            if preferred in available_names:
                logger.info(f"[GeminiModelSelector] 选择模型: {preferred}")
                return preferred

        # 如果没有匹配的优先模型，选择第一个可用的
        if available_names:
            selected = available_names[0]
            logger.info(f"[GeminiModelSelector] 使用第一个可用模型: {selected}")
            return selected

        # 兜底
        logger.warning("[GeminiModelSelector] 未找到合适模型，使用默认")
        return "gemini-2.5-flash"
