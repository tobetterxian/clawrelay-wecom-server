"""
产品画册导出管理器

负责把当前工作区里的 HTML/H5 画册导出为 PDF、PNG 预览图和 PPT。
默认通过 `npm exec` 按需拉起 Playwright / PptxGenJS，避免强依赖本地全局安装。
"""

from __future__ import annotations

import base64
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
import subprocess
from typing import Dict, List


@dataclass(frozen=True)
class BrochureExportResult:
    input_relative_path: str
    output_relative_path: str
    output_path: str
    tool_name: str


class BrochureExportManager:
    """画册导出执行器"""

    def __init__(self, env_vars: Dict[str, str] | None = None):
        self.env_vars = dict(env_vars or {})
        self.repo_root = Path(__file__).resolve().parents[2]
        self.tool_workspace = self.repo_root / "scripts" / "brochure"
        self._node_dependencies_ready = False
        self._playwright_browser_ready = False

    def export_pdf(
        self,
        workspace_path: str,
        html_path: str = "brochure/index.html",
        output_path: str = "dist/brochure.pdf",
    ) -> BrochureExportResult:
        return self._export_with_playwright(
            workspace_path=workspace_path,
            mode="pdf",
            html_path=html_path,
            output_path=output_path,
        )

    def export_image(
        self,
        workspace_path: str,
        html_path: str = "brochure/index.html",
        output_path: str = "dist/brochure-preview.png",
    ) -> BrochureExportResult:
        return self._export_with_playwright(
            workspace_path=workspace_path,
            mode="image",
            html_path=html_path,
            output_path=output_path,
        )

    def export_ppt(
        self,
        workspace_path: str,
        outline_path: str = "",
        output_path: str = "dist/brochure.pptx",
    ) -> BrochureExportResult:
        workspace_root = Path(workspace_path).expanduser().resolve()
        outline_file = self._resolve_outline_path(workspace_root, outline_path)
        output_file = self._resolve_workspace_path(
            workspace_root,
            output_path or "dist/brochure.pptx",
            must_exist=False,
        )
        output_file.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_node_dependencies()
        script_path = self.tool_workspace / "export_ppt.mjs"
        self._run_command(
            [
                "node",
                str(script_path),
                str(outline_file),
                str(output_file),
            ],
            friendly_name="导出画册 PPT",
            cwd=self.tool_workspace,
        )
        return BrochureExportResult(
            input_relative_path=outline_file.relative_to(workspace_root).as_posix(),
            output_relative_path=output_file.relative_to(workspace_root).as_posix(),
            output_path=str(output_file),
            tool_name="pptxgenjs",
        )

    @staticmethod
    def encode_image_file(image_path: str) -> tuple[str, str]:
        data = Path(image_path).read_bytes()
        return (
            base64.b64encode(data).decode("utf-8"),
            hashlib.md5(data).hexdigest(),
        )

    def _export_with_playwright(
        self,
        workspace_path: str,
        mode: str,
        html_path: str,
        output_path: str,
    ) -> BrochureExportResult:
        workspace_root = Path(workspace_path).expanduser().resolve()
        html_file = self._resolve_workspace_path(
            workspace_root,
            html_path or "brochure/index.html",
            must_exist=True,
        )
        output_file = self._resolve_workspace_path(
            workspace_root,
            output_path
            or ("dist/brochure.pdf" if mode == "pdf" else "dist/brochure-preview.png"),
            must_exist=False,
        )
        output_file.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_node_dependencies()
        self._ensure_playwright_chromium()

        script_path = self.tool_workspace / "export_playwright.mjs"
        self._run_command(
            [
                "node",
                str(script_path),
                mode,
                str(html_file),
                str(output_file),
            ],
            friendly_name="导出画册 PDF" if mode == "pdf" else "导出画册图片",
            cwd=self.tool_workspace,
        )
        return BrochureExportResult(
            input_relative_path=html_file.relative_to(workspace_root).as_posix(),
            output_relative_path=output_file.relative_to(workspace_root).as_posix(),
            output_path=str(output_file),
            tool_name="playwright",
        )

    def _ensure_playwright_chromium(self) -> None:
        if self._playwright_browser_ready:
            return
        self._ensure_node_dependencies()
        self._run_command(
            [
                "npm",
                "exec",
                "--",
                "playwright",
                "install",
                "chromium",
            ],
            friendly_name="安装 Playwright Chromium",
            cwd=self.tool_workspace,
        )
        self._playwright_browser_ready = True

    def _ensure_node_dependencies(self) -> None:
        if self._node_dependencies_ready:
            return
        package_json = self.tool_workspace / "package.json"
        if not package_json.exists():
            raise RuntimeError("未找到 `scripts/brochure/package.json`，无法安装画册导出依赖")
        self._run_command(
            ["npm", "install", "--no-fund", "--no-audit"],
            friendly_name="安装画册导出依赖",
            cwd=self.tool_workspace,
        )
        self._node_dependencies_ready = True

    def _run_command(self, command: List[str], friendly_name: str, cwd: Path | None = None) -> None:
        env = os.environ.copy()
        env.update(self.env_vars)
        try:
            completed = subprocess.run(
                command,
                cwd=str((cwd or self.repo_root).resolve()),
                check=False,
                capture_output=True,
                text=True,
                env=env,
            )
        except FileNotFoundError as exc:
            executable = command[0] if command else "命令"
            raise RuntimeError(f"{friendly_name}失败：未找到 `{executable}` 命令") from exc

        if completed.returncode == 0:
            return

        output = "\n".join(
            part.strip()
            for part in (completed.stdout or "", completed.stderr or "")
            if str(part or "").strip()
        ).strip()
        message = output.splitlines()[-1].strip() if output else f"exit code {completed.returncode}"
        raise RuntimeError(f"{friendly_name}失败：{message}")

    @staticmethod
    def _resolve_workspace_path(
        workspace_root: Path,
        relative_path: str,
        *,
        must_exist: bool,
    ) -> Path:
        normalized = str(relative_path or "").strip().replace("\\", "/")
        if not normalized:
            raise ValueError("路径不能为空")
        if normalized.startswith("/"):
            raise ValueError("路径必须位于当前项目工作区内，不能使用绝对路径")

        candidate = (workspace_root / normalized).resolve()
        try:
            candidate.relative_to(workspace_root)
        except ValueError as exc:
            raise ValueError("路径必须位于当前项目工作区内") from exc

        if must_exist and not candidate.exists():
            raise FileNotFoundError(f"未找到文件：{normalized}")
        return candidate

    def _resolve_outline_path(self, workspace_root: Path, outline_path: str) -> Path:
        normalized = str(outline_path or "").strip()
        if normalized:
            return self._resolve_workspace_path(workspace_root, normalized, must_exist=True)

        brochure_outline = workspace_root / "docs" / "brochure-outline.md"
        if brochure_outline.exists():
            return brochure_outline.resolve()

        requirement_doc = workspace_root / "docs" / "requirements.md"
        if requirement_doc.exists():
            return requirement_doc.resolve()

        raise FileNotFoundError("未找到 `docs/brochure-outline.md`，也未找到 `docs/requirements.md`")
