import logging
import subprocess
import asyncio
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
import os
from unittest.mock import patch
from urllib.error import HTTPError

from config.bot_config import BotConfig, BotConfigManager
from src.core.codex_cli_orchestrator import CodexCliOrchestrator
from src.core.github_repository_manager import GitHubRepositoryInfo, GitHubRepositoryManager
from src.core.json_state_store import JsonStateStore
from src.core.orchestrator_factory import OrchestratorFactory
from src.core.project_deployment_manager import ProjectDeploymentManager
from src.core.project_registry import ProjectRegistry
from src.core.workspace_init_modes import (
    WORKSPACE_INIT_EMPTY,
    WORKSPACE_INIT_GIT_REMOTE,
    WORKSPACE_INIT_LEGACY_COPY,
)
from src.core.workspace_manager import WorkspaceManager
from src.utils.codex_cli_runtime_checks import run_codex_cli_startup_check


def test_detects_transient_reconnect_message():
    assert CodexCliOrchestrator._is_transient_reconnect_message("Reconnecting... 1/5")
    assert not CodexCliOrchestrator._is_transient_reconnect_message("Process exited")


def test_normalizes_transient_reconnect_error_message():
    assert (
        CodexCliOrchestrator._normalize_codex_error_message("Reconnecting... 1/5")
        == "[CodexCLI] Reconnecting in progress"
    )


def test_friendly_error_maps_reconnect_messages():
    from src.transport.message_dispatcher import (
        _CODEX_CLI_RECONNECT_HINT,
        _friendly_error,
    )

    assert _friendly_error(Exception("Reconnecting... 1/5")) == _CODEX_CLI_RECONNECT_HINT
    assert (
        _friendly_error(Exception("[CodexCLI] Reconnecting in progress"))
        == _CODEX_CLI_RECONNECT_HINT
    )


def test_codex_cli_uses_shared_home_directory():
    with TemporaryDirectory() as tmpdir:
        working_dir = Path(tmpdir) / "project"
        working_dir.mkdir()
        orchestrator = CodexCliOrchestrator(
            bot_key="cx_bot",
            working_dir=str(working_dir),
        )
        expected_home = working_dir / ".codex_data" / "codex-home" / "cx_bot"
        assert Path(orchestrator.codex_home) == expected_home.resolve()
        assert orchestrator.adapter.env_vars["HOME"] == str(expected_home.resolve())


def test_display_path_wraps_windows_paths_for_markdown_safe_output():
    path_text = r"C:\next\.codex_data\projects\demo"
    assert CodexCliOrchestrator._display_path(path_text) == f"`{path_text}`"


def test_workspace_copy_ignores_codex_directory():
    with TemporaryDirectory() as tmpdir:
        source = Path(tmpdir) / "source"
        root = Path(tmpdir) / "root"
        source.mkdir()
        (source / ".codex").mkdir()
        (source / ".codex" / "auth.json").write_text("secret", encoding="utf-8")
        (source / "keep.txt").write_text("ok", encoding="utf-8")

        from src.core.workspace_manager import WorkspaceManager

        manager = WorkspaceManager(str(root))
        target = root / "projects" / "p1" / "workspaces" / "w1"
        target.mkdir(parents=True)
        manager._initialize_workspace_copy(source, target)

        assert not (target / ".codex").exists()
        assert (target / "keep.txt").read_text(encoding="utf-8") == "ok"


def test_default_personal_project_uses_empty_workspace():
    with TemporaryDirectory() as tmpdir:
        working_dir = Path(tmpdir) / "project"
        working_dir.mkdir()

        orchestrator = CodexCliOrchestrator(
            bot_key="cx_bot",
            working_dir=str(working_dir),
        )

        project, created = orchestrator._get_or_create_default_personal_project("alice")

        assert created is True
        assert project["workspace_init_mode"] == WORKSPACE_INIT_EMPTY
        assert project["source_path"] == ""
        assert project["git_remote_url"] == ""


def test_default_personal_project_notice_contains_usage_guidance():
    with TemporaryDirectory() as tmpdir:
        working_dir = Path(tmpdir) / "project"
        working_dir.mkdir()

        orchestrator = CodexCliOrchestrator(
            bot_key="cx_bot",
            working_dir=str(working_dir),
        )

        runtime_context, early_reply = orchestrator._ensure_single_runtime_context("alice", "alice")

        assert early_reply is None
        assert "默认个人项目" in runtime_context["initial_notice"]
        assert runtime_context["initial_notice"].strip() == "🆕 已自动创建默认个人项目：default"
        assert "首次使用说明" in runtime_context["first_reply_guidance"]
        assert "两级体系" in runtime_context["first_reply_guidance"]
        assert "6 hello-world" in runtime_context["first_reply_guidance"]
        assert "12" in runtime_context["first_reply_guidance"]
        assert "1" in runtime_context["first_reply_guidance"]


def test_workspace_copy_skips_windows_reserved_name():
    with TemporaryDirectory() as tmpdir:
        source = Path(tmpdir) / "source"
        root = Path(tmpdir) / "root"
        source.mkdir()
        (source / "nul").write_text("reserved", encoding="utf-8")
        (source / "keep.txt").write_text("ok", encoding="utf-8")

        manager = WorkspaceManager(str(root))
        target = root / "projects" / "p1" / "workspaces" / "w1"
        target.mkdir(parents=True)
        manager._initialize_workspace_copy(source, target)

        assert not (target / "nul").exists()
        assert (target / "keep.txt").read_text(encoding="utf-8") == "ok"


def test_git_remote_workspace_clones_repository():
    with TemporaryDirectory() as tmpdir:
        workspace_root = Path(tmpdir) / "workspace-root"
        repo = Path(tmpdir) / "remote-repo"
        repo.mkdir()

        _run_git("init", cwd=repo)
        _run_git("config", "user.email", "test@example.com", cwd=repo)
        _run_git("config", "user.name", "Test User", cwd=repo)
        (repo / "README.md").write_text("hello\n", encoding="utf-8")
        _run_git("add", "README.md", cwd=repo)
        _run_git("commit", "-m", "init", cwd=repo)

        registry = ProjectRegistry(str(workspace_root))
        manager = WorkspaceManager(str(workspace_root))
        project = registry.create_project(
            name="demo",
            kind="personal",
            owner_user_id="alice",
            workspace_init_mode=WORKSPACE_INIT_GIT_REMOTE,
            git_remote_url=str(repo),
        )

        workspace = manager.get_or_create_personal_workspace(project, "alice")

        assert project["workspace_init_mode"] == WORKSPACE_INIT_GIT_REMOTE
        assert (Path(workspace["path"]) / "README.md").exists()


