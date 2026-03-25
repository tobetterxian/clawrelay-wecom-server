"""
Codex App Server 适配器

通过 `codex app-server --listen stdio://` 使用原生 JSON-RPC 协议，
支持 thread/turn 生命周期、审批请求、文件变更审查与用户补充输入。
"""

import atexit
import asyncio
import json
import logging
import os
import re
import signal
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from src.utils.path_utils import resolve_local_path

logger = logging.getLogger(__name__)
CODEX_STREAM_RETRY_RE = re.compile(r"^Reconnecting\.\.\.\s+\d+/\d+$", re.IGNORECASE)
_SPAWNED_CODEX_PIDS: set[int] = set()
_SPAWNED_CODEX_PIDS_LOCK = threading.Lock()


def register_spawned_codex_pid(pid: int) -> None:
    try:
        normalized_pid = int(pid)
    except (TypeError, ValueError):
        return
    if normalized_pid <= 0:
        return
    with _SPAWNED_CODEX_PIDS_LOCK:
        _SPAWNED_CODEX_PIDS.add(normalized_pid)
    logger.info("[CodexApp] 注册子进程: pid=%s", normalized_pid)


def unregister_spawned_codex_pid(pid: int) -> None:
    try:
        normalized_pid = int(pid)
    except (TypeError, ValueError):
        return
    if normalized_pid <= 0:
        return
    with _SPAWNED_CODEX_PIDS_LOCK:
        _SPAWNED_CODEX_PIDS.discard(normalized_pid)


def list_spawned_codex_pids() -> list[int]:
    with _SPAWNED_CODEX_PIDS_LOCK:
        return sorted(_SPAWNED_CODEX_PIDS)


def cleanup_spawned_codex_processes(reason: str = "process_exit") -> list[int]:
    pids = list_spawned_codex_pids()
    if not pids:
        return []

    cleaned: list[int] = []
    for pid in pids:
        try:
            if os.name == "nt":
                result = subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
                if result.returncode not in (0, 128, 255):
                    logger.warning(
                        "[CodexApp] taskkill 返回非预期状态: pid=%s, code=%s, reason=%s",
                        pid,
                        result.returncode,
                        reason,
                    )
            else:
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            cleaned.append(pid)
        except Exception:
            logger.warning(
                "[CodexApp] 清理子进程失败: pid=%s, reason=%s",
                pid,
                reason,
                exc_info=True,
            )
        finally:
            unregister_spawned_codex_pid(pid)

    if cleaned:
        logger.warning(
            "[CodexApp] 已清理残留 codex 子进程: count=%s, pids=%s, reason=%s",
            len(cleaned),
            cleaned,
            reason,
        )
    return cleaned


atexit.register(cleanup_spawned_codex_processes, "atexit")


@dataclass
class CodexThreadStarted:
    thread_id: str


@dataclass
class CodexAgentMessage:
    text: str
    item_id: str = ""
    phase: str = ""
    is_new_message: bool = False


@dataclass
class CodexCommandExecutionStart:
    command: str
    item_id: str = ""


@dataclass
class CodexCommandExecutionComplete:
    command: str
    output: str
    exit_code: Optional[int]
    status: str
    item_id: str = ""


@dataclass
class CodexFileChangeStart:
    item_id: str
    changes: List[dict]
    status: str


@dataclass
class CodexFileChangeComplete:
    item_id: str
    changes: List[dict]
    status: str


@dataclass
class CodexTokenUsageBreakdown:
    total_tokens: int
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    reasoning_output_tokens: int


@dataclass
class CodexTokenUsageUpdate:
    thread_id: str
    turn_id: str
    last: CodexTokenUsageBreakdown
    total: CodexTokenUsageBreakdown
    model_context_window: Optional[int] = None


@dataclass
class CodexContextCompaction:
    item_id: str = ""
    thread_id: str = ""
    turn_id: str = ""
    source: str = "item"


