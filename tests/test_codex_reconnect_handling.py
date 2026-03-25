import logging
import subprocess
import asyncio
import json
import os
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError

from config.bot_config import BotConfig, BotConfigManager
from src.core.cloudflare_pages_manager import (
    CloudflarePagesDeploymentInfo,
    CloudflarePagesProjectInfo,
    CloudflareWorkerDeploymentInfo,
    CloudflareWorkerStatusInfo,
)
from src.core.base_orchestrator import BaseOrchestrator
from src.core.bot_delegate_manager import BotDelegateManager
from src.core.codex_cli_orchestrator import CodexCliOrchestrator
from src.core.github_repository_manager import (
    GitHubRepositoryInfo,
    GitHubRepositoryManager,
    GitHubWorkflowRunInfo,
)
from src.core.json_state_store import JsonStateStore
from src.core.orchestrator_factory import OrchestratorFactory
from src.core.project_deployment_manager import ProjectDeploymentManager
from src.core.project_registry import ProjectRegistry
from src.core.codex_runtime_state import CodexRuntimePendingState, CodexRuntimeState
from src.utils.path_utils import (
    resolve_local_path,
    resolve_workspace_root_with_legacy_fallback,
)
from src.utils.codex_app_server_compat import (
    check_schema_contract,
    diff_schema_dirs,
)
from src.core.workspace_init_modes import (
    WORKSPACE_INIT_EMPTY,
    WORKSPACE_INIT_GIT_REMOTE,
    WORKSPACE_INIT_LEGACY_COPY,
)
from src.core.workspace_manager import WorkspaceManager
from src.utils.codex_cli_runtime_checks import (
    CodexCliRuntimeCheckResult,
    format_codex_cli_check_result,
    run_codex_cli_startup_check,
)
from src.utils.brochure_generation import rewrite_brochure_generation_request
from src.utils.brochure_delegate import parse_brochure_delegate_request
from src.utils.brochure_source_materials import load_brochure_source_materials
from src.utils.quoted_requirement_doc import parse_quoted_requirement_doc_request


def test_detects_transient_reconnect_message():
    assert CodexCliOrchestrator._is_transient_reconnect_message("Reconnecting... 1/5")
    assert not CodexCliOrchestrator._is_transient_reconnect_message("Process exited")


def test_detects_interrupted_turn_message():
    assert CodexCliOrchestrator._is_interrupted_turn_message(
        "[CodexCLI] Turn interrupted before completion: stdout ended before turn/completed（code=0）"
    )
    assert not CodexCliOrchestrator._is_interrupted_turn_message("Reconnecting... 1/5")


def test_detects_context_window_message():
    assert CodexCliOrchestrator._is_context_window_message(
        "Codex ran out of room in the model's context window."
    )
    assert CodexCliOrchestrator._is_context_window_message("ContextWindowExceeded")
    assert not CodexCliOrchestrator._is_context_window_message("Process exited")


def test_agent_message_phase_classification():
    assert CodexCliOrchestrator._is_commentary_agent_message_phase("commentary")
    assert not CodexCliOrchestrator._is_commentary_agent_message_phase("final_answer")
    assert CodexCliOrchestrator._is_final_agent_message_phase("")
    assert CodexCliOrchestrator._is_final_agent_message_phase("final_answer")
    assert not CodexCliOrchestrator._is_final_agent_message_phase("commentary")


def test_codex_runtime_state_prefers_response_then_commentary_and_exports_pending_snapshot():
    state = CodexRuntimeState()
    state.add_static_line("已进入项目工作区")
    state.set_runtime_status_line("gpt-5.4/xhigh · Fast on · ≈80.0% left")
    state.append_detail_line("🔧 `rg --files`")
    state.append_commentary_text("先检查项目结构。")

    assert state.visible_text() == "先检查项目结构。"

    state.append_response_text("开始修复状态同步。")
    assert state.visible_text() == "开始修复状态同步。"

    state.set_pending(
        CodexRuntimePendingState(
            kind="command_approval",
            title="⚠️ Codex 请求执行命令",
            description="命令：rg --files",
            action_hint="请直接回复：批准 / 会话允许 / 拒绝 / 取消",
        )
    )
    payload = state.to_registry_payload()

    assert payload["runtime_visible_text"] == "开始修复状态同步。"
    assert payload["runtime_last_detail_line"] == "🔧 `rg --files`"
    assert payload["runtime_pending_kind"] == "command_approval"
    assert payload["runtime_pending_title"] == "⚠️ Codex 请求执行命令"


def test_codex_runtime_state_exports_current_stage_line():
    state = CodexRuntimeState()
    state.append_commentary_text("先检查项目结构并读取关键文件。")
    assert state.current_stage_line() == "先检查项目结构并读取关键文件。"

    state.append_detail_line("🔧 `rg --files`")
    assert state.current_stage_line() == "🔧 `rg --files`"

    state.set_pending(
        CodexRuntimePendingState(
            kind="tool_user_input",
            title="❓ Codex 需要你补充信息",
            description="请提供部署目标环境",
            action_hint="请直接发送你的回答",
        )
    )
    assert state.current_stage_line() == "❓ Codex 需要你补充信息"

    state.clear_pending()
    state.append_detail_line("✨ 回复完成")
    assert state.current_stage_line() == "🔧 `rg --files`"


def test_codex_runtime_state_exports_active_tool_snapshot():
    state = CodexRuntimeState()
    state.set_active_tool("command", "🔧 `rg --files`", status="running")
    payload = state.to_registry_payload()

    assert payload["runtime_active_tool_kind"] == "command"
    assert payload["runtime_active_tool_title"] == "🔧 `rg --files`"
    assert payload["runtime_active_tool_status"] == "running"

    state.clear_active_tool()
    payload = state.to_registry_payload()
    assert payload["runtime_active_tool_kind"] == ""


def test_message_dispatcher_runtime_preview_prefers_structured_snapshot_fields():
    from src.transport.message_dispatcher import MessageDispatcher

    payload = {
        "runtime_visible_text": "正在分析项目结构",
        "runtime_last_detail_line": "🔧 `rg --files`",
        "last_preview": "旧预览",
    }
    assert MessageDispatcher._runtime_preview_from_payload(payload) == "正在分析项目结构"

    payload = {
        "runtime_visible_text": "",
        "runtime_last_detail_line": "🔧 `rg --files`",
        "last_preview": "旧预览",
    }
    assert MessageDispatcher._runtime_preview_from_payload(payload) == "🔧 `rg --files`"


def test_message_dispatcher_runtime_pending_and_stage_lines_are_structured():
    from src.transport.message_dispatcher import MessageDispatcher

    payload = {
        "runtime_pending_title": "⚠️ Codex 请求执行命令",
        "runtime_pending_desc": "命令：rg --files",
        "runtime_pending_action_hint": "请直接回复：批准 / 会话允许 / 拒绝 / 取消",
        "runtime_stage_line": "⚠️ Codex 请求执行命令",
    }

    pending_lines = MessageDispatcher._runtime_pending_status_lines(payload)

    assert pending_lines[0] == "状态：⚠️ Codex 请求执行命令"
    assert "详情：命令：rg --files" in pending_lines
    assert "下一步：请直接回复：批准 / 会话允许 / 拒绝 / 取消" in pending_lines
    assert MessageDispatcher._runtime_stage_line(payload) == "⚠️ Codex 请求执行命令"

    payload["runtime_active_tool_title"] = "🔧 `rg --files`"
    payload["runtime_active_tool_status"] = "running"
    assert MessageDispatcher._runtime_active_tool_line(payload) == "活跃工具：🔧 `rg --files`（running）"


def test_message_dispatcher_runtime_preview_ignores_legacy_preview_when_structured_snapshot_exists():
    from src.transport.message_dispatcher import MessageDispatcher

    payload = {
        "runtime_stage_line": "🔄 上游流重连：等待恢复",
        "runtime_pending_title": "⚠️ Codex 请求执行命令",
        "last_preview": "旧 preview 不应再显示",
    }

    assert MessageDispatcher._runtime_preview_from_payload(payload) == ""


def test_detects_inferred_context_window_exhaustion_from_usage_payload():
    assert CodexCliOrchestrator._looks_like_context_window_exhausted(
        {
            "context_estimated_remaining_percent": 0.0,
            "context_estimated_used_tokens": 258400,
            "context_model_window_tokens": 258400,
        }
    )
    assert CodexCliOrchestrator._looks_like_context_window_exhausted(
        {
            "context_estimated_used_tokens": 258400,
            "context_model_window_tokens": 258400,
        }
    )
    assert not CodexCliOrchestrator._looks_like_context_window_exhausted(
        {
            "context_estimated_remaining_percent": 12.5,
            "context_estimated_used_tokens": 220000,
            "context_model_window_tokens": 258400,
        }
    )


def test_resolve_local_path_supports_windows_absolute_path():
    resolved = resolve_local_path("C:/Users/Administrator/.codex")

    if os.name == "nt":
        assert str(resolved) == "C:\\Users\\Administrator\\.codex"
    else:
        assert str(resolved) == "/mnt/c/Users/Administrator/.codex"


def test_resolve_workspace_root_with_legacy_fallback_prefers_legacy_state():
    with TemporaryDirectory() as tmpdir:
        working_dir = Path(tmpdir) / "project"
        working_dir.mkdir()
        legacy_root = working_dir / ".codex_data"
        configured_root = working_dir / "codex-workspaces"
        (legacy_root / "state").mkdir(parents=True)
        (configured_root / "state").mkdir(parents=True)
        (legacy_root / "state" / "projects.json").write_text(
            '[{"project_id":"proj_1"}]',
            encoding="utf-8",
        )
        (configured_root / "state" / "sessions.json").write_text("[]", encoding="utf-8")

        resolved_root, source = resolve_workspace_root_with_legacy_fallback(
            working_dir=str(working_dir),
            configured_root=str(configured_root),
        )

        assert resolved_root == legacy_root.resolve()
        assert source == "legacy_fallback"


def test_check_schema_contract_reports_missing_required_patterns():
    with TemporaryDirectory() as tmpdir:
        schema_root = Path(tmpdir)
        (schema_root / "v2").mkdir(parents=True)
        (schema_root / "v2" / "ThreadStartResponse.json").write_text("{}", encoding="utf-8")
        (schema_root / "ServerNotification.json").write_text("{}", encoding="utf-8")
        failures = check_schema_contract(schema_root)
        assert failures
        assert any("thread/start 返回有效 model 字段" in item for item in failures)


def test_diff_schema_dirs_reports_changed_files():
    with TemporaryDirectory() as tmpdir:
        baseline = Path(tmpdir) / "baseline"
        candidate = Path(tmpdir) / "candidate"
        baseline.mkdir()
        candidate.mkdir()
        (baseline / "a.json").write_text('{"a":1}', encoding="utf-8")
        (candidate / "a.json").write_text('{"a":2}', encoding="utf-8")
        changed = diff_schema_dirs(baseline, candidate)
        assert changed == ["a.json"]


def test_format_codex_cli_check_result_includes_version_and_compat_summary():
    result = CodexCliRuntimeCheckResult(
        bot_key="cx_bot",
        executable="codex",
        resolved_executable="/usr/bin/codex",
        working_dir="/tmp/workspace",
        workspace_root="/tmp/workspaces",
        codex_home="/tmp/codex-home",
        codex_version="codex-cli 0.116.0",
        git_version="git version 2.45.0",
        compat_status="ok",
    )

    lines = format_codex_cli_check_result(result, stage="startup")

    assert any("codex_version=codex-cli 0.116.0" in line for line in lines)
    assert any("compat_summary=status=ok" in line for line in lines)


def test_normalizes_transient_reconnect_error_message():
    assert (
        CodexCliOrchestrator._normalize_codex_error_message("Reconnecting... 1/5")
        == "[CodexCLI] Reconnecting in progress"
    )


def test_codex_app_session_does_not_override_config_toml_in_thread_params():
    from src.adapters.codex_app_server_adapter import CodexAppServerSession

    session = CodexAppServerSession(
        model="gpt-5.3-codex",
        working_dir=".",
        sandbox_mode="workspace-write",
        approval_policy="never",
        reasoning_effort="high",
    )

    params = session._build_thread_params("developer instructions")

    assert params == {
        "cwd": str(resolve_local_path(".")),
        "developerInstructions": "developer instructions",
    }


def test_codex_app_session_does_not_override_config_toml_in_turn_params():
    from src.adapters.codex_app_server_adapter import CodexAppServerSession

    session = CodexAppServerSession(
        model="gpt-5.3-codex",
        working_dir=".",
        sandbox_mode="workspace-write",
        approval_policy="never",
        reasoning_effort="high",
        add_dirs=["./tmp"],
    )
    session.thread_id = "thread_1"

    params = session._build_turn_params([{"type": "text", "text": "hello"}])

    assert params == {
        "threadId": "thread_1",
        "input": [{"type": "text", "text": "hello"}],
    }


def test_codex_app_session_applies_runtime_config_from_thread_start_response():
    from src.adapters.codex_app_server_adapter import CodexAppServerSession

    session = CodexAppServerSession(model="", working_dir=".")
    session._apply_thread_configuration(
        {
            "model": "gpt-5.3-codex",
            "reasoningEffort": "high",
            "cwd": "C:\\next\\workspace",
            "modelProvider": "openai",
        }
    )

    assert session.active_model == "gpt-5.3-codex"
    assert session.active_reasoning_effort == "high"
    assert session.active_cwd == "C:\\next\\workspace"
    assert session.active_model_provider == "openai"


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
    assert "建议下一步命令：" in _CODEX_CLI_RECONNECT_HINT
    assert "`当前任务`" in _CODEX_CLI_RECONNECT_HINT


def test_friendly_error_maps_codex_common_failures():
    from src.transport.message_dispatcher import (
        _CODEX_CLI_CONFIG_UTF8_HINT,
        _CODEX_CLI_CONTEXT_WINDOW_HINT,
        _CODEX_CLI_STALE_THREAD_HINT,
        _CODEX_CLI_TRUSTED_DIRECTORY_HINT,
        _CODEX_CLI_WINDOWS_BINARY_HINT,
        _friendly_error,
    )

    assert (
        _friendly_error(
            Exception(
                "Codex ran out of room in the model's context window. "
                "Start a new thread or clear earlier history before retrying."
            )
        )
        == _CODEX_CLI_CONTEXT_WINDOW_HINT
    )
    assert (
        _friendly_error(
            Exception(
                "[CodexCLI] Process exited with code 1: "
                "Not inside a trusted directory and --skip-git-repo-check was not specified."
            )
        )
        == _CODEX_CLI_TRUSTED_DIRECTORY_HINT
    )
    assert _friendly_error(Exception("[WinError 193] %1 不是有效的 Win32 应用程序。")) == _CODEX_CLI_WINDOWS_BINARY_HINT
    assert (
        _friendly_error(
            Exception(
                "{'code': -32600, 'message': 'failed to load configuration: "
                "Failed to read config file C:\\\\Users\\\\Administrator\\\\.codex\\\\config.toml: "
                "stream did not contain valid UTF-8'}"
            )
        )
        == _CODEX_CLI_CONFIG_UTF8_HINT
    )
    assert (
        _friendly_error(
            Exception("{'code': -32600, 'message': 'no rollout found for thread id 019d03a8'}")
        )
        == _CODEX_CLI_STALE_THREAD_HINT
    )
    assert "建议下一步命令：" in _CODEX_CLI_CONTEXT_WINDOW_HINT
    assert "`重置`" in _CODEX_CLI_CONTEXT_WINDOW_HINT
    assert "建议下一步命令：" in _CODEX_CLI_STALE_THREAD_HINT


def test_friendly_error_maps_codex_upstream_stream_disconnect():
    from src.transport.message_dispatcher import _friendly_error

    reply = _friendly_error(
        Exception(
            "stream disconnected before completion: "
            "error sending request for url (https://aixj.vip/responses)"
        )
    )

    assert "本地 Codex CLI 与模型服务的响应流中断。" in reply
    assert "https://aixj.vip/responses" in reply
    assert "原始异常：" in reply
    assert "`当前任务`" in reply


def test_compose_final_stream_content_clears_running_prefix():
    from src.transport.message_dispatcher import MessageDispatcher

    content = MessageDispatcher._compose_final_stream_content(
        {
            "prefix": (
                "⏳ 长任务继续后台执行，后续状态在这条新消息里实时显示。\n"
                "如需查看最终结果，请留意本条消息后续更新。"
            )
        },
        "✅ 已完成 · 5 分 38 秒 · gpt-5.4/xhigh · Fast on",
    )

    assert content == "✅ 已完成 · 5 分 38 秒 · gpt-5.4/xhigh · Fast on"


def test_friendly_error_maps_provider_configuration_failures():
    from src.transport.message_dispatcher import (
        _CLAUDE_RELAY_CLI_NOT_FOUND_HINT,
        _CLAUDE_RELAY_WORKDIR_HINT,
        _GEMINI_API_KEY_HINT,
        _GEMINI_LOCATION_HINT,
        _GEMINI_MODEL_NOT_FOUND_HINT,
        _GEMINI_PAYLOAD_HINT,
        _GEMINI_QUOTA_HINT,
        _MODEL_CHANNEL_UNAVAILABLE_HINT,
        _friendly_error,
    )

    assert (
        _friendly_error(
            Exception(
                '[ClaudeRelay] HTTP 500 error: {"error":{"code":500,"message":"failed to start claude: '
                'exec: \\"claude\\": executable file not found in %PATH%","type":"server_error"}}'
            )
        )
        == _CLAUDE_RELAY_CLI_NOT_FOUND_HINT
    )
    assert (
        _friendly_error(
            Exception(
                '[ClaudeRelay] HTTP 500 error: {"error":{"code":500,"message":"failed to start claude: '
                'chdir C:\\\\next: The filename, directory name, or volume label syntax is incorrect.",'
                '"type":"server_error"}}'
            )
        )
        == _CLAUDE_RELAY_WORKDIR_HINT
    )
    assert (
        _friendly_error(
            Exception(
                'Gemini API error: HTTP 400, {"error":{"message":"API key not valid. Please pass a valid API key."}}'
            )
        )
        == _GEMINI_API_KEY_HINT
    )
    assert (
        _friendly_error(
            Exception(
                'Gemini API error: HTTP 400, {"error":{"message":"User location is not supported for the API use."}}'
            )
        )
        == _GEMINI_LOCATION_HINT
    )
    assert (
        _friendly_error(
            Exception(
                'Gemini API error: HTTP 429, {"error":{"message":"You exceeded your current quota, please check your plan and billing details."}}'
            )
        )
        == _GEMINI_QUOTA_HINT
    )
    assert (
        _friendly_error(
            Exception(
                'Gemini API error: HTTP 400, {"error":{"message":"Invalid JSON payload received. Unknown name \\"systemInstruction\\": Cannot find field."}}'
            )
        )
        == _GEMINI_PAYLOAD_HINT
    )
    assert (
        _friendly_error(
            Exception(
                'Gemini API error: HTTP 404, {"error":{"message":"models/gemini-2.0-flash-exp is not found for API version v1beta"}}'
            )
        )
        == _GEMINI_MODEL_NOT_FOUND_HINT
    )
    assert (
        _friendly_error(
            Exception(
                "Error code: 503 - {'error': {'code': 'model_not_found', 'message': "
                "'分组 default 下模型 gpt-4o 无可用渠道（distributor）', 'type': 'new_api_error'}}"
            )
        )
        == _MODEL_CHANNEL_UNAVAILABLE_HINT
    )
    assert "建议下一步命令：" in _CLAUDE_RELAY_CLI_NOT_FOUND_HINT
    assert "`帮助`" in _CLAUDE_RELAY_CLI_NOT_FOUND_HINT
    assert "建议下一步命令：" in _GEMINI_QUOTA_HINT


