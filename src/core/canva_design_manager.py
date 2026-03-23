"""
Canva 精修版画册管理器

负责：
1. 刷新 Canva Connect API Access Token
2. 读取 Brand Template Dataset
3. 上传项目素材到 Canva
4. 创建并轮询 Autofill Job
5. 导出 PDF 到当前工作区
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time
from typing import Any, Dict, Optional

import requests

from src.utils.brochure_asset_manifest import load_brochure_asset_manifest
from src.utils.brochure_canva_payload import build_canva_autofill_plan
from src.utils.brochure_canva_state import (
    CANVA_BROCHURE_STATE_VERSION,
    load_canva_brochure_state,
    write_canva_brochure_state,
)

CANVA_API_BASE = "https://api.canva.com/rest/v1"
CANVA_TOKEN_URL = f"{CANVA_API_BASE}/oauth/token"
DEFAULT_CANVA_EXPORT_PATH = "dist/canva-brochure.pdf"


@dataclass(frozen=True)
class CanvaAutofillResult:
    state_relative_path: str
    design_id: str
    edit_url: str
    view_url: str
    design_title: str
    page_count: int
    dataset_field_count: int
    autofill_field_count: int
    asset_upload_count: int


@dataclass(frozen=True)
class CanvaExportResult:
    state_relative_path: str
    design_id: str
    output_relative_path: str
    output_path: str
    export_job_id: str


class CanvaDesignManager:
    """Canva Brand Template 自动填充与导出执行器。"""

    def __init__(self, env_vars: Optional[Dict[str, str]] = None):
        self.env_vars = dict(env_vars or {})
        self._cached_access_token = ""
        self._cached_token_expires_at = 0.0
        self._cached_refresh_token = self._get_env("CANVA_REFRESH_TOKEN")

    def is_enabled(self) -> bool:
        if not self._brand_template_id():
            return False
        if self._access_token():
            return True
        return all([self._client_id(), self._client_secret(), self._refresh_token()])

    def missing_configuration_items(self) -> list[str]:
        missing: list[str] = []
        if not self._brand_template_id():
            missing.append("CANVA_BRAND_TEMPLATE_ID")
        if self._access_token():
            return missing
        if not self._client_id():
            missing.append("CANVA_CLIENT_ID")
        if not self._client_secret():
            missing.append("CANVA_CLIENT_SECRET")
        if not self._refresh_token():
            missing.append("CANVA_REFRESH_TOKEN")
        return missing

    def load_state(self, workspace_path: str) -> Optional[dict]:
        return load_canva_brochure_state(workspace_path)

    def generate_polished_brochure(
        self,
        workspace_path: str,
        *,
        project_name: str = "",
        design_title: str = "",
    ) -> CanvaAutofillResult:
        if not self.is_enabled():
            missing = ", ".join(self.missing_configuration_items())
            raise RuntimeError(f"未完成 Canva 配置：缺少 {missing}")

        workspace_root = Path(workspace_path).expanduser().resolve()
        dataset = self._get_brand_template_dataset()
        asset_manifest = load_brochure_asset_manifest(str(workspace_root))
        plan = build_canva_autofill_plan(
            str(workspace_root),
            dataset,
            project_name=project_name,
            design_title=design_title,
            asset_manifest=asset_manifest,
        )

        request_data: Dict[str, Any] = {}
        asset_upload_count = 0
        for field_name, binding in plan.bindings.items():
            binding_type = str(binding.get("type") or "").strip().lower()
            if binding_type == "text":
                request_data[field_name] = {
                    "type": "text",
                    "text": str(binding.get("text") or "").strip(),
                }
                continue
            if binding_type != "image":
                continue
            source_file = str(binding.get("source_file") or "").strip()
            if not source_file:
                continue
            asset = self._upload_workspace_asset(
                workspace_root=workspace_root,
                relative_path=source_file,
            )
            asset_id = str(((asset or {}).get("asset") or {}).get("id") or "").strip()
            if not asset_id:
                continue
            request_data[field_name] = {
                "type": "image",
                "asset_id": asset_id,
            }
            asset_upload_count += 1

        if not request_data:
            raise RuntimeError("未能为 Canva 模板生成可用的自动填充数据，请先准备需求文档或图片素材")

        response = self._api_request(
            "POST",
            "/autofills",
            json_body={
                "brand_template_id": self._brand_template_id(),
                "title": plan.design_title,
                "data": request_data,
            },
        )
        job_id = str(((response or {}).get("job") or {}).get("id") or "").strip()
        if not job_id:
            raise RuntimeError("Canva Autofill 未返回任务 ID")

        job = self._poll_job(
            lambda: self._api_request("GET", f"/autofills/{job_id}"),
            action_label="生成 Canva 精修版",
        )
        result = dict(job.get("result") or {})
        design = dict(result.get("design") or {})
        urls = dict(design.get("urls") or {})
        design_id = str(design.get("id") or "").strip()
        if not design_id:
            raise RuntimeError("Canva 精修版生成成功，但未返回 design_id")

        payload = {
            "version": CANVA_BROCHURE_STATE_VERSION,
            "provider": "canva",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "project_name": str(project_name or workspace_root.name or "").strip(),
            "brand_template_id": self._brand_template_id(),
            "design_id": design_id,
            "design_title": str(design.get("title") or plan.design_title or "").strip(),
            "design_url": str(design.get("url") or "").strip(),
            "edit_url": str(urls.get("edit_url") or "").strip(),
            "view_url": str(urls.get("view_url") or "").strip(),
            "page_count": int(design.get("page_count") or 0),
            "thumbnail_url": str(((design.get("thumbnail") or {}).get("url")) or "").strip(),
            "autofill_job_id": job_id,
            "dataset_field_count": plan.dataset_field_count,
            "autofill_field_count": len(request_data),
            "asset_upload_count": asset_upload_count,
            "uploaded_asset_sources": [
                str(binding.get("source_file") or "").strip()
                for binding in plan.bindings.values()
                if str(binding.get("type") or "").strip().lower() == "image"
                and str(binding.get("source_file") or "").strip()
            ],
        }
        state_path = write_canva_brochure_state(str(workspace_root), payload)
        return CanvaAutofillResult(
            state_relative_path=state_path.relative_to(workspace_root).as_posix(),
            design_id=design_id,
            edit_url=str(payload.get("edit_url") or ""),
            view_url=str(payload.get("view_url") or ""),
            design_title=str(payload.get("design_title") or ""),
            page_count=int(payload.get("page_count") or 0),
            dataset_field_count=plan.dataset_field_count,
            autofill_field_count=len(request_data),
            asset_upload_count=asset_upload_count,
        )

    def export_design_pdf(
        self,
        workspace_path: str,
        *,
        output_path: str = DEFAULT_CANVA_EXPORT_PATH,
    ) -> CanvaExportResult:
        workspace_root = Path(workspace_path).expanduser().resolve()
        state = load_canva_brochure_state(str(workspace_root))
        design_id = str(((state or {}).get("design_id")) or "").strip()
        if not design_id:
            raise RuntimeError("未找到 Canva 设计状态，请先执行 `生成Canva精修版`")

        export_response = self._api_request(
            "POST",
            "/exports",
            json_body={
                "design_id": design_id,
                "format": {
                    "type": "pdf",
                    "export_quality": "regular",
                },
            },
        )
        job_id = str(((export_response or {}).get("job") or {}).get("id") or "").strip()
        if not job_id:
            raise RuntimeError("Canva 导出未返回任务 ID")

        job = self._poll_job(
            lambda: self._api_request("GET", f"/exports/{job_id}"),
            action_label="导出 Canva 画册 PDF",
        )
        urls = list(job.get("urls") or [])
        download_url = str(urls[0] or "").strip() if urls else ""
        if not download_url:
            raise RuntimeError("Canva 导出成功，但未返回下载链接")

        output_file = self._resolve_workspace_output_path(workspace_root, output_path)
        output_file.parent.mkdir(parents=True, exist_ok=True)
        self._download_file(download_url, output_file)

        updated_state = dict(state or {})
        updated_state["last_export"] = {
            "type": "pdf",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "export_job_id": job_id,
            "output_relative_path": output_file.relative_to(workspace_root).as_posix(),
        }
        state_path = write_canva_brochure_state(str(workspace_root), updated_state)
        return CanvaExportResult(
            state_relative_path=state_path.relative_to(workspace_root).as_posix(),
            design_id=design_id,
            output_relative_path=output_file.relative_to(workspace_root).as_posix(),
            output_path=str(output_file),
            export_job_id=job_id,
        )

    def _get_brand_template_dataset(self) -> Dict[str, Any]:
        response = self._api_request(
            "GET",
            f"/brand-templates/{self._brand_template_id()}/dataset",
        )
        dataset = response.get("dataset") or {}
        if not isinstance(dataset, dict):
            raise RuntimeError("Canva Brand Template Dataset 返回格式不正确")
        return dataset

    def _upload_workspace_asset(self, workspace_root: Path, relative_path: str) -> Dict[str, Any]:
        normalized = str(relative_path or "").strip().replace("\\", "/")
        if not normalized:
            raise RuntimeError("Canva 上传素材失败：素材路径为空")
        local_path = (workspace_root / normalized).resolve()
        try:
            local_path.relative_to(workspace_root)
        except ValueError as exc:
            raise RuntimeError(f"Canva 上传素材失败：路径超出项目范围：{normalized}") from exc
        if not local_path.exists() or not local_path.is_file():
            raise RuntimeError(f"Canva 上传素材失败：未找到素材文件：{normalized}")

        asset_name = self._asset_display_name(local_path.name)
        metadata_header = {
            "name_base64": base64.b64encode(asset_name.encode("utf-8")).decode("utf-8")
        }
        response = self._api_request(
            "POST",
            "/asset-uploads",
            headers={
                "Content-Type": "application/octet-stream",
                "Asset-Upload-Metadata": json.dumps(metadata_header, ensure_ascii=False),
            },
            body_bytes=local_path.read_bytes(),
        )
        job_id = str(((response or {}).get("job") or {}).get("id") or "").strip()
        if not job_id:
            raise RuntimeError(f"Canva 上传素材失败：未返回任务 ID（{normalized}）")

        job = self._poll_job(
            lambda: self._api_request("GET", f"/asset-uploads/{job_id}"),
            action_label=f"上传 Canva 素材 {normalized}",
        )
        if not isinstance(job.get("asset"), dict):
            raise RuntimeError(f"Canva 上传素材失败：未返回资产信息（{normalized}）")
        return job

    def _download_file(self, url: str, target_path: Path) -> None:
        response = requests.get(url, timeout=120)
        if response.status_code >= 400:
            raise RuntimeError(f"下载 Canva 导出文件失败：HTTP {response.status_code}")
        target_path.write_bytes(response.content)

    def _resolve_workspace_output_path(self, workspace_root: Path, output_path: str) -> Path:
        normalized = str(output_path or "").strip().replace("\\", "/")
        if not normalized:
            normalized = DEFAULT_CANVA_EXPORT_PATH
        if normalized.startswith("/"):
            raise RuntimeError("Canva 导出路径必须位于当前项目工作区内，不能使用绝对路径")
        output_file = (workspace_root / normalized).resolve()
        try:
            output_file.relative_to(workspace_root)
        except ValueError as exc:
            raise RuntimeError(f"Canva 导出路径超出项目范围：{normalized}") from exc
        return output_file

    def _poll_job(self, fetch_job, *, action_label: str, timeout_seconds: int = 120) -> Dict[str, Any]:
        deadline = time.time() + timeout_seconds
        last_status = ""
        while time.time() < deadline:
            payload = fetch_job() or {}
            job = dict(payload.get("job") or {})
            status = str(job.get("status") or "").strip().lower()
            if status == "success":
                return job
            if status == "failed":
                error = dict(job.get("error") or {})
                message = str(error.get("message") or "").strip() or "未知错误"
                raise RuntimeError(f"{action_label}失败：{message}")
            last_status = status or last_status
            time.sleep(2)
        raise RuntimeError(f"{action_label}超时：最后状态 {last_status or '-'}")

    def _api_request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        body_bytes: Optional[bytes] = None,
        retry_on_unauthorized: bool = True,
    ) -> Dict[str, Any]:
        url = f"{CANVA_API_BASE}{path}"
        request_headers = {
            "Authorization": f"Bearer {self._get_access_token()}",
        }
        if json_body is not None:
            request_headers["Content-Type"] = "application/json"
        if headers:
            request_headers.update({key: value for key, value in headers.items() if value is not None})

        response = requests.request(
            method=method.upper(),
            url=url,
            headers=request_headers,
            json=json_body,
            data=body_bytes,
            timeout=120,
        )
        if response.status_code == 401 and retry_on_unauthorized and self._can_refresh_token():
            self._refresh_access_token(force_refresh=True)
            return self._api_request(
                method,
                path,
                json_body=json_body,
                headers=headers,
                body_bytes=body_bytes,
                retry_on_unauthorized=False,
            )

        try:
            payload = response.json()
        except Exception:
            payload = {}

        if response.status_code >= 400:
            message = str((payload.get("message") if isinstance(payload, dict) else "") or "").strip()
            code = str((payload.get("code") if isinstance(payload, dict) else "") or "").strip()
            detail = f"{code}: {message}" if code and message else (message or response.text[:200] or f"HTTP {response.status_code}")
            raise RuntimeError(f"Canva API 请求失败：{detail}")

        if isinstance(payload, dict):
            return payload
        raise RuntimeError("Canva API 返回了无法解析的响应")

    def _get_access_token(self) -> str:
        direct_access_token = self._access_token()
        if direct_access_token and not self._can_refresh_token():
            return direct_access_token

        now = time.time()
        if self._cached_access_token and self._cached_token_expires_at - now > 60:
            return self._cached_access_token

        if direct_access_token and not self._cached_access_token:
            self._cached_access_token = direct_access_token
            self._cached_token_expires_at = now + 300
            return self._cached_access_token

        return self._refresh_access_token(force_refresh=False)

    def _refresh_access_token(self, *, force_refresh: bool) -> str:
        now = time.time()
        if not force_refresh and self._cached_access_token and self._cached_token_expires_at - now > 60:
            return self._cached_access_token
        if not self._can_refresh_token():
            raise RuntimeError("未配置 Canva OAuth Refresh Token，无法刷新 Access Token")

        basic_token = base64.b64encode(f"{self._client_id()}:{self._client_secret()}".encode("utf-8")).decode("utf-8")
        response = requests.post(
            CANVA_TOKEN_URL,
            headers={
                "Authorization": f"Basic {basic_token}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "grant_type": "refresh_token",
                "refresh_token": self._refresh_token(),
            },
            timeout=60,
        )
        try:
            payload = response.json()
        except Exception as exc:
            raise RuntimeError(f"Canva Token 刷新失败：HTTP {response.status_code}") from exc

        if response.status_code >= 400:
            message = str((payload.get("message") if isinstance(payload, dict) else "") or "").strip()
            raise RuntimeError(f"Canva Token 刷新失败：{message or f'HTTP {response.status_code}'}")

        access_token = str(payload.get("access_token") or "").strip()
        if not access_token:
            raise RuntimeError("Canva Token 刷新失败：未返回 access_token")

        expires_in = int(payload.get("expires_in") or 0)
        refresh_token = str(payload.get("refresh_token") or "").strip()
        self._cached_access_token = access_token
        self._cached_token_expires_at = now + max(60, expires_in)
        if refresh_token:
            self._cached_refresh_token = refresh_token
        return access_token

    def _can_refresh_token(self) -> bool:
        return all([self._client_id(), self._client_secret(), self._refresh_token()])

    def _client_id(self) -> str:
        return self._get_env("CANVA_CLIENT_ID")

    def _client_secret(self) -> str:
        return self._get_env("CANVA_CLIENT_SECRET")

    def _refresh_token(self) -> str:
        return str(self._cached_refresh_token or self._get_env("CANVA_REFRESH_TOKEN") or "").strip()

    def _access_token(self) -> str:
        return self._get_env("CANVA_ACCESS_TOKEN")

    def _brand_template_id(self) -> str:
        return self._get_env("CANVA_BRAND_TEMPLATE_ID")

    def _get_env(self, key: str) -> str:
        return str(self.env_vars.get(key) or os.getenv(key) or "").strip()

    @staticmethod
    def _asset_display_name(filename: str) -> str:
        name = str(filename or "").strip() or "brochure-asset"
        if len(name) <= 50:
            return name
        suffix = Path(name).suffix
        stem = Path(name).stem[: max(1, 50 - len(suffix))]
        return f"{stem}{suffix}"