def test_project_create_command_supports_new_modes():
    with TemporaryDirectory() as tmpdir:
        working_dir = Path(tmpdir) / "project"
        working_dir.mkdir()
        orchestrator = CodexCliOrchestrator(
            bot_key="cx_bot",
            working_dir=str(working_dir),
        )

        create_request, usage = orchestrator._parse_project_create_command(
            "新建仓库项目 demo https://example.com/demo.git"
        )
        assert usage is None
        assert create_request["workspace_init_mode"] == WORKSPACE_INIT_GIT_REMOTE
        assert create_request["git_remote_url"] == "https://example.com/demo.git"

        create_request, usage = orchestrator._parse_project_create_command("新建复制项目 demo")
        assert usage is None
        assert create_request["workspace_init_mode"] == WORKSPACE_INIT_LEGACY_COPY
        assert create_request["source_path"] == str(working_dir.resolve())

        create_request, usage = orchestrator._parse_project_create_command(
            "从仓库派生项目 demo https://example.com/base.git"
        )
        assert usage is None
        assert create_request["workspace_init_mode"] == WORKSPACE_INIT_GIT_REMOTE
        assert create_request["git_remote_url"] == "https://example.com/base.git"


def test_project_help_mentions_default_project_flow():
    help_text = CodexCliOrchestrator._project_command_help()
    assert "一级控制命令菜单" in help_text
    assert "二级普通对话" in help_text
    assert "default" in help_text
    assert "6. 新建项目 <名称>" in help_text
    assert "从仓库派生项目 <名称> <源Git地址>" in help_text
    assert "创建GitHub仓库 <仓库名>" in help_text
    assert "创建GitHub仓库并发布 <仓库名>" in help_text
    assert "GitHub仓库列表 [关键词]" in help_text
    assert "选择仓库 <序号>" in help_text
    assert "Git身份状态" in help_text
    assert "设置Git身份 <name> <email>" in help_text
    assert "推送到GitHub [仓库名]" in help_text
    assert "30. 部署帮助" in help_text
    assert "准备GitHub仓库 <Git地址>" in help_text
    assert "发布到新仓库 <新Git地址>" in help_text
    assert "帮助" in help_text
    assert help_text.index("27. 启用Worker部署") < help_text.index("28. 使用个人工作区")
    assert help_text.index("28. 使用个人工作区") < help_text.index("29. 使用共享工作区")
    assert help_text.index("29. 使用共享工作区") < help_text.index("30. 部署帮助")


def test_parse_deployment_commands():
    with TemporaryDirectory() as tmpdir:
        working_dir = Path(tmpdir) / "project"
        working_dir.mkdir()
        orchestrator = CodexCliOrchestrator(
            bot_key="cx_bot",
            working_dir=str(working_dir),
        )

        request, usage = orchestrator._parse_deployment_command(
            "准备GitHub仓库 git@github.com:demo/hello.git"
        )
        assert usage is None
        assert request["action"] == "prepare_github_remote"
        assert request["remote_url"] == "git@github.com:demo/hello.git"

        request, usage = orchestrator._parse_deployment_command(
            "发布到新仓库 git@github.com:demo/publish.git"
        )
        assert usage is None
        assert request["action"] == "publish_new_remote"
        assert request["remote_url"] == "git@github.com:demo/publish.git"

        request, usage = orchestrator._parse_deployment_command("同步上游")
        assert usage is None
        assert request["action"] == "sync_upstream"
        assert request["upstream_remote_url"] == ""

        request, usage = orchestrator._parse_deployment_command("启用Pages部署 hello-pages dist")
        assert usage is None
        assert request["action"] == "enable_pages"
        assert request["pages_project_name"] == "hello-pages"
        assert request["build_dir"] == "dist"

        request, usage = orchestrator._parse_deployment_command(
            "启用Worker部署 hello-worker src/index.ts"
        )
        assert usage is None
        assert request["action"] == "enable_worker"
        assert request["worker_name"] == "hello-worker"
        assert request["entry_file"] == "src/index.ts"


def test_parse_github_repository_commands():
    with TemporaryDirectory() as tmpdir:
        working_dir = Path(tmpdir) / "project"
        working_dir.mkdir()
        orchestrator = CodexCliOrchestrator(
            bot_key="cx_bot",
            working_dir=str(working_dir),
        )

        request, usage = orchestrator._parse_github_repository_command("GitHub仓库列表 hello")
        assert usage is None
        assert request["action"] == "list_user_repositories"
        assert request["query"] == "hello"

        request, usage = orchestrator._parse_github_repository_command("GitHub组织仓库 openai sdk")
        assert usage is None
        assert request["action"] == "list_org_repositories"
        assert request["org"] == "openai"
        assert request["query"] == "sdk"

        request, usage = orchestrator._parse_github_repository_command("选择仓库 3")
        assert usage is None
        assert request["action"] == "select_repository"
        assert request["index"] == 3

        request, usage = orchestrator._parse_github_repository_command("从选中仓库派生项目 hello-app")
        assert usage is None
        assert request["action"] == "derive_from_selected_repository"
        assert request["name"] == "hello-app"

        request, usage = orchestrator._parse_github_repository_command("创建GitHub仓库 hello-repo")
        assert usage is None
        assert request["action"] == "create_user_repository"
        assert request["name"] == "hello-repo"
        assert request["private"] is True
        assert request["publish_after_create"] is False

        request, usage = orchestrator._parse_github_repository_command("创建GitHub公开仓库并发布 hello-repo")
        assert usage is None
        assert request["action"] == "create_user_repository"
        assert request["name"] == "hello-repo"
        assert request["private"] is False
        assert request["publish_after_create"] is True

        request, usage = orchestrator._parse_github_repository_command(
            "创建GitHub组织仓库 demo-org hello-repo"
        )
        assert usage is None
        assert request["action"] == "create_org_repository"
        assert request["org"] == "demo-org"
        assert request["name"] == "hello-repo"


def test_parse_git_identity_commands():
    with TemporaryDirectory() as tmpdir:
        working_dir = Path(tmpdir) / "project"
        working_dir.mkdir()
        orchestrator = CodexCliOrchestrator(
            bot_key="cx_bot",
            working_dir=str(working_dir),
        )

        request, usage = orchestrator._parse_git_identity_command(
            '设置Git身份 "Kangaroo 117" kangaroo117@users.noreply.github.com'
        )

        assert usage is None
        assert request["action"] == "set_git_identity"
        assert request["name"] == "Kangaroo 117"
        assert request["email"] == "kangaroo117@users.noreply.github.com"


def test_parse_git_identity_command_returns_richer_usage():
    with TemporaryDirectory() as tmpdir:
        working_dir = Path(tmpdir) / "project"
        working_dir.mkdir()
        orchestrator = CodexCliOrchestrator(
            bot_key="cx_bot",
            working_dir=str(working_dir),
        )

        request, usage = orchestrator._parse_git_identity_command("设置Git身份 onlyname")

        assert request is None
        assert "Git 身份命令格式不完整" in usage
        assert "11 <name> <email>" in usage
        assert "kangaroo117@users.noreply.github.com" in usage


def test_parse_github_push_commands():
    with TemporaryDirectory() as tmpdir:
        working_dir = Path(tmpdir) / "project"
        working_dir.mkdir()
        orchestrator = CodexCliOrchestrator(
            bot_key="cx_bot",
            working_dir=str(working_dir),
        )

        request, usage = orchestrator._parse_github_push_command("推送到GitHub hello-world")
        assert usage is None
        assert request["action"] == "push_to_github"
        assert request["name"] == "hello-world"
        assert request["private"] is True

        request, usage = orchestrator._parse_github_push_command("推送到GitHub公开 hello-world")
        assert usage is None
        assert request["name"] == "hello-world"
        assert request["private"] is False


