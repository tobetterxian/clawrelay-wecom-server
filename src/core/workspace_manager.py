"""
工作区管理器

负责 personal / shared workspace 的创建、定位与初始化。
"""

import logging
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from .json_state_store import JsonStateStore
from .workspace_init_modes import (
    DEFAULT_WORKSPACE_INIT_MODE,
    WORKSPACE_INIT_GIT_REMOTE,
    WORKSPACE_INIT_LEGACY_COPY,
    infer_project_workspace_init_mode,
)

logger = logging.getLogger(__name__)


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def _slugify(value: str, fallback: str = 'item') -> str:
    slug = re.sub(r'[^a-zA-Z0-9._-]+', '-', (value or '').strip()).strip('-_.').lower()
    return slug or fallback


class WorkspaceManager:
    """工作区元数据与目录初始化管理器"""

    def __init__(
        self,
        workspace_root: str,
        workspace_strategy: str = '',
        default_workspace_init_mode: str = DEFAULT_WORKSPACE_INIT_MODE,
    ):
        self.workspace_root = Path(workspace_root).expanduser().resolve()
        self.workspace_strategy = workspace_strategy or ''
        self.default_workspace_init_mode = infer_project_workspace_init_mode(
            {'workspace_init_mode': default_workspace_init_mode or workspace_strategy},
            fallback=DEFAULT_WORKSPACE_INIT_MODE,
        )
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

        init_mode = infer_project_workspace_init_mode(
            project,
            fallback=self.default_workspace_init_mode,
        )
        try:
            self._initialize_workspace(project, workspace_path, init_mode)
        except Exception:
            shutil.rmtree(workspace_path, ignore_errors=True)
            raise

        now = _utc_now()
        workspace = {
            'workspace_id': workspace_id,
            'project_id': project['project_id'],
            'workspace_type': workspace_type,
            'owner_user_id': owner_user_id,
            'owner_chat_id': owner_chat_id,
            'path': str(workspace_path),
            'branch_name': _slugify(owner_user_id or owner_chat_id or workspace_type, workspace_type),
            'init_mode': init_mode,
            'created_at': now,
            'updated_at': now,
        }

        self.store.update_list(lambda rows: rows.append(workspace))
        logger.info(
            '[WorkspaceManager] 创建工作区: workspace_id=%s, project_id=%s, init_mode=%s, path=%s',
            workspace_id,
            project['project_id'],
            init_mode,
            workspace_path,
        )
        return workspace

    def _initialize_workspace(self, project: dict, workspace_path: Path, init_mode: str) -> None:
        if init_mode == WORKSPACE_INIT_GIT_REMOTE:
            self._initialize_workspace_git_remote(
                str(project.get('git_remote_url') or '').strip(),
                workspace_path,
            )
            return
        if init_mode == WORKSPACE_INIT_LEGACY_COPY:
            source_path_value = str(
                project.get('source_path') or project.get('repo_path') or ''
            ).strip()
            if not source_path_value:
                raise ValueError('兼容复制模式缺少 source_path 配置')
            self._initialize_workspace_copy(
                Path(source_path_value).expanduser().resolve(),
                workspace_path,
            )

    def _initialize_workspace_git_remote(self, remote_url: str, workspace_path: Path) -> None:
        if any(workspace_path.iterdir()):
            return
        if not remote_url:
            raise ValueError('远程 Git 工作区初始化缺少仓库地址')

        result = subprocess.run(
            ['git', 'clone', remote_url, str(workspace_path)],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            error_message = (result.stderr or result.stdout or '').strip() or 'git clone failed'
            raise RuntimeError(f'远程 Git 工作区初始化失败: {error_message}')

    def _initialize_workspace_copy(self, source_path: Path, workspace_path: Path) -> None:
        if any(workspace_path.iterdir()):
            return

        ignored_names = {'.codex', '.codex_data', '__pycache__', '.pytest_cache'}
        if self.workspace_root.parent == source_path:
            ignored_names.add(self.workspace_root.name)

        self._copy_directory_contents(source_path, workspace_path, ignored_names)

    def _copy_directory_contents(
        self,
        source_path: Path,
        workspace_path: Path,
        ignored_names: set[str],
    ) -> None:
        try:
            children = list(source_path.iterdir())
        except OSError as exc:
            logger.warning(
                '[WorkspaceManager] 读取目录失败，已跳过: path=%s, error=%s',
                source_path,
                exc,
            )
            return

        for child in children:
            if child.name in ignored_names or self._is_windows_reserved_name(child.name):
                continue
            if child == workspace_path:
                continue

            target = workspace_path / child.name
            try:
                if child.is_symlink():
                    logger.warning('[WorkspaceManager] 跳过符号链接: %s', child)
                    continue
                if child.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    self._copy_directory_contents(child, target, ignored_names)
                elif child.is_file():
                    shutil.copy2(child, target)
                else:
                    logger.warning('[WorkspaceManager] 跳过特殊文件: %s', child)
            except OSError as exc:
                logger.warning(
                    '[WorkspaceManager] 复制路径失败，已跳过: path=%s, error=%s',
                    child,
                    exc,
                )

    @staticmethod
    def _is_windows_reserved_name(name: str) -> bool:
        reserved = {
            'con',
            'prn',
            'aux',
            'nul',
            'com1',
            'com2',
            'com3',
            'com4',
            'com5',
            'com6',
            'com7',
            'com8',
            'com9',
            'lpt1',
            'lpt2',
            'lpt3',
            'lpt4',
            'lpt5',
            'lpt6',
            'lpt7',
            'lpt8',
            'lpt9',
        }
        stem = (name or '').strip().split('.')[0].lower()
        return stem in reserved
