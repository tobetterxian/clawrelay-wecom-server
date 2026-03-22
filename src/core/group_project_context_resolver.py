"""
群项目上下文解析器

为非 codex_cli 机器人提供“当前群项目”只读上下文。
数据来源于 codex_cli 机器人的项目注册表与会话绑定状态。
"""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

from config.bot_config import BotConfig

from .project_registry import ProjectRegistry
from .session_binding_manager import SessionBindingManager
from .workspace_manager import WorkspaceManager

logger = logging.getLogger(__name__)

_DEFAULT_CODEX_CLI_WORKSPACE_ROOT_NAME = ".codex_data"


@dataclass
class GroupProjectContext:
    source_bot_key: str
    workspace_root: str
    project_id: str
    project_name: str
    project_kind: str
    project_root: str
    mode: str
    workspace_id: str
    workspace_type: str
    workspace_path: str
    source_type: str
    git_remote_url: str
    publish_git_remote_url: str
    deployment_type: str


class _WorkspaceContextStore:
    def __init__(self, workspace_root: str):
        self.workspace_root = str(Path(workspace_root).expanduser().resolve())
        self.project_registry = ProjectRegistry(self.workspace_root)
        self.workspace_manager = WorkspaceManager(self.workspace_root)
        self.binding_manager = SessionBindingManager(self.workspace_root)
        self.bot_keys: set[str] = set()


class GroupProjectContextResolver:
    """跨机器人解析群项目上下文。"""

    def __init__(self, stores: Dict[str, _WorkspaceContextStore]):
        self._stores = stores

    @classmethod
    def from_bot_configs(cls, bot_configs: Dict[str, BotConfig]) -> "GroupProjectContextResolver":
        stores: Dict[str, _WorkspaceContextStore] = {}
        for bot_key, bot_config in (bot_configs or {}).items():
            if not cls._is_project_context_source(bot_config):
                continue
            workspace_root = cls._resolve_workspace_root(bot_config)
            if not workspace_root:
                continue
            store = stores.get(workspace_root)
            if store is None:
                store = _WorkspaceContextStore(workspace_root)
                stores[workspace_root] = store
            store.bot_keys.add(bot_key)
        return cls(stores)

    @staticmethod
    def _is_truthy(value) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on", "enabled"}
        return False

    @classmethod
    def _is_project_context_source(cls, bot_config: BotConfig) -> bool:
        if (bot_config.bot_type or "").strip() != "codex_cli":
            return False
        provider_config = bot_config.provider_config or {}
        enabled = provider_config.get("enable_project_workspace_mode")
        if enabled is None:
            return True
        return cls._is_truthy(enabled)

    @staticmethod
    def _resolve_workspace_root(bot_config: BotConfig) -> str:
        provider_config = bot_config.provider_config or {}
        configured_root = str(provider_config.get("workspace_root") or "").strip()
        if configured_root:
            return str(Path(configured_root).expanduser().resolve())
        working_dir = str(bot_config.working_dir or "").strip()
        if not working_dir:
            return ""
        return str(
            (Path(working_dir).expanduser().resolve() / _DEFAULT_CODEX_CLI_WORKSPACE_ROOT_NAME).resolve()
        )

    def has_sources(self) -> bool:
        return bool(self._stores)

    def resolve(self, bot_config: BotConfig, chat_id: str, user_id: str) -> Optional[GroupProjectContext]:
        if not chat_id or not user_id or not self._stores:
            return None

        provider_config = bot_config.provider_config or {}
        preferred_bot_key = str(provider_config.get("group_project_context_bot_key") or "").strip()
        preferred_workspace_root = str(
            provider_config.get("group_project_context_workspace_root") or ""
        ).strip()

        candidate_stores = self._candidate_stores(
            preferred_workspace_root=preferred_workspace_root,
            preferred_bot_key=preferred_bot_key,
        )

        best_context: Optional[GroupProjectContext] = None
        best_updated_at = ""

        for store in candidate_stores:
            bot_keys = self._ordered_bot_keys(store, preferred_bot_key)
            for source_bot_key in bot_keys:
                binding = store.binding_manager.get_binding(source_bot_key, chat_id)
                if not binding or not binding.get("project_id"):
                    continue

                project = store.project_registry.get_project(binding.get("project_id", ""))
                if not project:
                    continue

                workspace = self._resolve_workspace(
                    store=store,
                    project=project,
                    binding=binding,
                    chat_id=chat_id,
                    user_id=user_id,
                )
                if not workspace:
                    continue

                context = GroupProjectContext(
                    source_bot_key=source_bot_key,
                    workspace_root=store.workspace_root,
                    project_id=str(project.get("project_id") or ""),
                    project_name=str(project.get("name") or ""),
                    project_kind=str(project.get("kind") or ""),
                    project_root=str(project.get("project_root") or ""),
                    mode=str(binding.get("mode") or "personal_workspace"),
                    workspace_id=str(workspace.get("workspace_id") or ""),
                    workspace_type=str(workspace.get("workspace_type") or ""),
                    workspace_path=str(workspace.get("path") or ""),
                    source_type=str(project.get("source_type") or ""),
                    git_remote_url=str(
                        project.get("source_git_remote_url")
                        or project.get("git_remote_url")
                        or project.get("upstream_remote_url")
                        or ""
                    ),
                    publish_git_remote_url=str(project.get("publish_git_remote_url") or ""),
                    deployment_type=str(project.get("deployment_type") or ""),
                )
                updated_at = str(
                    binding.get("updated_at")
                    or binding.get("last_active")
                    or project.get("updated_at")
                    or ""
                )
                if best_context is None or updated_at > best_updated_at:
                    best_context = context
                    best_updated_at = updated_at

        return best_context

    def _candidate_stores(
        self,
        preferred_workspace_root: str = "",
        preferred_bot_key: str = "",
    ) -> list[_WorkspaceContextStore]:
        if preferred_workspace_root:
            normalized_root = str(Path(preferred_workspace_root).expanduser().resolve())
            store = self._stores.get(normalized_root)
            return [store] if store else []

        candidates = list(self._stores.values())
        if preferred_bot_key:
            prioritized = [store for store in candidates if preferred_bot_key in store.bot_keys]
            remaining = [store for store in candidates if preferred_bot_key not in store.bot_keys]
            return prioritized + remaining
        return candidates

    @staticmethod
    def _ordered_bot_keys(store: _WorkspaceContextStore, preferred_bot_key: str = "") -> list[str]:
        bot_keys = sorted(store.bot_keys)
        if preferred_bot_key and preferred_bot_key in store.bot_keys:
            return [preferred_bot_key] + [bot_key for bot_key in bot_keys if bot_key != preferred_bot_key]
        return bot_keys

    @staticmethod
    def _resolve_workspace(
        store: _WorkspaceContextStore,
        project: dict,
        binding: dict,
        chat_id: str,
        user_id: str,
    ) -> Optional[dict]:
        mode = str(binding.get("mode") or "personal_workspace")
        try:
            if mode == "shared_workspace":
                return store.workspace_manager.get_or_create_shared_workspace(project, chat_id)
            return store.workspace_manager.get_or_create_personal_workspace(project, user_id)
        except Exception as exc:
            logger.warning(
                "[GroupProjectContext] 解析工作区失败: workspace_root=%s, project_id=%s, mode=%s, error=%s",
                store.workspace_root,
                project.get("project_id", ""),
                mode,
                exc,
            )
            return None
