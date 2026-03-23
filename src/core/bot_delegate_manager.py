"""
跨机器人运行时委托管理器

用于在同一服务进程内查找和复用其它机器人的 Orchestrator，
避免通过“群里再 @ 另一个机器人”的方式做二次调用。
"""

from __future__ import annotations

from typing import Dict, Optional, Tuple

from config.bot_config import BotConfig

from .base_orchestrator import BaseOrchestrator


class BotDelegateManager:
    """按 bot_key 管理可复用的运行时编排器。"""

    def __init__(
        self,
        bot_configs: Dict[str, BotConfig],
        prepared_orchestrators: Optional[Dict[str, BaseOrchestrator]] = None,
    ):
        self._bot_configs = dict(bot_configs or {})
        self._orchestrators: Dict[str, BaseOrchestrator] = dict(prepared_orchestrators or {})

    def get_bot_config(self, bot_key: str) -> Optional[BotConfig]:
        return self._bot_configs.get(str(bot_key or "").strip())

    def get_orchestrator(self, bot_key: str) -> Optional[BaseOrchestrator]:
        normalized_bot_key = str(bot_key or "").strip()
        if not normalized_bot_key:
            return None

        orchestrator = self._orchestrators.get(normalized_bot_key)
        if orchestrator is not None:
            return orchestrator

        bot_config = self.get_bot_config(normalized_bot_key)
        if bot_config is None:
            return None

        from .orchestrator_factory import OrchestratorFactory

        orchestrator = OrchestratorFactory.create(bot_config)
        self._orchestrators[normalized_bot_key] = orchestrator
        return orchestrator

    def resolve_codex_cli_delegate(
        self,
        preferred_bot_key: str = "",
    ) -> Optional[Tuple[str, BotConfig, BaseOrchestrator]]:
        normalized_preferred = str(preferred_bot_key or "").strip()
        if normalized_preferred:
            preferred_config = self.get_bot_config(normalized_preferred)
            if preferred_config and str(preferred_config.bot_type or "").strip() == "codex_cli":
                preferred_orchestrator = self.get_orchestrator(normalized_preferred)
                if preferred_orchestrator is not None:
                    return normalized_preferred, preferred_config, preferred_orchestrator

        for bot_key in sorted(self._bot_configs):
            bot_config = self._bot_configs.get(bot_key)
            if bot_config is None:
                continue
            if str(bot_config.bot_type or "").strip() != "codex_cli":
                continue
            orchestrator = self.get_orchestrator(bot_key)
            if orchestrator is not None:
                return bot_key, bot_config, orchestrator
        return None
