"""
项目注册表

负责管理项目元数据以及项目目录初始化。
"""

import logging
import re
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


class ProjectRegistry:
    """项目元数据注册表"""

    def __init__(self, workspace_root: str):
        self.workspace_root = Path(workspace_root).expanduser().resolve()
        self.state_path = self.workspace_root / 'state' / 'projects.json'
        self.projects_root = self.workspace_root / 'projects'
        self.store = JsonStateStore(str(self.state_path))
        self.projects_root.mkdir(parents=True, exist_ok=True)

    def list_projects(self, user_id: str, chat_id: str = '') -> List[dict]:
        visible: List[dict] = []
        for project in self.store.read_list():
            if self._is_visible(project, user_id=user_id, chat_id=chat_id):
                visible.append(project)
        return sorted(visible, key=lambda item: item.get('updated_at', ''), reverse=True)

    def get_project(self, project_id: str) -> Optional[dict]:
        for project in self.store.read_list():
            if project.get('project_id') == project_id:
                return project
        return None

    def resolve_project(self, name_or_id: str, user_id: str, chat_id: str = '') -> Optional[dict]:
        key = (name_or_id or '').strip()
        if not key:
            return None

        visible = self.list_projects(user_id=user_id, chat_id=chat_id)
        for project in visible:
            if project.get('project_id') == key:
                return project

        lowered = key.lower()
        for project in visible:
            if str(project.get('name', '')).lower() == lowered:
                return project
        return None

    def create_project(
        self,
        name: str,
        kind: str,
        owner_user_id: str,
        owner_chat_id: str = '',
        source_type: str = 'empty',
        source_path: str = '',
    ) -> dict:
        normalized_name = (name or '').strip()
        if not normalized_name:
            raise ValueError('项目名称不能为空')

        existing = self.resolve_project(
            normalized_name,
            user_id=owner_user_id,
            chat_id=owner_chat_id,
        )
        if existing:
            raise ValueError(f'项目已存在：{normalized_name}')

        project_slug = _slugify(normalized_name, 'project')
        owner_slug = _slugify(owner_chat_id or owner_user_id or 'owner', 'owner')
        base_id = f'proj_{owner_slug}_{project_slug}'
        project_id = self._dedupe_project_id(base_id)

        repo_path = self._resolve_repo_path(project_id, source_type, source_path)
        if source_type == 'empty':
            Path(repo_path).mkdir(parents=True, exist_ok=True)

        now = _utc_now()
        project = {
            'project_id': project_id,
            'name': normalized_name,
            'kind': kind or 'personal',
            'owner_user_id': owner_user_id or '',
            'owner_chat_id': owner_chat_id or '',
            'source_type': source_type or 'empty',
            'source_path': str(Path(source_path).expanduser().resolve()) if source_path else '',
            'repo_path': repo_path,
            'created_at': now,
            'updated_at': now,
        }

        self.store.update_list(lambda rows: rows.append(project))
        logger.info(
            '[ProjectRegistry] 创建项目: project_id=%s, name=%s, kind=%s',
            project_id,
            normalized_name,
            kind,
        )
        return project

    def touch_project(self, project_id: str) -> Optional[dict]:
        now = _utc_now()

        def updater(rows: List[dict]) -> Optional[dict]:
            for row in rows:
                if row.get('project_id') == project_id:
                    row['updated_at'] = now
                    return row
            return None

        return self.store.update_list(updater)

    @staticmethod
    def _is_visible(project: dict, user_id: str, chat_id: str = '') -> bool:
        if project.get('owner_user_id') == user_id:
            return True
        if chat_id and project.get('owner_chat_id') == chat_id:
            return True
        return False

    def _resolve_repo_path(self, project_id: str, source_type: str, source_path: str) -> str:
        if source_type == 'local_path':
            source = Path(source_path).expanduser().resolve()
            if not source.exists():
                raise ValueError(f'项目源目录不存在: {source}')
            if not source.is_dir():
                raise ValueError(f'项目源目录不是文件夹: {source}')
            return str(source)
        return str((self.projects_root / project_id / 'repo').resolve())

    def _dedupe_project_id(self, base_id: str) -> str:
        project_ids = {row.get('project_id', '') for row in self.store.read_list()}
        if base_id not in project_ids:
            return base_id
        index = 2
        while f'{base_id}_{index}' in project_ids:
            index += 1
        return f'{base_id}_{index}'
