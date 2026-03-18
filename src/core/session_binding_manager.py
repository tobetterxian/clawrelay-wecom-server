"""
会话绑定管理器

负责持久化 session -> project / workspace / codex thread 绑定关系。
"""

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from .json_state_store import JsonStateStore

logger = logging.getLogger(__name__)


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


class SessionBindingManager:
    """会话绑定持久化管理器"""

    def __init__(self, workspace_root: str, session_timeout_seconds: int = 7200):
        self.session_timeout_seconds = max(int(session_timeout_seconds or 7200), 0)
        self.store = JsonStateStore(
            str(Path(workspace_root).expanduser().resolve() / 'state' / 'sessions.json')
        )

    def get_binding(self, bot_key: str, session_key: str) -> Optional[dict]:
        now = datetime.now(timezone.utc)
        changed = False
        found = None

        def updater(rows: List[dict]) -> Optional[dict]:
            nonlocal changed, found
            for row in rows:
                if row.get('bot_key') != bot_key or row.get('session_key') != session_key:
                    continue
                found = row
                last_active = row.get('last_active', '')
                if self.session_timeout_seconds > 0 and last_active:
                    try:
                        last_dt = datetime.fromisoformat(last_active.replace('Z', '+00:00'))
                    except Exception:
                        last_dt = None
                    if last_dt and (now - last_dt).total_seconds() > self.session_timeout_seconds:
                        if row.get('codex_thread_id'):
                            row['codex_thread_id'] = ''
                            row['updated_at'] = _utc_now()
                            changed = True
                return row
            return None

        result = self.store.update_list(updater)
        if changed:
            logger.info(
                '[SessionBinding] 会话线程已超时并清空: bot=%s, session_key=%s',
                bot_key,
                session_key,
            )
        return result or found

    def bind_session(
        self,
        bot_key: str,
        session_key: str,
        project_id: str,
        workspace_id: str = '',
        mode: str = 'personal_workspace',
        keep_thread: bool = False,
    ) -> dict:
        now = _utc_now()

        def updater(rows: List[dict]) -> dict:
            for row in rows:
                if row.get('bot_key') == bot_key and row.get('session_key') == session_key:
                    same_target = (
                        row.get('project_id') == project_id
                        and row.get('workspace_id', '') == (workspace_id or '')
                        and row.get('mode', '') == mode
                    )
                    row['project_id'] = project_id
                    row['workspace_id'] = workspace_id or ''
                    row['mode'] = mode
                    row['last_active'] = now
                    row['updated_at'] = now
                    if not keep_thread and not same_target:
                        row['codex_thread_id'] = ''
                    return row

            row = {
                'bot_key': bot_key,
                'session_key': session_key,
                'project_id': project_id,
                'workspace_id': workspace_id or '',
                'codex_thread_id': '',
                'mode': mode,
                'last_active': now,
                'created_at': now,
                'updated_at': now,
            }
            rows.append(row)
            return row

        binding = self.store.update_list(updater)
        logger.info(
            '[SessionBinding] 绑定会话: bot=%s, session_key=%s, project_id=%s, workspace_id=%s, mode=%s',
            bot_key,
            session_key,
            project_id,
            workspace_id,
            mode,
        )
        return binding

    def save_thread_id(self, bot_key: str, session_key: str, thread_id: str) -> Optional[dict]:
        now = _utc_now()

        def updater(rows: List[dict]) -> Optional[dict]:
            for row in rows:
                if row.get('bot_key') == bot_key and row.get('session_key') == session_key:
                    row['codex_thread_id'] = thread_id or ''
                    row['last_active'] = now
                    row['updated_at'] = now
                    return row
            return None

        return self.store.update_list(updater)

    def touch_binding(self, bot_key: str, session_key: str) -> Optional[dict]:
        now = _utc_now()

        def updater(rows: List[dict]) -> Optional[dict]:
            for row in rows:
                if row.get('bot_key') == bot_key and row.get('session_key') == session_key:
                    row['last_active'] = now
                    row['updated_at'] = now
                    return row
            return None

        return self.store.update_list(updater)

    def clear_thread(self, bot_key: str, session_key: str) -> Optional[dict]:
        now = _utc_now()

        def updater(rows: List[dict]) -> Optional[dict]:
            for row in rows:
                if row.get('bot_key') == bot_key and row.get('session_key') == session_key:
                    row['codex_thread_id'] = ''
                    row['last_active'] = now
                    row['updated_at'] = now
                    return row
            return None

        return self.store.update_list(updater)

    def clear_binding(self, bot_key: str, session_key: str) -> None:
        def updater(rows: List[dict]) -> None:
            rows[:] = [
                row
                for row in rows
                if not (row.get('bot_key') == bot_key and row.get('session_key') == session_key)
            ]

        self.store.update_list(updater)
