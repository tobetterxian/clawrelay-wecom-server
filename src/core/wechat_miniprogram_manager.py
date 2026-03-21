"""
微信小程序 OpenAPI 管理器

负责调用微信小程序代码管理相关 OpenAPI：
- 提交审核
- 查询审核状态
- 撤回审核
- 正式发布
"""

from __future__ import annotations

import json
import logging
import os
from typing import Dict, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

WECHAT_API_BASE = "https://api.weixin.qq.com"


class WeChatMiniProgramManager:
    def __init__(self, env_vars: Optional[Dict[str, str]] = None):
        self.env_vars = dict(env_vars or {})

    def submit_audit(
        self,
        appid: str,
        appsecret: str,
        payload: Dict[str, object],
    ) -> Dict[str, object]:
        token = self.get_access_token(appid, appsecret)
        return self._request_json(
            endpoint="/wxa/submit_audit",
            method="POST",
            payload=payload,
            access_token=token,
        )

    def get_audit_status(
        self,
        appid: str,
        appsecret: str,
        audit_id: int,
    ) -> Dict[str, object]:
        token = self.get_access_token(appid, appsecret)
        return self._request_json(
            endpoint="/wxa/get_auditstatus",
            method="POST",
            payload={"auditid": int(audit_id)},
            access_token=token,
        )

    def undo_code_audit(
        self,
        appid: str,
        appsecret: str,
    ) -> Dict[str, object]:
        token = self.get_access_token(appid, appsecret)
        return self._request_json(
            endpoint="/wxa/undocodeaudit",
            method="POST",
            payload={},
            access_token=token,
        )

    def release(
        self,
        appid: str,
        appsecret: str,
    ) -> Dict[str, object]:
        token = self.get_access_token(appid, appsecret)
        return self._request_json(
            endpoint="/wxa/release",
            method="POST",
            payload={},
            access_token=token,
        )

    def get_access_token(
        self,
        appid: str,
        appsecret: str,
        force_refresh: bool = False,
    ) -> str:
        normalized_appid = str(appid or "").strip()
        normalized_secret = str(appsecret or "").strip()
        if not normalized_appid:
            raise ValueError("微信小程序 AppID 不能为空")
        if not normalized_secret:
            raise ValueError("微信小程序 AppSecret 不能为空")

        try:
            data = self._request_json(
                endpoint="/cgi-bin/stable_token",
                method="POST",
                payload={
                    "grant_type": "client_credential",
                    "appid": normalized_appid,
                    "secret": normalized_secret,
                    "force_refresh": bool(force_refresh),
                },
            )
        except Exception:
            data = self._request_json(
                endpoint="/cgi-bin/token",
                method="GET",
                query_params={
                    "grant_type": "client_credential",
                    "appid": normalized_appid,
                    "secret": normalized_secret,
                },
            )

        access_token = str((data or {}).get("access_token") or "").strip()
        if not access_token:
            raise RuntimeError(f"获取微信小程序 access_token 失败：{data}")
        return access_token

    def _request_json(
        self,
        endpoint: str,
        method: str = "GET",
        payload: Optional[Dict[str, object]] = None,
        query_params: Optional[Dict[str, str]] = None,
        access_token: str = "",
    ) -> Dict[str, object]:
        normalized_query = dict(query_params or {})
        if access_token:
            normalized_query["access_token"] = access_token

        query = urlencode(normalized_query)
        url = f"{WECHAT_API_BASE}{endpoint}"
        if query:
            url = f"{url}?{query}"

        data_bytes = None
        if payload is not None:
            data_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        request = Request(
            url,
            method=method.upper(),
            data=data_bytes,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "clawrelay-wecom-server",
            },
        )
        try:
            with urlopen(request, timeout=20) as response:
                body = response.read().decode("utf-8") or "{}"
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="ignore") if hasattr(exc, "read") else ""
            raise RuntimeError(
                f"微信 OpenAPI 请求失败：{method.upper()} {endpoint} [{int(exc.code)}] {body or exc.reason}"
            ) from exc
        except URLError as exc:
            raise RuntimeError(f"微信 OpenAPI 网络请求失败：{exc}") from exc

        try:
            data = json.loads(body or "{}")
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"微信 OpenAPI 返回了无法解析的 JSON：{exc}") from exc

        if not isinstance(data, dict):
            raise RuntimeError("微信 OpenAPI 返回格式异常")

        errcode = data.get("errcode")
        if errcode not in (None, 0, "0"):
            errmsg = str(data.get("errmsg") or "").strip()
            raise RuntimeError(
                f"微信 OpenAPI 调用失败：{method.upper()} {endpoint} / errcode={errcode} / errmsg={errmsg or 'unknown error'}"
            )
        return data

    def read_runtime_secret(self, key: str) -> str:
        value = self.env_vars.get(key) or os.getenv(key) or ""
        normalized = str(value).strip()
        if not normalized:
            raise RuntimeError(f"未配置 {key}")
        return normalized