def test_friendly_error_fallback_includes_next_step_commands():
    from src.transport.message_dispatcher import _friendly_error

    reply = _friendly_error(Exception("some unknown internal error"))

    assert "抱歉，处理出错，请稍后重试。" in reply
    assert "原始异常：some unknown internal error" in reply
    assert "建议下一步命令：" in reply
    assert "`当前任务`" in reply
    assert "`重置`" in reply


def test_build_interrupted_turn_resume_prompt_mentions_continue_without_repeating():
    prompt = CodexCliOrchestrator._build_interrupted_turn_resume_prompt("继续开发企业官网")

    assert "执行通道意外中断" in prompt
    assert "继续完成同一个任务" in prompt
    assert "不要重复已经完成的修改" in prompt
    assert "继续开发企业官网" in prompt


def test_build_context_window_resume_prompt_mentions_workspace_and_partial_output():
    prompt = CodexCliOrchestrator._build_context_window_resume_prompt(
        original_message="继续开发企业官网",
        partial_response_text="已完成首页布局，接下来补 API 对接。",
        commands_seen=["rg --files", "pytest -q"],
        runtime_context={
            "project": {"name": "corp-site"},
            "working_dir": "/tmp/corp-site",
        },
        resume_attempt=2,
    )

    assert "上下文过长已自动切换到新线程" in prompt
    assert "corp-site" in prompt
    assert "/tmp/corp-site" in prompt
    assert "rg --files, pytest -q" in prompt
    assert "已完成首页布局" in prompt
    assert "这是第 2 次自动续跑" in prompt


def test_build_context_window_estimate_uses_last_usage_for_context_window():
    from src.adapters.codex_app_server_adapter import (
        CodexTokenUsageBreakdown,
        CodexTokenUsageUpdate,
    )

    event = CodexTokenUsageUpdate(
        thread_id="thread_1",
        turn_id="turn_1",
        last=CodexTokenUsageBreakdown(
            total_tokens=5000,
            input_tokens=3500,
            cached_input_tokens=1000,
            output_tokens=500,
            reasoning_output_tokens=200,
        ),
        total=CodexTokenUsageBreakdown(
            total_tokens=45000,
            input_tokens=28000,
            cached_input_tokens=12000,
            output_tokens=5000,
            reasoning_output_tokens=1800,
        ),
        model_context_window=100000,
    )

    estimate = CodexCliOrchestrator._build_context_window_estimate(event)

    assert estimate["context_estimated_used_tokens"] == 5000
    assert estimate["context_estimated_remaining_tokens"] == 95000
    assert round(estimate["context_estimated_used_percent"], 1) == 0.0
    assert round(estimate["context_estimated_remaining_percent"], 1) == 100.0
    assert estimate["context_cumulative_total_tokens"] == 45000
    assert estimate["context_last_total_tokens"] == 5000
    assert estimate["context_estimate_source"] == "last.totalTokens"


def test_build_runtime_status_strip_uses_model_reasoning_and_project_root():
    orchestrator = CodexCliOrchestrator(bot_key="cx_bot", working_dir="C:/next")

    line = orchestrator._build_runtime_status_strip(
        {
            "working_dir": "C:\\next\\codex-workspaces\\projects\\proj_1\\workspaces\\ws_1",
            "project": {"project_root": "C:\\next\\codex-workspaces\\projects\\proj_1"},
        },
        {
            "status_model": "gpt-5.3-codex",
            "status_reasoning_effort": "high",
            "status_working_dir": "C:\\next\\codex-workspaces\\projects\\proj_1\\workspaces\\ws_1",
            "status_project_root": "C:\\next\\codex-workspaces\\projects\\proj_1",
            "context_estimated_remaining_percent": 42.5,
            "context_model_window_tokens": 272000,
        },
    )

    assert line == (
        "gpt-5.3-codex/high · Fast on · ≈42.5% left · "
        "C:\\next\\codex-workspaces\\projects\\proj_1\\workspaces\\ws_1 · "
        "C:\\next\\codex-workspaces\\projects\\proj_1 · 272K w"
    )


def test_codex_app_session_raises_when_stream_ends_before_turn_completed():
    from src.adapters.codex_app_server_adapter import CodexAppServerError, CodexAppServerSession

    async def run_flow():
        session = CodexAppServerSession(model="", working_dir=".")
        session.thread_id = "thread_1"
        session.process = SimpleNamespace(returncode=0)

        async def fake_rpc_request(method: str, params: dict):
            assert method == "turn/start"
            return {"turn": {"id": "turn_1"}}

        session._rpc_request = fake_rpc_request
        await session._events.put(None)

        try:
            async for _event in session.stream_turn([{"type": "text", "text": "hello"}]):
                pass
        except CodexAppServerError as exc:
            return str(exc)
        raise AssertionError("expected CodexAppServerError")

    message = asyncio.run(run_flow())
    assert "Turn interrupted before completion" in message


def test_codex_app_session_emits_retryable_stream_error_without_interrupting_turn():
    from src.adapters.codex_app_server_adapter import CodexAppServerSession, CodexStreamError

    async def run_flow():
        session = CodexAppServerSession(model="", working_dir=".")
        session.thread_id = "thread_1"
        session.process = SimpleNamespace(returncode=0)

        async def fake_rpc_request(method: str, params: dict):
            assert method == "turn/start"
            return {"turn": {"id": "turn_1"}}

        session._rpc_request = fake_rpc_request
        await session._events.put(
            {
                "method": "error",
                "params": {
                    "error": {
                        "message": "Reconnecting... 1/5",
                        "additionalDetails": (
                            "stream disconnected before completion: "
                            "error sending request for url (https://aixj.vip/responses)"
                        ),
                    },
                    "willRetry": True,
                    "threadId": "thread_1",
                    "turnId": "turn_1",
                },
            }
        )
        await session._events.put({"method": "turn/completed", "params": {"turn": {"id": "turn_1"}}})

        events = []
        async for event in session.stream_turn([{"type": "text", "text": "hello"}]):
            events.append(event)
        return events

    events = asyncio.run(run_flow())

    assert len(events) == 1
    assert isinstance(events[0], CodexStreamError)
    assert events[0].message == "Reconnecting... 1/5"
    assert "https://aixj.vip/responses" in events[0].additional_details


def test_codex_app_session_uses_retryable_stream_error_details_for_terminal_failure():
    from src.adapters.codex_app_server_adapter import CodexAppServerError, CodexAppServerSession

    async def run_flow():
        session = CodexAppServerSession(model="", working_dir=".")
        session.thread_id = "thread_1"
        session.process = SimpleNamespace(returncode=0)

        async def fake_rpc_request(method: str, params: dict):
            assert method == "turn/start"
            return {"turn": {"id": "turn_1"}}

        session._rpc_request = fake_rpc_request
        await session._events.put(
            {
                "method": "error",
                "params": {
                    "error": {
                        "message": "Reconnecting... 1/5",
                        "additionalDetails": (
                            "stream disconnected before completion: "
                            "error sending request for url (https://aixj.vip/responses)"
                        ),
                    },
                    "willRetry": True,
                    "threadId": "thread_1",
                    "turnId": "turn_1",
                },
            }
        )
        await session._events.put(None)

        try:
            async for _event in session.stream_turn([{"type": "text", "text": "hello"}]):
                pass
        except CodexAppServerError as exc:
            return str(exc)
        raise AssertionError("expected CodexAppServerError")

    message = asyncio.run(run_flow())

    assert "Turn interrupted before completion" in message
    assert "stream disconnected before completion" in message
    assert "https://aixj.vip/responses" in message


def test_codex_app_session_emits_thread_compacted_event():
    from src.adapters.codex_app_server_adapter import CodexAppServerSession, CodexContextCompaction

    async def run_flow():
        session = CodexAppServerSession(model="", working_dir=".")
        session.thread_id = "thread_1"
        session.process = SimpleNamespace(returncode=0)

        async def fake_rpc_request(method: str, params: dict):
            assert method == "turn/start"
            return {"turn": {"id": "turn_1"}}

        session._rpc_request = fake_rpc_request
        await session._events.put(
            {
                "method": "thread/compacted",
                "params": {"threadId": "thread_1", "turnId": "turn_1"},
            }
        )
        await session._events.put({"method": "turn/completed", "params": {"turn": {"id": "turn_1"}}})

        events = []
        async for event in session.stream_turn([{"type": "text", "text": "hello"}]):
            events.append(event)
        return events

    events = asyncio.run(run_flow())

    assert len(events) == 1
    assert isinstance(events[0], CodexContextCompaction)
    assert events[0].thread_id == "thread_1"
    assert events[0].turn_id == "turn_1"
    assert events[0].source == "thread"


def test_runtime_elapsed_seconds_uses_whole_task_clock():
    assert abs(CodexCliOrchestrator._runtime_elapsed_seconds(100.0, now=121.8) - 21.8) < 1e-6
    assert CodexCliOrchestrator._runtime_elapsed_seconds(100.0, now=99.0) == 0.0


def test_runtime_keepalive_initial_delay_does_not_restart_after_retry():
    assert abs(
        CodexCliOrchestrator._runtime_keepalive_initial_delay(100.0, 20.0, now=105.0) - 15.0
    ) < 1e-6
    assert CodexCliOrchestrator._runtime_keepalive_initial_delay(100.0, 20.0, now=121.0) == 0.0


def test_render_runtime_thinking_lines_includes_running_elapsed_status():
    lines = CodexCliOrchestrator._render_runtime_thinking_lines(
        ["🤖 Codex 正在处理...", "📁 项目：demo"],
        elapsed_seconds=21.0,
        finished=False,
        allow_keepalive=True,
        has_pending_interaction=False,
        keepalive_after_seconds=20.0,
    )

    assert lines[0] == "🤖 Codex 正在处理..."
    assert "⏳ 状态：仍在处理中（已运行 21 秒；可回复“停止”）" in lines


def test_render_runtime_thinking_lines_marks_completion_with_total_duration():
    lines = CodexCliOrchestrator._render_runtime_thinking_lines(
        ["🤖 Codex 正在处理...", "📁 项目：demo"],
        elapsed_seconds=42.0,
        finished=True,
        allow_keepalive=False,
        has_pending_interaction=False,
        keepalive_after_seconds=20.0,
    )

    assert lines[0] == "✅ Codex 已完成"
    assert "✅ 状态：已完成（总耗时 42 秒）" in lines


def test_ws_client_send_reply_waits_for_reconnect_ready():
    from src.transport.ws_client import WsClient

    class DummyWs:
        def __init__(self):
            self.sent = []

        async def send(self, raw: str):
            self.sent.append(raw)

    async def run_flow():
        client = WsClient("bot-id", "secret", bot_key="cx_bot")
        client._running = True
        dummy_ws = DummyWs()

        async def delayed_ready():
            await asyncio.sleep(0.05)
            client._ws = dummy_ws
            client._ready_event.set()

        waiter = asyncio.create_task(client.send_reply({"cmd": "aibot_respond_msg"}))
        notifier = asyncio.create_task(delayed_ready())
        await asyncio.gather(waiter, notifier)
        return dummy_ws.sent

    sent_payloads = asyncio.run(run_flow())
    assert len(sent_payloads) == 1
    assert '"cmd": "aibot_respond_msg"' in sent_payloads[0]


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


def test_help_menu_card_contains_expected_topics():
    card = CodexCliOrchestrator.build_help_menu_card("menu@help@cx_bot")

    assert card["card_type"] == "vote_interaction"
    assert card["task_id"] == "menu@help@cx_bot"
    option_ids = [item["id"] for item in card["checkbox"]["option_list"]]
    assert option_ids == [
        "quick_start",
        "project_workspace",
        "github_repository",
        "website_publish",
        "wechat_miniprogram",
        "status_troubleshooting",
        "full_help",
    ]


def test_help_menu_reply_for_github_repository_topic():
    reply = CodexCliOrchestrator.build_help_menu_reply("github_repository")

    assert "更多分类：" in reply
    assert "当前在：`3` Git 与 GitHub" in reply
    assert "Git 与 GitHub：" in reply
    assert "先用这些：" in reply
    assert "3.2 设置Git身份 [name] [email]" in reply
    assert "3.3 GitHub仓库列表 [关键词]" in reply
    assert "3.10 推送到GitHub [仓库名]" in reply
    assert "default_github_owner" in reply
    assert "3.7 创建GitHub仓库 <仓库名>" not in reply


def test_help_menu_reply_for_quick_start_topic():
    reply = CodexCliOrchestrator.build_help_menu_reply("quick_start")

    assert "当前在：`1` 新手开始" in reply
    assert "新手开始" in reply
    assert "`1.1 hello-world`" in reply
    assert "`1.2 hello-world <Git地址>`" in reply
    assert "`1.3`" in reply
    assert "`1.4`" in reply
    assert "`1.5`" in reply


def test_help_menu_reply_for_deployment_topic_mentions_wechat_miniprogram():
    reply = CodexCliOrchestrator.build_help_menu_reply("deployment")

    assert "发布部署怎么走" in reply
    assert "5.1" in reply
    assert "5.2" in reply
    assert "上传体验版" in reply


def test_help_topic_card_for_github_repository_topic():
    card = CodexCliOrchestrator.build_help_topic_card(
        "github_repository",
        "menu@help@cx_bot@github_repository",
    )

    assert card["card_type"] == "text_notice"
    assert card["task_id"] == "menu@help@cx_bot@github_repository"
    assert "Git 与 GitHub" in card["main_title"]["title"]
    assert "设置 Git、选仓、建仓、推送" in card["main_title"]["desc"]


def test_parse_quoted_requirement_doc_request_supports_brochure_alias():
    request = parse_quoted_requirement_doc_request(
        "【引用消息】\n这是产品画册需求。\n\n【当前消息】\n保存为画册需求文档"
    )

    assert request is not None
    assert request.target_path == "docs/requirements.md"
    assert request.workflow == "brochure"


def test_rewrite_brochure_generation_request_expands_short_request():
    rewritten = rewrite_brochure_generation_request(
        "【引用消息】\n这是产品画册需求文档。\n\n【当前消息】\n生成画册"
    )

    assert "【引用需求文档】" in rewritten
    assert "这是产品画册需求文档。" in rewritten
    assert "【画册生成任务】" in rewritten
    assert "HTML/H5 产品画册" in rewritten
    assert "`brochure/index.html`" in rewritten
    assert "`docs/image-prompts.md`" in rewritten


def test_parse_brochure_delegate_request_supports_full_flow_and_direct_control():
    full_flow_request = parse_brochure_delegate_request("【当前消息】\n做完整画册并导出PDF")
    assert full_flow_request is not None
    assert full_flow_request.mode == "full_flow"
    assert full_flow_request.planning_needed is True
    assert full_flow_request.final_control_command == "导出画册PDF"

    direct_request = parse_brochure_delegate_request("【当前消息】\n导出画册PDF brochure/index.html dist/custom.pdf")
    assert direct_request is not None
    assert direct_request.mode == "direct_control"
    assert direct_request.planning_needed is False
    assert direct_request.final_control_command == "导出画册PDF brochure/index.html dist/custom.pdf"


def test_is_control_command_recognizes_help_subtopic():
    with TemporaryDirectory() as tmpdir:
        working_dir = Path(tmpdir) / "project"
        working_dir.mkdir()
        orchestrator = CodexCliOrchestrator(
            bot_key="cx_bot",
            working_dir=str(working_dir),
        )

        assert orchestrator.is_control_command("帮助 1")
        assert orchestrator.is_control_command("1 4")
        assert orchestrator.is_control_command("1.4")


def test_message_dispatcher_extracts_card_selected_values():
    from src.transport.message_dispatcher import MessageDispatcher

    selected = MessageDispatcher._extract_card_selected_values(
        {
            "checkbox": {
                "question_key": "help_topic",
                "selected_ids": ["github_repository"],
            },
            "submit_button": {"key": "submit_help_menu"},
        }
    )

    assert selected == ["github_repository"]


def test_help_menu_card_disabled_by_default():
    from src.transport.message_dispatcher import MessageDispatcher

    class DummyWs:
        async def send_reply(self, payload: dict):
            return None

    with TemporaryDirectory() as tmpdir:
        working_dir = Path(tmpdir) / "project"
        working_dir.mkdir()
        orchestrator = CodexCliOrchestrator(
            bot_key="cx_bot",
            working_dir=str(working_dir),
        )
        dispatcher = MessageDispatcher(
            DummyWs(),
            BotConfig(
                bot_key="cx_bot",
                bot_id="test_bot_id",
                secret="test_secret",
                bot_type="codex_cli",
            ),
            orchestrator=orchestrator,
        )

        assert dispatcher._supports_help_menu_card() is False


def test_message_dispatcher_help_menu_click_updates_template_card():
    from src.transport.message_dispatcher import MessageDispatcher

    class DummyWs:
        def __init__(self):
            self.payloads = []

        async def send_reply(self, payload: dict):
            self.payloads.append(payload)

    with TemporaryDirectory() as tmpdir:
        working_dir = Path(tmpdir) / "project"
        working_dir.mkdir()
        orchestrator = CodexCliOrchestrator(
            bot_key="cx_bot",
            working_dir=str(working_dir),
        )
        ws = DummyWs()
        dispatcher = MessageDispatcher(
            ws,
            BotConfig(
                bot_key="cx_bot",
                bot_id="test_bot_id",
                secret="test_secret",
                bot_type="codex_cli",
            ),
            orchestrator=orchestrator,
        )

        asyncio.run(
            dispatcher._handle_menu_card_event(
                "req_help_menu",
                {
                    "event": {
                        "task_id": "menu@help@cx_bot",
                        "response_code": "response_help_123",
                        "checkbox": {
                            "question_key": "help_topic",
                            "selected_ids": ["github_repository"],
                        },
                        "submit_button": {"key": "submit_help_menu"},
                    },
                },
                "alice",
            )
        )

        assert len(ws.payloads) == 1
        payload = ws.payloads[0]
        assert payload["cmd"] == "aibot_respond_update_msg"
        assert payload["body"]["response_code"] == "response_help_123"
        assert payload["body"]["template_card"]["card_type"] == "text_notice"
        assert "GitHub 仓库" in payload["body"]["template_card"]["main_title"]["title"]