@dataclass
class CodexStreamError:
    message: str
    additional_details: str = ""
    codex_error_info: Optional[Any] = None
    will_retry: bool = True


@dataclass
class CodexInteractionRequest:
    interaction_type: str
    request_id: Union[int, str]
    thread_id: str
    turn_id: str
    item_id: str
    raw_params: Dict[str, Any]
    item: Optional[dict] = None


StreamEvent = Union[
    CodexThreadStarted,
    CodexAgentMessage,
    CodexCommandExecutionStart,
    CodexCommandExecutionComplete,
    CodexFileChangeStart,
    CodexFileChangeComplete,
    CodexTokenUsageUpdate,
    CodexContextCompaction,
    CodexStreamError,
    CodexInteractionRequest,
]


class CodexAppServerError(Exception):
    pass


class CodexAppServerSession:
    """单个 Codex thread/turn 运行时会话"""

    def __init__(
        self,
        *,
        model: str,
        working_dir: str,
        env_vars: Optional[Dict[str, str]] = None,
        sandbox_mode: str = "workspace-write",
        skip_git_repo_check: bool = False,
        dangerously_bypass_approvals_and_sandbox: bool = False,
        add_dirs: Optional[List[str]] = None,
        profile: str = "",
        executable: str = "codex",
        approval_policy: str = "on-request",
        reasoning_effort: str = "",
    ):
        self.model = model or ""
        self.working_dir = str(resolve_local_path(working_dir))
        self.env_vars = {str(k): str(v) for k, v in (env_vars or {}).items()}
        self.sandbox_mode = sandbox_mode or "workspace-write"
        self.skip_git_repo_check = bool(skip_git_repo_check)
        self.dangerously_bypass_approvals_and_sandbox = bool(
            dangerously_bypass_approvals_and_sandbox
        )
        self.add_dirs = [str(resolve_local_path(p)) for p in (add_dirs or []) if p]
        self.profile = profile or ""
        self.executable = executable or "codex"
        self.approval_policy = approval_policy or "on-request"
        self.reasoning_effort = str(reasoning_effort or "").strip()

        self.process: Optional[asyncio.subprocess.Process] = None
        self.thread_id: str = ""
        self.turn_id: str = ""
        self._request_seq = 1
        self._rpc_futures: Dict[Union[int, str], asyncio.Future] = {}
        self._events: asyncio.Queue = asyncio.Queue()
        self._stdout_task: Optional[asyncio.Task] = None
        self._stderr_task: Optional[asyncio.Task] = None
        self._wait_task: Optional[asyncio.Task] = None
        self._closed = False
        self._stderr_lines: List[str] = []
        self._items: Dict[str, dict] = {}
        self._agent_message_lengths: Dict[str, int] = {}
        self._context_compaction_item_ids: set[str] = set()
        self._context_compaction_thread_turn_pairs: set[tuple[str, str]] = set()
        self._last_retryable_stream_error_message: str = ""
        self._last_retryable_stream_error_details: str = ""
        self.active_model: str = ""
        self.active_model_provider: str = ""
        self.active_reasoning_effort: str = ""
        self.active_cwd: str = ""
        self.config_model_context_window: Optional[int] = None
        self.config_auto_compact_token_limit: Optional[int] = None
        self.pending_interaction: Optional[CodexInteractionRequest] = None
        self._pending_interaction_future: Optional[asyncio.Future] = None

    async def start(self, thread_id: str, developer_instructions: str) -> str:
        await self._spawn()
        await self._rpc_request(
            "initialize",
            {
                "clientInfo": {
                    "name": "clawrelay-wecom-server",
                    "version": "1.0.0",
                },
                "capabilities": {"experimentalApi": True},
            },
        )
        await self._send_notification("initialized")
        await self._load_effective_config()

        if thread_id:
            response = await self._rpc_request(
                "thread/resume",
                self._build_thread_params(
                    developer_instructions=developer_instructions,
                    thread_id=thread_id,
                ),
            )
        else:
            response = await self._rpc_request(
                "thread/start",
                self._build_thread_params(
                    developer_instructions=developer_instructions,
                ),
            )

        self._apply_thread_configuration(response)
        thread = (response or {}).get("thread") or {}
        self.thread_id = thread.get("id", "") or thread_id
        if not self.thread_id:
            raise CodexAppServerError("Codex app-server 未返回有效 thread_id")
        return self.thread_id

    async def stream_turn(self, inputs: List[dict]):
        if not self.thread_id:
            raise CodexAppServerError("thread 未初始化")

        response = await self._rpc_request(
            "turn/start",
            self._build_turn_params(inputs),
        )
        turn = (response or {}).get("turn") or {}
        self.turn_id = turn.get("id", "") or self.turn_id
        turn_completed = False
        self._last_retryable_stream_error_message = ""
        self._last_retryable_stream_error_details = ""

        while True:
            message = await self._events.get()
            if message is None:
                break

            method = message.get("method")
            if not method:
                continue

            params = message.get("params") or {}

            if method == "thread/started":
                thread = params.get("thread") or {}
                thread_id = thread.get("id", "")
                if thread_id:
                    self.thread_id = thread_id
                    yield CodexThreadStarted(thread_id=thread_id)
                continue

            if method == "turn/started":
                turn = params.get("turn") or {}
                self.turn_id = turn.get("id", "") or self.turn_id
                continue

            if method == "thread/tokenUsage/updated":
                token_usage = params.get("tokenUsage") or {}
                yield CodexTokenUsageUpdate(
                    thread_id=params.get("threadId", self.thread_id),
                    turn_id=params.get("turnId", self.turn_id),
                    last=self._build_token_usage_breakdown(token_usage.get("last")),
                    total=self._build_token_usage_breakdown(token_usage.get("total")),
                    model_context_window=self._coerce_optional_int(
                        token_usage.get("modelContextWindow")
                    ),
                )
                continue

            if method == "thread/compacted":
                event = self._build_context_compaction_event(
                    thread_id=params.get("threadId", self.thread_id),
                    turn_id=params.get("turnId", self.turn_id),
                    source="thread",
                )
                if event is not None:
                    yield event
                continue

            if method == "item/started":
                item = params.get("item") or {}
                item_id = item.get("id", "")
                item_type = item.get("type", "")
                if item_id:
                    self._items[item_id] = item

                if item_type == "contextCompaction":
                    event = self._build_context_compaction_event(
                        item_id=item_id,
                        thread_id=self.thread_id,
                        turn_id=self.turn_id,
                        source="item",
                    )
                    if event is not None:
                        yield event
                elif item_type == "commandExecution":
                    yield CodexCommandExecutionStart(
                        command=item.get("command", ""),
                        item_id=item_id,
                    )
                elif item_type == "fileChange":
                    yield CodexFileChangeStart(
                        item_id=item_id,
                        changes=item.get("changes") or [],
                        status=item.get("status", "inProgress"),
                    )
                continue

            if method == "item/completed":
                item = params.get("item") or {}
                item_id = item.get("id", "")
                item_type = item.get("type", "")
                if item_id:
                    self._items[item_id] = item

                if item_type == "contextCompaction":
                    event = self._build_context_compaction_event(
                        item_id=item_id,
                        thread_id=self.thread_id,
                        turn_id=self.turn_id,
                        source="item",
                    )
                    if event is not None:
                        yield event
                elif item_type == "agentMessage":
                    full_text = item.get("text", "") or ""
                    sent_len = self._agent_message_lengths.get(item_id, 0)
                    if full_text and len(full_text) > sent_len:
                        is_new = sent_len == 0
                        self._agent_message_lengths[item_id] = len(full_text)
                        yield CodexAgentMessage(
                            text=full_text[sent_len:],
                            item_id=item_id,
                            phase=item.get("phase", ""),
                            is_new_message=is_new,
                        )
                elif item_type == "commandExecution":
                    yield CodexCommandExecutionComplete(
                        command=item.get("command", ""),
                        output=item.get("aggregatedOutput", ""),
                        exit_code=item.get("exitCode"),
                        status=item.get("status", "unknown"),
                        item_id=item_id,
                    )
                elif item_type == "fileChange":
                    yield CodexFileChangeComplete(
                        item_id=item_id,
                        changes=item.get("changes") or [],
                        status=item.get("status", "unknown"),
                    )
                continue

            if method == "item/agentMessage/delta":
                item_id = params.get("itemId", "")
                delta = params.get("delta", "") or ""
                if delta:
                    is_new = self._agent_message_lengths.get(item_id, 0) == 0
                    self._agent_message_lengths[item_id] = (
                        self._agent_message_lengths.get(item_id, 0) + len(delta)
                    )
                    item = self._items.get(item_id) or {}
                    yield CodexAgentMessage(
                        text=delta,
                        item_id=item_id,
                        phase=item.get("phase", ""),
                        is_new_message=is_new,
                    )
                continue

            if method in (
                "item/commandExecution/requestApproval",
                "item/fileChange/requestApproval",
                "item/permissions/requestApproval",
                "item/tool/requestUserInput",
                "mcpServer/elicitation/request",
            ):
                interaction = self._build_interaction_request(message)
                self.pending_interaction = interaction
                self._pending_interaction_future = asyncio.get_running_loop().create_future()
                yield interaction
                response_payload = await self._pending_interaction_future
                await self._send_response(message.get("id"), response_payload)
                self.pending_interaction = None
                self._pending_interaction_future = None
                continue

            if method == "error":
                error = params.get("error") or {}
                will_retry = bool(params.get("willRetry"))
                error_message = self._extract_turn_error_primary_message(error)
                additional_details = self._extract_turn_error_additional_details(error)
                codex_error_info = error.get("codexErrorInfo") or error.get("codex_error_info")
                if will_retry:
                    self._remember_retryable_stream_error(error_message, additional_details)
                    yield CodexStreamError(
                        message=error_message,
                        additional_details=additional_details,
                        codex_error_info=codex_error_info,
                        will_retry=True,
                    )
                    continue
                raise CodexAppServerError(self._build_turn_error_exception_message(error))

            if method == "turn/completed":
                turn = params.get("turn") or {}
                self.turn_id = turn.get("id", "") or self.turn_id
                turn_error = turn.get("error")
                if turn_error and (
                    turn_error.get("message")
                    or turn_error.get("additionalDetails")
                    or turn_error.get("additional_details")
                ):
                    raise CodexAppServerError(
                        self._build_turn_error_exception_message(turn_error)
                    )
                turn_completed = True
                break

        if not turn_completed and not self._closed:
            if self._process_return_code not in (None, 0):
                raise CodexAppServerError(self._build_process_error())
            raise CodexAppServerError(self._build_unexpected_stream_end_error())

        if self._process_return_code not in (None, 0) and not self._closed:
            raise CodexAppServerError(self._build_process_error())

    def has_pending_interaction(self) -> bool:
        return self.pending_interaction is not None and self._pending_interaction_future is not None

    def submit_pending_interaction(self, response_payload: dict) -> bool:
        if not self._pending_interaction_future or self._pending_interaction_future.done():
            return False
        self._pending_interaction_future.set_result(response_payload)
        return True

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True

        if self._pending_interaction_future and not self._pending_interaction_future.done():
            self._pending_interaction_future.cancel()

        if self.process and self.process.returncode is None:
            self.process.kill()

        for task in (self._stdout_task, self._stderr_task, self._wait_task):
            if task:
                task.cancel()

        if self.process:
            try:
                await asyncio.wait_for(self.process.wait(), timeout=5)
            except Exception:
                pass
            finally:
                unregister_spawned_codex_pid(self.process.pid or 0)

    @property
    def _process_return_code(self) -> Optional[int]:
        return None if not self.process else self.process.returncode

    async def _spawn(self) -> None:
        cmd = self._build_command()
        env = os.environ.copy()
        env.update(self.env_vars)

        logger.info("[CodexApp] 启动 app-server: cwd=%s, cmd=%s", self.working_dir, " ".join(cmd))
        self._log_spawn_environment_diagnostics(env)

        try:
            self.process = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.working_dir,
                env=env,
            )
        except FileNotFoundError as e:
            raise CodexAppServerError(
                f"[CodexCLI] 未找到 codex 命令: {self.executable}。请先安装 Codex CLI，或在 bots.yaml 中设置 provider_config.codex_path。"
            ) from e

        register_spawned_codex_pid(getattr(self.process, "pid", 0))

        self._stdout_task = asyncio.create_task(self._stdout_loop())
        self._stderr_task = asyncio.create_task(self._stderr_loop())
        self._wait_task = asyncio.create_task(self._wait_loop())

    async def _stdout_loop(self) -> None:
        assert self.process and self.process.stdout
        try:
            async for raw_line in self.process.stdout:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning("[CodexApp] 跳过非 JSON 输出: %s", line[:300])
                    continue

                if "id" in message and "method" not in message and (
                    "result" in message or "error" in message
                ):
                    future = self._rpc_futures.pop(message.get("id"), None)
                    if future and not future.done():
                        if "error" in message:
                            future.set_exception(CodexAppServerError(str(message["error"])))
                        else:
                            future.set_result(message.get("result"))
                else:
                    await self._events.put(message)
        finally:
            await self._events.put(None)

    async def _stderr_loop(self) -> None:
        assert self.process and self.process.stderr
        try:
            async for raw_line in self.process.stderr:
                line = raw_line.decode("utf-8", errors="replace").rstrip()
                if line:
                    self._stderr_lines.append(line)
                    logger.warning("[CodexApp stderr] %s", line)
        except asyncio.CancelledError:
            return

    async def _wait_loop(self) -> None:
        assert self.process
        try:
            return_code = await self.process.wait()
            logger.info("[CodexApp] app-server exited: code=%s", return_code)
            unregister_spawned_codex_pid(self.process.pid or 0)
        except asyncio.CancelledError:
            return

    def _build_command(self) -> List[str]:
        cmd = [self.executable, "app-server", "--listen", "stdio://"]
        if self.profile:
            cmd.extend(["-p", self.profile])
        if self.skip_git_repo_check:
            cmd.extend(["-c", "skip_git_repo_check=true"])
        return cmd

    def _build_thread_params(self, developer_instructions: str, thread_id: str = "") -> dict:
        params = {
            "cwd": self.working_dir,
            "developerInstructions": developer_instructions,
        }
        if self.skip_git_repo_check:
            params["config"] = {"skip_git_repo_check": True}
        if thread_id:
            params["threadId"] = thread_id
        return params

    def _build_turn_params(self, inputs: List[dict]) -> dict:
        params = {
            "threadId": self.thread_id,
            "input": inputs,
        }
        return params

    def _apply_thread_configuration(self, response: Any) -> None:
        payload = dict(response or {})
        self.active_model = str(payload.get("model") or self.active_model or "").strip()
        self.active_model_provider = str(
            payload.get("modelProvider") or self.active_model_provider or ""
        ).strip()
        self.active_reasoning_effort = str(
            payload.get("reasoningEffort") or self.active_reasoning_effort or ""
        ).strip()
        self.active_cwd = str(payload.get("cwd") or self.active_cwd or self.working_dir).strip()

    async def _load_effective_config(self) -> None:
        try:
            response = await self._rpc_request(
                "config/read",
                {
                    "cwd": self.working_dir,
                    "includeLayers": False,
                },
            )
        except Exception:
            logger.debug("[CodexApp] 读取有效配置失败", exc_info=True)
            return

        config = (response or {}).get("config") or {}
        self.config_model_context_window = self._coerce_optional_int(
            config.get("model_context_window")
        )
        self.config_auto_compact_token_limit = self._coerce_optional_int(
            config.get("model_auto_compact_token_limit")
        )
        logger.info(
            "[CodexApp] 有效配置诊断: model_context_window=%s, model_auto_compact_token_limit=%s",
            self.config_model_context_window or "-",
            self.config_auto_compact_token_limit or "-",
        )

    def _log_spawn_environment_diagnostics(self, env: Dict[str, str]) -> None:
        home_value = str(env.get("HOME") or "").strip()
        userprofile_value = str(env.get("USERPROFILE") or "").strip()
        appdata_value = str(env.get("APPDATA") or "").strip()
        localappdata_value = str(env.get("LOCALAPPDATA") or "").strip()

        home_codex_dir = self._codex_dir_from_env_value(home_value)
        userprofile_codex_dir = self._codex_dir_from_env_value(userprofile_value)

        logger.info(
            "[CodexApp] 启动环境诊断: HOME=%s, USERPROFILE=%s, APPDATA=%s, LOCALAPPDATA=%s",
            home_value or "-",
            userprofile_value or "-",
            appdata_value or "-",
            localappdata_value or "-",
        )
        logger.info(
            "[CodexApp] 配置文件诊断: HOME/.codex=%s, config=%s(exists=%s), auth=%s(exists=%s), USERPROFILE/.codex=%s, config=%s(exists=%s), auth=%s(exists=%s)",
            home_codex_dir or "-",
            self._join_child_path(home_codex_dir, "config.toml") or "-",
            self._path_exists(home_codex_dir, "config.toml"),
            self._join_child_path(home_codex_dir, "auth.json") or "-",
            self._path_exists(home_codex_dir, "auth.json"),
            userprofile_codex_dir or "-",
            self._join_child_path(userprofile_codex_dir, "config.toml") or "-",
            self._path_exists(userprofile_codex_dir, "config.toml"),
            self._join_child_path(userprofile_codex_dir, "auth.json") or "-",
            self._path_exists(userprofile_codex_dir, "auth.json"),
        )

    @staticmethod
    def _join_child_path(base_dir: str, child_name: str) -> str:
        base_value = str(base_dir or "").strip()
        if not base_value:
            return ""
        return str(Path(base_value) / child_name)

    @staticmethod
    def _path_exists(base_dir: str, child_name: str) -> bool:
        base_value = str(base_dir or "").strip()
        if not base_value:
            return False
        return (Path(base_value) / child_name).exists()

    @staticmethod
    def _codex_dir_from_env_value(path_value: str) -> str:
        normalized = str(path_value or "").strip()
        if not normalized:
            return ""
        try:
            return str((resolve_local_path(normalized) / ".codex").resolve())
        except Exception:
            return str(Path(normalized) / ".codex")

    async def _rpc_request(self, method: str, params: dict) -> Any:
        request_id = self._next_request_id()
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._rpc_futures[request_id] = future
        await self._send_message(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params,
            }
        )
        return await future

    async def _send_notification(self, method: str, params: Optional[dict] = None) -> None:
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        await self._send_message(message)

    async def _send_response(self, request_id: Union[int, str], result: dict) -> None:
        await self._send_message(
            {"jsonrpc": "2.0", "id": request_id, "result": result}
        )

    async def _send_message(self, message: dict) -> None:
        if not self.process or not self.process.stdin:
            raise CodexAppServerError("Codex app-server 尚未启动")
        line = json.dumps(message, ensure_ascii=False) + "\n"
        self.process.stdin.write(line.encode("utf-8"))
        await self.process.stdin.drain()

    def _next_request_id(self) -> int:
        request_id = self._request_seq
        self._request_seq += 1
        return request_id

    def _build_interaction_request(self, message: dict) -> CodexInteractionRequest:
        method = message.get("method", "")
        params = message.get("params") or {}
        item_id = params.get("itemId", "")
        item = self._items.get(item_id)
        interaction_type = {
            "item/commandExecution/requestApproval": "command_approval",
            "item/fileChange/requestApproval": "file_change_approval",
            "item/permissions/requestApproval": "permissions_approval",
            "item/tool/requestUserInput": "tool_user_input",
            "mcpServer/elicitation/request": "mcp_elicitation",
        }.get(method, "unknown")
        return CodexInteractionRequest(
            interaction_type=interaction_type,
            request_id=message.get("id"),
            thread_id=params.get("threadId", self.thread_id),
            turn_id=params.get("turnId", self.turn_id),
            item_id=item_id,
            raw_params=params,
            item=item,
        )

    @staticmethod
    def _build_token_usage_breakdown(payload: Any) -> CodexTokenUsageBreakdown:
        data = payload or {}
        return CodexTokenUsageBreakdown(
            total_tokens=CodexAppServerSession._coerce_int(data.get("totalTokens")),
            input_tokens=CodexAppServerSession._coerce_int(data.get("inputTokens")),
            cached_input_tokens=CodexAppServerSession._coerce_int(data.get("cachedInputTokens")),
            output_tokens=CodexAppServerSession._coerce_int(data.get("outputTokens")),
            reasoning_output_tokens=CodexAppServerSession._coerce_int(
                data.get("reasoningOutputTokens")
            ),
        )

    @staticmethod
    def _coerce_int(value: Any) -> int:
        try:
            return max(int(value), 0)
        except (TypeError, ValueError):
            return 0

    @classmethod
    def _coerce_optional_int(cls, value: Any) -> Optional[int]:
        try:
            normalized = int(value)
        except (TypeError, ValueError):
            return None
        return normalized if normalized > 0 else None

    def _build_context_compaction_event(
        self,
        item_id: str = "",
        thread_id: str = "",
        turn_id: str = "",
        source: str = "item",
    ) -> Optional[CodexContextCompaction]:
        normalized_item_id = str(item_id or "").strip()
        if normalized_item_id:
            if normalized_item_id in self._context_compaction_item_ids:
                return None
            self._context_compaction_item_ids.add(normalized_item_id)
        normalized_thread_id = str(thread_id or self.thread_id or "").strip()
        normalized_turn_id = str(turn_id or self.turn_id or "").strip()
        if normalized_thread_id and normalized_turn_id and source == "thread":
            key = (normalized_thread_id, normalized_turn_id)
            if key in self._context_compaction_thread_turn_pairs:
                return None
            self._context_compaction_thread_turn_pairs.add(key)
        return CodexContextCompaction(
            item_id=normalized_item_id,
            thread_id=normalized_thread_id,
            turn_id=normalized_turn_id,
            source=str(source or "item").strip() or "item",
        )

    def _build_process_error(self) -> str:
        detail = self._build_terminal_error_detail(
            f"Codex app-server 进程异常退出（code={self._process_return_code}）"
        )
        return f"[CodexCLI] Process exited: {detail}"

    def _build_unexpected_stream_end_error(self) -> str:
        detail = self._build_terminal_error_detail(
            f"stdout ended before turn/completed（code={self._process_return_code}）"
        )
        return f"[CodexCLI] Turn interrupted before completion: {detail}"

    @staticmethod
    def _extract_turn_error_primary_message(
        error_payload: Any,
        fallback: str = "Codex app-server 错误",
    ) -> str:
        value = str((error_payload or {}).get("message") or fallback).strip()
        return value or fallback

    @staticmethod
    def _extract_turn_error_additional_details(error_payload: Any) -> str:
        return str(
            (error_payload or {}).get("additionalDetails")
            or (error_payload or {}).get("additional_details")
            or ""
        ).strip()

    @classmethod
    def _build_turn_error_exception_message(
        cls,
        error_payload: Any,
        fallback: str = "Codex app-server 错误",
    ) -> str:
        message = cls._extract_turn_error_primary_message(error_payload, fallback=fallback)
        additional_details = cls._extract_turn_error_additional_details(error_payload)
        if additional_details and additional_details != message:
            if cls._is_retrying_message(message):
                return additional_details
            return f"{message}\n{additional_details}"
        return message

    @classmethod
    def _is_retrying_message(cls, message: str) -> bool:
        return bool(CODEX_STREAM_RETRY_RE.match(str(message or "").strip()))

    def _remember_retryable_stream_error(self, message: str, additional_details: str) -> None:
        self._last_retryable_stream_error_message = str(message or "").strip()
        self._last_retryable_stream_error_details = str(additional_details or "").strip()

    def _best_retryable_stream_error_detail(self) -> str:
        if self._last_retryable_stream_error_details:
            return self._last_retryable_stream_error_details
        if (
            self._last_retryable_stream_error_message
            and not self._is_retrying_message(self._last_retryable_stream_error_message)
        ):
            return self._last_retryable_stream_error_message
        return ""

    def _build_terminal_error_detail(self, fallback: str) -> str:
        details: List[str] = []
        retryable_detail = self._best_retryable_stream_error_detail()
        stderr_text = "\n".join(self._stderr_lines[-20:]).strip()
        for candidate in (retryable_detail, stderr_text):
            normalized = str(candidate or "").strip()
            if normalized and normalized not in details:
                details.append(normalized)
        return "\n".join(details) if details else fallback


