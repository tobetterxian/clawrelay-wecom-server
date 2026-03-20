"""
Cloudflare 部署状态管理器

当前文件仍沿用原命名，但职责已扩展为：

- 查询和创建 Cloudflare Pages 项目
- 查询 Cloudflare Pages 最近一次部署
- 查询 Cloudflare Worker 的 workers.dev / 部署状态
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

CLOUDFLARE_API_BASE = "https://api.cloudflare.com/client/v4"


@dataclass
class CloudflarePagesProjectInfo:
    name: str
    subdomain: str = ""
    production_branch: str = ""
    created: bool = False


@dataclass
class CloudflarePagesDeploymentInfo:
    deployment_id: str
    environment: str = ""
    url: str = ""
    stage_name: str = ""
    stage_status: str = ""
    created_on: str = ""
    modified_on: str = ""


@dataclass
class CloudflareWorkerDeploymentInfo:
    deployment_id: str
    created_on: str = ""
    source: str = ""


@dataclass
class CloudflareWorkerStatusInfo:
    name: str
    exists: bool = False
    workers_dev_enabled: bool = False
    previews_enabled: bool = False
    account_subdomain: str = ""
    workers_dev_url: str = ""
    latest_deployment: Optional[CloudflareWorkerDeploymentInfo] = None


class CloudflarePagesManager:
    def __init__(self, env_vars: Optional[Dict[str, str]] = None):
        self.env_vars = dict(env_vars or {})

    def ensure_project(
        self,
        project_name: str,
        production_branch: str = "main",
    ) -> CloudflarePagesProjectInfo:
        normalized_name = str(project_name or "").strip()
        if not normalized_name:
            raise ValueError("Cloudflare Pages 项目名不能为空")

        existing = self.get_project(normalized_name)
        if existing:
            existing.created = False
            return existing

        data = self._request_json(
            method="POST",
            path=f"/accounts/{self._resolve_account_id()}/pages/projects",
            payload={
                "name": normalized_name,
                "production_branch": str(production_branch or "main").strip() or "main",
            },
        )
        result = self._extract_result(data)
        return CloudflarePagesProjectInfo(
            name=str(result.get("name") or normalized_name).strip(),
            subdomain=str(result.get("subdomain") or "").strip(),
            production_branch=str(result.get("production_branch") or production_branch or "main").strip(),
            created=True,
        )

    def get_project(self, project_name: str) -> Optional[CloudflarePagesProjectInfo]:
        normalized_name = str(project_name or "").strip()
        if not normalized_name:
            raise ValueError("Cloudflare Pages 项目名不能为空")

        try:
            data = self._request_json(
                method="GET",
                path=f"/accounts/{self._resolve_account_id()}/pages/projects/{normalized_name}",
            )
        except RuntimeError as exc:
            message = str(exc or "")
            if "HTTP 404" in message:
                return None
            raise

        result = self._extract_result(data)
        return CloudflarePagesProjectInfo(
            name=str(result.get("name") or normalized_name).strip(),
            subdomain=str(result.get("subdomain") or "").strip(),
            production_branch=str(result.get("production_branch") or "").strip(),
            created=False,
        )

    def get_latest_deployment(self, project_name: str) -> Optional[CloudflarePagesDeploymentInfo]:
        normalized_name = str(project_name or "").strip()
        if not normalized_name:
            raise ValueError("Cloudflare Pages 项目名不能为空")

        try:
            data = self._request_json(
                method="GET",
                path=f"/accounts/{self._resolve_account_id()}/pages/projects/{normalized_name}/deployments",
            )
        except RuntimeError as exc:
            message = str(exc or "")
            if "HTTP 404" in message:
                return None
            raise

        deployments = self._extract_result_list(data)
        if not deployments:
            return None

        production_candidate = None
        for item in deployments:
            if str(item.get("environment") or "").strip().lower() == "production":
                production_candidate = item
                break
        target = production_candidate or deployments[0]
        latest_stage = target.get("latest_stage") or {}
        return CloudflarePagesDeploymentInfo(
            deployment_id=str(target.get("id") or "").strip(),
            environment=str(target.get("environment") or "").strip(),
            url=str(target.get("url") or "").strip(),
            stage_name=str((latest_stage or {}).get("name") or "").strip(),
            stage_status=str((latest_stage or {}).get("status") or "").strip(),
            created_on=str(target.get("created_on") or "").strip(),
            modified_on=str(target.get("modified_on") or "").strip(),
        )

    def get_worker_status(self, worker_name: str) -> CloudflareWorkerStatusInfo:
        normalized_name = str(worker_name or "").strip()
        if not normalized_name:
            raise ValueError("Cloudflare Worker 名称不能为空")

        account_subdomain = self._get_workers_account_subdomain()
        try:
            subdomain_data = self._request_json(
                method="GET",
                path=f"/accounts/{self._resolve_account_id()}/workers/scripts/{normalized_name}/subdomain",
            )
        except RuntimeError as exc:
            message = str(exc or "")
            if "HTTP 404" in message:
                return CloudflareWorkerStatusInfo(
                    name=normalized_name,
                    exists=False,
                    account_subdomain=account_subdomain,
                )
            raise

        subdomain_result = self._extract_result(subdomain_data)
        latest_deployment = self._get_latest_worker_deployment(normalized_name)
        workers_dev_enabled = bool(subdomain_result.get("enabled"))
        workers_dev_url = ""
        if workers_dev_enabled and account_subdomain:
            workers_dev_url = f"https://{normalized_name}.{account_subdomain}.workers.dev"

        return CloudflareWorkerStatusInfo(
            name=normalized_name,
            exists=True,
            workers_dev_enabled=workers_dev_enabled,
            previews_enabled=bool(subdomain_result.get("previews_enabled")),
            account_subdomain=account_subdomain,
            workers_dev_url=workers_dev_url,
            latest_deployment=latest_deployment,
        )

    def _resolve_api_token(self) -> str:
        token = self.env_vars.get("CLOUDFLARE_API_TOKEN") or os.getenv("CLOUDFLARE_API_TOKEN") or ""
        normalized = str(token).strip()
        if not normalized:
            raise RuntimeError("未检测到 CLOUDFLARE_API_TOKEN，无法初始化 Cloudflare Pages 项目")
        return normalized

    def _resolve_account_id(self) -> str:
        account_id = self.env_vars.get("CLOUDFLARE_ACCOUNT_ID") or os.getenv("CLOUDFLARE_ACCOUNT_ID") or ""
        normalized = str(account_id).strip()
        if not normalized:
            raise RuntimeError("未检测到 CLOUDFLARE_ACCOUNT_ID，无法初始化 Cloudflare Pages 项目")
        return normalized

    @staticmethod
    def _extract_result(data):
        if not isinstance(data, dict):
            raise RuntimeError("Cloudflare API 返回格式异常")
        if data.get("success") is False:
            errors = data.get("errors") or []
            error_text = "; ".join(
                str((item or {}).get("message") or "").strip()
                for item in errors
                if isinstance(item, dict)
            ).strip()
            raise RuntimeError(error_text or "Cloudflare API 请求失败")
        result = data.get("result")
        if not isinstance(result, dict):
            raise RuntimeError("Cloudflare API 返回缺少 result")
        return result

    @staticmethod
    def _extract_result_list(data) -> List[dict]:
        if not isinstance(data, dict):
            raise RuntimeError("Cloudflare API 返回格式异常")
        if data.get("success") is False:
            errors = data.get("errors") or []
            error_text = "; ".join(
                str((item or {}).get("message") or "").strip()
                for item in errors
                if isinstance(item, dict)
            ).strip()
            raise RuntimeError(error_text or "Cloudflare API 请求失败")
        result = data.get("result")
        if not isinstance(result, list):
            raise RuntimeError("Cloudflare API 返回缺少结果列表")
        return [item for item in result if isinstance(item, dict)]

    def _get_workers_account_subdomain(self) -> str:
        try:
            data = self._request_json(
                method="GET",
                path=f"/accounts/{self._resolve_account_id()}/workers/subdomain",
            )
        except RuntimeError as exc:
            message = str(exc or "")
            if "HTTP 404" in message:
                return ""
            raise
        result = self._extract_result(data)
        return str(result.get("subdomain") or "").strip()

    def _get_latest_worker_deployment(self, worker_name: str) -> Optional[CloudflareWorkerDeploymentInfo]:
        try:
            data = self._request_json(
                method="GET",
                path=f"/accounts/{self._resolve_account_id()}/workers/scripts/{worker_name}/deployments",
            )
        except RuntimeError as exc:
            message = str(exc or "")
            if "HTTP 404" in message:
                return None
            raise

        result = self._extract_result(data)
        deployments = result.get("deployments") or []
        if not isinstance(deployments, list) or not deployments:
            return None
        first = deployments[0] if isinstance(deployments[0], dict) else {}
        return CloudflareWorkerDeploymentInfo(
            deployment_id=str(first.get("id") or "").strip(),
            created_on=str(first.get("created_on") or "").strip(),
            source=str(first.get("source") or "").strip(),
        )

    def _request_json(
        self,
        method: str,
        path: str,
        payload: Optional[Dict[str, object]] = None,
    ):
        url = f"{CLOUDFLARE_API_BASE}{path}"
        data_bytes = None
        if payload is not None:
            data_bytes = json.dumps(payload).encode("utf-8")

        request = Request(
            url,
            method=method,
            data=data_bytes,
            headers={
                "Authorization": f"Bearer {self._resolve_api_token()}",
                "Content-Type": "application/json",
                "User-Agent": "clawrelay-wecom-server",
            },
        )
        try:
            with urlopen(request, timeout=20) as response:
                response_text = response.read().decode("utf-8") or "null"
                return json.loads(response_text)
        except HTTPError as exc:
            raw_body = exc.read().decode("utf-8", errors="ignore") if hasattr(exc, "read") else ""
            message = raw_body or str(exc)
            try:
                data = json.loads(raw_body) if raw_body else {}
                errors = data.get("errors") or []
                error_text = "; ".join(
                    str((item or {}).get("message") or "").strip()
                    for item in errors
                    if isinstance(item, dict)
                ).strip()
                if error_text:
                    message = error_text
            except json.JSONDecodeError:
                pass
            raise RuntimeError(f"Cloudflare API 请求失败：HTTP {exc.code} - {message}") from exc
        except URLError as exc:
            raise RuntimeError(f"Cloudflare API 网络异常：{exc}") from exc