def test_message_dispatcher_brochure_bot_help_uses_specialized_reply():
    from src.transport.message_dispatcher import MessageDispatcher

    class DummyWs:
        def __init__(self):
            self.payloads = []

        async def send_reply(self, payload: dict):
            self.payloads.append(payload)

    class StubOrchestrator(BaseOrchestrator):
        async def handle_text_message(
            self,
            user_id: str,
            message: str,
            stream_id: str,
            session_key: str = "",
            log_context: dict = None,
            on_stream_delta=None,
        ) -> str:
            raise AssertionError("help should not fall through to text handling")

        async def handle_multimodal_message(
            self,
            user_id: str,
            content_blocks: list[dict],
            stream_id: str,
            session_key: str = "",
            log_context: dict = None,
            on_stream_delta=None,
        ) -> str:
            raise AssertionError("help should not fall through to multimodal handling")

    with TemporaryDirectory() as tmpdir:
        working_dir = Path(tmpdir) / "project"
        working_dir.mkdir()
        ws = DummyWs()
        dispatcher = MessageDispatcher(
            ws,
            BotConfig(
                bot_key="brochure_bot",
                bot_id="test_bot_id",
                secret="test_secret",
                bot_type="gemini",
                name="产品画册",
                description="产品画册策划与文案机器人",
                provider_config={
                    "enable_brochure_internal_delegate": True,
                    "delegate_execution_bot_key": "cx_bot",
                },
            ),
            orchestrator=StubOrchestrator(),
        )

        asyncio.run(
            dispatcher.on_msg_callback(
                {
                    "headers": {"req_id": "req_brochure_help"},
                    "body": {
                        "msgid": "msg_brochure_help",
                        "msgtype": "text",
                        "chattype": "single",
                        "from": {"userid": "alice"},
                        "text": {"content": "帮助"},
                    },
                }
            )
        )

        assert len(ws.payloads) == 1
        payload = ws.payloads[0]
        assert payload["cmd"] == "aibot_respond_msg"
        content = payload["body"]["stream"]["content"]
        assert "产品画册机器人使用说明" in content
        assert "做完整画册" in content
        assert "`cx_bot`" in content
        assert "ClawRelay Bot - Demo Commands" not in content


def test_message_dispatcher_stream_reply_failure_marks_task_without_crashing():
    from src.core.task_registry import get_task_registry
    from src.transport.message_dispatcher import MessageDispatcher

    class FailingWs:
        async def send_reply(self, payload: dict):
            raise RuntimeError("websocket closed")

    async def run_flow(dispatcher: MessageDispatcher, task_key: str):
        registry = get_task_registry()

        async def sleeper():
            await asyncio.sleep(3600)

        task = asyncio.create_task(sleeper())
        registry.register(task_key, task, "stream_fail", req_id="req_fail")

        callback = dispatcher._make_stream_delta_callback(
            {"req_id": "req_fail", "stream_id": "stream_fail", "prefix": ""},
            task_key=task_key,
        )
        await callback("hello world", False)

        _task, _stream_id, extra = registry.get(task_key)

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await asyncio.sleep(0)
        return extra

    with TemporaryDirectory() as tmpdir:
        working_dir = Path(tmpdir) / "project"
        working_dir.mkdir()
        orchestrator = CodexCliOrchestrator(
            bot_key="cx_bot",
            working_dir=str(working_dir),
        )
        dispatcher = MessageDispatcher(
            FailingWs(),
            BotConfig(
                bot_key="cx_bot",
                bot_id="test_bot_id",
                secret="test_secret",
                bot_type="codex_cli",
            ),
            orchestrator=orchestrator,
        )
        task_key = dispatcher._task_registry_key("session-reply-fail")
        registry = get_task_registry()
        registry.forget(task_key)

        extra = asyncio.run(run_flow(dispatcher, task_key))

        assert extra["reply_delivery_failed"] is True
        assert extra["last_preview"] == "hello world"
        registry.forget(task_key)


def test_message_dispatcher_final_stream_update_wins_over_pending_throttled_update():
    from src.transport import message_dispatcher as dispatcher_module
    from src.transport.message_dispatcher import MessageDispatcher

    class DummyWs:
        def __init__(self):
            self.payloads = []

        async def send_reply(self, payload: dict):
            self.payloads.append(payload)

    async def run_flow(dispatcher: MessageDispatcher, ws: DummyWs):
        callback = dispatcher._make_stream_delta_callback(
            {"req_id": "req_race", "stream_id": "stream_race", "prefix": ""},
            task_key="",
        )
        await callback("处理中", False)
        await callback("处理中\n\n更多进度", False)
        await callback("已完成", True)
        await asyncio.sleep(0.1)
        return list(ws.payloads)

    original_interval = dispatcher_module.STREAM_THROTTLE_INTERVAL
    dispatcher_module.STREAM_THROTTLE_INTERVAL = 0.05
    try:
        with TemporaryDirectory() as tmpdir:
            working_dir = Path(tmpdir) / "project"
            working_dir.mkdir()
            orchestrator = CodexCliOrchestrator(
                bot_key="cx_bot",
                working_dir=str(working_dir),
            )
            ws = DummyWs()
            dispatcher = MessageDispatcher(
                ws,
                BotConfig(
                    bot_key="cx_bot",
                    bot_id="test_bot_id",
                    secret="test_secret",
                    bot_type="codex_cli",
                ),
                orchestrator=orchestrator,
            )

            payloads = asyncio.run(run_flow(dispatcher, ws))

            assert len(payloads) == 2
            assert payloads[-1]["body"]["stream"]["finish"] is True
            assert payloads[-1]["body"]["stream"]["content"] == "已完成"
    finally:
        dispatcher_module.STREAM_THROTTLE_INTERVAL = original_interval


def test_message_dispatcher_ignores_late_running_status_after_finish():
    from src.transport.message_dispatcher import MessageDispatcher

    class DummyWs:
        def __init__(self):
            self.payloads = []

        async def send_reply(self, payload: dict):
            self.payloads.append(payload)

    async def run_flow(dispatcher: MessageDispatcher, ws: DummyWs):
        callback = dispatcher._make_stream_delta_callback(
            {"req_id": "req_keepalive", "stream_id": "stream_keepalive", "prefix": ""},
            task_key="",
        )
        await callback("🤖 Codex 正在处理...\n⏳ 状态：仍在处理中（已运行 20 秒；可回复“停止”）", False)
        await callback("✅ Codex 已完成\n✅ 状态：已完成（总耗时 42 秒）", True)
        await callback("🤖 Codex 正在处理...\n⏳ 状态：仍在处理中（已运行 43 秒；可回复“停止”）", False)
        return list(ws.payloads)

    with TemporaryDirectory() as tmpdir:
        working_dir = Path(tmpdir) / "project"
        working_dir.mkdir()
        orchestrator = CodexCliOrchestrator(
            bot_key="cx_bot",
            working_dir=str(working_dir),
        )
        ws = DummyWs()
        dispatcher = MessageDispatcher(
            ws,
            BotConfig(
                bot_key="cx_bot",
                bot_id="test_bot_id",
                secret="test_secret",
                bot_type="codex_cli",
            ),
            orchestrator=orchestrator,
        )

        payloads = asyncio.run(run_flow(dispatcher, ws))

        assert len(payloads) == 2
        assert payloads[0]["body"]["stream"]["finish"] is False
        assert "已运行 20 秒" in payloads[0]["body"]["stream"]["content"]
        assert payloads[1]["body"]["stream"]["finish"] is True
        assert "总耗时 42 秒" in payloads[1]["body"]["stream"]["content"]


def test_run_with_task_registry_error_updates_recent_status_and_current_reply_target():
    from src.core.task_registry import get_task_registry
    from src.transport.message_dispatcher import MessageDispatcher

    class DummyWs:
        def __init__(self):
            self.payloads = []

        async def send_reply(self, payload: dict):
            self.payloads.append(payload)

    async def run_flow(dispatcher: MessageDispatcher, task_key: str, reply_state: dict):
        registry = get_task_registry()

        async def failing_coro():
            registry.touch(
                task_key,
                last_preview="🤖 Codex 正在处理...\n⏳ 状态：仍在处理中（已运行 25 分 37 秒；可回复“停止”）",
            )
            reply_state["req_id"] = "req_new"
            reply_state["stream_id"] = "stream_new"
            raise Exception(
                "Codex ran out of room in the model's context window. "
                "Start a new thread or clear earlier history before retrying."
            )

        await dispatcher._run_with_task_registry(
            "req_old",
            "stream_old",
            "session-error-status",
            failing_coro(),
            reply_state=reply_state,
        )
        await asyncio.sleep(0)
        return registry.get_recent(task_key)

    with TemporaryDirectory() as tmpdir:
        working_dir = Path(tmpdir) / "project"
        working_dir.mkdir()
        orchestrator = CodexCliOrchestrator(
            bot_key="cx_bot",
            working_dir=str(working_dir),
        )
        ws = DummyWs()
        dispatcher = MessageDispatcher(
            ws,
            BotConfig(
                bot_key="cx_bot",
                bot_id="test_bot_id",
                secret="test_secret",
                bot_type="codex_cli",
            ),
            orchestrator=orchestrator,
        )
        task_key = dispatcher._task_registry_key("session-error-status")
        registry = get_task_registry()
        registry.forget(task_key)
        reply_state = {"req_id": "req_old", "stream_id": "stream_old", "prefix": ""}

        recent = asyncio.run(run_flow(dispatcher, task_key, reply_state))
        reply = dispatcher._running_task_status_reply("session-error-status")

        assert recent["terminal_status"] == "error"
        assert recent["last_preview"].startswith("当前会话上下文已满")
        assert recent["terminal_error_user"].startswith("当前会话上下文已满")
        assert len(ws.payloads) == 1
        assert ws.payloads[0]["headers"]["req_id"] == "req_new"
        assert ws.payloads[0]["body"]["stream"]["id"] == "stream_new"
        assert ws.payloads[0]["body"]["stream"]["finish"] is True
        assert "当前会话上下文已满" in ws.payloads[0]["body"]["stream"]["content"]
        assert "最近错误：当前会话上下文已满" in reply
        assert "状态：仍在处理中" not in reply
        registry.forget(task_key)


def test_message_dispatcher_final_stream_failure_uses_proactive_reply_fallback():
    from src.core.task_registry import get_task_registry
    from src.transport.message_dispatcher import MessageDispatcher
    from src.utils.weixin_utils import ProactiveReplyClient

    class FailingWs:
        async def send_reply(self, payload: dict):
            raise RuntimeError("websocket closed")

    async def run_flow(dispatcher: MessageDispatcher, task_key: str):
        registry = get_task_registry()

        async def sleeper():
            await asyncio.sleep(3600)

        task = asyncio.create_task(sleeper())
        registry.register(
            task_key,
            task,
            "stream_fail",
            req_id="req_fail",
            reply_state={
                "req_id": "req_fail",
                "stream_id": "stream_fail",
                "prefix": "",
                "response_url": "https://example.com/response",
            },
        )

        callback = dispatcher._make_stream_delta_callback(
            {
                "req_id": "req_fail",
                "stream_id": "stream_fail",
                "prefix": "",
                "response_url": "https://example.com/response",
            },
            task_key=task_key,
        )
        await callback("final result", True)

        _task, _stream_id, extra = registry.get(task_key)

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await asyncio.sleep(0)
        return extra

    sent_payloads = []

    async def fake_send_markdown(response_url: str, content: str) -> bool:
        sent_payloads.append((response_url, content))
        return True

    original_send_markdown = ProactiveReplyClient.send_markdown
    ProactiveReplyClient.send_markdown = staticmethod(fake_send_markdown)
    try:
        with TemporaryDirectory() as tmpdir:
            working_dir = Path(tmpdir) / "project"
            working_dir.mkdir()
            orchestrator = CodexCliOrchestrator(
                bot_key="cx_bot",
                working_dir=str(working_dir),
            )
            dispatcher = MessageDispatcher(
                FailingWs(),
                BotConfig(
                    bot_key="cx_bot",
                    bot_id="test_bot_id",
                    secret="test_secret",
                    bot_type="codex_cli",
                ),
                orchestrator=orchestrator,
            )
            task_key = dispatcher._task_registry_key("session-proactive-fallback")
            registry = get_task_registry()
            registry.forget(task_key)

            extra = asyncio.run(run_flow(dispatcher, task_key))

            assert extra["reply_delivery_failed"] is True
            assert extra["proactive_reply_sent"] is True
            assert sent_payloads == [
                ("https://example.com/response", "final result")
            ]
            registry.forget(task_key)
    finally:
        ProactiveReplyClient.send_markdown = original_send_markdown


def test_running_task_status_reply_reports_recent_terminal_status():
    from src.core.task_registry import get_task_registry
    from src.transport.message_dispatcher import MessageDispatcher

    class DummyWs:
        async def send_reply(self, payload: dict):
            return None

    async def run_flow(task_key: str):
        registry = get_task_registry()
        task = asyncio.create_task(asyncio.sleep(0))
        registry.register(
            task_key,
            task,
            "stream_done",
            req_id="req_done",
            last_preview="任务已经完成",
            reply_delivery_failed=True,
        )
        await task
        await asyncio.sleep(0)
        return registry.get_recent(task_key)

    with TemporaryDirectory() as tmpdir:
        working_dir = Path(tmpdir) / "project"
        working_dir.mkdir()
        orchestrator = CodexCliOrchestrator(
            bot_key="cx_bot",
            working_dir=str(working_dir),
        )
        dispatcher = MessageDispatcher(
            DummyWs(),
            BotConfig(
                bot_key="cx_bot",
                bot_id="test_bot_id",
                secret="test_secret",
                bot_type="codex_cli",
            ),
            orchestrator=orchestrator,
        )
        task_key = dispatcher._task_registry_key("session-terminal-status")
        registry = get_task_registry()
        registry.forget(task_key)

        recent = asyncio.run(run_flow(task_key))
        reply = dispatcher._running_task_status_reply("session-terminal-status")

        assert recent["terminal_status"] == "completed"
        assert recent["reply_delivery_failed"] is True
        assert "当前没有正在运行的任务。" in reply
        assert "最近一次任务已于" in reply
        assert "结果可能没有成功送达" in reply
        assert "最近输出：任务已经完成" in reply
        registry.forget(task_key)


def test_running_task_status_reply_deduplicates_stage_and_preview():
    from src.core.task_registry import get_task_registry
    from src.transport.message_dispatcher import MessageDispatcher

    class DummyWs:
        async def send_reply(self, payload: dict):
            return None

    async def run_flow(task_key: str):
        registry = get_task_registry()
        task = asyncio.create_task(asyncio.sleep(0))
        registry.register(
            task_key,
            task,
            "stream_done",
            req_id="req_done",
            runtime_stage_line="⚠️ Codex 请求执行命令",
            runtime_pending_title="⚠️ Codex 请求执行命令",
            runtime_pending_desc="命令：rg --files",
            runtime_pending_action_hint="请直接回复：批准 / 会话允许 / 拒绝 / 取消",
        )
        await task
        await asyncio.sleep(0)
        return registry.get_recent(task_key)

    with TemporaryDirectory() as tmpdir:
        working_dir = Path(tmpdir) / "project"
        working_dir.mkdir()
        orchestrator = CodexCliOrchestrator(
            bot_key="cx_bot",
            working_dir=str(working_dir),
        )
        dispatcher = MessageDispatcher(
            DummyWs(),
            BotConfig(
                bot_key="cx_bot",
                bot_id="test_bot_id",
                secret="test_secret",
                bot_type="codex_cli",
            ),
            orchestrator=orchestrator,
        )
        task_key = dispatcher._task_registry_key("session-stage-dedupe")
        registry = get_task_registry()
        registry.forget(task_key)

        asyncio.run(run_flow(task_key))
        reply = dispatcher._running_task_status_reply("session-stage-dedupe")

        assert "当前阶段：⚠️ Codex 请求执行命令" in reply
        assert "最近输出：" not in reply
        registry.forget(task_key)


def test_handoff_running_reply_closes_old_bubble_and_switches_to_new_one():
    from src.core.task_registry import get_task_registry
    from src.transport.message_dispatcher import MessageDispatcher

    class DummyWs:
        def __init__(self):
            self.payloads = []

        async def send_reply(self, payload: dict):
            self.payloads.append(payload)

    async def run_flow(dispatcher: MessageDispatcher, task_key: str, ws: DummyWs):
        registry = get_task_registry()

        async def sleeper():
            await asyncio.sleep(3600)

        task = asyncio.create_task(sleeper())
        registry.register(
            task_key,
            task,
            "stream_old",
            req_id="req_old",
            last_preview="Codex 正在生成页面骨架",
            reply_state={
                "req_id": "req_old",
                "stream_id": "stream_old",
                "prefix": "",
            },
        )

        handed_off = await dispatcher._handoff_running_reply(
            "session-handoff",
            "req_new",
            "⏳ 已收到，继续处理。",
        )
        _task, current_stream_id, extra = registry.get(task_key)

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await asyncio.sleep(0)
        return handed_off, current_stream_id, extra, list(ws.payloads)

    with TemporaryDirectory() as tmpdir:
        working_dir = Path(tmpdir) / "project"
        working_dir.mkdir()
        orchestrator = CodexCliOrchestrator(
            bot_key="cx_bot",
            working_dir=str(working_dir),
        )
        ws = DummyWs()
        dispatcher = MessageDispatcher(
            ws,
            BotConfig(
                bot_key="cx_bot",
                bot_id="test_bot_id",
                secret="test_secret",
                bot_type="codex_cli",
            ),
            orchestrator=orchestrator,
        )
        task_key = dispatcher._task_registry_key("session-handoff")
        registry = get_task_registry()
        registry.forget(task_key)

        handed_off, current_stream_id, extra, payloads = asyncio.run(run_flow(dispatcher, task_key, ws))

        assert handed_off is True
        assert current_stream_id != "stream_old"
        assert extra["reply_state"]["req_id"] == "req_new"
        assert extra["reply_state"]["stream_id"] == current_stream_id
        assert len(payloads) == 2

        old_payload = payloads[0]
        assert old_payload["headers"]["req_id"] == "req_old"
        assert old_payload["body"]["stream"]["id"] == "stream_old"
        assert old_payload["body"]["stream"]["finish"] is True
        assert "切换到新的消息气泡继续显示" in old_payload["body"]["stream"]["content"]

        new_payload = payloads[1]
        assert new_payload["headers"]["req_id"] == "req_new"
        assert new_payload["body"]["stream"]["id"] == current_stream_id
        assert new_payload["body"]["stream"]["finish"] is False
        assert new_payload["body"]["stream"]["content"] == "⏳ 已收到，继续处理。"
        registry.forget(task_key)


