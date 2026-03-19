"""
编排器工厂模块

根据 bot_type 创建对应的 Orchestrator 实例。
"""

import logging
from typing import Optional

from config.bot_config import BotConfig
from .base_orchestrator import BaseOrchestrator
from .claude_relay_orchestrator import ClaudeRelayOrchestrator
from .codex_orchestrator import CodexOrchestrator
from .codex_cli_orchestrator import CodexCliOrchestrator
from .workspace_init_modes import DEFAULT_WORKSPACE_INIT_MODE

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

        elif bot_type == "codex":
            provider_config = bot_config.provider_config or {}
            api_key = provider_config.get("api_key")
            if not api_key:
                raise ValueError(
                    f"Codex 机器人 {bot_config.bot_key} 缺少 provider_config.api_key"
                )

            return CodexOrchestrator(
                bot_key=bot_config.bot_key,
                api_key=api_key,
                model=bot_config.model or "",
                system_prompt=bot_config.system_prompt or "",
                base_url=provider_config.get("base_url"),
                reasoning_effort=provider_config.get("reasoning_effort", "medium"),
            )

        elif bot_type == "codex_cli":
            provider_config = bot_config.provider_config or {}
            working_dir = bot_config.working_dir or ""
            if not working_dir:
                raise ValueError(
                    f"Codex CLI 机器人 {bot_config.bot_key} 缺少 working_dir 配置"
                )
            default_workspace_init_mode = (
                provider_config.get("default_workspace_init_mode")
                or provider_config.get("workspace_strategy")
                or DEFAULT_WORKSPACE_INIT_MODE
            )

            return CodexCliOrchestrator(
                bot_key=bot_config.bot_key,
                working_dir=working_dir,
                model=bot_config.model or "",
                system_prompt=bot_config.system_prompt or "",
                env_vars=bot_config.env_vars or None,
                sandbox_mode=provider_config.get("sandbox_mode", "workspace-write"),
                skip_git_repo_check=provider_config.get("skip_git_repo_check", False),
                dangerously_bypass_approvals_and_sandbox=provider_config.get(
                    "dangerously_bypass_approvals_and_sandbox", False
                ),
                add_dirs=provider_config.get("add_dirs") or None,
                profile=provider_config.get("profile", ""),
                executable=provider_config.get("codex_path", "codex"),
                approval_policy=provider_config.get("approval_policy", "on-request"),
                workspace_root=provider_config.get("workspace_root", ""),
                codex_home=provider_config.get("codex_home", ""),
                workspace_strategy=provider_config.get("workspace_strategy", ""),
                default_workspace_init_mode=default_workspace_init_mode,
                default_group_workspace_mode=provider_config.get("default_group_workspace_mode", "personal"),
                session_timeout_seconds=provider_config.get("session_timeout_seconds", 7200),
                enable_project_workspace_mode=provider_config.get("enable_project_workspace_mode", True),
            )

        else:
            raise ValueError(
                f"不支持的 bot_type: {bot_type}，支持的类型: "
                f"claude_code, gemini, openai, codex, codex_cli"
            )
