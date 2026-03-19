"""
GitHub 仓库管理器

负责通过 GitHub API 或本地 gh CLI 列出当前账号 / 组织下的仓库，
供对话式选择与项目派生使用。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

DEFAULT_GITHUB_LIST_LIMIT = 10
GITHUB_API_BASE = "https://api.github.com"


@dataclass
class GitHubRepositoryInfo:
    full_name: str
    name: str
    owner: str
    private: bool
    default_branch: str
    updated_at: str
    description: str
    clone_url: str
    ssh_url: str
    html_url: str

    @property
    def preferred_clone_url(self) -> str:
        return self.ssh_url or self.clone_url


class GitHubRepositoryManager:
    def __init__(self, env_vars: Optional[Dict[str, str]] = None, gh_executable: str = "gh"):
        self.env_vars = dict(env_vars or {})
        self.gh_executable = gh_executable

    def list_user_repositories(
        self,
        query: str = "",
        limit: int = DEFAULT_GITHUB_LIST_LIMIT,
    ) -> List[GitHubRepositoryInfo]:
        repositories = self._request_repositories(
            endpoint="/user/repos",
            query_params={
                "sort": "updated",
                "per_page": str(max(1, limit)),
                "affiliation": "owner,organization_member,collaborator",
            },
        )
        return self._filter_and_normalize_repositories(repositories, query=query, limit=limit)

    def list_org_repositories(
        self,
        org: str,
        query: str = "",
        limit: int = DEFAULT_GITHUB_LIST_LIMIT,
    ) -> List[GitHubRepositoryInfo]:
        normalized_org = str(org or "").strip()
        if not normalized_org:
            raise ValueError("GitHub 组织名不能为空")

        repositories = self._request_repositories(
            endpoint=f"/orgs/{normalized_org}/repos",
            query_params={
                "sort": "updated",
                "type": "all",
                "per_page": str(max(1, limit)),
            },
        )
        return self._filter_and_normalize_repositories(repositories, query=query, limit=limit)

    def _filter_and_normalize_repositories(
        self,
        repositories: List[dict],
        query: str = "",
        limit: int = DEFAULT_GITHUB_LIST_LIMIT,
    ) -> List[GitHubRepositoryInfo]:
        normalized_query = str(query or "").strip().lower()
        results: List[GitHubRepositoryInfo] = []
        for item in repositories or []:
            repository = self._normalize_repository(item)
            if not repository:
                continue
            if normalized_query:
                haystack = " ".join(
                    [
                        repository.full_name.lower(),
                        repository.name.lower(),
                        repository.description.lower(),
                    ]
                )
                if normalized_query not in haystack:
                    continue
            results.append(repository)
            if len(results) >= max(1, limit):
                break
        return results

    def _normalize_repository(self, payload: dict) -> Optional[GitHubRepositoryInfo]:
        if not isinstance(payload, dict):
            return None

        full_name = str(payload.get("full_name") or "").strip()
        name = str(payload.get("name") or "").strip()
        owner = str((payload.get("owner") or {}).get("login") or "").strip()
        if not full_name or not name:
            return None

        return GitHubRepositoryInfo(
            full_name=full_name,
            name=name,
            owner=owner,
            private=bool(payload.get("private")),
            default_branch=str(payload.get("default_branch") or "").strip(),
            updated_at=str(payload.get("updated_at") or "").strip(),
            description=str(payload.get("description") or "").strip(),
            clone_url=str(payload.get("clone_url") or "").strip(),
            ssh_url=str(payload.get("ssh_url") or "").strip(),
            html_url=str(payload.get("html_url") or "").strip(),
        )

    def _request_repositories(self, endpoint: str, query_params: Dict[str, str]) -> List[dict]:
        token = self._resolve_token()
        if token:
            return self._request_repositories_via_http(endpoint, query_params, token)
        if self._gh_available():
            return self._request_repositories_via_gh(endpoint, query_params)
        raise RuntimeError("未检测到 GitHub 凭证，请配置 GITHUB_TOKEN / GH_TOKEN 或先执行 gh auth login")

    def _request_repositories_via_http(
        self,
        endpoint: str,
        query_params: Dict[str, str],
        token: str,
    ) -> List[dict]:
        query = urlencode(query_params or {})
        url = f"{GITHUB_API_BASE}{endpoint}"
        if query:
            url = f"{url}?{query}"

        request = Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "User-Agent": "clawrelay-wecom-server",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with urlopen(request, timeout=15) as response:
                data = json.loads(response.read().decode("utf-8") or "[]")
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="ignore") if hasattr(exc, "read") else ""
            raise RuntimeError(f"GitHub API 请求失败（HTTP {exc.code}）：{body or exc.reason}") from exc
        except URLError as exc:
            raise RuntimeError(f"GitHub API 网络请求失败：{exc}") from exc

        if not isinstance(data, list):
            raise RuntimeError("GitHub API 返回格式异常")
        return data

    def _request_repositories_via_gh(
        self,
        endpoint: str,
        query_params: Dict[str, str],
    ) -> List[dict]:
        query = urlencode(query_params or {})
        path = endpoint.lstrip("/")
        api_target = f"{path}?{query}" if query else path
        env = os.environ.copy()
        env.update({key: value for key, value in self.env_vars.items() if value is not None})

        completed = subprocess.run(
            [self.gh_executable, "api", api_target],
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )
        if completed.returncode != 0:
            output = (completed.stderr or completed.stdout or "").strip()
            raise RuntimeError(f"gh api 调用失败：{output or 'unknown error'}")

        try:
            data = json.loads(completed.stdout or "[]")
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"gh api 返回了无法解析的 JSON：{exc}") from exc

        if not isinstance(data, list):
            raise RuntimeError("gh api 返回格式异常")
        return data

    def _resolve_token(self) -> str:
        for key in ("GITHUB_TOKEN", "GH_TOKEN"):
            value = str(self.env_vars.get(key) or os.environ.get(key) or "").strip()
            if value:
                return value
        return ""

    def _gh_available(self) -> bool:
        return shutil.which(self.gh_executable) is not None