def test_stream_delta_callback_handles_long_task_handoff_then_pending_then_completion():
    from src.core.task_registry import get_task_registry
    from src.transport.message_dispatcher import MessageDispatcher

    class DummyWs:
        def __init__(self):
            self.payloads = []

        async def send_reply(self, payload: dict):
            self.payloads.append(payload)

    async def run_flow(dispatcher: MessageDispatcher, task_key: str, ws: DummyWs):
        registry = get_task_registry()
        completion_event = asyncio.Event()

        async def runner():
            await completion_event.wait()

        task = asyncio.create_task(runner())
        reply_state = {
            "req_id": "req_old",
            "stream_id": "stream_old",
            "prefix": "",
            "response_url": "",
        }
        registry.register(
            task_key,
            task,
            "stream_old",
            req_id="req_old",
            reply_state=reply_state,
        )

        callback = dispatcher._make_stream_delta_callback(reply_state, task_key=task_key)
        await asyncio.sleep(0.02)
        await callback("先检查项目结构。", False)

        _task, current_stream_id, extra = registry.get(task_key)
        registry.annotate(
            task_key,
            runtime_pending_kind="command_approval",
            runtime_pending_title="⚠️ Codex 请求执行命令",
            runtime_pending_desc="命令：rg --files",
            runtime_pending_action_hint="请直接回复：批准 / 会话允许 / 拒绝 / 取消",
            runtime_stage_line="⚠️ Codex 请求执行命令",
        )
        running_reply = dispatcher._running_task_status_reply("session-handoff-pending-complete")

        await callback("最终结果：已完成修复。", True)
        completion_event.set()
        await task
        await asyncio.sleep(0)

        recent_reply = dispatcher._running_task_status_reply("session-handoff-pending-complete")
        return current_stream_id, extra, running_reply, recent_reply, list(ws.payloads)

    with TemporaryDirectory() as tmpdir:
        working_dir = Path(tmpdir) / "project"
        working_dir.mkdir()
        orchestrator = CodexCliOrchestrator(
            bot_key="cx_bot",
            working_dir=str(working_dir),
        )
        orchestrator.long_task_keepalive_after_seconds = 0.01
        ws = DummyWs()
        dispatcher = MessageDispatcher(
            ws,
            BotConfig(
                bot_key="cx_bot",
                bot_id="test_bot_id",
                secret="test_secret",
                bot_type="codex_cli",
            ),
            orchestrator=orchestrator,
        )
        task_key = dispatcher._task_registry_key("session-handoff-pending-complete")
        registry = get_task_registry()
        registry.forget(task_key)

        current_stream_id, extra, running_reply, recent_reply, payloads = asyncio.run(
            run_flow(dispatcher, task_key, ws)
        )

        assert extra["reply_state"]["stream_id"] == current_stream_id
        assert extra["status_live_mode"] is True
        assert "状态：⚠️ Codex 请求执行命令" in running_reply
        assert "详情：命令：rg --files" in running_reply
        assert "当前阶段：⚠️ Codex 请求执行命令" in running_reply
        assert "最近输出：" not in running_reply
        assert "当前没有正在运行的任务。" in recent_reply
        assert "最终结果：已完成修复。" in recent_reply
        assert len(payloads) >= 4
        assert payloads[0]["body"]["stream"]["id"] == "stream_old"
        assert payloads[0]["body"]["stream"]["finish"] is True
        assert payloads[1]["body"]["stream"]["id"] == current_stream_id
        assert payloads[1]["body"]["stream"]["finish"] is False
        final_payloads = [item for item in payloads if item["body"]["stream"]["id"] == current_stream_id]
        assert any(
            item["body"]["stream"]["finish"] is True
            and "最终结果：已完成修复。" in item["body"]["stream"]["content"]
            for item in final_payloads
        )
        registry.forget(task_key)


def test_running_task_status_reply_reports_proactive_reply_recovery():
    from src.core.task_registry import get_task_registry
    from src.transport.message_dispatcher import MessageDispatcher

    class DummyWs:
        async def send_reply(self, payload: dict):
            return None

    async def run_flow(task_key: str):
        registry = get_task_registry()
        task = asyncio.create_task(asyncio.sleep(0))
        registry.register(
            task_key,
            task,
            "stream_done",
            req_id="req_done",
            last_preview="画册已生成",
            reply_delivery_failed=True,
            proactive_reply_sent=True,
        )
        await task
        await asyncio.sleep(0)
        return registry.get_recent(task_key)

    with TemporaryDirectory() as tmpdir:
        working_dir = Path(tmpdir) / "project"
        working_dir.mkdir()
        orchestrator = CodexCliOrchestrator(
            bot_key="cx_bot",
            working_dir=str(working_dir),
        )
        dispatcher = MessageDispatcher(
            DummyWs(),
            BotConfig(
                bot_key="cx_bot",
                bot_id="test_bot_id",
                secret="test_secret",
                bot_type="codex_cli",
            ),
            orchestrator=orchestrator,
        )
        task_key = dispatcher._task_registry_key("session-proactive-status")
        registry = get_task_registry()
        registry.forget(task_key)

        recent = asyncio.run(run_flow(task_key))
        reply = dispatcher._running_task_status_reply("session-proactive-status")

        assert recent["terminal_status"] == "completed"
        assert recent["proactive_reply_sent"] is True
        assert "当前没有正在运行的任务。" in reply
        assert "系统已通过主动回复补发结果" in reply
        assert "最近输出：画册已生成" in reply
        registry.forget(task_key)


def test_message_dispatcher_group_text_command_strips_leading_mention():
    from src.transport.message_dispatcher import MessageDispatcher

    class DummyWs:
        def __init__(self):
            self.payloads = []

        async def send_reply(self, payload: dict):
            self.payloads.append(payload)

    with TemporaryDirectory() as tmpdir:
        working_dir = Path(tmpdir) / "project"
        working_dir.mkdir()
        orchestrator = CodexCliOrchestrator(
            bot_key="cx_bot",
            working_dir=str(working_dir),
        )
        ws = DummyWs()
        dispatcher = MessageDispatcher(
            ws,
            BotConfig(
                bot_key="cx_bot",
                bot_id="test_bot_id",
                secret="test_secret",
                bot_type="codex_cli",
                name="测试机器人",
            ),
            orchestrator=orchestrator,
        )

        asyncio.run(
            dispatcher.on_msg_callback(
                {
                    "headers": {"req_id": "req_group_text"},
                    "body": {
                        "msgid": "msg_group_text",
                        "msgtype": "text",
                        "chattype": "group",
                        "chatid": "group-1",
                        "from": {"userid": "alice"},
                        "text": {"content": "@机器人\u2005新建项目 hello-mini"},
                    },
                }
            )
        )

        assert len(ws.payloads) == 1
        payload = ws.payloads[0]
        assert payload["cmd"] == "aibot_respond_msg"
        assert "已创建群项目：hello-mini" in payload["body"]["stream"]["content"]


def test_message_dispatcher_group_mixed_text_routes_to_control_command():
    from src.transport.message_dispatcher import MessageDispatcher

    class DummyWs:
        def __init__(self):
            self.payloads = []

        async def send_reply(self, payload: dict):
            self.payloads.append(payload)

    with TemporaryDirectory() as tmpdir:
        working_dir = Path(tmpdir) / "project"
        working_dir.mkdir()
        orchestrator = CodexCliOrchestrator(
            bot_key="cx_bot",
            working_dir=str(working_dir),
        )
        ws = DummyWs()
        dispatcher = MessageDispatcher(
            ws,
            BotConfig(
                bot_key="cx_bot",
                bot_id="test_bot_id",
                secret="test_secret",
                bot_type="codex_cli",
            ),
            orchestrator=orchestrator,
        )

        asyncio.run(
            dispatcher.on_msg_callback(
                {
                    "headers": {"req_id": "req_group_mixed"},
                    "body": {
                        "msgid": "msg_group_mixed",
                        "msgtype": "mixed",
                        "chattype": "group",
                        "chatid": "group-2",
                        "from": {"userid": "alice"},
                        "mixed": {
                            "items": [
                                {"msgtype": "text", "text": {"content": "@机器人\u2005"}},
                                {"msgtype": "text", "text": {"content": "新建项目 hello-mixed"}},
                            ]
                        },
                    },
                }
            )
        )

        assert len(ws.payloads) == 1
        payload = ws.payloads[0]
        assert payload["cmd"] == "aibot_respond_msg"
        assert "已创建群项目：hello-mixed" in payload["body"]["stream"]["content"]


def test_message_dispatcher_quote_context_is_forwarded_to_text_message():
    from src.transport.message_dispatcher import MessageDispatcher

    class DummyWs:
        def __init__(self):
            self.payloads = []

        async def send_reply(self, payload: dict):
            self.payloads.append(payload)

    class FakeOrchestrator:
        def __init__(self):
            self.received_messages = []

        def get_runtime_session_key(self, user_id: str, session_key: str, log_context: dict = None) -> str:
            return session_key or user_id

        def has_pending_interaction(self, runtime_session_key: str) -> bool:
            return False

        async def handle_interaction_text(self, runtime_session_key: str, content: str):
            return None

        def is_control_command(self, content: str) -> bool:
            return False

        async def handle_control_command(self, user_id: str, content: str, session_key: str = "", log_context: dict = None):
            return None

        async def clear_session(self, session_key: str):
            return None

        async def handle_text_message(
            self,
            user_id: str,
            message: str,
            stream_id: str,
            session_key: str = "",
            log_context: dict = None,
            on_stream_delta=None,
            **kwargs,
        ):
            self.received_messages.append(message)
            if on_stream_delta:
                await on_stream_delta("已收到", True)
            return "已收到"

    orchestrator = FakeOrchestrator()
    ws = DummyWs()
    dispatcher = MessageDispatcher(
        ws,
        BotConfig(
            bot_key="cx_bot",
            bot_id="test_bot_id",
            secret="test_secret",
            bot_type="codex_cli",
        ),
        orchestrator=orchestrator,
    )

    asyncio.run(
        dispatcher.on_msg_callback(
            {
                "headers": {"req_id": "req_quote_text"},
                "body": {
                    "msgid": "msg_quote_text",
                    "msgtype": "text",
                    "chattype": "group",
                    "chatid": "group-quote-1",
                    "from": {"userid": "alice"},
                    "text": {"content": "请继续处理这个问题"},
                    "reply_to": {
                        "from": {"userid": "bob"},
                        "text": {"content": "上一条消息内容"},
                    },
                },
            }
        )
    )

    assert orchestrator.received_messages
    message = orchestrator.received_messages[0]
    assert "【引用消息】" in message
    assert "bob：上一条消息内容" in message
    assert "【当前消息】" in message
    assert "请继续处理这个问题" in message


def test_message_dispatcher_quote_command_uses_current_text_content():
    from src.transport.message_dispatcher import MessageDispatcher

    class DummyWs:
        def __init__(self):
            self.payloads = []

        async def send_reply(self, payload: dict):
            self.payloads.append(payload)

    class FakeOrchestrator:
        def __init__(self):
            self.control_commands = []

        def get_runtime_session_key(self, user_id: str, session_key: str, log_context: dict = None) -> str:
            return session_key or user_id

        def has_pending_interaction(self, runtime_session_key: str) -> bool:
            return False

        async def handle_interaction_text(self, runtime_session_key: str, content: str):
            return None

        def is_control_command(self, content: str) -> bool:
            return content.startswith("新建项目 ")

        async def handle_control_command(self, user_id: str, content: str, session_key: str = "", log_context: dict = None):
            self.control_commands.append(content)
            if self.is_control_command(content):
                return f"命中控制命令：{content}"
            return None

        async def clear_session(self, session_key: str):
            return None

        async def handle_text_message(self, *args, **kwargs):
            raise AssertionError("quoted control command should not fall through to handle_text_message")

    orchestrator = FakeOrchestrator()
    ws = DummyWs()
    dispatcher = MessageDispatcher(
        ws,
        BotConfig(
            bot_key="cx_bot",
            bot_id="test_bot_id",
            secret="test_secret",
            bot_type="codex_cli",
        ),
        orchestrator=orchestrator,
    )

    asyncio.run(
        dispatcher.on_msg_callback(
            {
                "headers": {"req_id": "req_quote_command"},
                "body": {
                    "msgid": "msg_quote_command",
                    "msgtype": "text",
                    "chattype": "group",
                    "chatid": "group-quote-2",
                    "from": {"userid": "alice"},
                    "text": {"content": "上一条消息内容\n新建项目 quoted-demo"},
                    "reply_to": {
                        "from": {"userid": "bob"},
                        "text": {"content": "上一条消息内容"},
                    },
                },
            }
        )
    )

    assert orchestrator.control_commands == ["新建项目 quoted-demo"]
    assert len(ws.payloads) == 1
    payload = ws.payloads[0]
    assert payload["cmd"] == "aibot_respond_msg"
    assert "命中控制命令：新建项目 quoted-demo" in payload["body"]["stream"]["content"]


def test_message_dispatcher_rewrites_quoted_development_handoff_for_all_supported_bot_types():
    from src.transport.message_dispatcher import MessageDispatcher

    class DummyWs:
        def __init__(self):
            self.payloads = []

        async def send_reply(self, payload: dict):
            self.payloads.append(payload)

    class FakeOrchestrator:
        def __init__(self):
            self.received_messages = []

        def get_runtime_session_key(self, user_id: str, session_key: str, log_context: dict = None) -> str:
            return session_key or user_id

        def has_pending_interaction(self, runtime_session_key: str) -> bool:
            return False

        async def handle_interaction_text(self, runtime_session_key: str, content: str):
            return None

        def is_control_command(self, content: str) -> bool:
            return False

        async def handle_control_command(self, user_id: str, content: str, session_key: str = "", log_context: dict = None):
            return None

        async def clear_session(self, session_key: str):
            return None

        async def handle_text_message(
            self,
            user_id: str,
            message: str,
            stream_id: str,
            session_key: str = "",
            log_context: dict = None,
            on_stream_delta=None,
            **kwargs,
        ):
            self.received_messages.append(message)
            if on_stream_delta:
                await on_stream_delta("已收到", True)
            return "已收到"

    for bot_type in ("claude_code", "gemini", "openai", "codex", "codex_cli"):
        orchestrator = FakeOrchestrator()
        ws = DummyWs()
        dispatcher = MessageDispatcher(
            ws,
            BotConfig(
                bot_key=f"{bot_type}_bot",
                bot_id=f"{bot_type}_id",
                secret="test_secret",
                bot_type=bot_type,
            ),
            orchestrator=orchestrator,
        )

        asyncio.run(
            dispatcher.on_msg_callback(
                {
                    "headers": {"req_id": f"req_{bot_type}"},
                    "body": {
                        "msgid": f"msg_{bot_type}",
                        "msgtype": "text",
                        "chattype": "group",
                        "chatid": "group-handoff",
                        "from": {"userid": "alice"},
                        "text": {"content": "开发"},
                        "reply_to": {
                            "from": {"userid": "gemini_bot"},
                            "text": {"content": "这是需求文档，先做一个 hello world 首页。"},
                        },
                    },
                }
            )
        )

        assert orchestrator.received_messages
        message = orchestrator.received_messages[0]
        assert "【引用需求文档】" in message
        assert "这是需求文档，先做一个 hello world 首页。" in message
        assert "用户正在引用上面的需求文档，并要求你在当前项目中直接开始开发" in message
        assert "用户原话：开发" in message


def test_message_dispatcher_non_codex_group_inherits_current_project_context():
    from src.core.group_project_context_resolver import GroupProjectContextResolver
    from src.core.project_registry import ProjectRegistry
    from src.core.session_binding_manager import SessionBindingManager
    from src.core.workspace_manager import WorkspaceManager
    from src.transport.message_dispatcher import MessageDispatcher

    class DummyWs:
        def __init__(self):
            self.payloads = []

        async def send_reply(self, payload: dict):
            self.payloads.append(payload)

    class FakeOrchestrator:
        def __init__(self):
            self.received_messages = []

        def get_runtime_session_key(self, user_id: str, session_key: str, log_context: dict = None) -> str:
            return session_key or user_id

        def has_pending_interaction(self, runtime_session_key: str) -> bool:
            return False

        async def handle_interaction_text(self, runtime_session_key: str, content: str):
            return None

        def is_control_command(self, content: str) -> bool:
            return False

        async def handle_control_command(self, user_id: str, content: str, session_key: str = "", log_context: dict = None):
            return None

        async def clear_session(self, session_key: str):
            return None

        async def handle_text_message(
            self,
            user_id: str,
            message: str,
            stream_id: str,
            session_key: str = "",
            log_context: dict = None,
            on_stream_delta=None,
            **kwargs,
        ):
            self.received_messages.append((message, log_context or {}))
            if on_stream_delta:
                await on_stream_delta("已收到", True)
            return "已收到"

    with TemporaryDirectory() as tmpdir:
        workspace_root = Path(tmpdir) / "shared-codex-data"
        workspace_root.mkdir()
        project_registry = ProjectRegistry(str(workspace_root))
        workspace_manager = WorkspaceManager(str(workspace_root))
        binding_manager = SessionBindingManager(str(workspace_root))

        project = project_registry.create_project(
            name="hello-mini",
            kind="shared",
            owner_user_id="alice",
            owner_chat_id="group-project",
        )
        workspace = workspace_manager.get_or_create_shared_workspace(project, "group-project")
        binding_manager.bind_session(
            "codex_cli_bot",
            "group-project",
            project["project_id"],
            workspace["workspace_id"],
            "shared_workspace",
        )

        resolver = GroupProjectContextResolver.from_bot_configs(
            {
                "codex_cli_bot": BotConfig(
                    bot_key="codex_cli_bot",
                    bot_id="bot1",
                    secret="secret1",
                    bot_type="codex_cli",
                    working_dir=str(Path(tmpdir) / "codex-working"),
                    provider_config={
                        "workspace_root": str(workspace_root),
                        "enable_project_workspace_mode": True,
                    },
                ),
                "gemini_bot": BotConfig(
                    bot_key="gemini_bot",
                    bot_id="bot2",
                    secret="secret2",
                    bot_type="gemini",
                ),
            }
        )

        orchestrator = FakeOrchestrator()
        ws = DummyWs()
        dispatcher = MessageDispatcher(
            ws,
            BotConfig(
                bot_key="gemini_bot",
                bot_id="bot2",
                secret="secret2",
                bot_type="gemini",
            ),
            orchestrator=orchestrator,
            group_project_context_resolver=resolver,
        )

        asyncio.run(
            dispatcher.on_msg_callback(
                {
                    "headers": {"req_id": "req_group_project_context"},
                    "body": {
                        "msgid": "msg_group_project_context",
                        "msgtype": "text",
                        "chattype": "group",
                        "chatid": "group-project",
                        "from": {"userid": "alice"},
                        "text": {"content": "请继续分析这个项目的首页结构"},
                    },
                }
            )
        )

        assert orchestrator.received_messages
        message, log_context = orchestrator.received_messages[0]
        assert "【当前群项目上下文】" in message
        assert "来源机器人：codex_cli_bot" in message
        assert "项目：hello-mini" in message
        assert "请继续分析这个项目的首页结构" in message
        assert log_context["project_name"] == "hello-mini"
        assert log_context["project_source_bot_key"] == "codex_cli_bot"


