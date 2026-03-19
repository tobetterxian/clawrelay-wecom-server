import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
import os

from config.bot_config import BotConfigManager
from src.core.codex_cli_orchestrator import CodexCliOrchestrator
from src.core.json_state_store import JsonStateStore
from src.core.project_registry import ProjectRegistry
from src.core.workspace_init_modes import (
    WORKSPACE_INIT_EMPTY,
    WORKSPACE_INIT_GIT_REMOTE,
    WORKSPACE_INIT_LEGACY_COPY,
)
from src.core.workspace_manager import WorkspaceManager


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


def test_json_state_store_recreates_missing_parent_directory():
    with TemporaryDirectory() as tmpdir:
        state_path = Path(tmpdir) / "state" / "sessions.json"
        store = JsonStateStore(str(state_path))

        state_path.parent.rmdir()
        store.write_list([{"ok": True}])

        assert state_path.exists()
        assert store.read_list() == [{"ok": True}]


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