def test_default_project_usage_hint_detects_named_project_request():
    hint = CodexCliOrchestrator._build_default_project_usage_hint(
        message_content="请帮我创建一个 hello world 项目并开始实现",
        runtime_context={"project": {"name": "default"}},
    )
    assert "默认项目 default" in hint
    assert "新建项目 <名称>" in hint


def test_default_project_usage_hint_ignores_regular_request():
    hint = CodexCliOrchestrator._build_default_project_usage_hint(
        message_content="请修复当前目录里的一个 bug",
        runtime_context={"project": {"name": "default"}},
    )
    assert hint == ""


def test_json_state_store_recreates_missing_parent_directory():
    with TemporaryDirectory() as tmpdir:
        state_path = Path(tmpdir) / "state" / "sessions.json"
        store = JsonStateStore(str(state_path))

        state_path.parent.rmdir()
        store.write_list([{"ok": True}])

        assert state_path.exists()
        assert store.read_list() == [{"ok": True}]


def test_prepare_github_remote_initializes_repo_and_origin():
    with TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir) / "workspace"
        workspace.mkdir()

        manager = ProjectDeploymentManager()
        result = manager.prepare_github_remote(
            str(workspace),
            "git@github.com:demo/hello.git",
        )

        assert result.repo_initialized is True
        assert result.origin_action == "added"
        assert result.origin_url == "git@github.com:demo/hello.git"
        assert result.current_branch == "main"
        assert manager.get_git_origin(workspace) == "git@github.com:demo/hello.git"


def test_github_repository_manager_filters_repositories():
    manager = GitHubRepositoryManager(env_vars={"GITHUB_TOKEN": "dummy"})
    manager._request_repositories = lambda endpoint, query_params: [
        {
            "full_name": "demo/hello-world",
            "name": "hello-world",
            "owner": {"login": "demo"},
            "private": False,
            "default_branch": "main",
            "updated_at": "2026-03-19T10:00:00Z",
            "description": "hello example",
            "clone_url": "https://github.com/demo/hello-world.git",
            "ssh_url": "git@github.com:demo/hello-world.git",
            "html_url": "https://github.com/demo/hello-world",
        },
        {
            "full_name": "demo/internal-tool",
            "name": "internal-tool",
            "owner": {"login": "demo"},
            "private": True,
            "default_branch": "main",
            "updated_at": "2026-03-18T10:00:00Z",
            "description": "private tool",
            "clone_url": "https://github.com/demo/internal-tool.git",
            "ssh_url": "git@github.com:demo/internal-tool.git",
            "html_url": "https://github.com/demo/internal-tool",
        },
    ]

    repositories = manager.list_user_repositories(query="hello", limit=10)

    assert len(repositories) == 1
    assert repositories[0].full_name == "demo/hello-world"
    assert repositories[0].preferred_clone_url == "git@github.com:demo/hello-world.git"


def test_github_repository_manager_creates_repository():
    manager = GitHubRepositoryManager(env_vars={"GITHUB_TOKEN": "dummy"})
    manager._request_json = lambda endpoint, query_params=None, method="GET", payload=None: {
        "full_name": "kangaroo117/hello-repo",
        "name": "hello-repo",
        "owner": {"login": "kangaroo117"},
        "private": True,
        "default_branch": "main",
        "updated_at": "2026-03-19T10:00:00Z",
        "description": "",
        "clone_url": "https://github.com/kangaroo117/hello-repo.git",
        "ssh_url": "git@github.com:kangaroo117/hello-repo.git",
        "html_url": "https://github.com/kangaroo117/hello-repo",
    }

    repository = manager.create_user_repository("hello-repo", private=True)

    assert repository.full_name == "kangaroo117/hello-repo"
    assert repository.private is True
    assert repository.ssh_url == "git@github.com:kangaroo117/hello-repo.git"


def test_publish_to_new_remote_preserves_upstream():
    with TemporaryDirectory() as tmpdir:
        source = Path(tmpdir) / "source"
        source.mkdir()

        _run_git("init", cwd=source)
        _run_git("symbolic-ref", "HEAD", "refs/heads/main", cwd=source)
        _run_git("config", "user.email", "test@example.com", cwd=source)
        _run_git("config", "user.name", "Test User", cwd=source)
        (source / "README.md").write_text("hello\n", encoding="utf-8")
        _run_git("add", "README.md", cwd=source)
        _run_git("commit", "-m", "init", cwd=source)

        workspace = Path(tmpdir) / "workspace"
        _run_git("clone", str(source), str(workspace), cwd=Path(tmpdir))

        manager = ProjectDeploymentManager()
        result = manager.publish_to_new_remote(
            str(workspace),
            "git@github.com:demo/publish.git",
            upstream_remote_url=str(source),
        )
        remotes = manager.list_git_remotes(workspace)

        assert result.origin_action == "added"
        assert result.upstream_action == "preserved_from_origin"
        assert remotes["origin"] == "git@github.com:demo/publish.git"
        assert remotes["upstream"] == str(source)


def test_project_deployment_manager_sets_git_identity_for_workspace():
    with TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir) / "workspace"
        workspace.mkdir()

        manager = ProjectDeploymentManager()
        before = manager.get_git_identity(str(workspace))
        result = manager.set_git_identity(
            str(workspace),
            "kangaroo117",
            "kangaroo117@users.noreply.github.com",
        )

        assert before.repo_exists is False
        assert before.is_configured is False
        assert result.repo_exists is True
        assert result.repo_initialized is True
        assert result.is_configured is True
        assert result.user_name == "kangaroo117"
        assert result.user_email == "kangaroo117@users.noreply.github.com"
        assert _git_output("config", "--local", "--get", "user.name", cwd=workspace) == "kangaroo117"
        assert _git_output("config", "--local", "--get", "user.email", cwd=workspace) == "kangaroo117@users.noreply.github.com"


def test_set_git_identity_ignores_global_git_env():
    with TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir) / "workspace"
        workspace.mkdir()

        manager = ProjectDeploymentManager()
        original_git_dir = os.environ.get("GIT_DIR")
        original_git_work_tree = os.environ.get("GIT_WORK_TREE")
        try:
            os.environ["GIT_DIR"] = str(Path(tmpdir) / "bogus-git-dir")
            os.environ["GIT_WORK_TREE"] = str(Path(tmpdir) / "bogus-work-tree")

            result = manager.set_git_identity(
                str(workspace),
                "kangaroo117",
                "kangaroo117@users.noreply.github.com",
            )
        finally:
            if original_git_dir is None:
                os.environ.pop("GIT_DIR", None)
            else:
                os.environ["GIT_DIR"] = original_git_dir
            if original_git_work_tree is None:
                os.environ.pop("GIT_WORK_TREE", None)
            else:
                os.environ["GIT_WORK_TREE"] = original_git_work_tree

        assert result.is_configured is True
        assert result.repo_exists is True