def test_message_dispatcher_forwards_full_quote_to_requirement_doc_command():
    from src.transport.message_dispatcher import MessageDispatcher

    class DummyWs:
        def __init__(self):
            self.payloads = []

        async def send_reply(self, payload: dict):
            self.payloads.append(payload)

    class FakeOrchestrator:
        def __init__(self):
            self.control_messages = []

        def get_runtime_session_key(self, user_id: str, session_key: str, log_context: dict = None) -> str:
            return session_key or user_id

        def has_pending_interaction(self, runtime_session_key: str) -> bool:
            return False

        async def handle_interaction_text(self, runtime_session_key: str, content: str):
            return None

        def is_control_command(self, content: str) -> bool:
            return str(content or "").strip() == "保存为需求文档"

        async def handle_control_command(self, user_id: str, content: str, session_key: str = "", log_context: dict = None):
            self.control_messages.append(content)
            return "已保存"

        async def clear_session(self, session_key: str):
            return None

    quoted_text = (
        "# 需求文档\n\n"
        "## 目标\n\n"
        "做一个 hello world 首页。\n\n"
        "## 功能\n\n"
        "- 顶部标题\n"
        "- 按钮\n"
        "- 页脚说明\n\n"
        + ("补充说明段落。\n" * 120)
    ).strip()

    orchestrator = FakeOrchestrator()
    dispatcher = MessageDispatcher(
        DummyWs(),
        BotConfig(
            bot_key="codex_cli_bot",
            bot_id="bot1",
            secret="secret",
            bot_type="codex_cli",
        ),
        orchestrator=orchestrator,
    )

    asyncio.run(
        dispatcher.on_msg_callback(
            {
                "headers": {"req_id": "req_save_doc"},
                "body": {
                    "msgid": "msg_save_doc",
                    "msgtype": "text",
                    "chattype": "single",
                    "from": {"userid": "alice"},
                    "text": {"content": "保存为需求文档"},
                    "reply_to": {
                        "from": {"userid": "gemini_bot"},
                        "text": {"content": quoted_text},
                    },
                },
            }
        )
    )

    assert orchestrator.control_messages
    control_message = orchestrator.control_messages[0]
    assert "【引用消息】" in control_message
    assert quoted_text in control_message
    assert "补充说明段落。" in control_message
    assert "【当前消息】\n保存为需求文档" in control_message


def test_message_dispatcher_can_disable_group_project_context_inheritance():
    from src.core.group_project_context_resolver import GroupProjectContextResolver
    from src.core.project_registry import ProjectRegistry
    from src.core.session_binding_manager import SessionBindingManager
    from src.core.workspace_manager import WorkspaceManager
    from src.transport.message_dispatcher import MessageDispatcher

    class DummyWs:
        def __init__(self):
            self.payloads = []

        async def send_reply(self, payload: dict):
            self.payloads.append(payload)

    class FakeOrchestrator:
        def __init__(self):
            self.received_messages = []

        def get_runtime_session_key(self, user_id: str, session_key: str, log_context: dict = None) -> str:
            return session_key or user_id

        def has_pending_interaction(self, runtime_session_key: str) -> bool:
            return False

        async def handle_interaction_text(self, runtime_session_key: str, content: str):
            return None

        def is_control_command(self, content: str) -> bool:
            return False

        async def handle_control_command(self, user_id: str, content: str, session_key: str = "", log_context: dict = None):
            return None

        async def clear_session(self, session_key: str):
            return None

        async def handle_text_message(
            self,
            user_id: str,
            message: str,
            stream_id: str,
            session_key: str = "",
            log_context: dict = None,
            on_stream_delta=None,
            **kwargs,
        ):
            self.received_messages.append(message)
            if on_stream_delta:
                await on_stream_delta("已收到", True)
            return "已收到"

    with TemporaryDirectory() as tmpdir:
        workspace_root = Path(tmpdir) / "shared-codex-data"
        workspace_root.mkdir()
        project_registry = ProjectRegistry(str(workspace_root))
        workspace_manager = WorkspaceManager(str(workspace_root))
        binding_manager = SessionBindingManager(str(workspace_root))

        project = project_registry.create_project(
            name="hello-mini",
            kind="shared",
            owner_user_id="alice",
            owner_chat_id="group-project",
        )
        workspace = workspace_manager.get_or_create_shared_workspace(project, "group-project")
        binding_manager.bind_session(
            "codex_cli_bot",
            "group-project",
            project["project_id"],
            workspace["workspace_id"],
            "shared_workspace",
        )

        resolver = GroupProjectContextResolver.from_bot_configs(
            {
                "codex_cli_bot": BotConfig(
                    bot_key="codex_cli_bot",
                    bot_id="bot1",
                    secret="secret1",
                    bot_type="codex_cli",
                    working_dir=str(Path(tmpdir) / "codex-working"),
                    provider_config={
                        "workspace_root": str(workspace_root),
                        "enable_project_workspace_mode": True,
                    },
                ),
            }
        )

        orchestrator = FakeOrchestrator()
        ws = DummyWs()
        dispatcher = MessageDispatcher(
            ws,
            BotConfig(
                bot_key="gemini_bot",
                bot_id="bot2",
                secret="secret2",
                bot_type="gemini",
                provider_config={"inherit_group_project_context": False},
            ),
            orchestrator=orchestrator,
            group_project_context_resolver=resolver,
        )

        asyncio.run(
            dispatcher.on_msg_callback(
                {
                    "headers": {"req_id": "req_group_project_context_disabled"},
                    "body": {
                        "msgid": "msg_group_project_context_disabled",
                        "msgtype": "text",
                        "chattype": "group",
                        "chatid": "group-project",
                        "from": {"userid": "alice"},
                        "text": {"content": "请继续分析这个项目的首页结构"},
                    },
                }
            )
        )

        assert orchestrator.received_messages == ["请继续分析这个项目的首页结构"]


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
        assert "2.5 hello-world" in runtime_context["first_reply_guidance"]
        assert "3.3" in runtime_context["first_reply_guidance"]
        assert "输入 `1`" in runtime_context["first_reply_guidance"]


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
    assert "最简单的用法" in help_text
    assert "不用先记命令" in help_text
    assert "当前项目继续开发" in help_text
    assert "按场景查看" in help_text
    assert "`1` 新手开始" in help_text
    assert "`6` 状态与排障" in help_text
    assert "`7` / `帮助 全部`" in help_text
    assert "`2.5 项目名`" in help_text
    assert "`3.2`" in help_text
    assert "`3.10`" in help_text
    assert "`4.2`" in help_text
    assert "`5.2`" in help_text
    assert len(help_text.splitlines()) <= 20
    assert "`6.2`" in help_text


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

        request, usage = orchestrator._parse_deployment_command("启用Pages部署")
        assert usage is None
        assert request["action"] == "enable_pages"
        assert request["pages_project_name"] == ""
        assert request["build_dir"] == "dist"

        request, usage = orchestrator._parse_deployment_command(
            "启用Worker部署 hello-worker src/index.ts"
        )
        assert usage is None
        assert request["action"] == "enable_worker"
        assert request["worker_name"] == "hello-worker"
        assert request["entry_file"] == "src/index.ts"

        request, usage = orchestrator._parse_deployment_command("启用Worker部署")
        assert usage is None
        assert request["action"] == "enable_worker"
        assert request["worker_name"] == ""
        assert request["entry_file"] == "src/index.ts"

        request, usage = orchestrator._parse_deployment_command(
            "一键发布Pages hello-pages hello-pages dist"
        )
        assert usage is None
        assert request["action"] == "publish_pages"
        assert request["repository_name"] == "hello-pages"
        assert request["pages_project_name"] == "hello-pages"
        assert request["build_dir"] == "dist"

        request, usage = orchestrator._parse_deployment_command("一键发布Pages")
        assert usage is None
        assert request["action"] == "publish_pages"
        assert request["repository_name"] == ""
        assert request["pages_project_name"] == ""
        assert request["build_dir"] == "dist"

        request, usage = orchestrator._parse_deployment_command(
            "一键发布Worker hello-worker hello-worker src/index.ts"
        )
        assert usage is None
        assert request["action"] == "publish_worker"
        assert request["repository_name"] == "hello-worker"
        assert request["worker_name"] == "hello-worker"
        assert request["entry_file"] == "src/index.ts"

        request, usage = orchestrator._parse_deployment_command("一键发布Worker")
        assert usage is None
        assert request["action"] == "publish_worker"
        assert request["repository_name"] == ""
        assert request["worker_name"] == ""
        assert request["entry_file"] == "src/index.ts"

        request, usage = orchestrator._parse_deployment_command("启用小程序上传")
        assert usage is None
        assert request["action"] == "enable_wechat_miniprogram"
        assert request["appid"] == ""
        assert request["project_path"] == ""

        request, usage = orchestrator._parse_deployment_command("启用小程序上传 wx1234567890ab")
        assert usage is None
        assert request["action"] == "enable_wechat_miniprogram"
        assert request["appid"] == "wx1234567890ab"
        assert request["project_path"] == ""

        request, usage = orchestrator._parse_deployment_command(
            "启用微信小程序上传 wx1234567890ab miniprogram"
        )
        assert usage is None
        assert request["action"] == "enable_wechat_miniprogram"
        assert request["appid"] == "wx1234567890ab"
        assert request["project_path"] == "miniprogram"

        request, usage = orchestrator._parse_deployment_command("启用小程序上传 miniprogram")
        assert usage is None
        assert request["action"] == "enable_wechat_miniprogram"
        assert request["appid"] == ""
        assert request["project_path"] == "miniprogram"

        request, usage = orchestrator._parse_deployment_command("一键上传小程序")
        assert usage is None
        assert request["action"] == "publish_wechat_miniprogram"
        assert request["repository_name"] == ""
        assert request["appid"] == ""
        assert request["project_path"] == ""

        request, usage = orchestrator._parse_deployment_command("一键上传小程序 hello-mini")
        assert usage is None
        assert request["action"] == "publish_wechat_miniprogram"
        assert request["repository_name"] == "hello-mini"
        assert request["appid"] == ""
        assert request["project_path"] == ""

        request, usage = orchestrator._parse_deployment_command("一键发布小程序 wx1234567890ab")
        assert usage is None
        assert request["action"] == "publish_wechat_miniprogram"
        assert request["repository_name"] == ""
        assert request["appid"] == "wx1234567890ab"
        assert request["project_path"] == ""

        request, usage = orchestrator._parse_deployment_command(
            "一键上传微信小程序 hello-mini wx1234567890ab miniprogram"
        )
        assert usage is None
        assert request["action"] == "publish_wechat_miniprogram"
        assert request["repository_name"] == "hello-mini"
        assert request["appid"] == "wx1234567890ab"
        assert request["project_path"] == "miniprogram"

        request, usage = orchestrator._parse_deployment_command("一键上传小程序 ./miniprogram")
        assert usage is None
        assert request["action"] == "publish_wechat_miniprogram"
        assert request["repository_name"] == ""
        assert request["appid"] == ""
        assert request["project_path"] == "./miniprogram"

        request, usage = orchestrator._parse_deployment_command("发布画册")
        assert usage is None
        assert request["action"] == "publish_brochure"
        assert request["repository_name"] == ""
        assert request["pages_project_name"] == ""
        assert request["build_dir"] == "brochure"

        request, usage = orchestrator._parse_deployment_command(
            "发布画册 hello-brochure hello-brochure-site"
        )
        assert usage is None
        assert request["action"] == "publish_brochure"
        assert request["repository_name"] == "hello-brochure"
        assert request["pages_project_name"] == "hello-brochure-site"
        assert request["build_dir"] == "brochure"

        request, usage = orchestrator._parse_deployment_command(
            "导出画册PDF brochure/index.html dist/custom.pdf"
        )
        assert usage is None
        assert request["action"] == "export_brochure_pdf"
        assert request["html_path"] == "brochure/index.html"
        assert request["output_path"] == "dist/custom.pdf"

        request, usage = orchestrator._parse_deployment_command(
            "导出画册图片 brochure/index.html dist/custom.png"
        )
        assert usage is None
        assert request["action"] == "export_brochure_image"
        assert request["html_path"] == "brochure/index.html"
        assert request["output_path"] == "dist/custom.png"

        request, usage = orchestrator._parse_deployment_command("回传画册图片")
        assert usage is None
        assert request["action"] == "return_brochure_image"
        assert request["html_path"] == ""
        assert request["output_path"] == ""

        request, usage = orchestrator._parse_deployment_command(
            "导出画册PPT docs/brochure-outline.md dist/custom.pptx"
        )
        assert usage is None
        assert request["action"] == "export_brochure_ppt"
        assert request["outline_path"] == "docs/brochure-outline.md"
        assert request["output_path"] == "dist/custom.pptx"


def test_orchestrator_reports_latest_pipeline_status():
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
                "2 5 hello-pages",
                session_key="alice",
            )
            runtime_context, _ = orchestrator._ensure_single_runtime_context("alice", "alice")
            workspace = Path(runtime_context["working_dir"])
            await orchestrator.handle_control_command(
                "alice",
                "3 2 kangaroo117 kangaroo117@users.noreply.github.com",
                session_key="alice",
            )
            await orchestrator.handle_control_command(
                "alice",
                "3 12 git@github.com:kangaroo117/hello-pages.git",
                session_key="alice",
            )
            await orchestrator.handle_control_command(
                "alice",
                "4 1 hello-pages dist",
                session_key="alice",
            )
            return await orchestrator.handle_control_command(
                "alice",
                "4 5",
                session_key="alice",
            )

        orchestrator.github_repository_manager.get_latest_workflow_run = lambda owner, repo, workflow_id="": GitHubWorkflowRunInfo(
            id=123,
            name="Deploy Cloudflare Pages",
            workflow_name="Deploy Cloudflare Pages",
            display_title="ci: enable Cloudflare Pages deploy",
            status="completed",
            conclusion="success",
            html_url="https://github.com/kangaroo117/hello-pages/actions/runs/123",
            event="push",
            head_branch="main",
            head_sha="abcdef1234567890",
            run_number=7,
            created_at="2026-03-20T10:00:00Z",
            updated_at="2026-03-20T10:01:00Z",
        )

        reply = asyncio.run(run_flow())

        assert "项目：hello-pages" in reply
        assert "工作流：deploy-cloudflare-pages.yml" in reply
        assert "最近运行：#7" in reply
        assert "状态：completed" in reply
        assert "结论：success" in reply


def test_orchestrator_reports_cloudflare_pages_project_status():
    with TemporaryDirectory() as tmpdir:
        working_dir = Path(tmpdir) / "project"
        working_dir.mkdir()
        orchestrator = CodexCliOrchestrator(
            bot_key="cx_bot",
            working_dir=str(working_dir),
            env_vars={
                "CLOUDFLARE_API_TOKEN": "token",
                "CLOUDFLARE_ACCOUNT_ID": "account-id",
            },
        )

        async def run_flow():
            await orchestrator.handle_control_command(
                "alice",
                "2 5 hello-pages",
                session_key="alice",
            )
            await orchestrator.handle_control_command(
                "alice",
                "4 1 hello-pages dist",
                session_key="alice",
            )
            return await orchestrator.handle_control_command(
                "alice",
                "4 6",
                session_key="alice",
            )

        orchestrator.cloudflare_pages_manager.get_project = lambda project_name: CloudflarePagesProjectInfo(
            name=project_name,
            subdomain=f"{project_name}.pages.dev",
            production_branch="main",
            created=False,
        )
        orchestrator.cloudflare_pages_manager.get_latest_deployment = (
            lambda project_name: CloudflarePagesDeploymentInfo(
                deployment_id="dep_pages_123",
                environment="production",
                url=f"https://{project_name}.pages.dev",
                stage_name="deploy",
                stage_status="success",
                created_on="2026-03-20T10:00:00Z",
                modified_on="2026-03-20T10:01:00Z",
            )
        )

        reply = asyncio.run(run_flow())

        assert "Cloudflare 类型：Pages" in reply
        assert "Pages 项目：hello-pages" in reply
        assert "Pages 域名：https://hello-pages.pages.dev" in reply
        assert "最近部署ID：dep_pages_123" in reply
        assert "阶段：deploy / success" in reply


def test_orchestrator_reports_cloudflare_worker_project_status():
    with TemporaryDirectory() as tmpdir:
        working_dir = Path(tmpdir) / "project"
        working_dir.mkdir()
        orchestrator = CodexCliOrchestrator(
            bot_key="cx_bot",
            working_dir=str(working_dir),
            env_vars={
                "CLOUDFLARE_API_TOKEN": "token",
                "CLOUDFLARE_ACCOUNT_ID": "account-id",
            },
        )

        async def run_flow():
            await orchestrator.handle_control_command(
                "alice",
                "2 5 hello-worker",
                session_key="alice",
            )
            await orchestrator.handle_control_command(
                "alice",
                "4 3 hello-worker src/index.ts",
                session_key="alice",
            )
            return await orchestrator.handle_control_command(
                "alice",
                "4 6",
                session_key="alice",
            )

        orchestrator.cloudflare_pages_manager.get_worker_status = lambda worker_name: CloudflareWorkerStatusInfo(
            name=worker_name,
            exists=True,
            workers_dev_enabled=True,
            previews_enabled=True,
            account_subdomain="kangaroo117",
            workers_dev_url=f"https://{worker_name}.kangaroo117.workers.dev",
            latest_deployment=CloudflareWorkerDeploymentInfo(
                deployment_id="dep_worker_456",
                created_on="2026-03-20T11:00:00Z",
                source="github",
            ),
        )

        reply = asyncio.run(run_flow())

        assert "Cloudflare 类型：Worker" in reply
        assert "Worker 名称：hello-worker" in reply
        assert "Workers.dev：已启用" in reply
        assert "Workers.dev 地址：https://hello-worker.kangaroo117.workers.dev" in reply
        assert "最近部署ID：dep_worker_456" in reply


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
        assert "完整用法：设置Git身份 <name> <email>" in usage


def test_parse_git_identity_command_supports_default_owner_shortcut():
    with TemporaryDirectory() as tmpdir:
        working_dir = Path(tmpdir) / "project"
        working_dir.mkdir()
        orchestrator = CodexCliOrchestrator(
            bot_key="cx_bot",
            working_dir=str(working_dir),
            default_github_owner="kangaroo117",
        )

        request, usage = orchestrator._parse_git_identity_command("设置Git身份")

        assert usage is None
        assert request["action"] == "set_git_identity"
        assert request["name"] == "kangaroo117"
        assert request["email"] == "kangaroo117@users.noreply.github.com"


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


