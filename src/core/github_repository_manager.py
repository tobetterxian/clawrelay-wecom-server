"""
GitHub 仓库管理器

负责通过 GitHub API 或本地 gh CLI 列出 / 创建当前账号或组织下的仓库，
供对话式选择、项目派生与发布使用。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
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


@dataclass
class GitHubWorkflowRunInfo:
    id: int
    name: str
    workflow_name: str
    display_title: str
    status: str
    conclusion: str
    html_url: str
    event: str
    head_branch: str
    head_sha: str
    run_number: int
    created_at: str
    updated_at: str


class GitHubRepositoryManager:
    def __init__(self, env_vars: Optional[Dict[str, str]] = None, gh_executable: str = "gh"):
        self.env_vars = dict(env_vars or {})
        self.gh_executable = gh_executable

    def get_current_user_login(self) -> str:
        data = self._request_json(endpoint="/user", method="GET")
        if not isinstance(data, dict):
            raise RuntimeError("GitHub API 返回格式异常")
        login = str(data.get("login") or "").strip()
        if not login:
            raise RuntimeError("无法识别当前 GitHub 账号")
        return login

    def list_user_repositories(
        self,
        query: str = "",
        limit: int = DEFAULT_GITHUB_LIST_LIMIT,
        owner_only: bool = False,
    ) -> List[GitHubRepositoryInfo]:
        repositories = self._request_repositories(
            endpoint="/user/repos",
            query_params={
                "sort": "updated",
                "per_page": str(max(1, limit)),
                "affiliation": "owner" if owner_only else "owner,organization_member,collaborator",
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

    def create_user_repository(
        self,
        name: str,
        private: bool = True,
        description: str = "",
        auto_init: bool = False,
    ) -> GitHubRepositoryInfo:
        normalized_name = str(name or "").strip()
        if not normalized_name:
            raise ValueError("GitHub 仓库名称不能为空")

        payload = {
            "name": normalized_name,
            "private": bool(private),
            "auto_init": bool(auto_init),
        }
        if str(description or "").strip():
            payload["description"] = str(description).strip()

        data = self._request_json(
            endpoint="/user/repos",
            method="POST",
            payload=payload,
        )
        repository = self._normalize_repository(data)
        if not repository:
            raise RuntimeError("创建 GitHub 仓库成功，但返回数据无法识别")
        return repository

    def create_org_repository(
        self,
        org: str,
        name: str,
        private: bool = True,
        description: str = "",
        auto_init: bool = False,
    ) -> GitHubRepositoryInfo:
        normalized_org = str(org or "").strip()
        normalized_name = str(name or "").strip()
        if not normalized_org:
            raise ValueError("GitHub 组织名不能为空")
        if not normalized_name:
            raise ValueError("GitHub 仓库名称不能为空")

        payload = {
            "name": normalized_name,
            "private": bool(private),
            "auto_init": bool(auto_init),
        }
        if str(description or "").strip():
            payload["description"] = str(description).strip()

        data = self._request_json(
            endpoint=f"/orgs/{normalized_org}/repos",
            method="POST",
            payload=payload,
        )
        repository = self._normalize_repository(data)
        if not repository:
            raise RuntimeError("创建 GitHub 组织仓库成功，但返回数据无法识别")
        return repository

    def get_latest_workflow_run(
        self,
        owner: str,
        repo: str,
        workflow_id: str = "",
    ) -> Optional[GitHubWorkflowRunInfo]:
        normalized_owner = str(owner or "").strip()
        normalized_repo = str(repo or "").strip()
        normalized_workflow_id = str(workflow_id or "").strip()
        if not normalized_owner:
            raise ValueError("GitHub owner 不能为空")
        if not normalized_repo:
            raise ValueError("GitHub repo 不能为空")

        if normalized_workflow_id:
            endpoint = f"/repos/{normalized_owner}/{normalized_repo}/actions/workflows/{normalized_workflow_id}/runs"
        else:
            endpoint = f"/repos/{normalized_owner}/{normalized_repo}/actions/runs"

        data = self._request_json(
            endpoint=endpoint,
            method="GET",
            query_params={"per_page": "1"},
        )
        if not isinstance(data, dict):
            raise RuntimeError("GitHub Actions 返回格式异常")
        runs = data.get("workflow_runs") or []
        if not isinstance(runs, list) or not runs:
            return None
        return self._normalize_workflow_run(runs[0])

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

    def _normalize_workflow_run(self, payload: dict) -> Optional[GitHubWorkflowRunInfo]:
        if not isinstance(payload, dict):
            return None
        run_id = payload.get("id")
        if run_id in (None, ""):
            return None
        try:
            normalized_id = int(run_id)
        except (TypeError, ValueError):
            return None

        run_number = payload.get("run_number")
        try:
            normalized_run_number = int(run_number) if run_number not in (None, "") else 0
        except (TypeError, ValueError):
            normalized_run_number = 0

        return GitHubWorkflowRunInfo(
            id=normalized_id,
            name=str(payload.get("name") or "").strip(),
            workflow_name=str(payload.get("name") or "").strip(),
            display_title=str(payload.get("display_title") or "").strip(),
            status=str(payload.get("status") or "").strip(),
            conclusion=str(payload.get("conclusion") or "").strip(),
            html_url=str(payload.get("html_url") or "").strip(),
            event=str(payload.get("event") or "").strip(),
            head_branch=str(payload.get("head_branch") or "").strip(),
            head_sha=str(payload.get("head_sha") or "").strip(),
            run_number=normalized_run_number,
            created_at=str(payload.get("created_at") or "").strip(),
            updated_at=str(payload.get("updated_at") or "").strip(),
        )

    def _request_repositories(self, endpoint: str, query_params: Dict[str, str]) -> List[dict]:
        data = self._request_json(endpoint=endpoint, query_params=query_params, method="GET")
        if not isinstance(data, list):
            raise RuntimeError("GitHub API 返回格式异常")
        return data

    def _request_json(
        self,
        endpoint: str,
        query_params: Optional[Dict[str, str]] = None,
        method: str = "GET",
        payload: Optional[Dict[str, object]] = None,
    ):
        token = self._resolve_token()
        if token:
            return self._request_json_via_http(endpoint, query_params or {}, token, method=method, payload=payload)
        if self._gh_available():
            return self._request_json_via_gh(endpoint, query_params or {}, method=method, payload=payload)
        raise RuntimeError("未检测到 GitHub 凭证，请配置 GITHUB_TOKEN / GH_TOKEN 或先执行 gh auth login")

    def _request_json_via_http(
        self,
        endpoint: str,
        query_params: Dict[str, str],
        token: str,
        method: str = "GET",
        payload: Optional[Dict[str, object]] = None,
    ):
        query = urlencode(query_params or {})
        url = f"{GITHUB_API_BASE}{endpoint}"
        if query:
            url = f"{url}?{query}"

        data_bytes = None
        if payload is not None:
            data_bytes = json.dumps(payload).encode("utf-8")

        request = Request(
            url,
            method=method,
            data=data_bytes,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "User-Agent": "clawrelay-wecom-server",
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/json",
            },
        )
        try:
            with urlopen(request, timeout=15) as response:
                response_text = response.read().decode("utf-8") or "null"
                data = json.loads(response_text)
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="ignore") if hasattr(exc, "read") else ""
            raise RuntimeError(
                self._format_http_error(
                    endpoint=endpoint,
                    method=method,
                    status_code=int(exc.code),
                    body=body,
                    reason=str(exc.reason or "").strip(),
                    headers=getattr(exc, "headers", None),
                )
            ) from exc
        except URLError as exc:
            raise RuntimeError(f"GitHub API 网络请求失败：{exc}") from exc

        return data

    def _request_json_via_gh(
        self,
        endpoint: str,
        query_params: Dict[str, str],
        method: str = "GET",
        payload: Optional[Dict[str, object]] = None,
    ):
        query = urlencode(query_params or {})
        path = endpoint.lstrip("/")
        api_target = f"{path}?{query}" if query else path
        env = os.environ.copy()
        env.update({key: value for key, value in self.env_vars.items() if value is not None})

        command = [self.gh_executable, "api"]
        if method and method.upper() != "GET":
            command.extend(["-X", method.upper()])
        if payload is not None:
            command.extend(["--input", "-"])
        command.append(api_target)

        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            env=env,
            input=json.dumps(payload) if payload is not None else None,
        )
        if completed.returncode != 0:
            output = (completed.stderr or completed.stdout or "").strip()
            raise RuntimeError(f"gh api 调用失败：{output or 'unknown error'}")

        try:
            data = json.loads(completed.stdout or "null")
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"gh api 返回了无法解析的 JSON：{exc}") from exc

        return data

    def _resolve_token(self) -> str:
        for key in ("GITHUB_TOKEN", "GH_TOKEN"):
            value = str(self.env_vars.get(key) or os.environ.get(key) or "").strip()
            if value:
                return value
        return ""

    def _gh_available(self) -> bool:
        return shutil.which(self.gh_executable) is not None

    def _format_http_error(
        self,
        endpoint: str,
        method: str,
        status_code: int,
        body: str,
        reason: str,
        headers=None,
    ) -> str:
        operation = self._describe_api_operation(endpoint, method)
        message_text, documentation_url = self._parse_error_body(body)
        normalized_message = str(message_text or reason or body or "unknown error").strip()
        accepted_permissions = self._extract_header_value(headers, "X-Accepted-GitHub-Permissions")

        if self._is_pat_permission_error(status_code, normalized_message):
            return self._format_pat_permission_error(
                endpoint=endpoint,
                method=method,
                operation=operation,
                status_code=status_code,
                message_text=normalized_message,
                accepted_permissions=accepted_permissions,
            )

        if status_code == 401 and normalized_message.lower() == "bad credentials":
            return "GitHub 凭证无效或已过期，请检查 GITHUB_TOKEN / GH_TOKEN 是否正确"

        if documentation_url:
            normalized_message = f"{normalized_message}（文档：{documentation_url}）"
        return f"GitHub API 请求失败（HTTP {status_code}，{operation}）：{normalized_message}"

    @staticmethod
    def _parse_error_body(body: str) -> Tuple[str, str]:
        content = str(body or "").strip()
        if not content:
            return "", ""
        try:
            payload = json.loads(content)
        except json.JSONDecodeError:
            return content, ""

        if not isinstance(payload, dict):
            return content, ""

        parts: List[str] = []
        message = str(payload.get("message") or "").strip()
        if message:
            parts.append(message)

        error_items = payload.get("errors")
        if isinstance(error_items, list):
            normalized_errors: List[str] = []
            for item in error_items:
                if isinstance(item, dict):
                    error_text = " ".join(
                        str(value).strip()
                        for value in (
                            item.get("resource"),
                            item.get("field"),
                            item.get("code"),
                            item.get("message"),
                        )
                        if str(value or "").strip()
                    )
                    if not error_text:
                        error_text = json.dumps(item, ensure_ascii=False)
                else:
                    error_text = str(item or "").strip()
                if error_text:
                    normalized_errors.append(error_text)
            if normalized_errors:
                parts.append("；".join(normalized_errors))

        documentation_url = str(payload.get("documentation_url") or "").strip()
        return "；".join(part for part in parts if part).strip(), documentation_url

    @staticmethod
    def _describe_api_operation(endpoint: str, method: str) -> str:
        normalized_endpoint = str(endpoint or "").strip()
        normalized_method = str(method or "GET").strip().upper()
        if normalized_endpoint == "/user":
            return "校验当前 GitHub 账号"
        if normalized_endpoint == "/user/repos" and normalized_method == "GET":
            return "列出当前账号仓库"
        if normalized_endpoint == "/user/repos" and normalized_method == "POST":
            return "创建当前账号仓库"
        if normalized_endpoint.startswith("/orgs/") and normalized_endpoint.endswith("/repos"):
            org = normalized_endpoint.split("/")[2]
            if normalized_method == "GET":
                return f"列出组织 {org} 仓库"
            if normalized_method == "POST":
                return f"创建组织 {org} 仓库"
        return f"{normalized_method} {normalized_endpoint}"

    @staticmethod
    def _extract_header_value(headers, key: str) -> str:
        if headers is None:
            return ""
        if hasattr(headers, "get"):
            value = headers.get(key) or headers.get(key.lower()) or headers.get(key.upper())
            return str(value or "").strip()
        if isinstance(headers, dict):
            for header_key, value in headers.items():
                if str(header_key or "").lower() == key.lower():
                    return str(value or "").strip()
        return ""

    @staticmethod
    def _is_pat_permission_error(status_code: int, message_text: str) -> bool:
        if int(status_code) not in (403, 404):
            return False
        return "resource not accessible by personal access token" in str(message_text or "").lower()

    def _format_pat_permission_error(
        self,
        endpoint: str,
        method: str,
        operation: str,
        status_code: int,
        message_text: str,
        accepted_permissions: str,
    ) -> str:
        normalized_endpoint = str(endpoint or "").strip()
        normalized_method = str(method or "GET").strip().upper()
        lines = [
            f"GitHub Token 权限不足，无法{operation}（HTTP {status_code}）",
            "当前大概率在使用 fine-grained PAT，但该接口所需权限未授予，或令牌只允许访问部分仓库。",
            "请更新机器人配置中的 GITHUB_TOKEN / GH_TOKEN 后重试。",
        ]
        if accepted_permissions:
            lines.append(f"GitHub 返回的接口权限提示：{accepted_permissions}")

        if normalized_endpoint == "/user":
            lines.extend(
                [
                    "请检查：",
                    "- 当前 Token 是否属于你要操作的 GitHub 账号",
                    "- Token 是否已过期、被撤销，或未完成必要授权",
                ]
            )
        elif normalized_endpoint == "/user/repos" and normalized_method == "GET":
            lines.extend(
                [
                    "请检查：",
                    "- Resource owner 是否选择了目标 GitHub 账号",
                    "- Repository permissions 至少包含仓库元数据读取权限",
                    "- 如果后续还要自动创建新仓库，建议同时补齐仓库管理写权限",
                    "- 若使用 classic PAT，通常至少需要 `repo` scope",
                ]
            )
        elif normalized_endpoint == "/user/repos" and normalized_method == "POST":
            lines.extend(
                [
                    "请检查：",
                    "- Resource owner 是否选择了目标 GitHub 账号",
                    "- Repository access 是否允许创建新仓库；更稳妥的配置是 `All repositories`",
                    "- Repository permissions 是否包含仓库管理写权限",
                    "- 若使用 classic PAT，通常至少需要 `repo` scope",
                    "- 如果暂时不想重配 Token，也可以先在 GitHub 手动创建空仓库，再让机器人走 SSH 推送",
                ]
            )
        elif normalized_endpoint.startswith("/orgs/") and normalized_endpoint.endswith("/repos"):
            lines.extend(
                [
                    "请检查：",
                    "- Token 的 Resource owner 是否就是该组织，或该账号对组织有足够权限",
                    "- Organization / Repository 权限是否已授予创建或读取组织仓库所需权限",
                ]
            )

        lines.append(f"GitHub 原始提示：{message_text}")
        return "\n".join(lines)
