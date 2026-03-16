"""
编排器工厂模块

根据 bot_type 创建对应的 Orchestrator 实例。
"""

import logging
from typing import Optional

from config.bot_config import BotConfig
from .base_orchestrator import BaseOrchestrator
from .claude_relay_orchestrator import ClaudeRelayOrchestrator

logger = logging.getLogger(__name__)


class OrchestratorFactory:
    """编排器工厂

    根据 bot_type 创建对应的 Orchestrator 实例。
    """

    @staticmethod
    def create(bot_config: BotConfig) -> BaseOrchestrator:
        """创建编排器实例

        Args:
            bot_config: 机器人配置

        Returns:
            BaseOrchestrator: 编排器实例

        Raises:
            ValueError: 不支持的 bot_type
            ImportError: 缺少必要的依赖包
        """
        bot_type = bot_config.bot_type or "claude_code"

        logger.info(
            f"[OrchestratorFactory] 创建编排器: bot_key={bot_config.bot_key}, "
            f"bot_type={bot_type}"
        )

        if bot_type == "claude_code":
            return ClaudeRelayOrchestrator(
                bot_key=bot_config.bot_key,
                relay_url=bot_config.relay_url or "http://localhost:50009",
                working_dir=bot_config.working_dir or "",
                model=bot_config.model or "",
                system_prompt=bot_config.system_prompt or "",
                env_vars=bot_config.env_vars or None,
            )

        elif bot_type == "gemini":
            from .gemini_orchestrator import GeminiOrchestrator

            provider_config = bot_config.provider_config or {}
            api_key = provider_config.get("api_key")
            if not api_key:
                raise ValueError(
                    f"Gemini 机器人 {bot_config.bot_key} 缺少 provider_config.api_key"
                )

            return GeminiOrchestrator(
                bot_key=bot_config.bot_key,
                api_key=api_key,
                model=bot_config.model or "",  # 空字符串表示自动选择
                system_prompt=bot_config.system_prompt or "",
                enable_search=provider_config.get("enable_search", False),
            )

        elif bot_type == "openai":
            from .openai_orchestrator import OpenAIOrchestrator

            provider_config = bot_config.provider_config or {}
            api_key = provider_config.get("api_key")
            if not api_key:
                raise ValueError(
                    f"OpenAI 机器人 {bot_config.bot_key} 缺少 provider_config.api_key"
                )

            base_url = provider_config.get("base_url")

            return OpenAIOrchestrator(
                bot_key=bot_config.bot_key,
                api_key=api_key,
                model=bot_config.model or "",  # 空字符串表示自动选择
                system_prompt=bot_config.system_prompt or "",
                base_url=base_url,
            )

        else:
            raise ValueError(
                f"不支持的 bot_type: {bot_type}，支持的类型: "
                f"claude_code, gemini, openai"
            )
