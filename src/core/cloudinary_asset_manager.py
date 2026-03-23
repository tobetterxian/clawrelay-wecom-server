"""
Cloudinary 画册素材管理器

负责扫描当前工作区内的候选图片，上传到 Cloudinary，
并生成 `docs/brochure-assets.json` 供画册生成流程复用。
"""

from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import quote

import requests

from src.utils.brochure_asset_manifest import (
    MANIFEST_VERSION,
    load_brochure_asset_manifest,
    write_brochure_asset_manifest,
)

CLOUDINARY_API_BASE = "https://api.cloudinary.com/v1_1"
DEFAULT_CLOUDINARY_UPLOAD_FOLDER = "clawrelay-brochure"
SUPPORTED_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
DEFAULT_ASSET_SEARCH_DIRS = ("brochure/assets", "assets", "uploads", "images")


@dataclass(frozen=True)
class CloudinarySyncResult:
    manifest_relative_path: str
    asset_count: int
    source_count: int
    provider: str = "cloudinary"


class CloudinaryAssetManager:
    """负责同步画册素材到 Cloudinary。"""

    def __init__(self, env_vars: Optional[Dict[str, str]] = None):
        self.env_vars = dict(env_vars or {})

    def is_enabled(self) -> bool:
        return all([self._cloud_name(), self._api_key(), self._api_secret()])

    def missing_configuration_items(self) -> List[str]:
        missing: List[str] = []
        if not self._cloud_name():
            missing.append("CLOUDINARY_CLOUD_NAME")
        if not self._api_key():
            missing.append("CLOUDINARY_API_KEY")
        if not self._api_secret():
            missing.append("CLOUDINARY_API_SECRET")
        return missing

    def load_manifest(self, workspace_path: str) -> Optional[dict]:
        return load_brochure_asset_manifest(workspace_path)

    def sync_workspace_assets(
        self,
        workspace_path: str,
        project_name: str = "",
    ) -> CloudinarySyncResult:
        if not self.is_enabled():
            missing = ", ".join(self.missing_configuration_items())
            raise RuntimeError(f"未完成 Cloudinary 配置：缺少 {missing}")

        workspace_root = Path(workspace_path).expanduser().resolve()
        candidates = self.collect_candidate_assets(workspace_path)
        assets = [
            self.upload_asset(
                local_path=asset_path,
                workspace_root=workspace_root,
                project_name=project_name,
            )
            for asset_path in candidates
        ]
        payload = {
            "version": MANIFEST_VERSION,
            "provider": "cloudinary",
            "project_name": str(project_name or workspace_root.name or "").strip(),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "asset_count": len(assets),
            "assets": assets,
        }
        manifest_path = write_brochure_asset_manifest(str(workspace_root), payload)
        return CloudinarySyncResult(
            manifest_relative_path=manifest_path.relative_to(workspace_root).as_posix(),
            asset_count=len(assets),
            source_count=len(candidates),
        )

    def collect_candidate_assets(self, workspace_path: str) -> List[Path]:
        workspace_root = Path(workspace_path).expanduser().resolve()
        results: List[Path] = []
        seen: set[Path] = set()
        for relative_dir in DEFAULT_ASSET_SEARCH_DIRS:
            search_root = (workspace_root / relative_dir).resolve()
            if not search_root.exists() or not search_root.is_dir():
                continue
            try:
                search_root.relative_to(workspace_root)
            except ValueError:
                continue
            for path in sorted(search_root.rglob("*")):
                if not path.is_file():
                    continue
                if path.suffix.lower() not in SUPPORTED_IMAGE_EXTENSIONS:
                    continue
                resolved = path.resolve()
                if resolved in seen:
                    continue
                seen.add(resolved)
                results.append(resolved)
        return results

    def upload_asset(
        self,
        local_path: Path,
        workspace_root: Path,
        project_name: str = "",
    ) -> dict:
        relative_path = local_path.resolve().relative_to(workspace_root).as_posix()
        public_id = self._build_public_id(relative_path)
        upload_url = f"{CLOUDINARY_API_BASE}/{quote(self._cloud_name())}/image/upload"
        timestamp = int(time.time())
        upload_folder = self._upload_folder(project_name)
        params = {
            "folder": upload_folder,
            "overwrite": "true",
            "public_id": public_id,
            "timestamp": str(timestamp),
        }
        signature = self._sign_upload_params(params)
        mime_type = mimetypes.guess_type(str(local_path))[0] or "application/octet-stream"

        with local_path.open("rb") as fp:
            response = requests.post(
                upload_url,
                data={
                    **params,
                    "api_key": self._api_key(),
                    "signature": signature,
                },
                files={"file": (local_path.name, fp, mime_type)},
                timeout=60,
            )

        try:
            data = response.json()
        except Exception as exc:
            raise RuntimeError(
                f"Cloudinary 上传失败：HTTP {response.status_code} - {response.text[:200]}"
            ) from exc

        if response.status_code >= 400:
            error_message = str(((data or {}).get("error") or {}).get("message") or "").strip()
            raise RuntimeError(
                f"Cloudinary 上传失败：HTTP {response.status_code} - {error_message or '未知错误'}"
            )

        asset_public_id = str(data.get("public_id") or "").strip() or f"{upload_folder}/{public_id}"
        asset_format = str(data.get("format") or local_path.suffix.lstrip(".") or "png").strip()
        secure_url = str(data.get("secure_url") or "").strip()
        return {
            "source_file": relative_path,
            "public_id": asset_public_id,
            "resource_type": str(data.get("resource_type") or "image").strip() or "image",
            "original_url": secure_url,
            "hero_url": self._build_transformed_url(
                asset_public_id,
                asset_format,
                "f_auto,q_auto,w_1600,h_900,c_pad,b_white",
            ),
            "square_url": self._build_transformed_url(
                asset_public_id,
                asset_format,
                "f_auto,q_auto,w_1200,h_1200,c_pad,b_white",
            ),
            "transparent_url": self._build_transformed_url(
                asset_public_id,
                "png",
                "f_png,q_auto,w_1200,h_1200,c_pad,b_transparent",
            ),
            "thumbnail_url": self._build_transformed_url(
                asset_public_id,
                asset_format,
                "f_auto,q_auto,w_480,h_480,c_fill,g_auto",
            ),
            "width": int(data.get("width") or 0),
            "height": int(data.get("height") or 0),
            "tags": self._infer_tags(local_path),
            "notes": self._infer_notes(local_path),
        }

    def _cloud_name(self) -> str:
        return self._get_env("CLOUDINARY_CLOUD_NAME")

    def _api_key(self) -> str:
        return self._get_env("CLOUDINARY_API_KEY")

    def _api_secret(self) -> str:
        return self._get_env("CLOUDINARY_API_SECRET")

    def _upload_folder(self, project_name: str = "") -> str:
        base_folder = self._get_env("CLOUDINARY_UPLOAD_FOLDER") or DEFAULT_CLOUDINARY_UPLOAD_FOLDER
        normalized_project = self._safe_segment(project_name or "")
        return f"{base_folder}/{normalized_project}" if normalized_project else base_folder

    def _get_env(self, key: str) -> str:
        return str(self.env_vars.get(key) or os.getenv(key) or "").strip()

    def _sign_upload_params(self, params: Dict[str, str]) -> str:
        sorted_items = sorted(
            (key, str(value).strip())
            for key, value in params.items()
            if str(value).strip()
        )
        signature_payload = "&".join(f"{key}={value}" for key, value in sorted_items)
        signature_payload = f"{signature_payload}{self._api_secret()}"
        return hashlib.sha1(signature_payload.encode("utf-8")).hexdigest()

    def _build_public_id(self, relative_path: str) -> str:
        normalized_relative = str(relative_path or "").strip().replace("\\", "/")
        stem = "/".join(
            self._safe_segment(part)
            for part in Path(normalized_relative).with_suffix("").parts
            if self._safe_segment(part)
        )
        if not stem:
            stem = "asset"
        return stem

    def _build_transformed_url(self, public_id: str, asset_format: str, transformation: str) -> str:
        normalized_public_id = quote(str(public_id or "").strip(), safe="/")
        normalized_format = self._safe_segment(asset_format or "png") or "png"
        return (
            f"https://res.cloudinary.com/{quote(self._cloud_name())}/image/upload/"
            f"{transformation}/{normalized_public_id}.{normalized_format}"
        )

    @staticmethod
    def _safe_segment(value: str) -> str:
        normalized = re.sub(r"[^a-zA-Z0-9._/-]+", "-", str(value or "").strip())
        normalized = normalized.strip("-./")
        normalized = normalized.replace("//", "/")
        return normalized.lower()

    @classmethod
    def _infer_tags(cls, local_path: Path) -> List[str]:
        name = local_path.stem.lower()
        tags: List[str] = []
        if any(token in name for token in ("cover", "hero", "banner")):
            tags.extend(["cover", "hero"])
        if "logo" in name:
            tags.append("logo")
        if any(token in name for token in ("product", "sku", "item")):
            tags.append("product")
        if not tags:
            tags.append("image")
        deduped: List[str] = []
        seen: set[str] = set()
        for tag in tags:
            if tag not in seen:
                deduped.append(tag)
                seen.add(tag)
        return deduped

    @staticmethod
    def _infer_notes(local_path: Path) -> str:
        suffix = local_path.suffix.lower()
        if suffix == ".png":
            return "已同步 PNG 素材，可优先用于封面或透明背景场景"
        if suffix == ".webp":
            return "已同步 WebP 素材，适合网页加载优化"
        return "已同步原始素材，可用于画册排版和封面主图"
