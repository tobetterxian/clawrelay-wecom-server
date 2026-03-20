"""
GitHub Actions Secrets 管理器

负责为目标仓库写入 Actions Secrets。
"""

from __future__ import annotations

import base64
import json
import logging
import os
from dataclasses import dataclass
from typing import Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

GITHUB_API_BASE = "https://api.github.com"


@dataclass
class GitHubRepositoryPublicKey:
    key_id: str
    key: str


class GitHubActionsSecretManager:
    def __init__(self, env_vars: Optional[Dict[str, str]] = None):
        self.env_vars = dict(env_vars or {})

    def seed_cloudflare_repository_secrets(
        self,
        owner: str,
        repo: str,
        api_token: str,
        account_id: str,
    ) -> List[str]:
        written: List[str] = []
        self.upsert_repository_secret(owner, repo, "CLOUDFLARE_API_TOKEN", api_token)
        written.append("CLOUDFLARE_API_TOKEN")
        self.upsert_repository_secret(owner, repo, "CLOUDFLARE_ACCOUNT_ID", account_id)
        written.append("CLOUDFLARE_ACCOUNT_ID")
        return written

    def seed_wechat_miniprogram_repository_secrets(
        self,
        owner: str,
        repo: str,
        private_key: str,
    ) -> List[str]:
        written: List[str] = []
        self.upsert_repository_secret(owner, repo, "WECHAT_MINIPROGRAM_PRIVATE_KEY", private_key)
        written.append("WECHAT_MINIPROGRAM_PRIVATE_KEY")
        return written

    def upsert_repository_secret(
        self,
        owner: str,
        repo: str,
        secret_name: str,
        secret_value: str,
    ) -> None:
        normalized_owner = str(owner or "").strip()
        normalized_repo = str(repo or "").strip()
        normalized_name = str(secret_name or "").strip().upper()
        if not normalized_owner:
            raise ValueError("GitHub owner 不能为空")
        if not normalized_repo:
            raise ValueError("GitHub repo 不能为空")
        if not normalized_name:
            raise ValueError("GitHub Secret 名称不能为空")
        if not str(secret_value or "").strip():
            raise ValueError(f"GitHub Secret {normalized_name} 的值不能为空")

        public_key = self.get_repository_public_key(normalized_owner, normalized_repo)
        encrypted_value = self._encrypt_secret(public_key.key, secret_value)
        self._request_json(
            endpoint=f"/repos/{normalized_owner}/{normalized_repo}/actions/secrets/{normalized_name}",
            method="PUT",
            payload={
                "encrypted_value": encrypted_value,
                "key_id": public_key.key_id,
            },
        )

    def get_repository_public_key(self, owner: str, repo: str) -> GitHubRepositoryPublicKey:
        normalized_owner = str(owner or "").strip()
        normalized_repo = str(repo or "").strip()
        if not normalized_owner:
            raise ValueError("GitHub owner 不能为空")
        if not normalized_repo:
            raise ValueError("GitHub repo 不能为空")

        data = self._request_json(
            endpoint=f"/repos/{normalized_owner}/{normalized_repo}/actions/secrets/public-key",
            method="GET",
        )
        if not isinstance(data, dict):
            raise RuntimeError("GitHub 仓库公钥返回格式异常")

        key_id = str(data.get("key_id") or "").strip()
        key = str(data.get("key") or "").strip()
        if not key_id or not key:
            raise RuntimeError("GitHub 仓库公钥数据不完整")
        return GitHubRepositoryPublicKey(key_id=key_id, key=key)

    @staticmethod
    def _encrypt_secret(public_key_base64: str, secret_value: str) -> str:
        try:
            from nacl import encoding, public
        except ImportError as exc:
            raise RuntimeError(
                "写入 GitHub Actions Secrets 需要 PyNaCl 依赖，请先安装 requirements.txt 中的 PyNaCl"
            ) from exc

        public_key = public.PublicKey(
            public_key_base64.encode("utf-8"),
            encoder=encoding.Base64Encoder(),
        )
        sealed_box = public.SealedBox(public_key)
        encrypted = sealed_box.encrypt(str(secret_value).encode("utf-8"))
        return base64.b64encode(encrypted).decode("utf-8")

    def _resolve_token(self) -> str:
        token = (
            self.env_vars.get("GITHUB_TOKEN")
            or self.env_vars.get("GH_TOKEN")
            or os.getenv("GITHUB_TOKEN")
            or os.getenv("GH_TOKEN")
            or ""
        )
        normalized = str(token).strip()
        if not normalized:
            raise RuntimeError("未检测到 GITHUB_TOKEN / GH_TOKEN，无法写入 GitHub Actions Secrets")
        return normalized

    def _request_json(
        self,
        endpoint: str,
        method: str = "GET",
        payload: Optional[Dict[str, object]] = None,
    ):
        token = self._resolve_token()
        url = f"{GITHUB_API_BASE}{endpoint}"
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
            with urlopen(request, timeout=20) as response:
                response_text = response.read().decode("utf-8") or "null"
                if not response_text.strip():
                    return {}
                return json.loads(response_text)
        except HTTPError as exc:
            raw_body = exc.read().decode("utf-8", errors="ignore") if hasattr(exc, "read") else ""
            message = raw_body or str(exc)
            try:
                data = json.loads(raw_body) if raw_body else {}
                api_message = str(data.get("message") or "").strip()
                if api_message:
                    message = api_message
            except json.JSONDecodeError:
                pass
            raise RuntimeError(
                f"GitHub Actions Secrets API 请求失败：HTTP {exc.code} - {message}"
            ) from exc
        except URLError as exc:
            raise RuntimeError(f"GitHub Actions Secrets API 网络异常：{exc}") from exc