def test_set_git_identity_repairs_bare_repository():
    with TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir) / "workspace"
        workspace.mkdir()

        _run_git("init", "--bare", cwd=workspace)

        manager = ProjectDeploymentManager()
        result = manager.set_git_identity(
            str(workspace),
            "kangaroo117",
            "kangaroo117@users.noreply.github.com",
        )

        assert result.is_configured is True
        assert result.repo_exists is True
        assert (workspace / ".git").exists()
        assert _git_output("config", "--local", "--get", "user.name", cwd=workspace) == "kangaroo117"


def test_git_command_includes_safe_directory():
    with TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir) / "workspace"
        workspace.mkdir()

        manager = ProjectDeploymentManager()
        command = manager._git_command(workspace, "status", "--porcelain")

        assert command[0] == "git"
        assert command[1] == "-c"
        assert command[2].startswith("safe.directory=")
        assert workspace.resolve().as_posix() in command[2]
        assert command[-2:] == ["status", "--porcelain"]


def test_run_git_process_auto_registers_safe_directory_on_dubious_ownership():
    with TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir) / "workspace"
        workspace.mkdir()

        manager = ProjectDeploymentManager()
        safe_directory = workspace.resolve().as_posix()
        original_run = subprocess.run
        actual_git_calls = {"count": 0}
        add_safe_directory_called = {"value": False}

        class _Result:
            def __init__(self, returncode=0, stdout="", stderr=""):
                self.returncode = returncode
                self.stdout = stdout
                self.stderr = stderr

        def fake_run(cmd, cwd=None, check=False, capture_output=False, text=False, env=None, input=None):
            if cmd == ["git", "config", "--global", "--get-all", "safe.directory"]:
                return _Result(returncode=1, stdout="", stderr="")
            if cmd == ["git", "config", "--global", "--add", "safe.directory", safe_directory]:
                add_safe_directory_called["value"] = True
                return _Result(returncode=0, stdout="", stderr="")
            if cmd[:3] == ["git", "-c", f"safe.directory={safe_directory}"] and cmd[-2:] == ["status", "--porcelain"]:
                actual_git_calls["count"] += 1
                if actual_git_calls["count"] == 1:
                    return _Result(
                        returncode=128,
                        stdout="",
                        stderr="fatal: detected dubious ownership in repository at 'C:/repo'",
                    )
                return _Result(returncode=0, stdout="", stderr="")
            return _Result(returncode=0, stdout="", stderr="")

        try:
            subprocess.run = fake_run
            completed = manager._run_git_process(workspace, "status", "--porcelain")
        finally:
            subprocess.run = original_run

        assert completed is not None
        assert completed.returncode == 0
        assert actual_git_calls["count"] == 2
        assert add_safe_directory_called["value"] is True


def test_sync_upstream_fetches_remote_updates():
    with TemporaryDirectory() as tmpdir:
        source = Path(tmpdir) / "source"
        source.mkdir()

        _run_git("init", cwd=source)
        _run_git("symbolic-ref", "HEAD", "refs/heads/main", cwd=source)
        _run_git("config", "user.email", "test@example.com", cwd=source)
        _run_git("config", "user.name", "Test User", cwd=source)
        (source / "README.md").write_text("hello\n", encoding="utf-8")
        _run_git("add", "README.md", cwd=source)
        _run_git("commit", "-m", "init", cwd=source)

        workspace = Path(tmpdir) / "workspace"
        _run_git("clone", str(source), str(workspace), cwd=Path(tmpdir))

        manager = ProjectDeploymentManager()
        manager.publish_to_new_remote(
            str(workspace),
            "git@github.com:demo/publish.git",
            upstream_remote_url=str(source),
        )

        (source / "CHANGELOG.md").write_text("update\n", encoding="utf-8")
        _run_git("add", "CHANGELOG.md", cwd=source)
        _run_git("commit", "-m", "update", cwd=source)

        result = manager.sync_upstream(str(workspace), str(source))
        remote_branches = _git_output("branch", "-r", cwd=workspace)

        assert result.remote_name == "upstream"
        assert "upstream/main" in remote_branches


def test_orchestrator_supports_derive_publish_and_remote_status_commands():
    with TemporaryDirectory() as tmpdir:
        source = Path(tmpdir) / "source"
        source.mkdir()

        _run_git("init", cwd=source)
        _run_git("symbolic-ref", "HEAD", "refs/heads/main", cwd=source)
        _run_git("config", "user.email", "test@example.com", cwd=source)
        _run_git("config", "user.name", "Test User", cwd=source)
        (source / "README.md").write_text("hello\n", encoding="utf-8")
        _run_git("add", "README.md", cwd=source)
        _run_git("commit", "-m", "init", cwd=source)

        working_dir = Path(tmpdir) / "project"
        working_dir.mkdir()
        orchestrator = CodexCliOrchestrator(
            bot_key="cx_bot",
            working_dir=str(working_dir),
        )

        async def run_flow():
            reply = await orchestrator.handle_control_command(
                "alice",
                f"从仓库派生项目 demo {source}",
                session_key="alice",
            )
            remote_before = await orchestrator.handle_control_command(
                "alice",
                "远程状态",
                session_key="alice",
            )
            publish_reply = await orchestrator.handle_control_command(
                "alice",
                "发布到新仓库 git@github.com:demo/publish.git",
                session_key="alice",
            )
            remote_after = await orchestrator.handle_control_command(
                "alice",
                "远程状态",
                session_key="alice",
            )
            return reply, remote_before, publish_reply, remote_after

        reply, remote_before, publish_reply, remote_after = asyncio.run(run_flow())

        assert "已创建个人项目：demo" in reply
        assert f"来源仓库：{source}" in remote_before
        assert "发布到新的 Git 仓库" in publish_reply
        assert "发布仓库：git@github.com:demo/publish.git" in remote_after
        assert f"当前 upstream：{source}" in remote_after


def test_orchestrator_lists_selects_and_derives_from_github_repository():
    with TemporaryDirectory() as tmpdir:
        source = Path(tmpdir) / "source"
        source.mkdir()

        _run_git("init", cwd=source)
        _run_git("symbolic-ref", "HEAD", "refs/heads/main", cwd=source)
        _run_git("config", "user.email", "test@example.com", cwd=source)
        _run_git("config", "user.name", "Test User", cwd=source)
        (source / "README.md").write_text("hello\n", encoding="utf-8")
        _run_git("add", "README.md", cwd=source)
        _run_git("commit", "-m", "init", cwd=source)

        working_dir = Path(tmpdir) / "project"
        working_dir.mkdir()
        orchestrator = CodexCliOrchestrator(
            bot_key="cx_bot",
            working_dir=str(working_dir),
        )
        orchestrator.github_repository_manager.list_user_repositories = lambda query="", limit=10, owner_only=False: [
            GitHubRepositoryInfo(
                full_name="demo/source",
                name="source",
                owner="demo",
                private=False,
                default_branch="main",
                updated_at="2026-03-19T12:00:00Z",
                description="demo repo",
                clone_url=str(source),
                ssh_url="",
                html_url="https://github.com/demo/source",
            )
        ]

        async def run_flow():
            list_reply = await orchestrator.handle_control_command(
                "alice",
                "GitHub仓库列表 source",
                session_key="alice",
            )
            select_reply = await orchestrator.handle_control_command(
                "alice",
                "选择仓库 1",
                session_key="alice",
            )
            current_reply = await orchestrator.handle_control_command(
                "alice",
                "当前选中仓库",
                session_key="alice",
            )
            derive_reply = await orchestrator.handle_control_command(
                "alice",
                "从选中仓库派生项目 derived-demo",
                session_key="alice",
            )
            project_reply = await orchestrator.handle_control_command(
                "alice",
                "当前项目",
                session_key="alice",
            )
            return list_reply, select_reply, current_reply, derive_reply, project_reply

        list_reply, select_reply, current_reply, derive_reply, project_reply = asyncio.run(run_flow())

        assert "GitHub 仓库列表（当前账号） / 关键词：source" in list_reply
        assert "1. demo/source" in list_reply
        assert "已选中 GitHub 仓库：demo/source" in select_reply
        assert f"克隆地址：{source}" in current_reply
        assert "已创建个人项目：derived-demo" in derive_reply
        assert f"来源仓库：{source}" in project_reply


