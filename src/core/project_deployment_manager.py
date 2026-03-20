"""
项目部署脚手架

负责为当前工作区写入 GitHub Actions / Cloudflare 部署配置，
并准备 / 切换 / 同步 Git 远程仓库。
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

logger = logging.getLogger(__name__)


def _utc_today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _slugify(value: str, fallback: str = "item") -> str:
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", (value or "").strip()).strip("-_.").lower()
    return slug or fallback


@dataclass
class FileWriteResult:
    relative_path: str
    action: str


@dataclass
class GitHubRemotePrepareResult:
    workspace_path: str
    origin_url: str
    repo_initialized: bool = False
    origin_action: str = "unchanged"
    current_branch: str = ""


@dataclass
class GitRemotePublishResult:
    workspace_path: str
    origin_url: str
    upstream_url: str = ""
    repo_initialized: bool = False
    origin_action: str = "unchanged"
    upstream_action: str = "unchanged"
    current_branch: str = ""
    remotes: Dict[str, str] = field(default_factory=dict)


@dataclass
class GitRemoteSyncResult:
    workspace_path: str
    remote_name: str
    remote_url: str
    fetch_action: str = "fetched"
    current_branch: str = ""
    remotes: Dict[str, str] = field(default_factory=dict)


@dataclass
class GitIdentityResult:
    workspace_path: str
    user_name: str = ""
    user_email: str = ""
    repo_exists: bool = False
    repo_initialized: bool = False
    is_configured: bool = False


@dataclass
class GitRemoteProbeResult:
    remote_url: str
    exists: bool = False
    error_kind: str = ""
    error_message: str = ""


@dataclass
class GitPushResult:
    workspace_path: str
    remote_name: str
    remote_url: str
    branch_name: str
    repo_initialized: bool = False
    had_changes: bool = False
    commit_created: bool = False
    commit_message: str = ""
    push_output: str = ""


@dataclass
class CloudflareDeployScaffoldResult:
    workspace_path: str
    deployment_type: str
    workflow_path: str
    files: List[FileWriteResult] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    pages_project_name: str = ""
    build_dir: str = ""
    worker_name: str = ""
    entry_file: str = ""
    compatibility_date: str = ""


class ProjectDeploymentManager:
    def __init__(self, repo_root: str = ""):
        self.repo_root = (
            Path(repo_root).expanduser().resolve()
            if repo_root
            else Path(__file__).resolve().parents[2]
        )
        self.examples_root = self.repo_root / "docs" / "examples"

    def prepare_github_remote(self, workspace_path: str, remote_url: str) -> GitHubRemotePrepareResult:
        root = self._resolve_workspace(workspace_path)
        normalized_remote_url = str(remote_url or "").strip()
        if not normalized_remote_url:
            raise ValueError("GitHub 仓库地址不能为空")

        result = GitHubRemotePrepareResult(
            workspace_path=str(root),
            origin_url=normalized_remote_url,
        )

        result.repo_initialized = self._ensure_git_repository(root)

        current_origin = self.get_git_remote(root, "origin")
        if not current_origin:
            self._run_git(root, "remote", "add", "origin", normalized_remote_url)
            result.origin_action = "added"
        elif current_origin != normalized_remote_url:
            self._run_git(root, "remote", "set-url", "origin", normalized_remote_url)
            result.origin_action = "updated"

        result.current_branch = self._current_branch(root)
        return result

    def publish_to_new_remote(
        self,
        workspace_path: str,
        publish_remote_url: str,
        upstream_remote_url: str = "",
    ) -> GitRemotePublishResult:
        root = self._resolve_workspace(workspace_path)
        normalized_publish_url = str(publish_remote_url or "").strip()
        normalized_upstream_url = str(upstream_remote_url or "").strip()
        if not normalized_publish_url:
            raise ValueError("新仓库地址不能为空")

        result = GitRemotePublishResult(
            workspace_path=str(root),
            origin_url=normalized_publish_url,
        )
        result.repo_initialized = self._ensure_git_repository(root)

        current_origin = self.get_git_remote(root, "origin")
        current_upstream = self.get_git_remote(root, "upstream")

        if normalized_upstream_url:
            if current_upstream and current_upstream != normalized_upstream_url:
                self._run_git(root, "remote", "set-url", "upstream", normalized_upstream_url)
                current_upstream = normalized_upstream_url
                result.upstream_action = "updated"
            elif not current_upstream and current_origin == normalized_upstream_url and current_origin != normalized_publish_url:
                self._run_git(root, "remote", "rename", "origin", "upstream")
                current_upstream = normalized_upstream_url
                current_origin = ""
                result.upstream_action = "preserved_from_origin"
            elif not current_upstream:
                self._run_git(root, "remote", "add", "upstream", normalized_upstream_url)
                current_upstream = normalized_upstream_url
                result.upstream_action = "added"
        elif not current_upstream and current_origin and current_origin != normalized_publish_url:
            self._run_git(root, "remote", "rename", "origin", "upstream")
            current_upstream = current_origin
            current_origin = ""
            result.upstream_action = "preserved_from_origin"

        current_origin = self.get_git_remote(root, "origin")
        if not current_origin:
            self._run_git(root, "remote", "add", "origin", normalized_publish_url)
            result.origin_action = "added"
        elif current_origin != normalized_publish_url:
            self._run_git(root, "remote", "set-url", "origin", normalized_publish_url)
            result.origin_action = "updated"

        result.remotes = self.list_git_remotes(root)
        result.upstream_url = result.remotes.get("upstream", "")
        result.current_branch = self._current_branch(root)
        return result

    def sync_upstream(
        self,
        workspace_path: str,
        upstream_remote_url: str = "",
    ) -> GitRemoteSyncResult:
        root = self._resolve_workspace(workspace_path)
        if not self._is_git_repository(root):
            raise RuntimeError("当前工作区还不是 Git 仓库")

        normalized_upstream_url = str(upstream_remote_url or "").strip()
        remotes = self.list_git_remotes(root)
        current_origin = remotes.get("origin", "")
        current_upstream = remotes.get("upstream", "")

        remote_name = ""
        remote_url = ""
        fetch_action = "fetched"

        if current_upstream:
            remote_name = "upstream"
            remote_url = current_upstream
            if normalized_upstream_url and current_upstream != normalized_upstream_url:
                self._run_git(root, "remote", "set-url", "upstream", normalized_upstream_url)
                remote_url = normalized_upstream_url
                fetch_action = "updated_upstream_and_fetched"
        elif normalized_upstream_url:
            if current_origin == normalized_upstream_url:
                remote_name = "origin"
                remote_url = current_origin
                fetch_action = "fetched_origin"
            else:
                self._run_git(root, "remote", "add", "upstream", normalized_upstream_url)
                remote_name = "upstream"
                remote_url = normalized_upstream_url
                fetch_action = "added_upstream_and_fetched"
        elif current_origin:
            remote_name = "origin"
            remote_url = current_origin
            fetch_action = "fetched_origin"
        else:
            raise RuntimeError("当前工作区没有可同步的远程仓库")

        self._run_git(root, "fetch", remote_name, "--prune")
        remotes = self.list_git_remotes(root)
        return GitRemoteSyncResult(
            workspace_path=str(root),
            remote_name=remote_name,
            remote_url=remote_url,
            fetch_action=fetch_action,
            current_branch=self._current_branch(root),
            remotes=remotes,
        )

    def scaffold_cloudflare_pages(
        self,
        workspace_path: str,
        pages_project_name: str,
        build_dir: str = "dist",
    ) -> CloudflareDeployScaffoldResult:
        root = self._resolve_workspace(workspace_path)
        normalized_project_name = _slugify(pages_project_name, "pages-project")
        normalized_build_dir = str(build_dir or "dist").strip() or "dist"

        workflow_content = self._load_text("github-actions/cloudflare-pages-deploy.yml")
        deploy_command = (
            f"pages deploy {shlex.quote(normalized_build_dir)} "
            f"--project-name={shlex.quote(normalized_project_name)}"
        )
        workflow_content = workflow_content.replace(
            "command: pages deploy ${{ vars.CF_PAGES_BUILD_DIR }} --project-name=${{ vars.CF_PAGES_PROJECT_NAME }}",
            f"command: {deploy_command}",
        )

        workflow_path = root / ".github" / "workflows" / "deploy-cloudflare-pages.yml"
        file_result = self._write_text(root, workflow_path, workflow_content)

        return CloudflareDeployScaffoldResult(
            workspace_path=str(root),
            deployment_type="cloudflare_pages",
            workflow_path=".github/workflows/deploy-cloudflare-pages.yml",
            files=[file_result],
            pages_project_name=normalized_project_name,
            build_dir=normalized_build_dir,
        )

    def scaffold_cloudflare_worker(
        self,
        workspace_path: str,
        worker_name: str,
        entry_file: str = "src/index.ts",
    ) -> CloudflareDeployScaffoldResult:
        root = self._resolve_workspace(workspace_path)
        normalized_worker_name = _slugify(worker_name, "worker")
        normalized_entry_file = str(entry_file or "src/index.ts").strip() or "src/index.ts"
        compatibility_date = _utc_today()

        workflow_content = self._load_text("github-actions/cloudflare-worker-deploy.yml")
        workflow_path = root / ".github" / "workflows" / "deploy-cloudflare-worker.yml"
        workflow_result = self._write_text(root, workflow_path, workflow_content)

        wrangler_path = root / "wrangler.toml"
        file_results: List[FileWriteResult] = [workflow_result]
        warnings: List[str] = []
        if wrangler_path.exists():
            file_results.append(FileWriteResult(relative_path="wrangler.toml", action="kept"))
            warnings.append("已存在 wrangler.toml，未覆盖，请自行确认 name/main/compatibility_date")
        else:
            wrangler_content = self._load_text("wrangler/worker/wrangler.toml.example")
            wrangler_content = wrangler_content.replace('name = "hello-worker"', f'name = "{normalized_worker_name}"')
            wrangler_content = wrangler_content.replace('main = "src/index.ts"', f'main = "{normalized_entry_file}"')
            wrangler_content = wrangler_content.replace(
                'compatibility_date = "2026-03-19"',
                f'compatibility_date = "{compatibility_date}"',
            )
            file_results.append(self._write_text(root, wrangler_path, wrangler_content))

        return CloudflareDeployScaffoldResult(
            workspace_path=str(root),
            deployment_type="cloudflare_worker",
            workflow_path=".github/workflows/deploy-cloudflare-worker.yml",
            files=file_results,
            warnings=warnings,
            worker_name=normalized_worker_name,
            entry_file=normalized_entry_file,
            compatibility_date=compatibility_date,
        )

    def get_git_origin(self, workspace_path: str | Path) -> str:
        return self.get_git_remote(workspace_path, "origin")

    def get_git_remote(self, workspace_path: str | Path, remote_name: str) -> str:
        root = self._resolve_workspace(workspace_path)
        if not self._is_git_repository(root):
            return ""
        completed = self._run_git_process(root, "remote", "get-url", remote_name)
        if completed is None:
            return ""
        output = (completed.stdout or completed.stderr or "").strip()
        if completed.returncode != 0:
            return ""
        return output

    def list_git_remotes(self, workspace_path: str | Path) -> Dict[str, str]:
        root = self._resolve_workspace(workspace_path)
        if not self._is_git_repository(root):
            return {}

        completed = self._run_git_process(root, "remote")
        if completed is None:
            return {}

        if completed.returncode != 0:
            return {}

        remotes: Dict[str, str] = {}
        for line in (completed.stdout or "").splitlines():
            name = line.strip()
            if not name:
                continue
            url = self.get_git_remote(root, name)
            if url:
                remotes[name] = url
        return remotes

    def get_git_identity(self, workspace_path: str | Path) -> GitIdentityResult:
        root = self._resolve_workspace(workspace_path)
        repo_exists = self._is_git_repository(root)
        if not repo_exists:
            return GitIdentityResult(
                workspace_path=str(root),
                repo_exists=False,
                is_configured=False,
            )

        user_name = self._read_git_config(root, "user.name")
        user_email = self._read_git_config(root, "user.email")
        return GitIdentityResult(
            workspace_path=str(root),
            user_name=user_name,
            user_email=user_email,
            repo_exists=True,
            is_configured=bool(user_name and user_email),
        )

    def set_git_identity(
        self,
        workspace_path: str | Path,
        user_name: str,
        user_email: str,
    ) -> GitIdentityResult:
        root = self._resolve_workspace(workspace_path)
        normalized_name = str(user_name or "").strip()
        normalized_email = str(user_email or "").strip()
        if not normalized_name:
            raise ValueError("Git user.name 不能为空")
        if not normalized_email:
            raise ValueError("Git user.email 不能为空")

        repo_initialized = self._ensure_git_repository(root)
        self._run_git(root, "config", "--local", "user.name", normalized_name)
        self._run_git(root, "config", "--local", "user.email", normalized_email)

        result = self.get_git_identity(root)
        result.repo_initialized = repo_initialized
        return result

    def probe_git_remote(self, remote_url: str) -> GitRemoteProbeResult:
        normalized_remote_url = str(remote_url or "").strip()
        if not normalized_remote_url:
            return GitRemoteProbeResult(
                remote_url="",
                exists=False,
                error_kind="empty_remote",
                error_message="远程仓库地址不能为空",
            )

        completed = self._run_git_process(self.repo_root, "ls-remote", normalized_remote_url, "HEAD")
        if completed is None:
            return GitRemoteProbeResult(
                remote_url=normalized_remote_url,
                exists=False,
                error_kind="git_not_found",
                error_message="未找到 git 命令",
            )

        output = (completed.stdout or completed.stderr or "").strip()
        if completed.returncode == 0:
            return GitRemoteProbeResult(
                remote_url=normalized_remote_url,
                exists=True,
            )

        lowered = output.lower()
        if "repository not found" in lowered:
            error_kind = "repository_not_found"
        elif "could not resolve host" in lowered or "name or service not known" in lowered:
            error_kind = "network_error"
        elif "permission denied" in lowered or "access rights" in lowered or "authentication failed" in lowered:
            error_kind = "auth_failed"
        else:
            error_kind = "unknown_error"

        return GitRemoteProbeResult(
            remote_url=normalized_remote_url,
            exists=False,
            error_kind=error_kind,
            error_message=output or f"git ls-remote {normalized_remote_url} failed",
        )

    def commit_and_push_current_branch(
        self,
        workspace_path: str | Path,
        commit_message: str,
        remote_name: str = "origin",
    ) -> GitPushResult:
        root = self._resolve_workspace(workspace_path)
        normalized_remote_name = str(remote_name or "origin").strip() or "origin"
        normalized_commit_message = str(commit_message or "").strip() or "chore: sync workspace changes"

        repo_initialized = self._ensure_git_repository(root)
        remote_url = self.get_git_remote(root, normalized_remote_name)
        if not remote_url:
            raise RuntimeError(f"当前工作区未配置远程 {normalized_remote_name}")

        branch_name = self._current_branch(root) or "main"
        had_changes = self._has_uncommitted_changes(root)
        commit_created = False
        if had_changes:
            self._run_git(root, "add", "-A")
            self._run_git(root, "commit", "-m", normalized_commit_message)
            commit_created = True

        if not self._has_head_commit(root):
            raise RuntimeError("当前工作区还没有可推送的提交，请先创建或修改文件后再推送")

        push_output = self._run_git(root, "push", "-u", normalized_remote_name, branch_name)
        return GitPushResult(
            workspace_path=str(root),
            remote_name=normalized_remote_name,
            remote_url=remote_url,
            branch_name=branch_name,
            repo_initialized=repo_initialized,
            had_changes=had_changes,
            commit_created=commit_created,
            commit_message=normalized_commit_message if commit_created else "",
            push_output=push_output,
        )

    @staticmethod
    def deployment_summary(project: dict) -> str:
        deployment_type = str(project.get("deployment_type") or "").strip()
        if not deployment_type:
            return "(未配置)"

        deployment_config = project.get("deployment_config") or {}
        github_remote_url = str(project.get("github_remote_url") or "").strip()
        lines = []
        if deployment_type == "cloudflare_pages":
            lines.append(
                "Cloudflare Pages"
                f" / project={deployment_config.get('pages_project_name', '-')}"
                f" / build_dir={deployment_config.get('build_dir', '-')}"
            )
        elif deployment_type == "cloudflare_worker":
            lines.append(
                "Cloudflare Worker"
                f" / name={deployment_config.get('worker_name', '-')}"
                f" / entry={deployment_config.get('entry_file', '-')}"
            )
        else:
            lines.append(deployment_type)

        if github_remote_url:
            lines.append(f"GitHub={github_remote_url}")
        return " | ".join(lines)

    def _resolve_workspace(self, workspace_path: str | Path) -> Path:
        root = Path(workspace_path).expanduser().resolve()
        if not root.exists():
            raise ValueError(f"工作区不存在: {root}")
        if not root.is_dir():
            raise ValueError(f"工作区不是目录: {root}")
        return root

    def _ensure_git_repository(self, root: Path) -> bool:
        if self._is_git_repository(root):
            return False
        if self._is_bare_git_repository(root):
            self._repair_bare_repository(root)
            if self._is_git_repository(root):
                return False

        self._run_git(root, "-c", "init.bare=false", "init")
        if self._is_bare_git_repository(root):
            self._repair_bare_repository(root)
        if not self._is_git_repository(root):
            raise RuntimeError(
                f"Git 初始化后仍无法识别当前工作区：{root}"
            )
        try:
            self._run_git(root, "symbolic-ref", "HEAD", "refs/heads/main")
        except RuntimeError:
            logger.warning("[Deploy] 设置默认分支为 main 失败，忽略")
        return True

    def _load_text(self, relative_path: str) -> str:
        return (self.examples_root / relative_path).read_text(encoding="utf-8")

    def _write_text(self, workspace_root: Path, path: Path, content: str) -> FileWriteResult:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            existing = path.read_text(encoding="utf-8")
            if existing == content:
                return FileWriteResult(relative_path=str(path.relative_to(workspace_root)), action="unchanged")
            action = "updated"
        else:
            action = "created"
        path.write_text(content, encoding="utf-8")
        return FileWriteResult(relative_path=str(path.relative_to(workspace_root)), action=action)

    def _run_git(self, cwd: Path, *args: str) -> str:
        completed = self._run_git_process(cwd, *args)
        if completed is None:
            raise RuntimeError("未找到 git 命令")

        output = (completed.stdout or completed.stderr or "").strip()
        if completed.returncode != 0:
            raise RuntimeError(output or f"git {' '.join(args)} failed")
        return output

    def _current_branch(self, cwd: Path) -> str:
        try:
            return self._run_git(cwd, "symbolic-ref", "--short", "HEAD").strip()
        except RuntimeError:
            return ""

    def _has_head_commit(self, cwd: Path) -> bool:
        completed = self._run_git_process(cwd, "rev-parse", "--verify", "HEAD")
        if completed is None:
            return False
        return completed.returncode == 0

    def _has_uncommitted_changes(self, cwd: Path) -> bool:
        completed = self._run_git_process(cwd, "status", "--porcelain")
        if completed is None:
            return False
        return bool((completed.stdout or "").strip())

    def _read_git_config(self, cwd: Path, key: str) -> str:
        completed = self._run_git_process(cwd, "config", "--local", "--get", key)
        if completed is None:
            return ""

        if completed.returncode != 0:
            return ""
        return (completed.stdout or "").strip()

    def _is_git_repository(self, cwd: Path) -> bool:
        completed = self._run_git_process(cwd, "rev-parse", "--is-inside-work-tree")
        if completed is None:
            return False
        return completed.returncode == 0 and (completed.stdout or "").strip().lower() == "true"

    def _is_bare_git_repository(self, cwd: Path) -> bool:
        completed = self._run_git_process(cwd, "rev-parse", "--is-bare-repository")
        if completed is None:
            return False
        return completed.returncode == 0 and (completed.stdout or "").strip().lower() == "true"

    def _repair_bare_repository(self, root: Path) -> None:
        git_dir = root / ".git"
        if git_dir.exists() and git_dir.is_dir():
            self._run_git(root, "config", "--bool", "core.bare", "false")
            return

        git_metadata_names = (
            "HEAD",
            "branches",
            "config",
            "description",
            "hooks",
            "info",
            "objects",
            "refs",
            "packed-refs",
            "logs",
            "index",
            "shallow",
            "commondir",
            "gitdir",
        )
        entries_to_move = [root / name for name in git_metadata_names if (root / name).exists()]
        if not entries_to_move:
            return

        git_dir.mkdir(parents=True, exist_ok=True)
        for entry in entries_to_move:
            target = git_dir / entry.name
            if target.exists():
                continue
            entry.replace(target)

        self._run_git(root, "config", "--bool", "core.bare", "false")

    @staticmethod
    def _git_env() -> Dict[str, str]:
        env = os.environ.copy()
        for key in (
            "GIT_DIR",
            "GIT_WORK_TREE",
            "GIT_COMMON_DIR",
            "GIT_INDEX_FILE",
            "GIT_OBJECT_DIRECTORY",
            "GIT_ALTERNATE_OBJECT_DIRECTORIES",
            "GIT_PREFIX",
        ):
            env.pop(key, None)
        return env

    def _git_command(self, cwd: Path, *args: str) -> List[str]:
        command = ["git"]
        safe_directory = self._safe_directory_value(cwd)
        if safe_directory:
            command.extend(["-c", f"safe.directory={safe_directory}"])
        command.extend(list(args))
        return command

    def _run_git_process(self, cwd: Path, *args: str):
        try:
            completed = subprocess.run(
                self._git_command(cwd, *args),
                cwd=str(cwd),
                check=False,
                capture_output=True,
                text=True,
                env=self._git_env(),
            )
        except FileNotFoundError:
            return None

        if completed.returncode == 0:
            return completed

        output = (completed.stdout or completed.stderr or "").strip().lower()
        if "detected dubious ownership" not in output:
            return completed

        self._ensure_global_safe_directory(cwd)
        try:
            return subprocess.run(
                self._git_command(cwd, *args),
                cwd=str(cwd),
                check=False,
                capture_output=True,
                text=True,
                env=self._git_env(),
            )
        except FileNotFoundError:
            return None

    def _ensure_global_safe_directory(self, cwd: Path) -> None:
        safe_directory = self._safe_directory_value(cwd)
        if not safe_directory:
            return
        if self._is_global_safe_directory_registered(safe_directory):
            return
        try:
            subprocess.run(
                ["git", "config", "--global", "--add", "safe.directory", safe_directory],
                cwd=str(self.repo_root),
                check=False,
                capture_output=True,
                text=True,
                env=self._git_env(),
            )
        except FileNotFoundError:
            return

    def _is_global_safe_directory_registered(self, safe_directory: str) -> bool:
        try:
            completed = subprocess.run(
                ["git", "config", "--global", "--get-all", "safe.directory"],
                cwd=str(self.repo_root),
                check=False,
                capture_output=True,
                text=True,
                env=self._git_env(),
            )
        except FileNotFoundError:
            return False

        if completed.returncode != 0:
            return False
        values = [
            str(line or "").strip().rstrip("/").lower()
            for line in (completed.stdout or "").splitlines()
            if str(line or "").strip()
        ]
        return safe_directory.rstrip("/").lower() in values

    @staticmethod
    def _safe_directory_value(cwd: Path) -> str:
        try:
            return cwd.resolve().as_posix()
        except OSError:
            return str(cwd)