def test_rewrite_quoted_development_request_expands_short_handoff():
    message = (
        "【当前群项目上下文】\n"
        "来源机器人：codex_cli_bot\n"
        "项目：hello-mini (proj_group_hello-mini)\n"
        "当前模式：共享工作区\n"
        "当前工作区：共享 / /tmp/hello-mini\n\n"
        "【引用消息】\n"
        "Gemini：这是首页改版需求文档，包含 Hero、功能区、底部 CTA。\n\n"
        "【当前消息】\n"
        "开发"
    )

    rewritten = CodexCliOrchestrator._rewrite_quoted_development_request(message)

    assert "【引用需求文档】" in rewritten
    assert "这是首页改版需求文档" in rewritten
    assert "用户正在引用上面的需求文档，并要求你在当前项目中直接开始开发" in rewritten
    assert "如果需求已经足够明确：直接开始实现" in rewritten
    assert "用户原话：开发" in rewritten


def test_handle_control_command_saves_quoted_requirement_doc_to_default_path():
    with TemporaryDirectory() as tmpdir:
        working_dir = Path(tmpdir) / "project"
        working_dir.mkdir()
        orchestrator = CodexCliOrchestrator(
            bot_key="cx_bot",
            working_dir=str(working_dir),
        )

        orchestrator._ensure_runtime_context = lambda user_id, session_key, log_context=None: (
            {
                "working_dir": str(working_dir),
                "project": {"name": "hello-mini", "project_id": "proj_hello"},
                "workspace": {"workspace_id": "ws_hello"},
                "runtime_session_key": session_key or user_id,
            },
            "",
        )

        message = (
            "【引用消息】\n"
            "# 需求文档\n\n"
            "## 页面目标\n\n"
            "做一个 hello world 首页。\n\n"
            "## 页面结构\n\n"
            "- 标题\n"
            "- 按钮\n"
            "- 底部说明\n\n"
            "【当前消息】\n"
            "保存为需求文档"
        )

        reply = asyncio.run(
            orchestrator.handle_control_command(
                user_id="alice",
                content=message,
                session_key="alice",
                log_context={},
            )
        )

        requirement_doc = working_dir / "docs" / "requirements.md"
        assert requirement_doc.exists()
        assert requirement_doc.read_text(encoding="utf-8") == (
            "# 需求文档\n\n"
            "## 页面目标\n\n"
            "做一个 hello world 首页。\n\n"
            "## 页面结构\n\n"
            "- 标题\n"
            "- 按钮\n"
            "- 底部说明\n"
        )
        assert "docs/requirements.md" in reply
        assert "根据 docs/requirements.md 开发" in reply


def test_handle_control_command_saves_quoted_requirement_doc_to_custom_path():
    with TemporaryDirectory() as tmpdir:
        working_dir = Path(tmpdir) / "project"
        working_dir.mkdir()
        orchestrator = CodexCliOrchestrator(
            bot_key="cx_bot",
            working_dir=str(working_dir),
        )

        orchestrator._ensure_runtime_context = lambda user_id, session_key, log_context=None: (
            {
                "working_dir": str(working_dir),
                "project": {"name": "hello-mini", "project_id": "proj_hello"},
                "workspace": {"workspace_id": "ws_hello"},
                "runtime_session_key": session_key or user_id,
            },
            "",
        )

        message = (
            "【引用消息】\n"
            "这是 PRD 正文。\n\n"
            "【当前消息】\n"
            "根据引用消息生成 docs/prd.md"
        )

        reply = asyncio.run(
            orchestrator.handle_control_command(
                user_id="alice",
                content=message,
                session_key="alice",
                log_context={},
            )
        )

        prd_doc = working_dir / "docs" / "prd.md"
        assert prd_doc.exists()
        assert prd_doc.read_text(encoding="utf-8") == "这是 PRD 正文。\n"
        assert "docs/prd.md" in reply
        assert "根据 docs/prd.md 开发" in reply


def test_handle_control_command_saves_brochure_requirement_doc_with_brochure_next_step():
    with TemporaryDirectory() as tmpdir:
        working_dir = Path(tmpdir) / "project"
        working_dir.mkdir()
        orchestrator = CodexCliOrchestrator(
            bot_key="cx_bot",
            working_dir=str(working_dir),
        )

        orchestrator._ensure_runtime_context = lambda user_id, session_key, log_context=None: (
            {
                "working_dir": str(working_dir),
                "project": {"name": "hello-brochure", "project_id": "proj_brochure"},
                "workspace": {"workspace_id": "ws_brochure"},
                "runtime_session_key": session_key or user_id,
            },
            "",
        )

        message = (
            "【引用消息】\n"
            "这是产品画册需求文档。\n\n"
            "【当前消息】\n"
            "保存为画册需求文档"
        )

        reply = asyncio.run(
            orchestrator.handle_control_command(
                user_id="alice",
                content=message,
                session_key="alice",
                log_context={},
            )
        )

        requirement_doc = working_dir / "docs" / "requirements.md"
        assert requirement_doc.exists()
        assert "docs/requirements.md" in reply
        assert "下一步可直接发送：生成画册" in reply


def test_handle_text_message_rewrites_brochure_generation_request_before_codex_turn():
    with TemporaryDirectory() as tmpdir:
        working_dir = Path(tmpdir) / "project"
        working_dir.mkdir()
        orchestrator = CodexCliOrchestrator(
            bot_key="cx_bot",
            working_dir=str(working_dir),
        )
        captured = {}

        async def fake_run_codex_turn(
            user_id: str,
            inputs,
            stream_id: str,
            session_key: str,
            log_context: dict,
            on_stream_delta,
            on_interaction_request,
            message_content: str,
            runtime_context=None,
        ) -> str:
            captured["message_content"] = message_content
            captured["input_text"] = inputs[0]["text"]
            return "ok"

        orchestrator._run_codex_turn = fake_run_codex_turn

        asyncio.run(
            orchestrator.handle_text_message(
                user_id="alice",
                message="生成画册",
                stream_id="stream_brochure",
                session_key="alice",
                log_context={},
            )
        )

        assert "【画册生成任务】" in captured["message_content"]
        assert "`brochure/index.html`" in captured["message_content"]
        assert "HTML/H5 产品画册" in captured["input_text"]


def test_handle_control_command_publish_brochure_reuses_pages_flow():
    with TemporaryDirectory() as tmpdir:
        working_dir = Path(tmpdir) / "project"
        working_dir.mkdir()
        orchestrator = CodexCliOrchestrator(
            bot_key="cx_bot",
            working_dir=str(working_dir),
        )
        captured = {}

        def fake_publish_pages(**kwargs):
            captured.update(kwargs)
            return "已完成一键发布 Cloudflare Pages\n项目：hello-brochure"

        orchestrator._handle_publish_pages_command = fake_publish_pages

        reply = asyncio.run(
            orchestrator.handle_control_command(
                user_id="alice",
                content="发布画册 hello-brochure hello-brochure-site",
                session_key="alice",
                log_context={},
            )
        )

        assert captured["repository_name"] == "hello-brochure"
        assert captured["pages_project_name"] == "hello-brochure-site"
        assert captured["build_dir"] == "brochure"
        assert "画册目录：brochure" in reply


def test_handle_control_command_exports_brochure_pdf_and_ppt():
    with TemporaryDirectory() as tmpdir:
        working_dir = Path(tmpdir) / "project"
        working_dir.mkdir()
        orchestrator = CodexCliOrchestrator(
            bot_key="cx_bot",
            working_dir=str(working_dir),
        )
        orchestrator._ensure_runtime_context = lambda user_id, session_key, log_context=None: (
            {
                "working_dir": str(working_dir),
                "project": {"project_id": "proj_1", "name": "hello-brochure"},
            },
            None,
        )
        captured = {}

        def fake_export_pdf(**kwargs):
            captured["pdf"] = kwargs
            return SimpleNamespace(
                input_relative_path="brochure/index.html",
                output_relative_path="dist/brochure.pdf",
                output_path=str(working_dir / "dist" / "brochure.pdf"),
            )

        def fake_export_ppt(**kwargs):
            captured["ppt"] = kwargs
            return SimpleNamespace(
                input_relative_path="docs/brochure-outline.md",
                output_relative_path="dist/brochure.pptx",
                output_path=str(working_dir / "dist" / "brochure.pptx"),
            )

        orchestrator.brochure_export_manager = SimpleNamespace(
            export_pdf=fake_export_pdf,
            export_ppt=fake_export_ppt,
        )

        pdf_reply = asyncio.run(
            orchestrator.handle_control_command(
                user_id="alice",
                content="导出画册PDF",
                session_key="alice",
                log_context={},
            )
        )
        ppt_reply = asyncio.run(
            orchestrator.handle_control_command(
                user_id="alice",
                content="导出画册PPT",
                session_key="alice",
                log_context={},
            )
        )

        assert captured["pdf"]["html_path"] == "brochure/index.html"
        assert captured["pdf"]["output_path"] == "dist/brochure.pdf"
        assert "已导出画册 PDF" in pdf_reply
        assert "dist/brochure.pdf" in pdf_reply
        assert captured["ppt"]["outline_path"] == ""
        assert captured["ppt"]["output_path"] == "dist/brochure.pptx"
        assert "已导出画册 PPT" in ppt_reply
        assert "dist/brochure.pptx" in ppt_reply


def test_handle_control_command_returns_brochure_preview_image_payload():
    with TemporaryDirectory() as tmpdir:
        working_dir = Path(tmpdir) / "project"
        working_dir.mkdir()
        orchestrator = CodexCliOrchestrator(
            bot_key="cx_bot",
            working_dir=str(working_dir),
        )
        orchestrator._ensure_runtime_context = lambda user_id, session_key, log_context=None: (
            {
                "working_dir": str(working_dir),
                "project": {"project_id": "proj_1", "name": "hello-brochure"},
            },
            None,
        )
        captured = {}

        def fake_export_image(**kwargs):
            captured["image"] = kwargs
            return SimpleNamespace(
                input_relative_path="brochure/index.html",
                output_relative_path="dist/brochure-preview.png",
                output_path=str(working_dir / "dist" / "brochure-preview.png"),
            )

        def fake_encode_image_file(output_path: str):
            captured["encoded_path"] = output_path
            return ("ZmFrZV9pbWFnZQ==", "fake-md5")

        orchestrator.brochure_export_manager = SimpleNamespace(
            export_image=fake_export_image,
            encode_image_file=fake_encode_image_file,
        )

        reply = asyncio.run(
            orchestrator.handle_control_command(
                user_id="alice",
                content="回传画册图片",
                session_key="alice",
                log_context={},
            )
        )

        assert captured["image"]["html_path"] == "brochure/index.html"
        assert captured["image"]["output_path"] == "dist/brochure-preview.png"
        assert captured["encoded_path"].endswith("dist/brochure-preview.png")
        assert isinstance(reply, dict)
        assert reply["type"] == "image"
        assert reply["image_base64"] == "ZmFrZV9pbWFnZQ=="
        assert "已回传画册预览图" in reply["content"]


def test_message_dispatcher_routes_structured_image_control_reply():
    from src.transport.message_dispatcher import MessageDispatcher

    class DummyWs:
        def __init__(self):
            self.payloads = []

        async def send_reply(self, payload: dict):
            self.payloads.append(payload)

    class DummyOrchestrator(BaseOrchestrator):
        async def handle_text_message(
            self,
            user_id: str,
            message: str,
            stream_id: str,
            session_key: str = "",
            log_context: dict = None,
            on_stream_delta=None,
        ) -> str:
            return ""

        async def handle_multimodal_message(
            self,
            user_id: str,
            content_blocks,
            stream_id: str,
            session_key: str = "",
            log_context: dict = None,
            on_stream_delta=None,
        ) -> str:
            return ""

        async def handle_control_command(
            self,
            user_id: str,
            content: str,
            session_key: str = "",
            log_context: dict = None,
        ):
            return {
                "type": "image",
                "image_base64": "ZmFrZV9pbWFnZQ==",
                "image_md5": "fake-md5",
                "content": "已回传画册预览图",
            }

        def is_control_command(self, content: str) -> bool:
            return True

    dispatcher = MessageDispatcher(
        DummyWs(),
        BotConfig(
            bot_key="cx_bot",
            bot_id="test_bot_id",
            secret="test_secret",
            bot_type="codex_cli",
        ),
        orchestrator=DummyOrchestrator(),
    )

    asyncio.run(
        dispatcher._handle_text(
            "req_brochure_image",
            {"msgtype": "text", "text": {"content": "回传画册图片"}},
            "alice",
            "alice",
            "single",
        )
    )

    payload = dispatcher.ws.payloads[-1]
    assert payload["cmd"] == "aibot_respond_msg"
    assert payload["body"]["stream"]["content"] == "已回传画册预览图"
    assert payload["body"]["stream"]["msg_item"][0]["msgtype"] == "image"
    assert payload["body"]["stream"]["msg_item"][0]["image"]["base64"] == "ZmFrZV9pbWFnZQ=="


def test_message_dispatcher_brochure_internal_delegate_runs_full_flow():
    from src.transport.message_dispatcher import MessageDispatcher

    class DummyWs:
        def __init__(self):
            self.payloads = []

        async def send_reply(self, payload: dict):
            self.payloads.append(payload)

    class BrochureOrchestrator(BaseOrchestrator):
        async def handle_text_message(
            self,
            user_id: str,
            message: str,
            stream_id: str,
            session_key: str = "",
            log_context: dict = None,
            on_stream_delta=None,
        ) -> str:
            assert "可直接交给 Codex CLI 落地执行" in message
            if on_stream_delta:
                await on_stream_delta("这是整理后的画册需求文档", True)
            return "这是整理后的画册需求文档"

        async def handle_multimodal_message(
            self,
            user_id: str,
            content_blocks,
            stream_id: str,
            session_key: str = "",
            log_context: dict = None,
            on_stream_delta=None,
        ) -> str:
            return ""

    class CodexDelegateOrchestrator(BaseOrchestrator):
        def __init__(self):
            self.calls = []

        async def handle_text_message(
            self,
            user_id: str,
            message: str,
            stream_id: str,
            session_key: str = "",
            log_context: dict = None,
            on_stream_delta=None,
            **kwargs,
        ) -> str:
            self.calls.append(("text", message))
            if on_stream_delta:
                await on_stream_delta("Codex 已生成画册文件", True)
            return "Codex 已生成画册文件"

        async def handle_multimodal_message(
            self,
            user_id: str,
            content_blocks,
            stream_id: str,
            session_key: str = "",
            log_context: dict = None,
            on_stream_delta=None,
        ) -> str:
            return ""

        async def handle_control_command(
            self,
            user_id: str,
            content: str,
            session_key: str = "",
            log_context: dict = None,
        ):
            self.calls.append(("control", content))
            if "保存为画册需求文档" in content:
                return "已保存需求文档：docs/requirements.md"
            if content == "回传画册图片":
                return {
                    "type": "image",
                    "image_base64": "ZmFrZV9pbWFnZQ==",
                    "image_md5": "fake-md5",
                    "content": "已回传画册预览图",
                }
            return f"done: {content}"

    brochure_config = BotConfig(
        bot_key="brochure_bot",
        bot_id="bot_brochure",
        secret="secret_brochure",
        bot_type="gemini",
        name="产品画册",
        description="产品画册策划与文案机器人",
        provider_config={
            "enable_brochure_internal_delegate": True,
            "delegate_execution_bot_key": "cx_bot",
        },
    )
    codex_config = BotConfig(
        bot_key="cx_bot",
        bot_id="bot_cx",
        secret="secret_cx",
        bot_type="codex_cli",
        working_dir="C:/next",
    )
    delegate_orchestrator = CodexDelegateOrchestrator()
    delegate_manager = BotDelegateManager(
        {"brochure_bot": brochure_config, "cx_bot": codex_config},
        {"cx_bot": delegate_orchestrator},
    )
    ws = DummyWs()
    dispatcher = MessageDispatcher(
        ws,
        brochure_config,
        orchestrator=BrochureOrchestrator(),
        delegate_manager=delegate_manager,
    )

    asyncio.run(
        dispatcher._handle_text(
            "req_full_brochure",
            {"msgtype": "text", "text": {"content": "做完整画册"}},
            "alice",
            "alice",
            "single",
        )
    )

    assert delegate_orchestrator.calls[0][0] == "control"
    assert "保存为画册需求文档" in delegate_orchestrator.calls[0][1]
    assert delegate_orchestrator.calls[1][0] == "text"
    assert "生成画册" in delegate_orchestrator.calls[1][1]
    assert delegate_orchestrator.calls[2] == ("control", "回传画册图片")
    assert any(payload["body"]["stream"]["content"].startswith("已进入画册自动落地流程") for payload in ws.payloads if payload["cmd"] == "aibot_respond_msg" and payload["body"].get("msgtype") == "stream")
    assert any(
        payload["body"]["stream"].get("msg_item")
        and payload["body"]["stream"]["msg_item"][0]["msgtype"] == "image"
        for payload in ws.payloads
    )


def test_message_dispatcher_brochure_internal_delegate_forwards_pending_interaction():
    from src.transport.message_dispatcher import MessageDispatcher

    class DummyWs:
        def __init__(self):
            self.payloads = []

        async def send_reply(self, payload: dict):
            self.payloads.append(payload)

    class BrochureOrchestrator(BaseOrchestrator):
        async def handle_text_message(
            self,
            user_id: str,
            message: str,
            stream_id: str,
            session_key: str = "",
            log_context: dict = None,
            on_stream_delta=None,
        ) -> str:
            return ""

        async def handle_multimodal_message(
            self,
            user_id: str,
            content_blocks,
            stream_id: str,
            session_key: str = "",
            log_context: dict = None,
            on_stream_delta=None,
        ) -> str:
            return ""

    class CodexDelegateOrchestrator(BaseOrchestrator):
        def __init__(self):
            self.interactions = []

        def get_runtime_session_key(
            self,
            user_id: str,
            session_key: str = "",
            log_context: dict = None,
        ) -> str:
            return f"delegate::{session_key or user_id}"

        def has_pending_interaction(self, session_key: str) -> bool:
            return session_key == "delegate::alice"

        async def handle_interaction_text(self, session_key: str, text: str):
            self.interactions.append((session_key, text))
            return {"submitted": True, "ack": "已提交给后台画册任务"}

        async def handle_text_message(
            self,
            user_id: str,
            message: str,
            stream_id: str,
            session_key: str = "",
            log_context: dict = None,
            on_stream_delta=None,
            **kwargs,
        ) -> str:
            return ""

        async def handle_multimodal_message(
            self,
            user_id: str,
            content_blocks,
            stream_id: str,
            session_key: str = "",
            log_context: dict = None,
            on_stream_delta=None,
        ) -> str:
            return ""

    brochure_config = BotConfig(
        bot_key="brochure_bot",
        bot_id="bot_brochure",
        secret="secret_brochure",
        bot_type="gemini",
        name="产品画册",
        description="产品画册策划与文案机器人",
        provider_config={
            "enable_brochure_internal_delegate": True,
            "delegate_execution_bot_key": "cx_bot",
        },
    )
    codex_config = BotConfig(
        bot_key="cx_bot",
        bot_id="bot_cx",
        secret="secret_cx",
        bot_type="codex_cli",
        working_dir="C:/next",
    )
    delegate_orchestrator = CodexDelegateOrchestrator()
    delegate_manager = BotDelegateManager(
        {"brochure_bot": brochure_config, "cx_bot": codex_config},
        {"cx_bot": delegate_orchestrator},
    )
    ws = DummyWs()
    dispatcher = MessageDispatcher(
        ws,
        brochure_config,
        orchestrator=BrochureOrchestrator(),
        delegate_manager=delegate_manager,
    )

    asyncio.run(
        dispatcher._handle_text(
            "req_delegate_interaction",
            {"msgtype": "text", "text": {"content": "同意，继续执行"}},
            "alice",
            "alice",
            "single",
        )
    )

    assert delegate_orchestrator.interactions == [("delegate::alice", "同意，继续执行")]
    payload = ws.payloads[-1]
    assert payload["cmd"] == "aibot_respond_msg"
    assert payload["body"]["stream"]["content"] == "已提交给后台画册任务"


