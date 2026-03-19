import logging
import subprocess
import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
import os

from config.bot_config import BotConfig, BotConfigManager
from src.core.codex_cli_orchestrator import CodexCliOrchestrator
from src.core.json_state_store import JsonStateStore
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
        assert "新建项目 <名称>" in runtime_context["first_reply_guidance"]
        assert "项目帮助 / 帮助" in runtime_context["first_reply_guidance"]


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
    assert "默认个人项目 default" in help_text
    assert "新建项目 <名称>" in help_text
    assert "从仓库派生项目 <名称> <源Git地址>" in help_text
    assert "部署帮助" in help_text
    assert "准备GitHub仓库 <Git地址>" in help_text
    assert "发布到新仓库 <新Git地址>" in help_text
    assert "帮助" in help_text


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