class CodexAppServerAdapter:
    """原生 Codex app-server 工厂"""

    def __init__(
        self,
        model: str,
        working_dir: str,
        env_vars: Optional[Dict[str, str]] = None,
        sandbox_mode: str = "workspace-write",
        skip_git_repo_check: bool = False,
        dangerously_bypass_approvals_and_sandbox: bool = False,
        add_dirs: Optional[List[str]] = None,
        profile: str = "",
        executable: str = "codex",
        approval_policy: str = "on-request",
        reasoning_effort: str = "",
    ):
        if not working_dir:
            raise ValueError("Codex CLI 机器人必须配置 working_dir")

        working_path = resolve_local_path(working_dir)
        if not working_path.exists():
            raise ValueError(f"Codex CLI working_dir 不存在: {working_path}")
        if not working_path.is_dir():
            raise ValueError(f"Codex CLI working_dir 不是目录: {working_path}")

        self.model = model
        self.working_dir = str(working_path)
        self.env_vars = env_vars or {}
        self.sandbox_mode = sandbox_mode
        self.skip_git_repo_check = skip_git_repo_check
        self.dangerously_bypass_approvals_and_sandbox = dangerously_bypass_approvals_and_sandbox
        self.add_dirs = add_dirs or []
        self.profile = profile
        self.executable = executable
        self.approval_policy = approval_policy
        self.reasoning_effort = str(reasoning_effort or "").strip()

        logger.info(
            "[CodexApp] 初始化适配器: working_dir=%s, profile=%s, requested_model=%s, requested_reasoning_effort=%s, requested_sandbox_mode=%s, requested_approval_policy=%s, bypass=%s, codex_config_precedence=%s",
            self.working_dir,
            self.profile or "-",
            self.model or "-",
            self.reasoning_effort or "-",
            self.sandbox_mode,
            self.approval_policy,
            self.dangerously_bypass_approvals_and_sandbox,
            "enabled",
        )

    def create_session(
        self,
        working_dir: str = "",
        add_dirs: Optional[List[str]] = None,
    ) -> CodexAppServerSession:
        effective_working_dir = working_dir or self.working_dir
        effective_add_dirs = self.add_dirs if add_dirs is None else add_dirs
        return CodexAppServerSession(
            model=self.model,
            working_dir=effective_working_dir,
            env_vars=self.env_vars,
            sandbox_mode=self.sandbox_mode,
            skip_git_repo_check=self.skip_git_repo_check,
            dangerously_bypass_approvals_and_sandbox=self.dangerously_bypass_approvals_and_sandbox,
            add_dirs=effective_add_dirs,
            profile=self.profile,
            executable=self.executable,
            approval_policy=self.approval_policy,
            reasoning_effort=self.reasoning_effort,
        )