def test_orchestrator_creates_github_repository_without_gh():
    with TemporaryDirectory() as tmpdir:
        working_dir = Path(tmpdir) / "project"
        working_dir.mkdir()
        orchestrator = CodexCliOrchestrator(
            bot_key="cx_bot",
            working_dir=str(working_dir),
        )
        orchestrator.github_repository_manager.create_user_repository = lambda name, private=True: GitHubRepositoryInfo(
            full_name=f"kangaroo117/{name}",
            name=name,
            owner="kangaroo117",
            private=private,
            default_branch="main",
            updated_at="2026-03-19T12:00:00Z",
            description="",
            clone_url=f"https://github.com/kangaroo117/{name}.git",
            ssh_url=f"git@github.com:kangaroo117/{name}.git",
            html_url=f"https://github.com/kangaroo117/{name}",
        )
        original_alias_checker = orchestrator._ssh_host_alias_configured
        orchestrator._ssh_host_alias_configured = lambda alias: alias == "github-kangaroo117"
        try:
            reply = asyncio.run(
                orchestrator.handle_control_command(
                    "alice",
                    "创建GitHub仓库 hello-repo",
                    session_key="alice",
                )
            )
        finally:
            orchestrator._ssh_host_alias_configured = original_alias_checker

        assert "已创建 GitHub 仓库：kangaroo117/hello-repo" in reply
        assert "推荐发布地址：git@github-kangaroo117:kangaroo117/hello-repo.git" in reply
        assert "可发送：发布到新仓库 git@github-kangaroo117:kangaroo117/hello-repo.git" in reply


def test_orchestrator_creates_and_publishes_github_repository():
    with TemporaryDirectory() as tmpdir:
        working_dir = Path(tmpdir) / "project"
        working_dir.mkdir()
        orchestrator = CodexCliOrchestrator(
            bot_key="cx_bot",
            working_dir=str(working_dir),
        )
        orchestrator.github_repository_manager.create_user_repository = lambda name, private=True: GitHubRepositoryInfo(
            full_name=f"kangaroo117/{name}",
            name=name,
            owner="kangaroo117",
            private=private,
            default_branch="main",
            updated_at="2026-03-19T12:00:00Z",
            description="",
            clone_url=f"https://github.com/kangaroo117/{name}.git",
            ssh_url=f"git@github.com:kangaroo117/{name}.git",
            html_url=f"https://github.com/kangaroo117/{name}",
        )
        original_alias_checker = orchestrator._ssh_host_alias_configured
        orchestrator._ssh_host_alias_configured = lambda alias: alias == "github-kangaroo117"
        try:
            reply = asyncio.run(
                orchestrator.handle_control_command(
                    "alice",
                    "创建GitHub仓库并发布 hello-repo",
                    session_key="alice",
                )
            )
            current_project = asyncio.run(
                orchestrator.handle_control_command(
                    "alice",
                    "当前项目",
                    session_key="alice",
                )
            )
        finally:
            orchestrator._ssh_host_alias_configured = original_alias_checker

        assert "已创建 GitHub 仓库：kangaroo117/hello-repo" in reply
        assert "已将当前项目发布到新的 Git 仓库" in reply
        assert "git@github-kangaroo117:kangaroo117/hello-repo.git" in current_project


def test_orchestrator_supports_git_identity_commands_and_project_status():
    with TemporaryDirectory() as tmpdir:
        working_dir = Path(tmpdir) / "project"
        working_dir.mkdir()
        orchestrator = CodexCliOrchestrator(
            bot_key="cx_bot",
            working_dir=str(working_dir),
        )

        async def run_flow():
            create_reply = await orchestrator.handle_control_command(
                "alice",
                "新建项目 demo",
                session_key="alice",
            )
            status_before = await orchestrator.handle_control_command(
                "alice",
                "Git身份状态",
                session_key="alice",
            )
            set_reply = await orchestrator.handle_control_command(
                "alice",
                "设置Git身份 kangaroo117 kangaroo117@users.noreply.github.com",
                session_key="alice",
            )
            status_after = await orchestrator.handle_control_command(
                "alice",
                "Git身份状态",
                session_key="alice",
            )
            project_reply = await orchestrator.handle_control_command(
                "alice",
                "当前项目",
                session_key="alice",
            )
            return create_reply, status_before, set_reply, status_after, project_reply

        create_reply, status_before, set_reply, status_after, project_reply = asyncio.run(run_flow())

        assert "已创建个人项目：demo" in create_reply
        assert "Git仓库：未初始化" in status_before
        assert "状态：未配置" in status_before
        assert "可发送：设置Git身份 <name> <email>" in status_before
        assert "已设置当前工作区 Git 身份" in set_reply
        assert "Git 初始化：已初始化新仓库" in set_reply
        assert "user.name：kangaroo117" in set_reply
        assert "user.email：kangaroo117@users.noreply.github.com" in set_reply
        assert "状态：已配置" in status_after
        assert "user.name：kangaroo117" in status_after
        assert "Git身份：kangaroo117 <kangaroo117@users.noreply.github.com>" in project_reply


def test_push_to_github_without_repo_name_requires_named_project():
    with TemporaryDirectory() as tmpdir:
        working_dir = Path(tmpdir) / "project"
        working_dir.mkdir()
        orchestrator = CodexCliOrchestrator(
            bot_key="cx_bot",
            working_dir=str(working_dir),
        )

        async def run_flow():
            await orchestrator.handle_control_command(
                "alice",
                "设置Git身份 kangaroo117 kangaroo117@users.noreply.github.com",
                session_key="alice",
            )
            return await orchestrator.handle_control_command(
                "alice",
                "推送到GitHub",
                session_key="alice",
            )

        reply = asyncio.run(run_flow())

        assert "当前项目还没有合适的 GitHub 仓库名" in reply
        assert "推送到GitHub <仓库名>" in reply