def test_message_dispatcher_brochure_image_upload_is_saved_into_delegate_workspace():
    from src.transport.message_dispatcher import MessageDispatcher
    from src.utils.weixin_utils import FileUtils

    class DummyWs:
        def __init__(self):
            self.payloads = []

        async def send_reply(self, payload: dict):
            self.payloads.append(payload)

    class BrochureOrchestrator(BaseOrchestrator):
        async def handle_text_message(self, user_id: str, message: str, stream_id: str, session_key: str = "", log_context: dict = None, on_stream_delta=None) -> str:
            return ""

        async def handle_multimodal_message(self, user_id: str, content_blocks, stream_id: str, session_key: str = "", log_context: dict = None, on_stream_delta=None) -> str:
            return ""

    class CodexDelegateOrchestrator(BaseOrchestrator):
        def __init__(self, workspace_path: str):
            self.workspace_path = workspace_path

        def _ensure_runtime_context(self, user_id: str, session_key: str = "", log_context: dict = None):
            return (
                {
                    "working_dir": self.workspace_path,
                    "project": {"project_id": "proj_1", "name": "hello-brochure"},
                },
                None,
            )

        async def handle_text_message(self, user_id: str, message: str, stream_id: str, session_key: str = "", log_context: dict = None, on_stream_delta=None, **kwargs) -> str:
            return ""

        async def handle_multimodal_message(self, user_id: str, content_blocks, stream_id: str, session_key: str = "", log_context: dict = None, on_stream_delta=None) -> str:
            return ""

    with TemporaryDirectory() as tmpdir:
        workspace_path = Path(tmpdir) / "workspace"
        workspace_path.mkdir()

        brochure_config = BotConfig(
            bot_key="brochure_bot",
            bot_id="bot_brochure",
            secret="secret_brochure",
            bot_type="gemini",
            provider_config={
                "enable_brochure_internal_delegate": True,
                "delegate_execution_bot_key": "cx_bot",
            },
        )
        codex_config = BotConfig(
            bot_key="cx_bot",
            bot_id="bot_cx",
            secret="secret_cx",
            bot_type="codex_cli",
            working_dir=str(workspace_path),
        )
        delegate_manager = BotDelegateManager(
            {"brochure_bot": brochure_config, "cx_bot": codex_config},
            {"cx_bot": CodexDelegateOrchestrator(str(workspace_path))},
        )
        ws = DummyWs()
        dispatcher = MessageDispatcher(
            ws,
            brochure_config,
            orchestrator=BrochureOrchestrator(),
            delegate_manager=delegate_manager,
        )

        original_download = FileUtils.download_and_decrypt

        async def fake_download(url: str, aes_key: str, timeout: int = 30, key_format: str = "auto"):
            return (b"\x89PNG\r\n\x1a\nfakepng", "")

        FileUtils.download_and_decrypt = staticmethod(fake_download)
        try:
            asyncio.run(
                dispatcher._handle_image(
                    "req_brochure_image",
                    {"image": {"url": "https://example.com/image", "aeskey": "abc"}},
                    "alice",
                    "alice",
                    "single",
                )
            )
        finally:
            FileUtils.download_and_decrypt = original_download

        payload = ws.payloads[-1]
        assert "已接收画册图片素材" in payload["body"]["stream"]["content"]
        manifest = load_brochure_source_materials(str(workspace_path))
        assert manifest
        assert manifest["image_count"] == 1
        entry = manifest["materials"][0]
        assert entry["kind"] == "image"
        assert entry["relative_path"].startswith("brochure/assets/")
        assert (workspace_path / entry["relative_path"]).exists()


def test_message_dispatcher_brochure_file_upload_is_saved_into_delegate_workspace():
    from src.transport.message_dispatcher import MessageDispatcher
    from src.utils.weixin_utils import FileUtils

    class DummyWs:
        def __init__(self):
            self.payloads = []

        async def send_reply(self, payload: dict):
            self.payloads.append(payload)

    class BrochureOrchestrator(BaseOrchestrator):
        async def handle_text_message(self, user_id: str, message: str, stream_id: str, session_key: str = "", log_context: dict = None, on_stream_delta=None) -> str:
            return ""

        async def handle_multimodal_message(self, user_id: str, content_blocks, stream_id: str, session_key: str = "", log_context: dict = None, on_stream_delta=None) -> str:
            return ""

    class CodexDelegateOrchestrator(BaseOrchestrator):
        def __init__(self, workspace_path: str):
            self.workspace_path = workspace_path

        def _ensure_runtime_context(self, user_id: str, session_key: str = "", log_context: dict = None):
            return (
                {
                    "working_dir": self.workspace_path,
                    "project": {"project_id": "proj_1", "name": "hello-brochure"},
                },
                None,
            )

        async def handle_text_message(self, user_id: str, message: str, stream_id: str, session_key: str = "", log_context: dict = None, on_stream_delta=None, **kwargs) -> str:
            return ""

        async def handle_multimodal_message(self, user_id: str, content_blocks, stream_id: str, session_key: str = "", log_context: dict = None, on_stream_delta=None) -> str:
            return ""

    with TemporaryDirectory() as tmpdir:
        workspace_path = Path(tmpdir) / "workspace"
        workspace_path.mkdir()

        brochure_config = BotConfig(
            bot_key="brochure_bot",
            bot_id="bot_brochure",
            secret="secret_brochure",
            bot_type="gemini",
            provider_config={
                "enable_brochure_internal_delegate": True,
                "delegate_execution_bot_key": "cx_bot",
            },
        )
        codex_config = BotConfig(
            bot_key="cx_bot",
            bot_id="bot_cx",
            secret="secret_cx",
            bot_type="codex_cli",
            working_dir=str(workspace_path),
        )
        delegate_manager = BotDelegateManager(
            {"brochure_bot": brochure_config, "cx_bot": codex_config},
            {"cx_bot": CodexDelegateOrchestrator(str(workspace_path))},
        )
        ws = DummyWs()
        dispatcher = MessageDispatcher(
            ws,
            brochure_config,
            orchestrator=BrochureOrchestrator(),
            delegate_manager=delegate_manager,
        )

        original_download = FileUtils.download_and_decrypt

        async def fake_download(url: str, aes_key: str, timeout: int = 30, key_format: str = "auto"):
            return ("型号,重量\nL1,2kg\n".encode("utf-8"), "specs.csv")

        FileUtils.download_and_decrypt = staticmethod(fake_download)
        try:
            asyncio.run(
                dispatcher._handle_file(
                    "req_brochure_file",
                    {"file": {"url": "https://example.com/file", "aeskey": "abc", "filename": "specs.csv"}},
                    "alice",
                    "alice",
                    "single",
                )
            )
        finally:
            FileUtils.download_and_decrypt = original_download

        payload = ws.payloads[-1]
        assert "已接收产品参数文档" in payload["body"]["stream"]["content"]
        manifest = load_brochure_source_materials(str(workspace_path))
        assert manifest
        assert manifest["document_count"] == 1
        entry = manifest["materials"][0]
        assert entry["kind"] == "document"
        assert entry["relative_path"].startswith("docs/source-materials/")
        assert "型号,重量" in entry["summary"]
        assert (workspace_path / entry["relative_path"]).exists()


def test_message_dispatcher_brochure_delegate_planning_prompt_includes_uploaded_materials():
    from src.transport.message_dispatcher import MessageDispatcher
    from src.utils.brochure_source_materials import write_brochure_source_materials

    class DummyWs:
        def __init__(self):
            self.payloads = []

        async def send_reply(self, payload: dict):
            self.payloads.append(payload)

    class BrochureOrchestrator(BaseOrchestrator):
        def __init__(self):
            self.messages = []

        async def handle_text_message(
            self,
            user_id: str,
            message: str,
            stream_id: str,
            session_key: str = "",
            log_context: dict = None,
            on_stream_delta=None,
        ) -> str:
            self.messages.append(message)
            if on_stream_delta:
                await on_stream_delta("# 画册需求\n\n- 首页", True)
            return "# 画册需求\n\n- 首页"

        async def handle_multimodal_message(
            self,
            user_id: str,
            content_blocks,
            stream_id: str,
            session_key: str = "",
            log_context: dict = None,
            on_stream_delta=None,
        ) -> str:
            return ""

    class CodexDelegateOrchestrator(BaseOrchestrator):
        def __init__(self, workspace_path: str):
            self.workspace_path = workspace_path

        def _ensure_runtime_context(self, user_id: str, session_key: str = "", log_context: dict = None):
            return (
                {
                    "working_dir": self.workspace_path,
                    "project": {"project_id": "proj_1", "name": "hello-brochure"},
                },
                None,
            )

        async def handle_text_message(
            self,
            user_id: str,
            message: str,
            stream_id: str,
            session_key: str = "",
            log_context: dict = None,
            on_stream_delta=None,
            **kwargs,
        ) -> str:
            if on_stream_delta:
                await on_stream_delta("已生成画册", True)
            return "已生成画册"

        async def handle_control_command(
            self,
            user_id: str,
            content: str,
            session_key: str = "",
            log_context: dict = None,
        ):
            return "已导出预览图"

        async def handle_multimodal_message(
            self,
            user_id: str,
            content_blocks,
            stream_id: str,
            session_key: str = "",
            log_context: dict = None,
            on_stream_delta=None,
        ) -> str:
            return ""

    with TemporaryDirectory() as tmpdir:
        workspace_path = Path(tmpdir) / "workspace"
        workspace_path.mkdir()
        write_brochure_source_materials(
            str(workspace_path),
            {
                "version": 1,
                "project_name": "hello-brochure",
                "generated_at": "2026-03-23T12:00:00Z",
                "material_count": 2,
                "image_count": 1,
                "document_count": 1,
                "materials": [
                    {"kind": "image", "relative_path": "brochure/assets/cover.png"},
                    {"kind": "document", "relative_path": "docs/source-materials/specs.csv", "summary": "型号,重量"},
                ],
            },
        )

        brochure_config = BotConfig(
            bot_key="brochure_bot",
            bot_id="bot_brochure",
            secret="secret_brochure",
            bot_type="gemini",
            provider_config={
                "enable_brochure_internal_delegate": True,
                "delegate_execution_bot_key": "cx_bot",
            },
        )
        codex_config = BotConfig(
            bot_key="cx_bot",
            bot_id="bot_cx",
            secret="secret_cx",
            bot_type="codex_cli",
            working_dir=str(workspace_path),
        )
        brochure_orchestrator = BrochureOrchestrator()
        delegate_manager = BotDelegateManager(
            {"brochure_bot": brochure_config, "cx_bot": codex_config},
            {"cx_bot": CodexDelegateOrchestrator(str(workspace_path))},
        )
        dispatcher = MessageDispatcher(
            DummyWs(),
            brochure_config,
            orchestrator=brochure_orchestrator,
            delegate_manager=delegate_manager,
        )

        asyncio.run(
            dispatcher._handle_text(
                "req_brochure_materials_context",
                {"msgtype": "text", "text": {"content": "做完整画册并回传图片"}},
                "alice",
                "alice",
                "single",
            )
        )

        assert brochure_orchestrator.messages
        planning_prompt = brochure_orchestrator.messages[0]
        assert "【当前已上传画册资料】" in planning_prompt
        assert "brochure/assets/cover.png" in planning_prompt
        assert "docs/source-materials/specs.csv" in planning_prompt


def test_message_dispatcher_brochure_internal_delegate_resumes_recent_interrupted_workflow():
    from src.transport.message_dispatcher import MessageDispatcher
    from src.core.task_registry import get_task_registry

    class DummyWs:
        def __init__(self):
            self.payloads = []

        async def send_reply(self, payload: dict):
            self.payloads.append(payload)

    class BrochureOrchestrator(BaseOrchestrator):
        async def handle_text_message(
            self,
            user_id: str,
            message: str,
            stream_id: str,
            session_key: str = "",
            log_context: dict = None,
            on_stream_delta=None,
        ) -> str:
            return ""

        async def handle_multimodal_message(
            self,
            user_id: str,
            content_blocks,
            stream_id: str,
            session_key: str = "",
            log_context: dict = None,
            on_stream_delta=None,
        ) -> str:
            return ""

    class CodexDelegateOrchestrator(BaseOrchestrator):
        def __init__(self):
            self.messages = []
            self.control_commands = []

        def get_runtime_session_key(
            self,
            user_id: str,
            session_key: str = "",
            log_context: dict = None,
        ) -> str:
            return f"delegate::{session_key or user_id}"

        def has_pending_interaction(self, session_key: str) -> bool:
            return False

        async def handle_text_message(
            self,
            user_id: str,
            message: str,
            stream_id: str,
            session_key: str = "",
            log_context: dict = None,
            on_stream_delta=None,
            **kwargs,
        ) -> str:
            self.messages.append(message)
            if on_stream_delta:
                await on_stream_delta("继续生成中", True)
            return "继续生成完成"

        async def handle_control_command(
            self,
            user_id: str,
            content: str,
            session_key: str = "",
            log_context: dict = None,
        ):
            self.control_commands.append(content)
            return "已导出PDF"

        async def handle_multimodal_message(
            self,
            user_id: str,
            content_blocks,
            stream_id: str,
            session_key: str = "",
            log_context: dict = None,
            on_stream_delta=None,
        ) -> str:
            return ""

    async def seed_recent_failure(task_key: str):
        registry = get_task_registry()

        async def fail():
            raise RuntimeError("delegate interrupted")

        task = asyncio.create_task(fail())
        registry.register(
            task_key,
            task,
            "stream_old",
            req_id="req_old",
            brochure_delegate_resume={
                "resumable": True,
                "mode": "full_flow",
                "planning_needed": True,
                "final_control_command": "导出画册PDF",
                "composed_message": "【当前消息】\n做完整画册并导出PDF",
                "target_bot_key": "cx_bot",
                "stage": "generate",
                "plan_text": "# 画册需求\n\n- 首页\n- 产品亮点",
            },
        )
        try:
            await task
        except RuntimeError:
            pass
        await asyncio.sleep(0)

    brochure_config = BotConfig(
        bot_key="brochure_bot",
        bot_id="bot_brochure",
        secret="secret_brochure",
        bot_type="gemini",
        name="产品画册",
        description="产品画册策划与文案机器人",
        provider_config={
            "enable_brochure_internal_delegate": True,
            "delegate_execution_bot_key": "cx_bot",
        },
    )
    codex_config = BotConfig(
        bot_key="cx_bot",
        bot_id="bot_cx",
        secret="secret_cx",
        bot_type="codex_cli",
        working_dir="C:/next",
    )
    delegate_orchestrator = CodexDelegateOrchestrator()
    delegate_manager = BotDelegateManager(
        {"brochure_bot": brochure_config, "cx_bot": codex_config},
        {"cx_bot": delegate_orchestrator},
    )
    ws = DummyWs()
    dispatcher = MessageDispatcher(
        ws,
        brochure_config,
        orchestrator=BrochureOrchestrator(),
        delegate_manager=delegate_manager,
    )

    task_key = dispatcher._task_registry_key("alice")
    registry = get_task_registry()
    registry.forget(task_key)
    asyncio.run(seed_recent_failure(task_key))

    asyncio.run(
        dispatcher._handle_text(
            "req_delegate_resume",
            {"msgtype": "text", "text": {"content": "继续"}},
            "alice",
            "alice",
            "single",
        )
    )

    assert len(delegate_orchestrator.messages) == 1
    assert "继续上次画册自动落地任务" in delegate_orchestrator.messages[0]
    assert "不要从头重做" in delegate_orchestrator.messages[0]
    assert "# 画册需求" in delegate_orchestrator.messages[0]
    assert delegate_orchestrator.control_commands == ["导出画册PDF"]
    assert ws.payloads
    assert "已恢复上次中断的画册自动落地流程" in ws.payloads[-1]["body"]["stream"]["content"]
    registry.forget(task_key)


def test_handle_text_message_rewrites_quoted_development_handoff_before_codex_turn():
    with TemporaryDirectory() as tmpdir:
        working_dir = Path(tmpdir) / "project"
        working_dir.mkdir()
        orchestrator = CodexCliOrchestrator(
            bot_key="cx_bot",
            working_dir=str(working_dir),
        )
        captured = {}

        async def fake_run_codex_turn(
            user_id: str,
            inputs,
            stream_id: str,
            session_key: str,
            log_context: dict,
            on_stream_delta,
            on_interaction_request,
            message_content: str,
            runtime_context=None,
        ) -> str:
            captured["message_content"] = message_content
            captured["input_text"] = inputs[0]["text"]
            return "ok"

        orchestrator._run_codex_turn = fake_run_codex_turn

        message = (
            "【引用消息】\n"
            "Gemini：这是需求文档，先做一个 hello world 首页。\n\n"
            "【当前消息】\n"
            "开始开发"
        )

        asyncio.run(
            orchestrator.handle_text_message(
                user_id="alice",
                message=message,
                stream_id="stream1",
                session_key="alice",
                log_context={},
            )
        )

        assert "【引用需求文档】" in captured["message_content"]
        assert "用户原话：开始开发" in captured["message_content"]
        assert "直接开始实现" in captured["input_text"]


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
        assert "可发送：3.2" in status_before
        assert "已设置当前工作区 Git 身份" in set_reply
        assert "Git 初始化：已初始化新仓库" in set_reply
        assert "user.name：kangaroo117" in set_reply
        assert "user.email：kangaroo117@users.noreply.github.com" in set_reply
        assert "状态：已配置" in status_after
        assert "user.name：kangaroo117" in status_after
        assert "Git身份：kangaroo117 <kangaroo117@users.noreply.github.com>" in project_reply


