"""
Codex CLI 运行时检查

用于：
1. 服务启动前的 Codex CLI 自检
2. Docker healthcheck
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from config.bot_config import BotConfig, BotConfigManager
from src.core.base_orchestrator import BaseOrchestrator
from src.core.codex_cli_orchestrator import CodexCliOrchestrator

logger = logging.getLogger(__name__)


@dataclass
class CodexCliRuntimeCheckResult:
    bot_key: str
    executable: str = ""
    resolved_executable: str = ""
    working_dir: str = ""
    workspace_root: str = ""
    codex_home: str = ""
    codex_version: str = ""
    git_version: str = ""
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    orchestrator: Optional[BaseOrchestrator] = None


def run_codex_cli_startup_check(bot_config: BotConfig) -> CodexCliRuntimeCheckResult:
    result = CodexCliRuntimeCheckResult(bot_key=bot_config.bot_key)

    try:
        orchestrator = _create_codex_cli_orchestrator(bot_config)
    except Exception as exc:
        result.errors.append(str(exc))
        return result

    result.orchestrator = orchestrator
    if not isinstance(orchestrator, CodexCliOrchestrator):
        result.errors.append(f"bot {bot_config.bot_key} 不是 codex_cli 类型")
        return result

    executable = str(orchestrator.adapter.executable or "codex").strip() or "codex"
    result.executable = executable
    result.working_dir = orchestrator.base_working_dir
    result.workspace_root = orchestrator.workspace_root
    result.codex_home = orchestrator.codex_home

    resolved_executable = _resolve_executable(executable)
    if not resolved_executable:
        result.errors.append(
            f"未找到 codex 可执行文件：{executable}"
        )
        return result
    result.resolved_executable = resolved_executable

    codex_ok, codex_output = _run_version_command([resolved_executable, "--version"])
    if codex_ok:
        result.codex_version = codex_output
    else:
        result.errors.append(f"codex 自检失败：{codex_output}")

    git_executable = shutil.which("git") or ""
    if git_executable:
        git_ok, git_output = _run_version_command([git_executable, "--version"])
        if git_ok:
            result.git_version = git_output
        else:
            result.warnings.append(f"git 自检失败：{git_output}")
    else:
        result.warnings.append("未找到 git，可继续运行，但 git_remote / git push 能力将不可用")

    _check_directory(
        result.working_dir,
        label="working_dir",
        warnings=result.warnings,
        errors=result.errors,
        require_writable=False,
    )
    _check_directory(
        result.workspace_root,
        label="workspace_root",
        warnings=result.warnings,
        errors=result.errors,
        require_writable=True,
    )
    _check_directory(
        result.codex_home,
        label="codex_home",
        warnings=result.warnings,
        errors=result.errors,
        require_writable=True,
    )

    if not _has_codex_credentials(bot_config, result.codex_home):
        result.warnings.append(
            "未检测到 OPENAI_API_KEY 或 Codex 登录态目录，首次调用前请先注入 API key 或完成 codex 登录"
        )

    return result


def run_codex_cli_startup_checks(bot_configs: Dict[str, BotConfig]) -> Dict[str, BaseOrchestrator]:
    prepared_orchestrators: Dict[str, BaseOrchestrator] = {}
    failures: List[str] = []

    for bot_key, bot_config in bot_configs.items():
        if (bot_config.bot_type or "").strip() != "codex_cli":
            continue

        result = run_codex_cli_startup_check(bot_config)
        emit_codex_cli_check_result(result, stage="startup")

        if result.errors:
            failures.append(f"{bot_key}: {'; '.join(result.errors)}")
            continue

        if result.orchestrator is not None:
            prepared_orchestrators[bot_key] = result.orchestrator

    if failures:
        raise RuntimeError(
            "Codex CLI 启动自检失败：\n- " + "\n- ".join(failures)
        )

    return prepared_orchestrators


def format_codex_cli_check_result(
    result: CodexCliRuntimeCheckResult,
    stage: str = "healthcheck",
) -> List[str]:
    lines = [
        (
            f"[CodexCLI:{stage}] bot={result.bot_key} "
            f"executable={result.executable or '-'} "
            f"working_dir={result.working_dir or '-'} "
            f"workspace_root={result.workspace_root or '-'} "
            f"codex_home={result.codex_home or '-'}"
        )
    ]
    if result.resolved_executable:
        lines.append(f"[CodexCLI:{stage}] resolved_executable={result.resolved_executable}")
    if result.codex_version:
        lines.append(f"[CodexCLI:{stage}] codex_version={result.codex_version}")
    if result.git_version:
        lines.append(f"[CodexCLI:{stage}] git_version={result.git_version}")
    for warning in result.warnings:
        lines.append(f"[CodexCLI:{stage}] warning: {warning}")
    for error in result.errors:
        lines.append(f"[CodexCLI:{stage}] error: {error}")
    if not result.warnings and not result.errors:
        lines.append(f"[CodexCLI:{stage}] status=ok")
    return lines


def emit_codex_cli_check_result(
    result: CodexCliRuntimeCheckResult,
    stage: str = "startup",
) -> None:
    for line in format_codex_cli_check_result(result, stage=stage):
        if " error: " in line:
            logger.error(line)
        elif " warning: " in line:
            logger.warning(line)
        else:
            logger.info(line)


def run_codex_cli_healthcheck() -> int:
    config_manager = BotConfigManager()
    all_configs = config_manager.get_all_bots()
    if not all_configs:
        print("[CodexCLI:healthcheck] error: 没有找到有效的机器人配置")
        return 1

    codex_bots = [
        bot_config
        for bot_config in all_configs.values()
        if (bot_config.bot_type or "").strip() == "codex_cli"
    ]
    if not codex_bots:
        print("[CodexCLI:healthcheck] status=ok no codex_cli bot configured")
        return 0

    has_error = False
    for bot_config in codex_bots:
        result = run_codex_cli_startup_check(bot_config)
        for line in format_codex_cli_check_result(result, stage="healthcheck"):
            print(line)
        if result.errors:
            has_error = True

    return 1 if has_error else 0


def _resolve_executable(executable: str) -> str:
    candidate = str(executable or "").strip()
    if not candidate:
        return ""

    path_candidate = Path(candidate).expanduser()
    has_path_separator = any(sep and sep in candidate for sep in (os.sep, os.altsep))
    if path_candidate.is_absolute() or has_path_separator:
        return str(path_candidate.resolve()) if path_candidate.exists() else ""

    return shutil.which(candidate) or ""


def _run_version_command(command: Iterable[str], timeout_seconds: int = 15) -> tuple[bool, str]:
    try:
        completed = subprocess.run(
            list(command),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except FileNotFoundError as exc:
        return False, str(exc)
    except subprocess.TimeoutExpired:
        return False, f"命令超时: {' '.join(command)}"
    except Exception as exc:
        return False, str(exc)

    output = (completed.stdout or completed.stderr or "").strip()
    first_line = next((line.strip() for line in output.splitlines() if line.strip()), "")
    if completed.returncode != 0:
        return False, first_line or f"exit_code={completed.returncode}"
    return True, first_line or "ok"


def _check_directory(
    path_value: str,
    *,
    label: str,
    warnings: List[str],
    errors: List[str],
    require_writable: bool,
) -> None:
    path = Path(path_value)
    if not path.exists():
        errors.append(f"{label} 不存在: {path}")
        return
    if not path.is_dir():
        errors.append(f"{label} 不是目录: {path}")
        return
    if require_writable and not os.access(path, os.W_OK):
        errors.append(f"{label} 不可写: {path}")
    elif not require_writable and not os.access(path, os.R_OK):
        warnings.append(f"{label} 不可读: {path}")


def _has_codex_credentials(bot_config: BotConfig, codex_home: str) -> bool:
    env_value = (
        (bot_config.env_vars or {}).get("OPENAI_API_KEY")
        or os.getenv("OPENAI_API_KEY")
        or ""
    )
    if str(env_value).strip():
        return True

    codex_config_dir = Path(codex_home) / ".codex"
    return codex_config_dir.exists()


def _create_codex_cli_orchestrator(bot_config: BotConfig) -> CodexCliOrchestrator:
    provider_config = bot_config.provider_config or {}
    working_dir = bot_config.working_dir or ""
    if not working_dir:
        raise ValueError(f"Codex CLI 机器人 {bot_config.bot_key} 缺少 working_dir 配置")

    default_workspace_init_mode = (
        provider_config.get("default_workspace_init_mode")
        or provider_config.get("workspace_strategy")
        or ""
    )

    return CodexCliOrchestrator(
        bot_key=bot_config.bot_key,
        working_dir=working_dir,
        model=bot_config.model or "",
        system_prompt=bot_config.system_prompt or "",
        env_vars=bot_config.env_vars or None,
        sandbox_mode=provider_config.get("sandbox_mode", "workspace-write"),
        skip_git_repo_check=provider_config.get("skip_git_repo_check", False),
        dangerously_bypass_approvals_and_sandbox=provider_config.get(
            "dangerously_bypass_approvals_and_sandbox", False
        ),
        add_dirs=provider_config.get("add_dirs") or None,
        profile=provider_config.get("profile", ""),
        executable=provider_config.get("codex_path", "codex"),
        approval_policy=provider_config.get("approval_policy", "on-request"),
        workspace_root=provider_config.get("workspace_root", ""),
        codex_home=provider_config.get("codex_home", ""),
        workspace_strategy=provider_config.get("workspace_strategy", ""),
        default_workspace_init_mode=default_workspace_init_mode,
        default_group_workspace_mode=provider_config.get("default_group_workspace_mode", "personal"),
        default_github_owner=provider_config.get("default_github_owner", ""),
        session_timeout_seconds=provider_config.get("session_timeout_seconds", 7200),
        enable_project_workspace_mode=provider_config.get("enable_project_workspace_mode", True),
        long_task_keepalive_after_seconds=provider_config.get(
            "long_task_keepalive_after_seconds",
            20,
        ),
        long_task_keepalive_interval_seconds=provider_config.get(
            "long_task_keepalive_interval_seconds"
        ),
        context_window_auto_resume_limit=provider_config.get(
            "context_window_auto_resume_limit",
            3,
        ),
        reasoning_effort=provider_config.get("reasoning_effort", ""),
    )


def main() -> int:
    return run_codex_cli_healthcheck()


if __name__ == "__main__":
    raise SystemExit(main())