def test_orchestrator_push_to_github_creates_remote_and_pushes():
    with TemporaryDirectory() as tmpdir:
        remote_repo = Path(tmpdir) / "remote.git"
        remote_repo.mkdir()
        _run_git("init", "--bare", cwd=remote_repo)

        working_dir = Path(tmpdir) / "project"
        working_dir.mkdir()
        orchestrator = CodexCliOrchestrator(
            bot_key="cx_bot",
            working_dir=str(working_dir),
        )
        orchestrator.github_repository_manager.create_user_repository = lambda name, private=True: GitHubRepositoryInfo(
            full_name=f"kangaroo117/{name}",
            name=name,
            owner="kangaroo117",
            private=private,
            default_branch="main",
            updated_at="2026-03-19T12:00:00Z",
            description="",
            clone_url=str(remote_repo),
            ssh_url="",
            html_url=f"https://github.com/kangaroo117/{name}",
        )
        original_alias_checker = orchestrator._ssh_host_alias_configured
        orchestrator._ssh_host_alias_configured = lambda alias: False

        try:
            async def run_flow():
                create_reply = await orchestrator.handle_control_command(
                    "alice",
                    "新建项目 hello-world",
                    session_key="alice",
                )
                runtime_context, _ = orchestrator._ensure_single_runtime_context("alice", "alice")
                workspace = Path(runtime_context["working_dir"])
                (workspace / "README.md").write_text("hello\n", encoding="utf-8")
                await orchestrator.handle_control_command(
                    "alice",
                    "设置Git身份 kangaroo117 kangaroo117@users.noreply.github.com",
                    session_key="alice",
                )
                push_reply = await orchestrator.handle_control_command(
                    "alice",
                    "推送到GitHub公开",
                    session_key="alice",
                )
                return create_reply, push_reply, workspace

            create_reply, push_reply, workspace = asyncio.run(run_flow())
        finally:
            orchestrator._ssh_host_alias_configured = original_alias_checker

        result = subprocess.run(
            ["git", "--git-dir", str(remote_repo), "rev-parse", "--verify", "refs/heads/main"],
            check=False,
            capture_output=True,
            text=True,
        )

        assert "已创建个人项目：hello-world" in create_reply
        assert "已提交并推送当前项目到 GitHub" in push_reply
        assert "自动创建仓库：kangaroo117/hello-world" in push_reply
        assert "origin：" in push_reply
        assert result.returncode == 0
        assert "README.md" in _git_output("ls-tree", "--name-only", "HEAD", cwd=workspace)


def test_push_intent_preflight_suggests_push_command():
    with TemporaryDirectory() as tmpdir:
        working_dir = Path(tmpdir) / "project"
        working_dir.mkdir()
        orchestrator = CodexCliOrchestrator(
            bot_key="cx_bot",
            working_dir=str(working_dir),
        )

        async def run_flow():
            await orchestrator.handle_control_command(
                "alice",
                "新建项目 hello-world",
                session_key="alice",
            )
            await orchestrator.handle_control_command(
                "alice",
                "设置Git身份 kangaroo117 kangaroo117@users.noreply.github.com",
                session_key="alice",
            )
            return orchestrator._maybe_handle_push_to_github_intent(
                "alice",
                "请帮我推送到 github",
                "alice",
                {},
            )

        reply = asyncio.run(run_flow())

        assert "看起来你是想把当前项目推送到 GitHub" in reply
        assert "一级控制命令" in reply
        assert "19 hello-world" in reply


def test_numeric_shortcuts_execute_control_commands():
    with TemporaryDirectory() as tmpdir:
        working_dir = Path(tmpdir) / "project"
        working_dir.mkdir()
        orchestrator = CodexCliOrchestrator(
            bot_key="cx_bot",
            working_dir=str(working_dir),
        )

        async def run_flow():
            help_reply = await orchestrator.handle_control_command(
                "alice",
                "1",
                session_key="alice",
            )
            create_reply = await orchestrator.handle_control_command(
                "alice",
                "6 hello-world",
                session_key="alice",
            )
            git_reply = await orchestrator.handle_control_command(
                "alice",
                "11 kangaroo117 kangaroo117@users.noreply.github.com",
                session_key="alice",
            )
            return help_reply, create_reply, git_reply

        help_reply, create_reply, git_reply = asyncio.run(run_flow())

        assert orchestrator.is_control_command("1") is True
        assert orchestrator.is_control_command("6 hello-world") is True
        assert "一级控制命令菜单" in help_reply
        assert "已创建个人项目：hello-world" in create_reply
        assert "已设置当前工作区 Git 身份" in git_reply


def test_numeric_shortcuts_do_not_capture_regular_dialogue():
    with TemporaryDirectory() as tmpdir:
        working_dir = Path(tmpdir) / "project"
        working_dir.mkdir()
        orchestrator = CodexCliOrchestrator(
            bot_key="cx_bot",
            working_dir=str(working_dir),
        )

        assert orchestrator.is_control_command("1 + 1 = ?") is False
        assert orchestrator._normalize_control_command_input("1 + 1 = ?") == "1 + 1 = ?"


def test_numeric_shortcuts_support_push_command_arguments():
    with TemporaryDirectory() as tmpdir:
        working_dir = Path(tmpdir) / "project"
        working_dir.mkdir()
        orchestrator = CodexCliOrchestrator(
            bot_key="cx_bot",
            working_dir=str(working_dir),
        )

        normalized = orchestrator._normalize_control_command_input("19 hello-world")
        request, usage = orchestrator._parse_github_push_command(normalized)

        assert usage is None
        assert normalized == "推送到GitHub hello-world"
        assert request["action"] == "push_to_github"
        assert request["name"] == "hello-world"


def test_list_github_repositories_uses_configured_owner():
    with TemporaryDirectory() as tmpdir:
        working_dir = Path(tmpdir) / "project"
        working_dir.mkdir()
        orchestrator = CodexCliOrchestrator(
            bot_key="cx_bot",
            working_dir=str(working_dir),
            default_github_owner="kangaroo117",
        )
        orchestrator.github_repository_manager.get_current_user_login = lambda: "kangaroo117"
        orchestrator.github_repository_manager.list_user_repositories = (
            lambda query="", limit=10, owner_only=False: [
                GitHubRepositoryInfo(
                    full_name="kangaroo117/hello-world",
                    name="hello-world",
                    owner="kangaroo117",
                    private=False,
                    default_branch="main",
                    updated_at="2026-03-19T12:00:00Z",
                    description="my repo",
                    clone_url="https://github.com/kangaroo117/hello-world.git",
                    ssh_url="git@github.com:kangaroo117/hello-world.git",
                    html_url="https://github.com/kangaroo117/hello-world",
                ),
                GitHubRepositoryInfo(
                    full_name="other/demo",
                    name="demo",
                    owner="other",
                    private=False,
                    default_branch="main",
                    updated_at="2026-03-19T11:00:00Z",
                    description="other repo",
                    clone_url="https://github.com/other/demo.git",
                    ssh_url="git@github.com:other/demo.git",
                    html_url="https://github.com/other/demo",
                ),
            ]
        )

        reply = asyncio.run(
            orchestrator.handle_control_command(
                "alice",
                "12",
                session_key="alice",
            )
        )

        assert "GitHub 仓库列表（账号 kangaroo117）" in reply
        assert "kangaroo117/hello-world" in reply
        assert "other/demo" not in reply