def test_create_existing_project_reuses_current_project_instead_of_error():
    with TemporaryDirectory() as tmpdir:
        working_dir = Path(tmpdir) / "project"
        working_dir.mkdir()
        orchestrator = CodexCliOrchestrator(
            bot_key="cx_bot",
            working_dir=str(working_dir),
        )

        async def run_flow():
            first_reply = await orchestrator.handle_control_command(
                "alice",
                "新建项目 hello-mini",
                session_key="alice",
            )
            second_reply = await orchestrator.handle_control_command(
                "alice",
                "2.5 hello-mini",
                session_key="alice",
            )
            return first_reply, second_reply

        first_reply, second_reply = asyncio.run(run_flow())

        assert "已创建个人项目：hello-mini" in first_reply
        assert "空工作区只会创建项目目录" in first_reply
        assert "项目已存在：hello-mini" in second_reply
        assert "已为你直接进入现有项目" in second_reply
        assert "已进入项目：hello-mini" in second_reply
        assert "2.5 hello-mini-2" in second_reply


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
        assert "3.10 <仓库名>" in reply


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
        assert "3.10 hello-world" in reply


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
                "2 5 hello-world",
                session_key="alice",
            )
            git_reply = await orchestrator.handle_control_command(
                "alice",
                "3 2 kangaroo117 kangaroo117@users.noreply.github.com",
                session_key="alice",
            )
            return help_reply, create_reply, git_reply

        help_reply, create_reply, git_reply = asyncio.run(run_flow())

        assert orchestrator.is_control_command("1") is True
        assert orchestrator.is_control_command("2 5 hello-world") is True
        assert "新手开始" in help_reply
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

        normalized = orchestrator._normalize_control_command_input("3.10 hello-world")
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


def test_publish_wechat_miniprogram_path_error_message_keeps_appid_optional():
    with TemporaryDirectory() as tmpdir:
        working_dir = Path(tmpdir) / "project"
        working_dir.mkdir()
        orchestrator = CodexCliOrchestrator(
            bot_key="cx_bot",
            working_dir=str(working_dir),
        )
        orchestrator._ensure_runtime_context = lambda user_id, session_key, log_context=None: (
            {
                "working_dir": str(working_dir),
                "project": {"name": "hello-mini"},
            },
            None,
        )
        orchestrator.project_deployment_manager.get_git_identity = lambda workspace_path: SimpleNamespace(
            is_configured=True
        )
        orchestrator._resolve_push_repository_name = lambda project, repository_name, workspace_path: (
            repository_name
        )
        orchestrator._resolve_wechat_miniprogram_appid = lambda project, explicit_appid="": (
            "wx1234567890ab"
        )

        reply = orchestrator._handle_publish_wechat_miniprogram_command(
            user_id="alice",
            repository_name="hello-mini",
            appid="",
            project_path="miniprogram",
            session_key="alice",
        )

        assert "微信小程序项目路径校验失败" in reply
        assert "未找到小程序项目配置文件：miniprogram/project.config.json" in reply
        assert "请发送：5.2 <仓库名> [AppID] <项目路径>" in reply
        assert "当前通常还是空工作区" in reply
        assert "项目路径请填 ." in reply


def test_sync_wechat_miniprogram_project_appid_updates_touristappid_from_default():
    with TemporaryDirectory() as tmpdir:
        working_dir = Path(tmpdir) / "project"
        working_dir.mkdir()
        config_path = working_dir / "project.config.json"
        config_path.write_text(
            json.dumps(
                {
                    "appid": "touristappid",
                    "projectname": "hello-mini",
                    "miniprogramRoot": "miniprogram/",
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        orchestrator = CodexCliOrchestrator(
            bot_key="cx_bot",
            working_dir=str(working_dir),
        )

        changed = orchestrator._sync_wechat_miniprogram_project_appid(
            str(working_dir),
            ".",
            "wx1234567890ab",
        )

        updated = json.loads(config_path.read_text(encoding="utf-8"))
        assert changed is True
        assert updated["appid"] == "wx1234567890ab"


def test_sync_wechat_miniprogram_project_appid_keeps_existing_real_appid_without_explicit():
    with TemporaryDirectory() as tmpdir:
        working_dir = Path(tmpdir) / "project"
        working_dir.mkdir()
        config_path = working_dir / "project.config.json"
        config_path.write_text(
            json.dumps(
                {
                    "appid": "wxexisting12345",
                    "projectname": "hello-mini",
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        orchestrator = CodexCliOrchestrator(
            bot_key="cx_bot",
            working_dir=str(working_dir),
        )

        changed = orchestrator._sync_wechat_miniprogram_project_appid(
            str(working_dir),
            ".",
            "wx1234567890ab",
        )

        updated = json.loads(config_path.read_text(encoding="utf-8"))
        assert changed is False
        assert updated["appid"] == "wxexisting12345"


def test_sync_wechat_miniprogram_project_appid_explicit_override_wins():
    with TemporaryDirectory() as tmpdir:
        working_dir = Path(tmpdir) / "project"
        working_dir.mkdir()
        config_path = working_dir / "project.config.json"
        config_path.write_text(
            json.dumps(
                {
                    "appid": "wxexisting12345",
                    "projectname": "hello-mini",
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        orchestrator = CodexCliOrchestrator(
            bot_key="cx_bot",
            working_dir=str(working_dir),
        )

        changed = orchestrator._sync_wechat_miniprogram_project_appid(
            str(working_dir),
            ".",
            "wx1234567890ab",
            explicit_appid="wx1234567890ab",
        )

        updated = json.loads(config_path.read_text(encoding="utf-8"))
        assert changed is True
        assert updated["appid"] == "wx1234567890ab"


def test_publish_wechat_miniprogram_success_reply_includes_next_step_hint():
    with TemporaryDirectory() as tmpdir:
        working_dir = Path(tmpdir) / "project"
        working_dir.mkdir()
        orchestrator = CodexCliOrchestrator(
            bot_key="cx_bot",
            working_dir=str(working_dir),
        )
        orchestrator._ensure_runtime_context = lambda user_id, session_key, log_context=None: (
            {
                "working_dir": str(working_dir),
                "project": {"project_id": "proj_1", "name": "hello-mini"},
            },
            None,
        )
        orchestrator.project_deployment_manager.get_git_identity = lambda workspace_path: SimpleNamespace(
            is_configured=True
        )
        orchestrator._resolve_push_repository_name = lambda project, repository_name, workspace_path: (
            repository_name
        )
        orchestrator._resolve_wechat_miniprogram_appid = lambda project, explicit_appid="": (
            "wx1234567890ab"
        )
        orchestrator._resolve_wechat_miniprogram_project_path = (
            lambda project, workspace_path, explicit_project_path="": "."
        )
        orchestrator._resolve_wechat_miniprogram_robot = lambda project: 1
        orchestrator._handle_push_to_github_command = (
            lambda **kwargs: "已提交并推送当前项目到 GitHub\n项目：hello-mini"
        )
        orchestrator.project_deployment_manager.get_git_origin = (
            lambda workspace_path: "git@github.com:kangaroo117/hello-mini.git"
        )
        orchestrator._parse_github_remote = lambda remote_url: ("kangaroo117", "hello-mini")
        orchestrator._read_runtime_secret = lambda key: "private-key"
        orchestrator.github_actions_secret_manager.seed_wechat_miniprogram_repository_secrets = (
            lambda owner, repo, private_key: ["WECHAT_MINIPROGRAM_PRIVATE_KEY"]
        )
        orchestrator.project_deployment_manager.scaffold_wechat_miniprogram_upload = lambda **kwargs: SimpleNamespace(
            deployment_type="wechat_miniprogram",
            workflow_path=".github/workflows/upload-wechat-miniprogram.yml",
            script_path=".github/scripts/upload-wechat-miniprogram.js",
            files=[
                SimpleNamespace(
                    relative_path=".github/workflows/upload-wechat-miniprogram.yml",
                    action="created",
                )
            ],
            appid="wx1234567890ab",
            project_path=".",
            robot=1,
        )
        orchestrator.project_deployment_manager.commit_and_push_current_branch = (
            lambda **kwargs: SimpleNamespace(branch_name="main")
        )
        orchestrator.project_registry.update_project = lambda *args, **kwargs: None

        reply = orchestrator._handle_publish_wechat_miniprogram_command(
            user_id="alice",
            repository_name="hello-mini",
            appid="",
            project_path=".",
            session_key="alice",
        )

        assert "已完成一键上传微信小程序的准备" in reply
        assert "下一步：先查看 GitHub Actions 上传结果" in reply
        assert "5.3 [配置文件]" in reply


def test_query_wechat_miniprogram_status_reply_includes_next_step_hint():
    with TemporaryDirectory() as tmpdir:
        working_dir = Path(tmpdir) / "project"
        working_dir.mkdir()
        orchestrator = CodexCliOrchestrator(
            bot_key="cx_bot",
            working_dir=str(working_dir),
        )
        orchestrator._ensure_runtime_context = lambda user_id, session_key, log_context=None: (
            {
                "working_dir": str(working_dir),
                "project": {"project_id": "proj_1", "name": "hello-mini"},
            },
            None,
        )
        orchestrator._resolve_wechat_miniprogram_appid = lambda project, explicit_appid="": (
            "wx1234567890ab"
        )
        orchestrator._resolve_wechat_miniprogram_app_secret = lambda: "app-secret"
        orchestrator._resolve_wechat_miniprogram_audit_id = (
            lambda project, explicit_audit_id="": 123456
        )
        orchestrator.wechat_miniprogram_manager.get_audit_status = lambda **kwargs: {
            "status": "approved",
            "errmsg": "ok",
        }
        orchestrator.project_registry.update_project = lambda *args, **kwargs: None

        reply = orchestrator._handle_query_wechat_miniprogram_audit_status_command(
            user_id="alice",
            audit_id="",
            session_key="alice",
        )

        assert "微信小程序审核状态" in reply
        assert "下一步：如已审核通过，可发送 5.6 正式发布" in reply


def test_release_wechat_miniprogram_reply_includes_next_step_hint():
    with TemporaryDirectory() as tmpdir:
        working_dir = Path(tmpdir) / "project"
        working_dir.mkdir()
        orchestrator = CodexCliOrchestrator(
            bot_key="cx_bot",
            working_dir=str(working_dir),
        )
        orchestrator._ensure_runtime_context = lambda user_id, session_key, log_context=None: (
            {
                "working_dir": str(working_dir),
                "project": {"project_id": "proj_1", "name": "hello-mini"},
            },
            None,
        )
        orchestrator._resolve_wechat_miniprogram_appid = lambda project, explicit_appid="": (
            "wx1234567890ab"
        )
        orchestrator._resolve_wechat_miniprogram_app_secret = lambda: "app-secret"
        orchestrator.wechat_miniprogram_manager.release = lambda **kwargs: {
            "errcode": 0,
            "errmsg": "ok",
        }
        orchestrator.project_registry.update_project = lambda *args, **kwargs: None

        reply = orchestrator._handle_release_wechat_miniprogram_command(
            user_id="alice",
            session_key="alice",
        )

        assert "已触发微信小程序正式发布" in reply
        assert "下一步：可在微信公众平台或客户端确认正式版是否已生效" in reply


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
                    "2 5 hello-world",
                    session_key="alice",
                )
                runtime_context, _ = orchestrator._ensure_single_runtime_context("alice", "alice")
                workspace = Path(runtime_context["working_dir"])
                (workspace / "README.md").write_text("hello\n", encoding="utf-8")
                await orchestrator.handle_control_command(
                    "alice",
                    "3 2 kangaroo117 kangaroo117@users.noreply.github.com",
                    session_key="alice",
                )
                await orchestrator.handle_control_command(
                    "alice",
                    "3 12 git@github.com:other/hello-world.git",
                    session_key="alice",
                )
                return await orchestrator.handle_control_command(
                    "alice",
                    "3 10",
                    session_key="alice",
                )

            push_reply = asyncio.run(run_flow())
        finally:
            orchestrator._ssh_host_alias_configured = original_alias_checker

        assert "已提交并推送当前项目到 GitHub" in push_reply
        assert "自动创建仓库：kangaroo117/hello-world" in push_reply
        assert "检测到当前远程账号为 other，已切换为统一 GitHub 账号 kangaroo117" in push_reply


def test_commit_and_push_treats_matching_remote_head_as_success():
    with TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir) / "workspace"
        workspace.mkdir()

        manager = ProjectDeploymentManager(repo_root=str(workspace))

        def fake_run_git_process(cwd: Path, *args: str):
            command = tuple(args)
            if command == ("remote", "get-url", "origin"):
                return subprocess.CompletedProcess(args, 0, stdout="git@github.com:kangaroo117/xxcb.git\n", stderr="")
            if command == ("symbolic-ref", "--short", "HEAD"):
                return subprocess.CompletedProcess(args, 0, stdout="main\n", stderr="")
            if command == ("status", "--porcelain"):
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            if command == ("rev-parse", "--verify", "HEAD"):
                return subprocess.CompletedProcess(args, 0, stdout="abcdef1234567890\n", stderr="")
            if command == ("rev-parse", "HEAD"):
                return subprocess.CompletedProcess(args, 0, stdout="abcdef1234567890\n", stderr="")
            if command == ("push", "-u", "origin", "main"):
                return subprocess.CompletedProcess(
                    args,
                    1,
                    stdout="",
                    stderr="branch 'main' set up to track 'origin/main'.\n",
                )
            if command == ("ls-remote", "origin", "refs/heads/main"):
                return subprocess.CompletedProcess(
                    args,
                    0,
                    stdout="abcdef1234567890\trefs/heads/main\n",
                    stderr="",
                )
            raise AssertionError(f"unexpected git command: {command}")

        manager._run_git_process = fake_run_git_process
        manager._ensure_git_repository = lambda root: False
        manager._is_git_repository = lambda root: True

        result = manager.commit_and_push_current_branch(
            workspace_path=str(workspace),
            commit_message="ci: enable Cloudflare Pages deploy for xxcb",
            remote_name="origin",
        )

        assert result.branch_name == "main"
        assert result.head_sha == "abcdef1234567890"
        assert result.remote_url == "git@github.com:kangaroo117/xxcb.git"


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
        assert result.app_dir == "."
        assert result.build_dir == "build-output"
        assert workflow_path.exists()
        assert "Detect Node.js project" in content
        assert 'if [ -f "package.json" ]; then' in content
        assert "Install dependencies with lockfile" in content
        assert "Install dependencies without lockfile" in content
        assert "working-directory: ." in content
        assert "npm run build --if-present" in content
        assert "Cloudflare Pages build directory not found: build-output" in content
        assert "Cloudflare Pages build directory is empty: build-output" in content
        assert "pages deploy build-output --project-name=hello-pages" in content


def test_scaffold_cloudflare_pages_infers_single_subdirectory_node_project():
    with TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir) / "workspace"
        app_dir = workspace / "xiaodaka"
        app_dir.mkdir(parents=True)
        (app_dir / "package.json").write_text('{"name":"xiaodaka"}', encoding="utf-8")

        manager = ProjectDeploymentManager()
        result = manager.scaffold_cloudflare_pages(
            str(workspace),
            pages_project_name="Hello Pages",
            build_dir="dist",
        )

        workflow_path = workspace / ".github" / "workflows" / "deploy-cloudflare-pages.yml"
        content = workflow_path.read_text(encoding="utf-8")

        assert result.deployment_type == "cloudflare_pages"
        assert result.pages_project_name == "hello-pages"
        assert result.app_dir == "xiaodaka"
        assert result.build_dir == "xiaodaka/dist"
        assert "检测到前端项目位于子目录 xiaodaka" in "\n".join(result.warnings)
        assert 'if [ -f "xiaodaka/package.json" ]; then' in content
        assert "hashFiles('xiaodaka/package-lock.json', 'xiaodaka/npm-shrinkwrap.json')" in content
        assert "working-directory: xiaodaka" in content
        assert "Cloudflare Pages build directory not found: xiaodaka/dist" in content
        assert "Cloudflare Pages build directory is empty: xiaodaka/dist" in content
        assert "pages deploy xiaodaka/dist --project-name=hello-pages" in content


def test_scaffold_cloudflare_pages_uses_root_for_static_site():
    with TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir) / "workspace"
        workspace.mkdir()
        (workspace / "index.html").write_text("<!doctype html><title>Hello</title>", encoding="utf-8")

        manager = ProjectDeploymentManager()
        result = manager.scaffold_cloudflare_pages(
            str(workspace),
            pages_project_name="Hello Pages",
            build_dir="dist",
        )

        workflow_path = workspace / ".github" / "workflows" / "deploy-cloudflare-pages.yml"
        content = workflow_path.read_text(encoding="utf-8")

        assert result.app_dir == "."
        assert result.build_dir == "."
        assert "检测到根目录存在 index.html" in "\n".join(result.warnings)
        assert "Cloudflare Pages build directory not found: ." in content
        assert "Cloudflare Pages build directory is empty: ." in content
        assert "pages deploy . --project-name=hello-pages" in content


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


def test_scaffold_wechat_miniprogram_writes_workflow_and_script():
    with TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir) / "workspace"
        workspace.mkdir()
        (workspace / "miniprogram").mkdir()

        manager = ProjectDeploymentManager()
        result = manager.scaffold_wechat_miniprogram_upload(
            str(workspace),
            appid="wx1234567890ab",
            project_path="./miniprogram",
            robot=2,
        )

        workflow_path = workspace / ".github" / "workflows" / "upload-wechat-miniprogram.yml"
        script_path = workspace / ".github" / "scripts" / "upload-wechat-miniprogram.js"
        workflow_content = workflow_path.read_text(encoding="utf-8")
        script_content = script_path.read_text(encoding="utf-8")

        assert result.deployment_type == "wechat_miniprogram"
        assert result.appid == "wx1234567890ab"
        assert result.project_path == "miniprogram"
        assert result.robot == 2
        assert workflow_path.exists()
        assert script_path.exists()
        assert 'WECHAT_MINIPROGRAM_APPID: "wx1234567890ab"' in workflow_content
        assert 'WECHAT_MINIPROGRAM_PROJECT_PATH: "miniprogram"' in workflow_content
        assert 'WECHAT_MINIPROGRAM_ROBOT: "2"' in workflow_content
        assert "project.config.json not found: miniprogram/project.config.json" in workflow_content
        assert 'const ci = require("miniprogram-ci");' in script_content
        assert 'type: "miniProgram"' in script_content


def test_project_deployment_summary_supports_wechat_miniprogram():
    summary = ProjectDeploymentManager.deployment_summary(
        {
            "deployment_type": "wechat_miniprogram",
            "github_remote_url": "git@github.com:kangaroo117/hello-mini.git",
            "deployment_config": {
                "appid": "wx1234567890ab",
                "project_path": "miniprogram",
                "robot": 3,
            },
        }
    )

    assert "微信小程序" in summary
    assert "appid=wx1234567890ab" in summary
    assert "path=miniprogram" in summary
    assert "robot=3" in summary
    assert "GitHub=git@github.com:kangaroo117/hello-mini.git" in summary


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
