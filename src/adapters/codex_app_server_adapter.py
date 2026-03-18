"""
Codex App Server 适配器

通过 `codex app-server --listen stdio://` 使用原生 JSON-RPC 协议，
支持 thread/turn 生命周期、审批请求、文件变更审查与用户补充输入。
"""

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

logger = logging.getLogger(__name__)


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
    ):
        self.model = model or ""
        self.working_dir = str(Path(working_dir).expanduser().resolve())
        self.env_vars = {str(k): str(v) for k, v in (env_vars or {}).items()}
        self.sandbox_mode = sandbox_mode or "workspace-write"
        self.skip_git_repo_check = bool(skip_git_repo_check)
        self.dangerously_bypass_approvals_and_sandbox = bool(
            dangerously_bypass_approvals_and_sandbox
        )
        self.add_dirs = [str(Path(p).expanduser().resolve()) for p in (add_dirs or []) if p]
        self.profile = profile or ""
        self.executable = executable or "codex"
        self.approval_policy = approval_policy or "on-request"

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

            if method == "item/started":
                item = params.get("item") or {}
                item_id = item.get("id", "")
                item_type = item.get("type", "")
                if item_id:
                    self._items[item_id] = item

                if item_type == "commandExecution":
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

                if item_type == "agentMessage":
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
                error = (params.get("error") or {}).get("message", "Codex app-server 错误")
                raise CodexAppServerError(error)

            if method == "turn/completed":
                turn = params.get("turn") or {}
                self.turn_id = turn.get("id", "") or self.turn_id
                turn_error = turn.get("error")
                if turn_error and turn_error.get("message"):
                    raise CodexAppServerError(turn_error["message"])
                break

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

    @property
    def _process_return_code(self) -> Optional[int]:
        return None if not self.process else self.process.returncode

    async def _spawn(self) -> None:
        cmd = self._build_command()
        env = os.environ.copy()
        env.update(self.env_vars)

        logger.info("[CodexApp] 启动 app-server: cwd=%s, cmd=%s", self.working_dir, " ".join(cmd))

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
            "approvalPolicy": self._approval_policy_value(),
            "approvalsReviewer": "user",
            "sandbox": self._thread_sandbox_mode(),
            "model": self.model,
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
            "approvalPolicy": self._approval_policy_value(),
            "approvalsReviewer": "user",
        }
        sandbox_policy = self._turn_sandbox_policy()
        if sandbox_policy:
            params["sandboxPolicy"] = sandbox_policy
        return params

    def _approval_policy_value(self) -> str:
        if self.dangerously_bypass_approvals_and_sandbox:
            return "never"
        return self.approval_policy

    def _thread_sandbox_mode(self) -> str:
        if self.dangerously_bypass_approvals_and_sandbox:
            return "danger-full-access"
        if self.sandbox_mode == "read-only":
            return "read-only"
        return "workspace-write"

    def _turn_sandbox_policy(self) -> Optional[dict]:
        if self.dangerously_bypass_approvals_and_sandbox:
            return {"type": "dangerFullAccess"}
        if self.sandbox_mode == "read-only":
            return {
                "type": "readOnly",
                "access": {"type": "fullAccess"},
            }
        return {
            "type": "workspaceWrite",
            "writableRoots": [path for path in self.add_dirs if path],
            "readOnlyAccess": {"type": "fullAccess"},
            "networkAccess": False,
            "excludeTmpdirEnvVar": False,
            "excludeSlashTmp": False,
        }

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

    def _build_process_error(self) -> str:
        stderr_text = "\n".join(self._stderr_lines[-20:]).strip()
        detail = stderr_text or f"Codex app-server 进程异常退出（code={self._process_return_code}）"
        return f"[CodexCLI] Process exited: {detail}"


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
    ):
        if not working_dir:
            raise ValueError("Codex CLI 机器人必须配置 working_dir")

        working_path = Path(working_dir).expanduser().resolve()
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

        logger.info(
            "[CodexApp] 初始化适配器: working_dir=%s, model=%s, sandbox_mode=%s, approval_policy=%s, bypass=%s",
            self.working_dir,
            self.model or "default",
            self.sandbox_mode,
            self.approval_policy,
            self.dangerously_bypass_approvals_and_sandbox,
        )

    def create_session(self) -> CodexAppServerSession:
        return CodexAppServerSession(
            model=self.model,
            working_dir=self.working_dir,
            env_vars=self.env_vars,
            sandbox_mode=self.sandbox_mode,
            skip_git_repo_check=self.skip_git_repo_check,
            dangerously_bypass_approvals_and_sandbox=self.dangerously_bypass_approvals_and_sandbox,
            add_dirs=self.add_dirs,
            profile=self.profile,
            executable=self.executable,
            approval_policy=self.approval_policy,
        )