def test_create_github_repository_rejects_configured_owner_mismatch():
    with TemporaryDirectory() as tmpdir:
        working_dir = Path(tmpdir) / "project"
        working_dir.mkdir()
        orchestrator = CodexCliOrchestrator(
            bot_key="cx_bot",
            working_dir=str(working_dir),
            default_github_owner="kangaroo117",
        )
        orchestrator.github_repository_manager.get_current_user_login = lambda: "someone-else"

        reply = asyncio.run(
            orchestrator.handle_control_command(
                "alice",
                "16 hello-repo",
                session_key="alice",
            )
        )

        assert "创建 GitHub 仓库失败" in reply
        assert "GITHUB_TOKEN 当前账号为 someone-else" in reply


def test_github_repository_manager_formats_pat_permission_error_for_listing():
    manager = GitHubRepositoryManager(env_vars={"GITHUB_TOKEN": "token"})
    error = HTTPError(
        url="https://api.github.com/user/repos",
        code=403,
        msg="Forbidden",
        hdrs={"X-Accepted-GitHub-Permissions": "metadata=read"},
        fp=BytesIO(
            b'{"message":"Resource not accessible by personal access token","documentation_url":"https://docs.github.com/rest/repos/repos#list-repositories-for-the-authenticated-user"}'
        ),
    )

    with patch("src.core.github_repository_manager.urlopen", side_effect=error):
        try:
            manager.list_user_repositories()
            raise AssertionError("expected RuntimeError")
        except RuntimeError as exc:
            message = str(exc)

    assert "GitHub Token 权限不足" in message
    assert "列出当前账号仓库" in message
    assert "metadata=read" in message
    assert "fine-grained PAT" in message


def test_github_repository_manager_formats_pat_permission_error_for_creation():
    manager = GitHubRepositoryManager(env_vars={"GITHUB_TOKEN": "token"})
    error = HTTPError(
        url="https://api.github.com/user/repos",
        code=403,
        msg="Forbidden",
        hdrs={"X-Accepted-GitHub-Permissions": "administration=write,metadata=read"},
        fp=BytesIO(
            b'{"message":"Resource not accessible by personal access token","documentation_url":"https://docs.github.com/rest/repos/repos#create-a-repository-for-the-authenticated-user"}'
        ),
    )

    with patch("src.core.github_repository_manager.urlopen", side_effect=error):
        try:
            manager.create_user_repository("hello-world")
            raise AssertionError("expected RuntimeError")
        except RuntimeError as exc:
            message = str(exc)

    assert "GitHub Token 权限不足" in message
    assert "创建当前账号仓库" in message
    assert "All repositories" in message
    assert "administration=write" in message
    assert "SSH 推送" in message


def test_push_to_github_rebinds_to_configured_owner():
    with TemporaryDirectory() as tmpdir:
        remote_repo = Path(tmpdir) / "remote.git"
        remote_repo.mkdir()
        _run_git("init", "--bare", cwd=remote_repo)

        working_dir = Path(tmpdir) / "project"
        working_dir.mkdir()
        orchestrator = CodexCliOrchestrator(
            bot_key="cx_bot",
            working_dir=str(working_dir),
            default_github_owner="kangaroo117",
        )
        orchestrator.github_repository_manager.get_current_user_login = lambda: "kangaroo117"
        orchestrator.github_repository_manager.create_user_repository = lambda name, private=True: GitHubRepositoryInfo(
            full_name=f"kangaroo117/{name}",
            name=name,
            owner="kangaroo117",
            private=private,
            default_branch="main",
            updated_at="2026-03-19T12:00:00Z",
            description="",
            clone_url=str(remote_repo),
            ssh_url="",
            html_url=f"https://github.com/kangaroo117/{name}",
        )
        original_alias_checker = orchestrator._ssh_host_alias_configured
        orchestrator._ssh_host_alias_configured = lambda alias: False

        try:
            async def run_flow():
                await orchestrator.handle_control_command(
                    "alice",
                    "6 hello-world",
                    session_key="alice",
                )
                runtime_context, _ = orchestrator._ensure_single_runtime_context("alice", "alice")
                workspace = Path(runtime_context["working_dir"])
                (workspace / "README.md").write_text("hello\n", encoding="utf-8")
                await orchestrator.handle_control_command(
                    "alice",
                    "11 kangaroo117 kangaroo117@users.noreply.github.com",
                    session_key="alice",
                )
                await orchestrator.handle_control_command(
                    "alice",
                    "23 git@github.com:other/hello-world.git",
                    session_key="alice",
                )
                return await orchestrator.handle_control_command(
                    "alice",
                    "19",
                    session_key="alice",
                )

            push_reply = asyncio.run(run_flow())
        finally:
            orchestrator._ssh_host_alias_configured = original_alias_checker

        assert "已提交并推送当前项目到 GitHub" in push_reply
        assert "自动创建仓库：kangaroo117/hello-world" in push_reply
        assert "检测到当前远程账号为 other，已切换为统一 GitHub 账号 kangaroo117" in push_reply


def test_scaffold_cloudflare_pages_writes_workflow():
    with TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir) / "workspace"
        workspace.mkdir()

        manager = ProjectDeploymentManager()
        result = manager.scaffold_cloudflare_pages(
            str(workspace),
            pages_project_name="Hello Pages",
            build_dir="build-output",
        )

        workflow_path = workspace / ".github" / "workflows" / "deploy-cloudflare-pages.yml"
        content = workflow_path.read_text(encoding="utf-8")

        assert result.deployment_type == "cloudflare_pages"
        assert result.pages_project_name == "hello-pages"
        assert result.build_dir == "build-output"
        assert workflow_path.exists()
        assert "pages deploy build-output --project-name=hello-pages" in content


def test_scaffold_cloudflare_worker_writes_workflow_and_wrangler():
    with TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir) / "workspace"
        workspace.mkdir()

        manager = ProjectDeploymentManager()
        result = manager.scaffold_cloudflare_worker(
            str(workspace),
            worker_name="Hello Worker",
            entry_file="src/worker.ts",
        )

        workflow_path = workspace / ".github" / "workflows" / "deploy-cloudflare-worker.yml"
        wrangler_path = workspace / "wrangler.toml"
        wrangler_content = wrangler_path.read_text(encoding="utf-8")

        assert result.deployment_type == "cloudflare_worker"
        assert result.worker_name == "hello-worker"
        assert result.entry_file == "src/worker.ts"
        assert workflow_path.exists()
        assert wrangler_path.exists()
        assert 'name = "hello-worker"' in wrangler_content
        assert 'main = "src/worker.ts"' in wrangler_content


def test_project_registry_update_project_supports_deployment_metadata():
    with TemporaryDirectory() as tmpdir:
        registry = ProjectRegistry(tmpdir)
        project = registry.create_project(
            name="demo",
            kind="personal",
            owner_user_id="alice",
            workspace_init_mode=WORKSPACE_INIT_EMPTY,
        )

        updated = registry.update_project(
            project["project_id"],
            github_remote_url="git@github.com:demo/hello.git",
            deployment_type="cloudflare_pages",
            deployment_config={
                "pages_project_name": "hello-pages",
                "build_dir": "dist",
            },
        )

        assert updated is not None
        assert updated["github_remote_url"] == "git@github.com:demo/hello.git"
        assert updated["deployment_type"] == "cloudflare_pages"
        assert updated["deployment_config"]["pages_project_name"] == "hello-pages"


