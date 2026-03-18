"""
工作区管理器

负责 personal / shared workspace 的创建、定位与初始化。
"""

import logging
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from .json_state_store import JsonStateStore

logger = logging.getLogger(__name__)


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def _slugify(value: str, fallback: str = 'item') -> str:
    slug = re.sub(r'[^a-zA-Z0-9._-]+', '-', (value or '').strip()).strip('-_.').lower()
    return slug or fallback


class WorkspaceManager:
    """工作区元数据与目录初始化管理器"""

    def __init__(self, workspace_root: str, workspace_strategy: str = 'copy'):
        self.workspace_root = Path(workspace_root).expanduser().resolve()
        self.workspace_strategy = workspace_strategy or 'copy'
        self.state_path = self.workspace_root / 'state' / 'workspaces.json'
        self.projects_root = self.workspace_root / 'projects'
        self.store = JsonStateStore(str(self.state_path))
        self.projects_root.mkdir(parents=True, exist_ok=True)

    def get_workspace(self, workspace_id: str) -> Optional[dict]:
        for workspace in self.store.read_list():
            if workspace.get('workspace_id') == workspace_id:
                return workspace
        return None

    def list_workspaces(self, project_id: str) -> List[dict]:
        return [
            workspace
            for workspace in self.store.read_list()
            if workspace.get('project_id') == project_id
        ]

    def get_or_create_personal_workspace(self, project: dict, user_id: str) -> dict:
        owner_slug = _slugify(user_id, 'user')
        project_slug = _slugify(project.get('project_id') or project.get('name'), 'project')
        workspace_id = f'ws_{project_slug}_{owner_slug}'

        existing = self.get_workspace(workspace_id)
        if existing:
            self.touch_workspace(workspace_id)
            return existing

        return self._create_workspace(
            project=project,
            workspace_id=workspace_id,
            workspace_type='personal',
            owner_user_id=user_id,
            owner_chat_id='',
        )

    def get_or_create_shared_workspace(self, project: dict, chat_id: str) -> dict:
        owner_slug = _slugify(chat_id, 'group')
        project_slug = _slugify(project.get('project_id') or project.get('name'), 'project')
        workspace_id = f'ws_{project_slug}_shared_{owner_slug}'

        existing = self.get_workspace(workspace_id)
        if existing:
            self.touch_workspace(workspace_id)
            return existing

        return self._create_workspace(
            project=project,
            workspace_id=workspace_id,
            workspace_type='shared',
            owner_user_id='',
            owner_chat_id=chat_id,
        )

    def touch_workspace(self, workspace_id: str) -> Optional[dict]:
        now = _utc_now()

        def updater(rows: List[dict]) -> Optional[dict]:
            for row in rows:
                if row.get('workspace_id') == workspace_id:
                    row['updated_at'] = now
                    return row
            return None

        return self.store.update_list(updater)

    def _create_workspace(
        self,
        project: dict,
        workspace_id: str,
        workspace_type: str,
        owner_user_id: str,
        owner_chat_id: str,
    ) -> dict:
        workspace_path = (
            self.projects_root / project['project_id'] / 'workspaces' / workspace_id
        ).resolve()
        workspace_path.mkdir(parents=True, exist_ok=True)

        source_path_value = project.get('repo_path') or project.get('source_path') or ''
        source_path = Path(source_path_value).expanduser().resolve() if source_path_value else None
        if self.workspace_strategy == 'copy' and source_path and source_path.exists():
            self._initialize_workspace_copy(source_path, workspace_path)

        now = _utc_now()
        workspace = {
            'workspace_id': workspace_id,
            'project_id': project['project_id'],
            'workspace_type': workspace_type,
            'owner_user_id': owner_user_id,
            'owner_chat_id': owner_chat_id,
            'path': str(workspace_path),
            'branch_name': _slugify(owner_user_id or owner_chat_id or workspace_type, workspace_type),
            'created_at': now,
            'updated_at': now,
        }

        self.store.update_list(lambda rows: rows.append(workspace))
        logger.info(
            '[WorkspaceManager] 创建工作区: workspace_id=%s, project_id=%s, path=%s',
            workspace_id,
            project['project_id'],
            workspace_path,
        )
        return workspace

    def _initialize_workspace_copy(self, source_path: Path, workspace_path: Path) -> None:
        if any(workspace_path.iterdir()):
            return

        ignored_names = {'.codex_data', '__pycache__', '.pytest_cache'}
        if self.workspace_root.parent == source_path:
            ignored_names.add(self.workspace_root.name)

        for child in source_path.iterdir():
            if child.name in ignored_names:
                continue
            if child.resolve() == workspace_path:
                continue
            target = workspace_path / child.name
            if child.is_dir():
                shutil.copytree(
                    child,
                    target,
                    dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns(*sorted(ignored_names)),
                )
            else:
                shutil.copy2(child, target)