def test_git_remote_project_tracks_source_metadata():
    with TemporaryDirectory() as tmpdir:
        registry = ProjectRegistry(tmpdir)
        project = registry.create_project(
            name="demo",
            kind="personal",
            owner_user_id="alice",
            workspace_init_mode=WORKSPACE_INIT_GIT_REMOTE,
            git_remote_url="https://example.com/base.git",
        )

        assert project["git_remote_url"] == "https://example.com/base.git"
        assert project["source_git_remote_url"] == "https://example.com/base.git"
        assert project["github_remote_url"] == "https://example.com/base.git"
        assert project["publish_git_remote_url"] == ""


def test_bot_config_expands_env_placeholders():
    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "bots.yaml"
        config_path.write_text(
            """
bots:
  demo:
    bot_id: "${BOT_ID}"
    secret: "${BOT_SECRET}"
    bot_type: "openai"
    working_dir: "${WORK_DIR:-/workspace}"
    env_vars:
      OPENAI_API_KEY: "${OPENAI_API_KEY}"
    provider_config:
      api_key: "${OPENAI_API_KEY}"
      base_url: "${OPENAI_BASE_URL:-https://api.openai.com/v1}"
""".strip(),
            encoding="utf-8",
        )

        original_values = {
            "BOT_ID": os.environ.get("BOT_ID"),
            "BOT_SECRET": os.environ.get("BOT_SECRET"),
            "OPENAI_API_KEY": os.environ.get("OPENAI_API_KEY"),
            "OPENAI_BASE_URL": os.environ.get("OPENAI_BASE_URL"),
            "WORK_DIR": os.environ.get("WORK_DIR"),
        }
        try:
            os.environ["BOT_ID"] = "bot-1"
            os.environ["BOT_SECRET"] = "secret-1"
            os.environ["OPENAI_API_KEY"] = "sk-test"
            os.environ.pop("OPENAI_BASE_URL", None)
            os.environ.pop("WORK_DIR", None)

            manager = BotConfigManager(str(config_path))
            bot = manager.get_bot("demo")

            assert bot is not None
            assert bot.bot_id == "bot-1"
            assert bot.secret == "secret-1"
            assert bot.working_dir == "/workspace"
            assert bot.env_vars["OPENAI_API_KEY"] == "sk-test"
            assert bot.provider_config["api_key"] == "sk-test"
            assert bot.provider_config["base_url"] == "https://api.openai.com/v1"
        finally:
            for key, value in original_values.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def test_bot_config_aggregates_missing_env_warnings(caplog):
    with TemporaryDirectory() as tmpdir:
        caplog.set_level(logging.WARNING)
        config_path = Path(tmpdir) / "bots.yaml"
        config_path.write_text(
            """
bots:
  demo:
    bot_id: "${BOT_ID}"
    secret: "${BOT_SECRET}"
    bot_type: "openai"
    env_vars:
      OPENAI_API_KEY: "${OPENAI_API_KEY}"
    provider_config:
      api_key: "${OPENAI_API_KEY}"
      base_url: "${OPENAI_BASE_URL:-https://api.openai.com/v1}"
""".strip(),
            encoding="utf-8",
        )

        original_values = {
            "BOT_ID": os.environ.get("BOT_ID"),
            "BOT_SECRET": os.environ.get("BOT_SECRET"),
            "OPENAI_API_KEY": os.environ.get("OPENAI_API_KEY"),
            "OPENAI_BASE_URL": os.environ.get("OPENAI_BASE_URL"),
        }
        try:
            os.environ.pop("BOT_ID", None)
            os.environ.pop("BOT_SECRET", None)
            os.environ.pop("OPENAI_API_KEY", None)
            os.environ.pop("OPENAI_BASE_URL", None)

            manager = BotConfigManager(str(config_path))
            assert manager.get_bot("demo") is None

            warning_messages = [
                record.getMessage()
                for record in caplog.records
                if "未设置的环境变量" in record.getMessage()
            ]
            assert len(warning_messages) == 1
            warning_message = warning_messages[0]
            assert "BOT_ID" in warning_message
            assert "BOT_SECRET" in warning_message
            assert "OPENAI_API_KEY" in warning_message
            assert "bots.demo.bot_id" in warning_message
            assert "bots.demo.secret" in warning_message
        finally:
            for key, value in original_values.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def test_setup_wizard_reports_non_interactive_hint(capsys):
    with TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "bots.yaml"
        manager = BotConfigManager(str(config_path))

        assert manager.run_setup_wizard() is False

        output = capsys.readouterr().out
        assert "非交互环境" in output
        assert ".env.example" in output
        assert "config/bots.yaml.example" in output


def test_codex_docker_yaml_examples_parse():
    import yaml

    for relative_path in [
        "docker-compose.yml",
        "docker-compose.codex.yml",
        "docker-compose.override.example.yml",
        "config/bots.yaml.example",
        "config/bots.codex-cli.docker.yaml.example",
    ]:
        with open(relative_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        assert data is not None


def test_factory_passes_default_github_owner():
    with TemporaryDirectory() as tmpdir:
        working_dir = Path(tmpdir) / "workspace"
        working_dir.mkdir()
        bot_config = BotConfig(
            bot_key="codex_bot",
            bot_id="bot-1",
            secret="secret-1",
            bot_type="codex_cli",
            working_dir=str(working_dir),
            provider_config={
                "default_github_owner": "kangaroo117",
            },
        )

        orchestrator = OrchestratorFactory.create(bot_config)

        assert isinstance(orchestrator, CodexCliOrchestrator)
        assert orchestrator.default_github_owner == "kangaroo117"


def test_runtime_checks_pass_default_github_owner():
    with TemporaryDirectory() as tmpdir:
        working_dir = Path(tmpdir) / "workspace"
        working_dir.mkdir()

        bot_config = BotConfig(
            bot_key="codex_bot",
            bot_id="bot-1",
            secret="secret-1",
            bot_type="codex_cli",
            working_dir=str(working_dir),
            provider_config={
                "default_github_owner": "kangaroo117",
            },
        )

        orchestrator = run_codex_cli_startup_check(bot_config).orchestrator

        assert isinstance(orchestrator, CodexCliOrchestrator)
        assert orchestrator.default_github_owner == "kangaroo117"


def test_codex_cli_startup_check_reports_missing_executable():
    with TemporaryDirectory() as tmpdir:
        working_dir = Path(tmpdir) / "workspace"
        working_dir.mkdir()

        bot_config = BotConfig(
            bot_key="codex_bot",
            bot_id="bot-1",
            secret="secret-1",
            bot_type="codex_cli",
            working_dir=str(working_dir),
            provider_config={
                "codex_path": "definitely-not-installed-codex",
                "workspace_root": str(Path(tmpdir) / "workspaces"),
                "codex_home": str(Path(tmpdir) / "codex-home"),
            },
        )

        result = run_codex_cli_startup_check(bot_config)
        assert result.errors
        assert "未找到 codex 可执行文件" in result.errors[0]


def _run_git(*args: str, cwd: Path) -> None:
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr or result.stdout or f"git {' '.join(args)} failed")


def _git_output(*args: str, cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr or result.stdout or f"git {' '.join(args)} failed")
    return (result.stdout or "").strip()
