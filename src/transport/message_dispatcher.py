"""
WebSocket消息分发器

接收WsClient分发的消息回调，路由到现有handler处理，
通过WebSocket推送流式回复（500ms节流）。
"""

import asyncio
import logging
import re
import time
import uuid
from pathlib import Path
from typing import Optional

from config.bot_config import BotConfig
from src.core.base_orchestrator import BaseOrchestrator
from src.core.bot_delegate_manager import BotDelegateManager
from src.transport.ws_client import WsClient
from src.core.orchestrator_factory import OrchestratorFactory
from src.core.session_manager import SessionManager
from src.core.group_project_context_resolver import (
    GroupProjectContext,
    GroupProjectContextResolver,
)
from src.handlers.command_handlers import CommandRouter
from src.utils.brochure_delegate import (
    BrochureDelegateRequest,
    build_brochure_delegate_planning_prompt,
    parse_brochure_delegate_request,
)
from src.utils.brochure_source_materials import (
    DEFAULT_BROCHURE_SOURCE_MATERIALS_PATH,
    load_brochure_source_materials,
    summarize_brochure_source_materials,
    write_brochure_source_materials,
)
from src.utils.quoted_handoff import rewrite_quoted_development_request
from src.utils.quoted_requirement_doc import parse_quoted_requirement_doc_request
from src.utils.weixin_utils import ImageUtils, FileUtils, ProactiveReplyClient

logger = logging.getLogger(__name__)

_RELAY_CONNECTION_HINT = (
    "AI 服务暂时无法连接，请联系管理员检查：\n"
    "1. ClawRelay 服务是否正常运行\n"
    "2. bots.yaml 中的 relay_url 配置是否正确\n"
    "修改配置后需要重启服务才能生效。"
)
_RELAY_HTTP_ERROR_HINT = (
    "AI 服务返回异常，请联系管理员检查 ClawRelay 服务状态。"
)
_CODEX_CLI_NOT_FOUND_HINT = (
    "本地 Codex CLI 不可用，请联系管理员检查：\n"
    "1. 是否已安装 codex CLI\n"
    "2. codex 是否在服务进程的 PATH 中\n"
    "3. 服务重启后是否生效。"
)
_CODEX_CLI_SANDBOX_HINT = (
    "本地 Codex CLI 沙箱不可用，请联系管理员检查：\n"
    "1. 当前系统是否支持 bwrap/user namespace\n"
    "2. 若在可信环境运行，可在 bots.yaml 中为 codex_cli 机器人设置\n"
    "   provider_config.dangerously_bypass_approvals_and_sandbox: true\n"
    "3. 修改配置后需要重启服务。"
)
_CODEX_CLI_EXEC_HINT = (
    "本地 Codex CLI 调用失败，请联系管理员检查 codex 登录状态、网络连通性和工作目录配置。"
)
_CODEX_CLI_RECONNECT_HINT = (
    "本地 Codex CLI 与服务端连接暂时中断，系统正在重连，请稍后再试。"
)
_CODEX_CLI_RECONNECT_RE = re.compile(r"^Reconnecting\.\.\.\s+\d+/\d+$", re.IGNORECASE)
_HELP_MENU_TRIGGER_RE = re.compile(r"^\s*1[.、:：)]?\s*$")
_LEADING_GROUP_MENTION_RE = re.compile(r"^(?:(?:<@[^>]+>|@[^\s@]+)\s+)+")
_QUOTE_HINT_TOKENS = ("quote", "quoted", "reply", "refer", "reference", "citation")

def _friendly_error(e: Exception) -> str:
    """将内部异常转为用户友好的错误提示"""
    msg = str(e)
    if "[ClaudeRelay] Connection error" in msg:
        return _RELAY_CONNECTION_HINT
    if "[ClaudeRelay] HTTP" in msg:
        return _RELAY_HTTP_ERROR_HINT
    if "[CodexCLI] 未找到 codex 命令" in msg:
        return _CODEX_CLI_NOT_FOUND_HINT
    if "bwrap: Creating new namespace failed" in msg:
        return _CODEX_CLI_SANDBOX_HINT
    if "[CodexCLI] Reconnecting in progress" in msg or _CODEX_CLI_RECONNECT_RE.match(msg.strip()):
        return _CODEX_CLI_RECONNECT_HINT
    if "[CodexCLI] Turn interrupted before completion" in msg:
        return _CODEX_CLI_EXEC_HINT
    if "[CodexCLI] Process exited" in msg or "Codex app-server 进程异常退出" in msg:
        return _CODEX_CLI_EXEC_HINT
    return f"抱歉，处理出错，请稍后重试。如问题持续，请联系管理员。"

# 节流间隔(秒)
STREAM_THROTTLE_INTERVAL = 0.3
RUNNING_TASK_SILENT_WARNING_SECONDS = 90


class MessageDispatcher:
    """WebSocket消息分发与回复"""

    def __init__(
        self,
        ws_client: WsClient,
        bot_config: BotConfig,
        orchestrator: Optional[BaseOrchestrator] = None,
        group_project_context_resolver: Optional[GroupProjectContextResolver] = None,
        delegate_manager: Optional[BotDelegateManager] = None,
    ):
        self.ws = ws_client
        self.config = bot_config
        self.bot_key = bot_config.bot_key

        # 命令路由器
        self.command_router = CommandRouter()

        # 使用工厂创建编排器
        self.orchestrator = orchestrator or OrchestratorFactory.create(bot_config)

        # 会话管理
        self.session_manager = SessionManager()
        self.group_project_context_resolver = group_project_context_resolver
        self.delegate_manager = delegate_manager

        # 加载自定义命令
        self._load_custom_commands()

        # 机器人名称（用于过滤@提及）
        self.bot_name = bot_config.name or ""

        # 消息去重集合
        self._processed_msgids: dict[str, float] = {}

        logger.info("[Dispatcher:%s] 初始化完成", self.bot_key)

    def _resolve_session_key(self, body: dict, user_id: str) -> str:
        """从消息或事件体中提取会话 key"""
        event = body.get("event", {}) or {}
        chattype = body.get("chattype") or event.get("chattype") or "single"
        chatid = (
            body.get("chatid")
            or event.get("chatid")
            or body.get("chat", {}).get("chatid", "")
        )
        if chatid and chattype == "group":
            return chatid
        return chatid or user_id

    @staticmethod
    def _build_log_context(body: dict, chattype: str, message_type: str, **extra) -> dict:
        context = {
            "chat_type": chattype,
            "chat_id": body.get("chatid", ""),
            "message_type": message_type,
        }
        context.update(extra)
        return context

    @staticmethod
    def _extract_response_url(body: dict) -> str:
        stack = [body or {}]
        seen: set[int] = set()

        while stack:
            current = stack.pop()
            if not isinstance(current, (dict, list)):
                continue
            current_id = id(current)
            if current_id in seen:
                continue
            seen.add(current_id)

            if isinstance(current, dict):
                for key in ("response_url", "responseUrl"):
                    value = current.get(key)
                    if isinstance(value, str) and value.strip():
                        return value.strip()
                for value in reversed(list(current.values())):
                    if isinstance(value, (dict, list)):
                        stack.append(value)
            else:
                for value in reversed(current):
                    if isinstance(value, (dict, list)):
                        stack.append(value)

        return ""

    def _build_reply_state(
        self,
        req_id: str,
        stream_id: str,
        *,
        prefix: str = "",
        body: Optional[dict] = None,
    ) -> dict:
        return {
            "req_id": req_id,
            "stream_id": stream_id,
            "prefix": prefix or "",
            "response_url": self._extract_response_url(body or {}),
        }

    def _resolve_brochure_delegate_runtime_context(
        self,
        user_id: str,
        session_key: str,
        log_context: dict,
    ):
        delegate_target = self._resolve_codex_cli_delegate_target()
        if not delegate_target:
            return None, None, None, ""

        target_bot_key, target_bot_config, target_orchestrator = delegate_target
        delegated_log_context = self._delegated_log_context(log_context, self.bot_key, target_bot_key)
        resolver = getattr(target_orchestrator, "_ensure_runtime_context", None)
        if not callable(resolver):
            return delegate_target, delegated_log_context, None, ""

        runtime_context, early_reply = resolver(
            user_id=user_id,
            session_key=session_key,
            log_context=delegated_log_context,
        )
        return delegate_target, delegated_log_context, runtime_context, early_reply

    @staticmethod
    def _unique_material_path(directory: Path, filename: str) -> Path:
        safe_name = re.sub(r"[^a-zA-Z0-9._-]+", "-", str(filename or "").strip()).strip("-_.") or "material"
        candidate = directory / safe_name
        if not candidate.exists():
            return candidate

        stem = candidate.stem or "material"
        suffix = candidate.suffix
        index = 2
        while True:
            next_candidate = directory / f"{stem}-{index}{suffix}"
            if not next_candidate.exists():
                return next_candidate
            index += 1

    @staticmethod
    def _detect_image_extension(file_bytes: bytes) -> str:
        if file_bytes[:8] == b"\x89PNG\r\n\x1a\n":
            return ".png"
        if file_bytes[:2] == b"\xff\xd8":
            return ".jpg"
        if file_bytes[:4] == b"GIF8":
            return ".gif"
        if file_bytes[:4] == b"RIFF" and file_bytes[8:12] == b"WEBP":
            return ".webp"
        return ".bin"

    @classmethod
    def _summarize_uploaded_document_text(cls, filename: str, file_bytes: bytes) -> str:
        suffix = Path(str(filename or "")).suffix.lower()
        if suffix not in {
            ".txt", ".md", ".csv", ".json", ".xml", ".yaml", ".yml",
            ".py", ".js", ".ts", ".go", ".java", ".html", ".css",
            ".sql", ".sh", ".log",
        }:
            return ""

        try:
            text = file_bytes.decode("utf-8", errors="ignore")
        except Exception:
            return ""
        return cls._clean_document_fragment(text, limit=240)

    def _upsert_brochure_source_materials(
        self,
        workspace_path: str,
        *,
        project_name: str,
        new_entries: list[dict],
    ) -> dict:
        payload = load_brochure_source_materials(workspace_path) or {
            "version": 1,
            "project_name": str(project_name or "").strip(),
            "generated_at": "",
            "materials": [],
        }
        materials = list(payload.get("materials") or [])
        materials.extend(new_entries or [])
        payload["project_name"] = str(project_name or payload.get("project_name") or "").strip()
        payload["generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        payload["materials"] = materials
        payload["material_count"] = len(materials)
        payload["image_count"] = sum(1 for item in materials if str((item or {}).get("kind") or "") == "image")
        payload["document_count"] = sum(1 for item in materials if str((item or {}).get("kind") or "") == "document")
        write_brochure_source_materials(workspace_path, payload)
        return payload

    @classmethod
    def _build_brochure_source_materials_context(
        cls,
        workspace_path: str,
    ) -> str:
        payload = load_brochure_source_materials(workspace_path)
        materials = list((payload or {}).get("materials") or [])
        if not materials:
            return ""

        lines = [
            "【当前已上传画册资料】",
            f"资料清单：{DEFAULT_BROCHURE_SOURCE_MATERIALS_PATH}",
            "这些资料已保存在当前项目工作区，请优先读取并用于画册策划与落地：",
        ]
        for item in materials[:8]:
            kind = "图片" if str((item or {}).get("kind") or "") == "image" else "文档"
            relative_path = str((item or {}).get("relative_path") or "").strip()
            if not relative_path:
                continue
            lines.append(f"- {kind}：{relative_path}")
            summary = cls._clean_document_fragment(str((item or {}).get("summary") or ""), limit=160)
            if summary:
                lines.append(f"  摘要：{summary}")
        if len(materials) > 8:
            lines.append(f"- 其余资料：还有 {len(materials) - 8} 项，可查看清单文件")
        lines.append(f"资料概览：{summarize_brochure_source_materials(payload)}")
        return "\n".join(lines).strip()

    @classmethod
    def _append_brochure_source_materials_context(
        cls,
        message: str,
        workspace_path: str,
    ) -> str:
        materials_context = cls._build_brochure_source_materials_context(workspace_path)
        if not materials_context:
            return str(message or "").strip()
        normalized_message = str(message or "").strip()
        if materials_context in normalized_message:
            return normalized_message
        if not normalized_message:
            return materials_context
        return f"{materials_context}\n\n{normalized_message}"

    def _resolve_runtime_session_key(self, user_id: str, session_key: str, log_context: dict = None) -> str:
        return self.orchestrator.get_runtime_session_key(
            user_id=user_id,
            session_key=session_key,
            log_context=log_context or {},
        )

    def _should_inherit_group_project_context(self, chattype: str) -> bool:
        if chattype != "group":
            return False
        if self.config.bot_type == "codex_cli":
            return False
        if not self.group_project_context_resolver or not self.group_project_context_resolver.has_sources():
            return False
        provider_config = self.config.provider_config or {}
        raw_value = provider_config.get("inherit_group_project_context")
        if raw_value is None:
            return True
        return self._is_truthy_config(raw_value)

    def _resolve_group_project_context(
        self,
        user_id: str,
        session_key: str,
        chattype: str,
    ) -> Optional[GroupProjectContext]:
        if not self._should_inherit_group_project_context(chattype):
            return None
        try:
            return self.group_project_context_resolver.resolve(
                bot_config=self.config,
                chat_id=session_key,
                user_id=user_id,
            )
        except Exception as e:
            logger.warning("[Dispatcher:%s] 解析群项目上下文失败: %s", self.bot_key, e)
            return None

    @staticmethod
    def _group_project_context_log_fields(context: Optional[GroupProjectContext]) -> dict:
        if not context:
            return {}
        return {
            "project_id": context.project_id,
            "project_name": context.project_name,
            "project_workspace_path": context.workspace_path,
            "project_source_bot_key": context.source_bot_key,
            "project_mode": context.mode,
        }

    @staticmethod
    def _format_group_project_context(context: Optional[GroupProjectContext]) -> str:
        if not context:
            return ""

        mode_text = "共享工作区" if context.mode == "shared_workspace" else "个人工作区"
        workspace_kind = "共享" if context.workspace_type == "shared" else "个人"
        lines = [
            f"来源机器人：{context.source_bot_key}",
            f"项目：{context.project_name} ({context.project_id})",
            f"当前模式：{mode_text}",
            f"当前工作区：{workspace_kind} / {context.workspace_path}",
        ]
        if context.project_root:
            lines.append(f"项目目录：{context.project_root}")
        if context.git_remote_url:
            lines.append(f"仓库地址：{context.git_remote_url}")
        elif context.publish_git_remote_url:
            lines.append(f"发布仓库：{context.publish_git_remote_url}")
        if context.deployment_type:
            lines.append(f"部署类型：{context.deployment_type}")
        lines.append("说明：这是当前群聊绑定的项目上下文。除非用户明确切换话题，否则请默认围绕这个项目回答。")
        lines.append("如果当前机器人不具备该目录访问能力，请先明确说明限制，不要假装已经读取了项目文件。")
        return "\n".join(lines)

    @classmethod
    def _compose_message_with_group_project_context(cls, content: str, context: Optional[GroupProjectContext]) -> str:
        normalized_content = str(content or "").strip()
        context_text = cls._format_group_project_context(context)
        if not context_text:
            return normalized_content
        if not normalized_content:
            return f"【当前群项目上下文】\n{context_text}"
        return f"【当前群项目上下文】\n{context_text}\n\n{normalized_content}"

    def _make_orchestrator_call_kwargs(
        self,
        req_id: str,
        bot_config: Optional[BotConfig] = None,
        on_interaction_request=None,
    ) -> dict:
        effective_bot_config = bot_config or self.config
        if (effective_bot_config.bot_type or "").strip() != "codex_cli":
            return {}

        async def relay_interaction_request(payload: dict):
            template_card = (payload or {}).get("template_card")
            if template_card:
                logger.info(
                    "[Dispatcher:%s] Codex 交互已触发，当前通道以文字授权为主，卡片仅作兼容保留: task_id=%s",
                    self.bot_key,
                    (payload or {}).get("task_id", ""),
                )
            if callable(on_interaction_request):
                try:
                    await on_interaction_request(payload or {})
                except Exception as e:
                    logger.warning(
                        "[Dispatcher:%s] Codex 交互回调处理失败: req_id=%s err=%s",
                        self.bot_key,
                        req_id,
                        e,
                    )

        return {"on_interaction_request": relay_interaction_request}

    def _supports_brochure_internal_delegate(self) -> bool:
        provider_config = self.config.provider_config or {}
        explicit_value = provider_config.get("enable_brochure_internal_delegate")
        if explicit_value is not None:
            return self._is_truthy_config(explicit_value)

        bot_key = str(self.config.bot_key or "").strip().lower()
        bot_name = str(self.config.name or "").strip().lower()
        bot_desc = str(self.config.description or "").strip().lower()
        return "brochure" in bot_key or "画册" in bot_name or "画册" in bot_desc

    def _preferred_delegate_bot_key(self) -> str:
        provider_config = self.config.provider_config or {}
        return str(
            provider_config.get("delegate_execution_bot_key")
            or provider_config.get("group_project_context_bot_key")
            or ""
        ).strip()

    def _resolve_codex_cli_delegate_target(self):
        if not self.delegate_manager:
            return None
        return self.delegate_manager.resolve_codex_cli_delegate(
            preferred_bot_key=self._preferred_delegate_bot_key()
        )

    @staticmethod
    def _is_truthy_config(value) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on", "enabled"}
        return False

    def _supports_help_menu_card(self) -> bool:
        provider_config = self.config.provider_config or {}
        return (
            self.config.bot_type == "codex_cli"
            and self._is_truthy_config(provider_config.get("help_menu_card_enabled"))
            and callable(getattr(self.orchestrator, "build_help_menu_card", None))
            and callable(getattr(self.orchestrator, "build_help_menu_reply", None))
        )

    def _is_brochure_specialist_bot(self) -> bool:
        candidates = [
            str(self.config.bot_key or "").strip().lower(),
            str(self.config.name or "").strip().lower(),
            str(self.config.description or "").strip().lower(),
        ]
        return any(("brochure" in value or "画册" in value) for value in candidates if value)

    def _build_specialized_help_reply(self) -> Optional[str]:
        provider_config = self.config.provider_config or {}
        custom_help = str(provider_config.get("help_text") or "").strip()
        if custom_help:
            return custom_help

        if not self._is_brochure_specialist_bot():
            return None

        lines = [
            "📘 产品画册机器人使用说明",
            "",
            "你可以直接这样发：",
            "- 发产品名、网址、卖点、参数、适用场景、竞品信息、参考链接或图片资料",
            "- 例如：`帮我做一版智能灯具产品画册方案`",
            "",
            "常用说法：",
            "- `帮我出一版产品画册方案`",
            "- `做完整画册`",
            "- `做完整画册并导出PDF`",
            "- `做完整画册并发布画册`",
            "",
            "推荐流程：",
            "- 第一步：先把产品资料或网址发给我",
            "- 第二步：我先输出画册结构、每页文案和配图建议",
            "- 第三步：你确认后，可直接发 `做完整画册` 继续自动落地",
        ]

        if self._supports_brochure_internal_delegate():
            delegate_bot_key = self._preferred_delegate_bot_key() or "cx_bot"
            lines.extend(
                [
                    "",
                    f"当前已支持后台自动落地：我会把执行委托给 `{delegate_bot_key}` 继续完成 HTML/PDF/发布。",
                ]
            )
        else:
            lines.extend(
                [
                    "",
                    "如需自动生成 HTML、PDF 或发布，请联系管理员启用画册后台执行机器人。",
                ]
            )

        return "\n".join(lines)

    @staticmethod
    def _is_help_menu_trigger(content: str) -> bool:
        normalized = str(content or "").strip().lower()
        if normalized in {
            "帮助",
            "help",
            "?",
            "？",
            "菜单",
            "项目帮助",
            "项目命令",
            "工作区帮助",
            "怎么用",
        }:
            return True
        return _HELP_MENU_TRIGGER_RE.match(str(content or "").strip()) is not None

    async def _reply_help_menu_card(self, req_id: str) -> bool:
        if not self._supports_help_menu_card():
            return False
        template_card = self.orchestrator.build_help_menu_card(
            task_id=f"menu@help@{self.bot_key}"
        )
        await self._reply_template_card(
            req_id,
            template_card,
            plain_card=True,
        )
        return True

    def _is_orchestrator_control_command(self, content: str) -> bool:
        checker = getattr(self.orchestrator, "is_control_command", None)
        if not callable(checker):
            return False
        try:
            return bool(checker(content))
        except Exception as e:
            logger.warning("[Dispatcher:%s] 检查控制命令失败: %s", self.bot_key, e)
            return False

    def _normalize_text_content(self, content: str, chattype: str) -> str:
        value = str(content or "").strip()
        if not value:
            return ""

        if self.bot_name and value.startswith(f"@{self.bot_name} "):
            value = value[len(f"@{self.bot_name} "):].strip()
        elif self.bot_name and value.startswith(f"@{self.bot_name}"):
            value = value[len(f"@{self.bot_name}"):].strip()

        if chattype == "group":
            stripped = _LEADING_GROUP_MENTION_RE.sub("", value).strip()
            if stripped:
                value = stripped

        return value.strip()

    @staticmethod
    def _clean_message_fragment(value: str, limit: int = 280) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        text = "\n".join(line.strip() for line in text.splitlines() if line.strip())
        if len(text) <= limit:
            return text
        return text[: max(0, limit - 3)] + "..."

    @classmethod
    def _clean_document_fragment(cls, value: str, limit: int = 20000) -> str:
        text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        if not text:
            return ""
        text = re.sub(r"\n{3,}", "\n\n", text)
        if len(text) <= limit:
            return text
        return text[: max(0, limit - 3)] + "..."

    @classmethod
    def _is_quote_hint(cls, value: str) -> bool:
        normalized = str(value or "").strip().lower()
        return bool(normalized) and any(token in normalized for token in _QUOTE_HINT_TOKENS)

    @classmethod
    def _extract_text_fragments_from_node(
        cls,
        node,
        depth: int = 0,
        fragment_limit: int = 280,
        max_fragments: int = 3,
        preserve_formatting: bool = False,
    ) -> list[str]:
        if depth > 4 or node is None:
            return []
        if isinstance(node, str):
            cleaner = cls._clean_document_fragment if preserve_formatting else cls._clean_message_fragment
            cleaned = cleaner(node, limit=fragment_limit)
            return [cleaned] if cleaned else []
        if isinstance(node, list):
            fragments: list[str] = []
            for item in node[:10]:
                fragments.extend(
                    cls._extract_text_fragments_from_node(
                        item,
                        depth + 1,
                        fragment_limit=fragment_limit,
                        max_fragments=max_fragments,
                        preserve_formatting=preserve_formatting,
                    )
                )
            return fragments
        if not isinstance(node, dict):
            return []

        fragments: list[str] = []
        priority_keys = (
            "content",
            "text",
            "body",
            "message",
            "title",
            "desc",
            "description",
            "quote_text",
            "quoted_text",
            "reply_text",
        )
        skip_nested_keys = {
            "aeskey",
            "url",
            "msgid",
            "msg_id",
            "message_id",
            "chatid",
            "chat_id",
            "eventtype",
            "msgtype",
            "userid",
            "user_id",
            "username",
            "nickname",
            "name",
            "id",
            "key",
            "from",
            "sender",
            "author",
        }

        for key in priority_keys:
            value = node.get(key)
            if isinstance(value, str):
                cleaner = cls._clean_document_fragment if preserve_formatting else cls._clean_message_fragment
                cleaned = cleaner(value, limit=fragment_limit)
                if cleaned:
                    fragments.append(cleaned)
            elif isinstance(value, (dict, list)):
                fragments.extend(
                    cls._extract_text_fragments_from_node(
                        value,
                        depth + 1,
                        fragment_limit=fragment_limit,
                        max_fragments=max_fragments,
                        preserve_formatting=preserve_formatting,
                    )
                )

        for key, value in node.items():
            normalized_key = str(key or "").strip().lower()
            if normalized_key in priority_keys or normalized_key in skip_nested_keys:
                continue
            if isinstance(value, (dict, list)):
                fragments.extend(
                    cls._extract_text_fragments_from_node(
                        value,
                        depth + 1,
                        fragment_limit=fragment_limit,
                        max_fragments=max_fragments,
                        preserve_formatting=preserve_formatting,
                    )
                )

        deduped: list[str] = []
        seen: set[str] = set()
        for fragment in fragments:
            normalized = fragment.strip()
            if not normalized or normalized in seen:
                continue
            deduped.append(normalized)
            seen.add(normalized)
            if len(deduped) >= max_fragments:
                break
        return deduped

    @classmethod
    def _extract_quote_speaker(cls, node) -> str:
        if not isinstance(node, dict):
            return ""

        candidates = []
        from_data = node.get("from")
        if isinstance(from_data, dict):
            candidates.extend(
                [
                    from_data.get("name"),
                    from_data.get("nickname"),
                    from_data.get("username"),
                    from_data.get("userid"),
                    from_data.get("user_id"),
                ]
            )

        sender_data = node.get("sender")
        if isinstance(sender_data, dict):
            candidates.extend(
                [
                    sender_data.get("name"),
                    sender_data.get("nickname"),
                    sender_data.get("username"),
                    sender_data.get("userid"),
                    sender_data.get("user_id"),
                ]
            )

        candidates.extend(
            [
                node.get("sender_name"),
                node.get("nickname"),
                node.get("name"),
                node.get("username"),
                node.get("userid"),
                node.get("user_id"),
            ]
        )

        for candidate in candidates:
            cleaned = cls._clean_message_fragment(candidate, limit=80)
            if cleaned:
                return cleaned
        return ""

    @classmethod
    def _collect_quote_nodes(cls, node, depth: int = 0) -> list:
        if depth > 5 or node is None:
            return []
        if isinstance(node, list):
            nodes = []
            for item in node[:20]:
                nodes.extend(cls._collect_quote_nodes(item, depth + 1))
            return nodes
        if not isinstance(node, dict):
            return []

        nodes = []
        msgtype_value = node.get("msgtype")
        if isinstance(msgtype_value, str) and cls._is_quote_hint(msgtype_value):
            nodes.append(node)

        for key, value in node.items():
            normalized_key = str(key or "").strip().lower()
            if cls._is_quote_hint(normalized_key):
                nodes.append(value)
            if isinstance(value, (dict, list)):
                nodes.extend(cls._collect_quote_nodes(value, depth + 1))

        return nodes

    @classmethod
    def _extract_quote_context(cls, body: dict) -> str:
        fragments: list[str] = []
        seen: set[str] = set()

        for node in cls._collect_quote_nodes(body):
            speaker = cls._extract_quote_speaker(node)
            texts = cls._extract_text_fragments_from_node(node)
            if not texts:
                continue
            snippet = "\n".join(texts)
            if speaker and not snippet.startswith(f"{speaker}："):
                snippet = f"{speaker}：{snippet}"
            cleaned = cls._clean_message_fragment(snippet, limit=360)
            if not cleaned or cleaned in seen:
                continue
            fragments.append(cleaned)
            seen.add(cleaned)
            if len(fragments) >= 2:
                break

        return "\n\n".join(fragments)

    @classmethod
    def _extract_full_quote_context(cls, body: dict) -> str:
        fragments: list[str] = []
        seen: set[str] = set()

        for node in cls._collect_quote_nodes(body):
            texts = cls._extract_text_fragments_from_node(
                node,
                fragment_limit=20000,
                max_fragments=10,
                preserve_formatting=True,
            )
            if not texts:
                continue
            snippet = "\n\n".join(texts).strip()
            cleaned = cls._clean_document_fragment(snippet, limit=20000)
            if not cleaned or cleaned in seen:
                continue
            fragments.append(cleaned)
            seen.add(cleaned)
            if len(fragments) >= 1:
                break

        return "\n\n".join(fragments)

    def _resolve_control_command_content(self, content: str, quote_context: str = "") -> str:
        normalized = str(content or "").strip()
        if not normalized:
            return ""
        if not quote_context or self._is_orchestrator_control_command(normalized):
            return normalized

        candidates = []
        lines = [line.strip() for line in normalized.splitlines() if line.strip()]
        if lines:
            candidates.append(lines[-1])
        paragraphs = [item.strip() for item in re.split(r"\n\s*\n", normalized) if item.strip()]
        if paragraphs:
            candidates.append(paragraphs[-1])

        for candidate in candidates:
            if candidate and candidate != normalized and self._is_orchestrator_control_command(candidate):
                return candidate
        return normalized

    @staticmethod
    def _compose_message_with_quote(content: str, quote_context: str = "") -> str:
        normalized_content = str(content or "").strip()
        normalized_quote = str(quote_context or "").strip()
        if not normalized_quote:
            return normalized_content
        if normalized_quote in normalized_content:
            return normalized_content
        if not normalized_content:
            return f"【引用消息】\n{normalized_quote}"
        return f"【引用消息】\n{normalized_quote}\n\n【当前消息】\n{normalized_content}"

    @staticmethod
    def _pending_interaction_notice() -> str:
        return "当前 Codex 正等待你的确认或补充信息，请直接发送文字回复。"

    @staticmethod
    def _is_running_task_status_command(content: str) -> bool:
        normalized = re.sub(r"[^\w\u4e00-\u9fff]", "", str(content or "").strip().lower())
        return normalized in {"当前任务", "当前任务状态", "任务状态", "开发状态"}

    @staticmethod
    def _format_elapsed_duration(seconds: float) -> str:
        total_seconds = max(int(seconds), 0)
        if total_seconds < 60:
            return f"{total_seconds} 秒"
        minutes, remain_seconds = divmod(total_seconds, 60)
        if minutes < 60:
            if remain_seconds:
                return f"{minutes} 分 {remain_seconds} 秒"
            return f"{minutes} 分钟"
        hours, remain_minutes = divmod(minutes, 60)
        if remain_minutes:
            return f"{hours} 小时 {remain_minutes} 分"
        return f"{hours} 小时"

    @staticmethod
    def _summarize_stream_preview(content: str, limit: int = 120) -> str:
        normalized = re.sub(r"</?think>", " ", str(content or ""))
        normalized = re.sub(r"\s+", " ", normalized).strip()
        if len(normalized) <= limit:
            return normalized
        return normalized[: max(limit - 1, 0)].rstrip() + "…"

    def _task_registry_key(self, runtime_session_key: str) -> str:
        return f"{self.bot_key}:{runtime_session_key}"

    def _running_task_status_reply(self, runtime_session_key: str) -> str:
        from src.core.task_registry import get_task_registry

        registry = get_task_registry()
        task, _stream_id, extra = registry.get(self._task_registry_key(runtime_session_key))
        if not task or task.done():
            recent = registry.get_recent(self._task_registry_key(runtime_session_key))
            if not recent:
                return "当前没有正在运行的任务。"

            now = time.time()
            finished_at = float((recent or {}).get("finished_at") or now)
            preview = self._summarize_stream_preview((recent or {}).get("last_preview") or "")
            terminal_status = str((recent or {}).get("terminal_status") or "completed").strip().lower()
            reply_delivery_failed = bool((recent or {}).get("reply_delivery_failed"))
            terminal_error = self._summarize_stream_preview(
                str((recent or {}).get("terminal_error") or ""),
                limit=180,
            )

            if terminal_status == "cancelled":
                status_line = f"最近一次任务已于 {self._format_elapsed_duration(now - finished_at)} 前停止。"
            elif terminal_status == "error":
                status_line = f"最近一次任务已于 {self._format_elapsed_duration(now - finished_at)} 前异常结束。"
            else:
                status_line = f"最近一次任务已于 {self._format_elapsed_duration(now - finished_at)} 前完成。"

            lines = ["当前没有正在运行的任务。", status_line]
            if recent.get("proactive_reply_sent"):
                lines.append("提示：原回复通道异常，系统已通过主动回复补发结果。")
            elif reply_delivery_failed:
                lines.append("提示：执行期间回复通道异常，最终结果可能没有成功送达。")
            if terminal_error and terminal_status == "error":
                lines.append(f"最近错误：{terminal_error}")
            if preview:
                lines.append(f"最近输出：{preview}")
            resume_state = recent.get("brochure_delegate_resume")
            if (
                isinstance(resume_state, dict)
                and resume_state.get("resumable")
                and str(resume_state.get("stage") or "").strip()
                and str(resume_state.get("stage") or "").strip() != "completed"
            ):
                lines.append("下一步：可直接回复“继续”，从上次中断的画册阶段恢复。")
            return "\n".join(lines)

        now = time.time()
        started_at = float((extra or {}).get("started_at") or now)
        last_activity_at = float((extra or {}).get("last_activity_at") or started_at)
        running_seconds = max(now - started_at, 0)
        silent_seconds = max(now - last_activity_at, 0)
        preview = self._summarize_stream_preview((extra or {}).get("last_preview") or "")

        stale_after_seconds = RUNNING_TASK_SILENT_WARNING_SECONDS
        keepalive_after_seconds = int(
            max(getattr(self.orchestrator, "long_task_keepalive_after_seconds", 0) or 0, 0)
        )
        keepalive_interval_seconds = int(
            max(getattr(self.orchestrator, "long_task_keepalive_interval_seconds", 0) or 0, 0)
        )
        if keepalive_after_seconds > 0:
            stale_after_seconds = max(
                stale_after_seconds,
                keepalive_after_seconds + max(keepalive_interval_seconds, keepalive_after_seconds) + 15,
            )

        lines = ["当前任务状态"]
        if self.orchestrator.has_pending_interaction(runtime_session_key):
            lines.append("状态：等待你的确认或补充信息")
            lines.append(f"已运行：{self._format_elapsed_duration(running_seconds)}")
            lines.append("下一步：请直接回复答案；如需取消请回复“停止”")
        elif silent_seconds >= stale_after_seconds:
            lines.append(f"状态：疑似卡住（已静默 {self._format_elapsed_duration(silent_seconds)}）")
            lines.append(f"已运行：{self._format_elapsed_duration(running_seconds)}")
            lines.append("建议：可回复“停止”后重新发送需求")
        else:
            lines.append("状态：运行中")
            lines.append(f"已运行：{self._format_elapsed_duration(running_seconds)}")
            lines.append(f"最近进度：{self._format_elapsed_duration(silent_seconds)}前")
            lines.append("如需中断可回复：停止")

        if preview:
            lines.append(f"最近输出：{preview}")
        return "\n".join(lines)

    async def _maybe_handoff_running_task(self, runtime_session_key: str, req_id: str) -> bool:
        from src.core.task_registry import get_task_registry

        task, _stream_id, _extra = get_task_registry().get(self._task_registry_key(runtime_session_key))
        if not task or task.done():
            return False

        ack = (
            "⏳ 当前任务仍在处理中，本条消息未新开任务；"
            "已把进度切到这里继续显示。\n"
            "可回复“停止”中断，或发送“当前任务”查看状态。"
        )
        if await self._handoff_running_reply(runtime_session_key, req_id, ack):
            return True
        await self._reply_text(req_id, ack, finish=True)
        return True

    def _normalize_control_command_for_ack(self, content: str) -> str:
        command = str(content or "").strip()
        normalizer = getattr(self.orchestrator, "_normalize_control_command_input", None)
        if callable(normalizer):
            try:
                normalized = str(normalizer(command) or "").strip()
                if normalized:
                    return normalized
            except Exception:
                logger.debug("[Dispatcher:%s] 归一化控制命令失败，回退原文", self.bot_key, exc_info=True)
        return command

    def _should_ack_control_command(self, content: str) -> bool:
        command = self._normalize_control_command_for_ack(content)
        if not command:
            return False
        long_running_prefixes = (
            "新建仓库项目",
            "从仓库派生项目",
            "从选中仓库派生项目",
            "创建GitHub仓库",
            "创建GitHub公开仓库",
            "创建GitHub仓库并发布",
            "推送到GitHub",
            "推送到GitHub公开",
            "准备GitHub仓库",
            "发布到新仓库",
            "同步上游",
            "启用Pages部署",
            "一键发布Pages",
            "一键部署Pages",
            "一键发布Cloudflare Pages",
            "一键部署Cloudflare Pages",
            "发布流水线状态",
            "流水线状态",
            "CI状态",
            "GitHub Actions状态",
            "Cloudflare项目状态",
            "Cloudflare状态",
            "Cloudflare部署状态",
            "启用Worker部署",
            "一键发布Worker",
            "一键部署Worker",
            "启用小程序上传",
            "一键上传小程序",
            "启用小程序提审",
            "提交小程序审核",
            "正式发布小程序",
            "撤回小程序审核",
            "回退小程序版本",
        )
        return any(command.startswith(prefix) for prefix in long_running_prefixes)

    def _control_command_processing_ack(self, content: str) -> str:
        command = self._normalize_control_command_for_ack(content)
        if command.startswith(("一键发布Pages", "一键部署Pages", "一键发布Cloudflare Pages", "一键部署Cloudflare Pages")):
            return "⏳ 已收到，正在处理 Cloudflare Pages 发布，请稍候。"
        if command.startswith(("启用Pages部署",)):
            return "⏳ 已收到，正在写入 Cloudflare Pages 部署配置，请稍候。"
        if command.startswith(("发布流水线状态", "流水线状态", "CI状态", "GitHub Actions状态")):
            return "⏳ 已收到，正在查询 GitHub Actions 状态，请稍候。"
        if command.startswith(("Cloudflare项目状态", "Cloudflare状态", "Cloudflare部署状态")):
            return "⏳ 已收到，正在查询 Cloudflare 部署状态，请稍候。"
        if command.startswith(("一键发布Worker", "一键部署Worker")):
            return "⏳ 已收到，正在处理 Cloudflare Worker 发布，请稍候。"
        if command.startswith(("启用Worker部署",)):
            return "⏳ 已收到，正在写入 Cloudflare Worker 部署配置，请稍候。"
        if command.startswith(("一键上传小程序",)):
            return "⏳ 已收到，正在处理微信小程序上传，请稍候。"
        if command.startswith(("启用小程序上传", "启用小程序提审", "提交小程序审核", "正式发布小程序", "撤回小程序审核", "回退小程序版本")):
            return "⏳ 已收到，正在处理微信小程序发布流程，请稍候。"
        if command.startswith(("推送到GitHub", "推送到GitHub公开", "创建GitHub仓库", "创建GitHub公开仓库", "创建GitHub仓库并发布", "准备GitHub仓库", "发布到新仓库", "同步上游")):
            return "⏳ 已收到，正在处理 GitHub 仓库与推送，请稍候。"
        if command.startswith(("新建仓库项目", "从仓库派生项目", "从选中仓库派生项目")):
            return "⏳ 已收到，正在准备项目工作区，请稍候。"
        return "⏳ 已收到，正在处理中，请稍候。"

    @staticmethod
    def _compose_stream_content(reply_state: dict, content: str) -> str:
        prefix = ((reply_state or {}).get("prefix") or "").strip()
        if prefix and content:
            return f"{prefix}\n\n{content}"
        return prefix or content

    @staticmethod
    def _join_delegate_sections(sections: list[str], live_text: str = "") -> str:
        parts = [str(item or "").strip() for item in (sections or []) if str(item or "").strip()]
        if str(live_text or "").strip():
            parts.append(str(live_text or "").strip())
        return "\n\n".join(parts).strip()

    @staticmethod
    def _delegated_log_context(log_context: dict, source_bot_key: str, target_bot_key: str) -> dict:
        delegated_context = dict(log_context or {})
        delegated_context["delegated_from_bot_key"] = source_bot_key
        delegated_context["delegated_execution_bot_key"] = target_bot_key
        delegated_context["delegated_execution_mode"] = "internal"
        return delegated_context

    @classmethod
    def _build_delegate_interaction_notice(cls, payload: dict, target_bot_key: str) -> str:
        prompt = cls._clean_document_fragment((payload or {}).get("text_prompt") or "", limit=2000)
        lines = [
            "【等待确认】",
            f"后台执行机器人 `{target_bot_key}` 需要你的确认或补充信息。",
            "请直接继续在当前画册机器人里回复文字，无需再切到其它机器人。",
        ]
        if prompt:
            lines.append(prompt)
        return "\n".join(lines).strip()

    @staticmethod
    def _is_brochure_continue_request(content: str) -> bool:
        normalized = re.sub(r"\s+", "", str(content or "").strip().lower())
        if not normalized:
            return False
        if normalized in {
            "继续",
            "继续做",
            "继续执行",
            "继续生成",
            "继续完成",
            "接着做",
            "接着继续",
            "resume",
            "continue",
        }:
            return True
        return "继续" in normalized or normalized.startswith(("接着", "resume", "continue"))

    @staticmethod
    def _initial_brochure_delegate_resume_state(
        delegate_request: BrochureDelegateRequest,
        *,
        target_bot_key: str,
        composed_message: str,
    ) -> dict:
        stage = "planning" if delegate_request.planning_needed else "final_delivery"
        return {
            "resumable": True,
            "mode": delegate_request.mode,
            "planning_needed": bool(delegate_request.planning_needed),
            "final_control_command": str(delegate_request.final_control_command or "").strip(),
            "composed_message": str(composed_message or "").strip(),
            "target_bot_key": str(target_bot_key or "").strip(),
            "stage": stage,
            "plan_text": "",
        }

    def _touch_brochure_delegate_resume_state(self, task_key: str, **updates) -> None:
        if not task_key:
            return
        from src.core.task_registry import get_task_registry

        registry = get_task_registry()
        _task, _stream_id, extra = registry.get(task_key)
        current = dict((extra or {}).get("brochure_delegate_resume") or {})
        current.update(updates)
        registry.touch(task_key, brochure_delegate_resume=current)

    def _get_recent_brochure_delegate_resume_state(self, runtime_session_key: str) -> tuple[dict, dict] | tuple[None, None]:
        from src.core.task_registry import get_task_registry

        recent = get_task_registry().get_recent(self._task_registry_key(runtime_session_key))
        resume_state = recent.get("brochure_delegate_resume")
        if not isinstance(resume_state, dict):
            return None, None
        if not resume_state.get("resumable"):
            return None, None
        stage = str(resume_state.get("stage") or "").strip()
        if not stage or stage == "completed":
            return None, None
        terminal_status = str(recent.get("terminal_status") or "").strip()
        if terminal_status not in {"error", "cancelled"}:
            return None, None
        return recent, dict(resume_state)

    @classmethod
    def _build_brochure_delegate_generate_prompt(
        cls,
        *,
        resume_state: dict,
        continue_user_message: str = "",
        resumed: bool = False,
    ) -> str:
        plan_text = str((resume_state or {}).get("plan_text") or "").strip()
        composed_message = str((resume_state or {}).get("composed_message") or "").strip()
        base_message = (
            cls._compose_message_with_quote("生成画册", plan_text)
            if plan_text
            else composed_message
        )
        if not resumed:
            return base_message

        extra_message = str(continue_user_message or "").strip()
        normalized_extra = re.sub(r"\s+", "", extra_message.lower())
        extra_lines = []
        if extra_message and normalized_extra not in {
            "继续",
            "继续做",
            "继续执行",
            "继续生成",
            "继续完成",
            "接着做",
            "接着继续",
            "resume",
            "continue",
        }:
            extra_lines.append(f"用户补充：{extra_message}")

        guidance = [
            "【继续上次画册自动落地任务】",
            "请基于当前工作区文件状态、已有线程上下文和已经完成的步骤，继续完成未完成部分。",
            "不要从头重做，也不要覆盖已经完成的结果。",
        ]
        guidance.extend(extra_lines)
        guidance.append(base_message or "请继续完成上次未完成的画册任务。")
        return "\n\n".join(part for part in guidance if str(part or "").strip()).strip()

    def _build_brochure_delegate_resume_sections(
        self,
        *,
        target_bot_key: str,
        resume_state: dict,
        recent: dict | None = None,
    ) -> list[str]:
        stage = str((resume_state or {}).get("stage") or "").strip()
        plan_text = str((resume_state or {}).get("plan_text") or "").strip()
        sections = [
            "\n".join(
                [
                    "已恢复上次中断的画册自动落地流程",
                    f"前台机器人：{self.bot_key}",
                    f"后台执行：{target_bot_key}",
                ]
            )
        ]
        if recent and recent.get("terminal_error"):
            sections.append(f"【上次中断】\n{recent.get('terminal_error')}")
        if plan_text:
            sections.append(f"【画册方案】\n{plan_text}".strip())
        if stage in {"generate", "final_delivery"} and plan_text:
            sections.append("【保存需求】\n已恢复到保存后的阶段")
        if stage == "final_delivery":
            sections.append("【Codex 落地】\n已恢复到成品交付前阶段")
        return sections

    def _resolve_pending_brochure_delegate_interaction(
        self,
        user_id: str,
        session_key: str,
        log_context: dict,
    ):
        if not self._supports_brochure_internal_delegate():
            return None

        delegate_target = self._resolve_codex_cli_delegate_target()
        if not delegate_target:
            return None

        target_bot_key, target_bot_config, target_orchestrator = delegate_target
        delegated_log_context = self._delegated_log_context(log_context, self.bot_key, target_bot_key)
        target_runtime_session_key = target_orchestrator.get_runtime_session_key(
            user_id=user_id,
            session_key=session_key,
            log_context=delegated_log_context,
        )
        if not target_orchestrator.has_pending_interaction(target_runtime_session_key):
            return None

        return (
            target_bot_key,
            target_bot_config,
            target_orchestrator,
            delegated_log_context,
            target_runtime_session_key,
        )

    async def _clear_brochure_delegate_session(
        self,
        user_id: str,
        session_key: str,
        log_context: dict,
    ) -> None:
        if not self._supports_brochure_internal_delegate():
            return

        delegate_target = self._resolve_codex_cli_delegate_target()
        if not delegate_target:
            return

        target_bot_key, _target_bot_config, target_orchestrator = delegate_target
        delegated_log_context = self._delegated_log_context(log_context, self.bot_key, target_bot_key)
        target_runtime_session_key = target_orchestrator.get_runtime_session_key(
            user_id=user_id,
            session_key=session_key,
            log_context=delegated_log_context,
        )
        await target_orchestrator.clear_session(target_runtime_session_key)

    async def _maybe_store_brochure_image_material(
        self,
        req_id: str,
        body: dict,
        user_id: str,
        session_key: str,
        log_context: dict,
    ) -> bool:
        if not self._supports_brochure_internal_delegate():
            return False

        image_info = body.get("image", {}) or {}
        image_url = str(image_info.get("url") or "").strip()
        aeskey = str(image_info.get("aeskey") or "").strip()
        if not image_url or not aeskey:
            return False

        delegate_target, _delegated_log_context, runtime_context, early_reply = (
            self._resolve_brochure_delegate_runtime_context(user_id, session_key, log_context)
        )
        if not delegate_target:
            return False
        if early_reply:
            await self._reply_text(req_id, early_reply, finish=True)
            return True
        if not runtime_context or not runtime_context.get("working_dir"):
            return False

        workspace_path = str(runtime_context["working_dir"])
        workspace_root = Path(workspace_path).expanduser().resolve()
        target_dir = (workspace_root / "brochure" / "assets").resolve()
        target_dir.mkdir(parents=True, exist_ok=True)

        try:
            file_bytes, _detected_name = await FileUtils.download_and_decrypt(
                image_url,
                aeskey,
                key_format="auto",
            )
        except Exception as e:
            logger.error("[Dispatcher:%s] 画册图片素材保存失败: %s", self.bot_key, e, exc_info=True)
            await self._reply_text(req_id, "图片处理失败，请重试。", finish=True)
            return True

        target_path = self._unique_material_path(
            target_dir,
            f"wecom-image-{uuid.uuid4().hex[:8]}{self._detect_image_extension(file_bytes)}",
        )
        target_path.write_bytes(file_bytes)

        relative_path = target_path.relative_to(workspace_root).as_posix()
        project = runtime_context.get("project") or {}
        payload = self._upsert_brochure_source_materials(
            workspace_path,
            project_name=str(project.get("name") or ""),
            new_entries=[
                {
                    "kind": "image",
                    "relative_path": relative_path,
                    "original_filename": target_path.name,
                    "size_bytes": len(file_bytes),
                    "added_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                }
            ],
        )

        await self._reply_text(
            req_id,
            (
                "已接收画册图片素材\n"
                f"项目：{project.get('name', '-')}\n"
                f"工作区：{workspace_path}\n"
                f"已保存：{relative_path}\n"
                f"资料清单：{DEFAULT_BROCHURE_SOURCE_MATERIALS_PATH}\n"
                f"{summarize_brochure_source_materials(payload)}\n"
                "下一步：可继续上传产品参数文档，或直接发送 `做完整画册`"
            ),
            finish=True,
        )
        return True

    async def _maybe_store_brochure_file_material(
        self,
        req_id: str,
        body: dict,
        user_id: str,
        session_key: str,
        log_context: dict,
    ) -> bool:
        if not self._supports_brochure_internal_delegate():
            return False

        file_info = body.get("file", {}) or {}
        file_url = str(file_info.get("url") or "").strip()
        file_name = str(file_info.get("filename") or "").strip()
        aeskey = str(file_info.get("aeskey") or "").strip()
        if not file_url or not aeskey:
            return False

        delegate_target, _delegated_log_context, runtime_context, early_reply = (
            self._resolve_brochure_delegate_runtime_context(user_id, session_key, log_context)
        )
        if not delegate_target:
            return False
        if early_reply:
            await self._reply_text(req_id, early_reply, finish=True)
            return True
        if not runtime_context or not runtime_context.get("working_dir"):
            return False

        workspace_path = str(runtime_context["working_dir"])
        workspace_root = Path(workspace_path).expanduser().resolve()
        target_dir = (workspace_root / "docs" / "source-materials").resolve()
        target_dir.mkdir(parents=True, exist_ok=True)

        try:
            file_bytes, header_filename = await FileUtils.download_and_decrypt(
                file_url,
                aeskey,
                key_format="auto",
            )
            if not file_name:
                file_name = header_filename or FileUtils.detect_filename_from_bytes(file_bytes)
            if not FileUtils.is_allowed(file_name):
                await self._reply_text(req_id, f"不支持的文件类型: {file_name}", finish=True)
                return True
        except Exception as e:
            logger.error("[Dispatcher:%s] 画册参数文档保存失败: %s", self.bot_key, e, exc_info=True)
            await self._reply_text(req_id, "文件处理失败，请重试。", finish=True)
            return True

        target_path = self._unique_material_path(target_dir, file_name)
        target_path.write_bytes(file_bytes)

        relative_path = target_path.relative_to(workspace_root).as_posix()
        summary = self._summarize_uploaded_document_text(target_path.name, file_bytes)
        project = runtime_context.get("project") or {}
        payload = self._upsert_brochure_source_materials(
            workspace_path,
            project_name=str(project.get("name") or ""),
            new_entries=[
                {
                    "kind": "document",
                    "relative_path": relative_path,
                    "original_filename": file_name,
                    "size_bytes": len(file_bytes),
                    "summary": summary,
                    "added_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                }
            ],
        )

        summary_line = f"\n文档摘要：{summary}" if summary else ""
        await self._reply_text(
            req_id,
            (
                "已接收产品参数文档\n"
                f"项目：{project.get('name', '-')}\n"
                f"工作区：{workspace_path}\n"
                f"已保存：{relative_path}\n"
                f"资料清单：{DEFAULT_BROCHURE_SOURCE_MATERIALS_PATH}"
                f"{summary_line}\n"
                f"{summarize_brochure_source_materials(payload)}\n"
                "下一步：可继续上传更多资料，或直接发送 `做完整画册`"
            ),
            finish=True,
        )
        return True

    async def _run_delegate_text_stage(
        self,
        on_stream_delta,
        sections: list[str],
        stage_title: str,
        execute_coro,
    ) -> str:
        final_text = ""

        async def stage_stream(accumulated_text: str, finish: bool):
            nonlocal final_text
            live_section = f"{stage_title}\n{accumulated_text}".strip() if accumulated_text else stage_title
            await on_stream_delta(self._join_delegate_sections(sections, live_section), False)
            if finish:
                final_text = str(accumulated_text or "").strip()

        result = await execute_coro(stage_stream)
        result_text = str(result or final_text or "").strip()
        if result_text:
            sections.append(f"{stage_title}\n{result_text}".strip())
            await on_stream_delta(self._join_delegate_sections(sections), False)
        return result_text

    async def _run_delegate_control_stage(
        self,
        on_stream_delta,
        sections: list[str],
        stage_title: str,
        execute_coro,
    ):
        result = await execute_coro()
        if isinstance(result, dict):
            result_text = str(result.get("content") or "").strip()
        else:
            result_text = str(result or "").strip()
        sections.append(f"{stage_title}\n{result_text}".strip() if result_text else stage_title)
        await on_stream_delta(self._join_delegate_sections(sections), False)
        return result

    async def _execute_brochure_delegate_workflow(
        self,
        req_id: str,
        user_id: str,
        session_key: str,
        runtime_session_key: str,
        log_context: dict,
        delegate_request: BrochureDelegateRequest,
        delegate_target,
        composed_message: str,
        on_stream_delta,
        resume_state: dict | None = None,
        continue_user_message: str = "",
    ) -> str:
        target_bot_key, target_bot_config, target_orchestrator = delegate_target
        delegated_log_context = self._delegated_log_context(log_context, self.bot_key, target_bot_key)
        task_key = self._task_registry_key(runtime_session_key)
        current_resume_state = dict(resume_state or {})
        resumed = bool(current_resume_state)
        if not current_resume_state:
            current_resume_state = self._initial_brochure_delegate_resume_state(
                delegate_request,
                target_bot_key=target_bot_key,
                composed_message=composed_message,
            )
        current_resume_state["target_bot_key"] = target_bot_key
        current_resume_state["composed_message"] = str(
            current_resume_state.get("composed_message") or composed_message or ""
        ).strip()
        current_resume_state["final_control_command"] = str(
            current_resume_state.get("final_control_command")
            or delegate_request.final_control_command
            or ""
        ).strip()
        current_resume_state["planning_needed"] = bool(
            current_resume_state.get("planning_needed", delegate_request.planning_needed)
        )
        if not current_resume_state.get("stage"):
            current_resume_state["stage"] = (
                "planning" if current_resume_state["planning_needed"] else "final_delivery"
            )

        if resumed:
            from src.core.task_registry import get_task_registry

            recent = get_task_registry().get_recent(task_key)
            sections = self._build_brochure_delegate_resume_sections(
                target_bot_key=target_bot_key,
                resume_state=current_resume_state,
                recent=recent,
            )
        else:
            sections = [
                "\n".join(
                    [
                        "已进入画册自动落地流程",
                        f"前台机器人：{self.bot_key}",
                        f"后台执行：{target_bot_key}",
                    ]
                )
            ]

        self._touch_brochure_delegate_resume_state(task_key, **current_resume_state)
        await on_stream_delta(self._join_delegate_sections(sections), False)

        final_delivery_result = None
        plan_text = str(current_resume_state.get("plan_text") or "").strip()
        latest_interaction_notice = ""

        async def on_delegate_interaction_request(payload: dict):
            nonlocal latest_interaction_notice
            notice = self._build_delegate_interaction_notice(payload, target_bot_key)
            if not notice or notice == latest_interaction_notice:
                return
            latest_interaction_notice = notice
            await on_stream_delta(self._join_delegate_sections(sections, notice), False)

        current_stage = str(current_resume_state.get("stage") or "").strip()

        if current_resume_state["planning_needed"] and current_stage == "planning":
            self._touch_brochure_delegate_resume_state(task_key, stage="planning")
            planning_prompt = build_brochure_delegate_planning_prompt(composed_message)
            plan_text = await self._run_delegate_text_stage(
                on_stream_delta=on_stream_delta,
                sections=sections,
                stage_title="【画册方案】",
                execute_coro=lambda stage_stream: self.orchestrator.handle_text_message(
                    user_id=user_id,
                    message=planning_prompt,
                    stream_id=uuid.uuid4().hex[:12],
                    session_key=session_key,
                    log_context=log_context,
                    on_stream_delta=stage_stream,
                ),
            )
            current_resume_state["plan_text"] = plan_text
            current_resume_state["stage"] = "save_requirement"
            self._touch_brochure_delegate_resume_state(
                task_key,
                plan_text=plan_text,
                stage="save_requirement",
            )

        if current_resume_state["planning_needed"] and str(current_resume_state.get("stage") or "") == "save_requirement":
            save_command = self._compose_message_with_quote("保存为画册需求文档", plan_text)
            self._touch_brochure_delegate_resume_state(task_key, stage="save_requirement")
            await self._run_delegate_control_stage(
                on_stream_delta=on_stream_delta,
                sections=sections,
                stage_title="【保存需求】",
                execute_coro=lambda: target_orchestrator.handle_control_command(
                    user_id=user_id,
                    content=save_command,
                    session_key=session_key,
                    log_context=delegated_log_context,
                ),
            )
            current_resume_state["stage"] = "generate"
            self._touch_brochure_delegate_resume_state(task_key, stage="generate")

        if current_resume_state["planning_needed"] and str(current_resume_state.get("stage") or "") == "generate":
            generate_command = self._build_brochure_delegate_generate_prompt(
                resume_state=current_resume_state,
                continue_user_message=continue_user_message,
                resumed=resumed,
            )
            self._touch_brochure_delegate_resume_state(task_key, stage="generate")
            await self._run_delegate_text_stage(
                on_stream_delta=on_stream_delta,
                sections=sections,
                stage_title="【Codex 落地】",
                execute_coro=lambda stage_stream: target_orchestrator.handle_text_message(
                    user_id=user_id,
                    message=generate_command,
                    stream_id=uuid.uuid4().hex[:12],
                    session_key=session_key,
                    log_context=delegated_log_context,
                    on_stream_delta=stage_stream,
                    **self._make_orchestrator_call_kwargs(
                        req_id,
                        target_bot_config,
                        on_interaction_request=on_delegate_interaction_request,
                    ),
                ),
            )
            current_resume_state["stage"] = "final_delivery"
            self._touch_brochure_delegate_resume_state(task_key, stage="final_delivery")

        if not current_resume_state["planning_needed"]:
            current_resume_state["stage"] = "final_delivery"
            self._touch_brochure_delegate_resume_state(task_key, stage="final_delivery")

        final_delivery_result = await self._run_delegate_control_stage(
            on_stream_delta=on_stream_delta,
            sections=sections,
            stage_title="【成品交付】",
            execute_coro=lambda: target_orchestrator.handle_control_command(
                user_id=user_id,
                content=current_resume_state["final_control_command"],
                session_key=session_key,
                log_context=delegated_log_context,
            ),
        )

        current_resume_state["stage"] = "completed"
        current_resume_state["resumable"] = False
        self._touch_brochure_delegate_resume_state(
            task_key,
            stage="completed",
            resumable=False,
        )

        final_text = self._join_delegate_sections(sections)
        await on_stream_delta(final_text, True)

        if isinstance(final_delivery_result, dict):
            await self._reply_control_result(req_id, final_delivery_result)
            return str(final_delivery_result.get("content") or final_text or "").strip()
        return str(final_delivery_result or final_text).strip()

    async def _maybe_handle_brochure_internal_delegate(
        self,
        req_id: str,
        body: dict,
        user_id: str,
        session_key: str,
        runtime_session_key: str,
        content: str,
        quote_context: str,
        group_project_context: Optional[GroupProjectContext],
        log_context: dict,
    ) -> bool:
        if not self._supports_brochure_internal_delegate():
            return False

        composed_message = self._compose_message_with_group_project_context(
            self._compose_message_with_quote(content, quote_context),
            group_project_context,
        )
        delegate_request = parse_brochure_delegate_request(composed_message)
        if not delegate_request:
            return False

        delegate_target, _delegated_log_context, delegate_runtime_context, early_reply = (
            self._resolve_brochure_delegate_runtime_context(user_id, session_key, log_context)
        )
        if not delegate_target:
            await self._reply_text(
                req_id,
                "当前没有可用的 `codex_cli` 机器人用于画册落地，请先配置并启动 `cx_bot` 一类的执行机器人。",
                finish=True,
            )
            return True
        if early_reply:
            await self._reply_text(req_id, early_reply, finish=True)
            return True
        if delegate_runtime_context and delegate_runtime_context.get("working_dir"):
            composed_message = self._append_brochure_source_materials_context(
                composed_message,
                delegate_runtime_context["working_dir"],
            )

        stream_id = uuid.uuid4().hex[:12]
        reply_state = self._build_reply_state(req_id, stream_id, body=body)
        on_stream_delta = self._make_stream_delta_callback(
            reply_state,
            task_key=self._task_registry_key(runtime_session_key),
        )

        await self._run_with_task_registry(
            req_id,
            stream_id,
            runtime_session_key,
            self._execute_brochure_delegate_workflow(
                req_id=req_id,
                user_id=user_id,
                session_key=session_key,
                runtime_session_key=runtime_session_key,
                log_context=log_context,
                delegate_request=delegate_request,
                delegate_target=delegate_target,
                composed_message=composed_message,
                on_stream_delta=on_stream_delta,
            ),
            reply_state=reply_state,
        )
        return True

    async def _maybe_resume_recent_brochure_delegate(
        self,
        req_id: str,
        body: dict,
        user_id: str,
        session_key: str,
        runtime_session_key: str,
        content: str,
        log_context: dict,
    ) -> bool:
        if not self._supports_brochure_internal_delegate():
            return False
        if not self._is_brochure_continue_request(content):
            return False

        recent, resume_state = self._get_recent_brochure_delegate_resume_state(runtime_session_key)
        if not recent or not resume_state:
            return False

        delegate_target, _delegated_log_context, delegate_runtime_context, early_reply = (
            self._resolve_brochure_delegate_runtime_context(user_id, session_key, log_context)
        )
        if not delegate_target:
            return False
        if early_reply:
            await self._reply_text(req_id, early_reply, finish=True)
            return True

        target_bot_key, target_bot_config, _target_orchestrator = delegate_target
        expected_target = str(resume_state.get("target_bot_key") or "").strip()
        if expected_target and expected_target != target_bot_key:
            logger.warning(
                "[Dispatcher:%s] 画册恢复目标机器人不一致: expected=%s actual=%s",
                self.bot_key,
                expected_target,
                target_bot_key,
            )

        delegate_request = BrochureDelegateRequest(
            mode=str(resume_state.get("mode") or "full_flow"),
            planning_needed=bool(resume_state.get("planning_needed")),
            final_control_command=str(resume_state.get("final_control_command") or "").strip(),
            original_message="",
            current_message="",
        )
        composed_message = str(resume_state.get("composed_message") or "").strip()
        if delegate_runtime_context and delegate_runtime_context.get("working_dir"):
            composed_message = self._append_brochure_source_materials_context(
                composed_message,
                delegate_runtime_context["working_dir"],
            )
        delegate_request = BrochureDelegateRequest(
            mode=delegate_request.mode,
            planning_needed=delegate_request.planning_needed,
            final_control_command=delegate_request.final_control_command,
            original_message=composed_message,
            current_message=composed_message,
        )

        stream_id = uuid.uuid4().hex[:12]
        reply_state = self._build_reply_state(req_id, stream_id, body=body)
        on_stream_delta = self._make_stream_delta_callback(
            reply_state,
            task_key=self._task_registry_key(runtime_session_key),
        )

        await self._run_with_task_registry(
            req_id,
            stream_id,
            runtime_session_key,
            self._execute_brochure_delegate_workflow(
                req_id=req_id,
                user_id=user_id,
                session_key=session_key,
                runtime_session_key=runtime_session_key,
                log_context=log_context,
                delegate_request=delegate_request,
                delegate_target=delegate_target,
                composed_message=composed_message,
                on_stream_delta=on_stream_delta,
                resume_state=resume_state,
                continue_user_message=content,
            ),
            reply_state=reply_state,
        )
        return True

    async def _handoff_running_reply(self, session_key: str, req_id: str, ack: str) -> bool:
        from src.core.task_registry import get_task_registry

        registry = get_task_registry()
        task_key = f"{self.bot_key}:{session_key}"
        task, _old_stream_id, extra = registry.get(task_key)
        if not task or task.done():
            return False

        reply_state = extra.get("reply_state") if isinstance(extra, dict) else None
        if not isinstance(reply_state, dict):
            return False

        new_stream_id = uuid.uuid4().hex[:12]
        reply_state["req_id"] = req_id
        reply_state["stream_id"] = new_stream_id
        reply_state["prefix"] = ack or ""

        if not registry.update_stream(
            task_key,
            new_stream_id,
            req_id=req_id,
            reply_state=reply_state,
        ):
            return False

        if ack:
            await self._reply_stream(req_id, new_stream_id, ack, finish=False)
        return True

    def _load_custom_commands(self):
        """加载自定义命令模块"""
        if not self.config.custom_commands:
            return
        for module_path in self.config.custom_commands:
            try:
                import importlib
                module = importlib.import_module(module_path)
                if hasattr(module, 'register_commands'):
                    module.register_commands(self.command_router)
                    logger.info("[Dispatcher:%s] 加载自定义命令: %s", self.bot_key, module_path)
            except Exception as e:
                logger.error("[Dispatcher:%s] 加载自定义命令失败: %s (%s)", self.bot_key, module_path, e)

    # ---- 消息回调 ----

    async def on_msg_callback(self, msg: dict):
        """处理 aibot_msg_callback"""
        req_id = msg["headers"]["req_id"]
        body = msg["body"]
        msgid = body.get("msgid", "")

        # 消息去重
        if msgid and msgid in self._processed_msgids:
            logger.info("[Dispatcher:%s] 重复消息，跳过: msgid=%s", self.bot_key, msgid)
            return
        if msgid:
            self._processed_msgids[msgid] = time.time()
            self._cleanup_processed_msgids()

        user_id = body.get("from", {}).get("userid", "")
        msgtype = body.get("msgtype", "")
        chattype = body.get("chattype", "single")
        chatid = body.get("chatid", "")
        session_key = chatid if chattype == "group" else user_id

        logger.info(
            "[Dispatcher:%s] 收到消息: msgtype=%s, user=%s, chattype=%s, session_key=%s",
            self.bot_key, msgtype, user_id, chattype, session_key
        )

        # 用户白名单检查
        if self.config.allowed_users and user_id not in self.config.allowed_users:
            logger.warning("[Dispatcher:%s] 用户 %s 不在白名单中", self.bot_key, user_id)
            await self._reply_text(req_id, "抱歉，您没有使用此机器人的权限。\n\n如需开通权限，请联系管理员。", finish=True)
            return

        # 按消息类型路由
        if msgtype == "text":
            await self._handle_text(req_id, body, user_id, session_key, chattype)
        elif msgtype == "image":
            await self._handle_image(req_id, body, user_id, session_key, chattype)
        elif msgtype == "voice":
            await self._handle_voice(req_id, body, user_id, session_key, chattype)
        elif msgtype == "file":
            await self._handle_file(req_id, body, user_id, session_key, chattype)
        elif msgtype == "mixed":
            await self._handle_mixed(req_id, body, user_id, session_key, chattype)
        else:
            logger.warning("[Dispatcher:%s] 不支持的消息类型: %s", self.bot_key, msgtype)

    async def _handle_text(self, req_id: str, body: dict, user_id: str, session_key: str, chattype: str):
        """处理文本消息"""
        content = self._normalize_text_content(
            body.get("text", {}).get("content", ""),
            chattype,
        )
        if not content:
            return

        quote_context = self._extract_quote_context(body)
        full_quote_context = self._extract_full_quote_context(body)
        command_content = self._resolve_control_command_content(content, quote_context)
        normalized = command_content.strip().lower()
        group_project_context = self._resolve_group_project_context(user_id, session_key, chattype)
        log_context = self._build_log_context(
            body,
            chattype,
            "text",
            **self._group_project_context_log_fields(group_project_context),
        )
        runtime_session_key = self._resolve_runtime_session_key(user_id, session_key, log_context)

        if normalized in ("reset", "new", "clear", "重置", "清空"):
            await self.session_manager.clear_session(self.bot_key, runtime_session_key)
            await self.orchestrator.clear_session(runtime_session_key)
            await self._clear_brochure_delegate_session(user_id, session_key, log_context)
            from src.core.task_registry import get_task_registry
            get_task_registry().forget(self._task_registry_key(runtime_session_key))
            await self._reply_text(req_id, "会话已重置，可以开始新的对话。", finish=True)
            return

        import re
        stop_msg = re.sub(r'[^\w\u4e00-\u9fff]', '', normalized)
        if stop_msg in ("stop", "停止", "暂停", "停"):
            from src.core.task_registry import get_task_registry
            cancelled, old_stream_id, extra = get_task_registry().cancel(f"{self.bot_key}:{runtime_session_key}")
            if cancelled:
                old_req_id = extra.get("req_id")
                if old_stream_id and old_req_id:
                    await self._reply_stream(old_req_id, old_stream_id, "⏹ 任务已被用户停止。", finish=True)
                await self._clear_brochure_delegate_session(user_id, session_key, log_context)
                await self._reply_text(req_id, "⏹ 已停止当前任务。", finish=True)
            else:
                await self._reply_text(req_id, "当前没有正在运行的任务。", finish=True)
            return

        if self._is_running_task_status_command(content):
            await self._reply_text(
                req_id,
                self._running_task_status_reply(runtime_session_key),
                finish=True,
            )
            return

        is_control_command = self._is_orchestrator_control_command(command_content)

        if self.orchestrator.has_pending_interaction(runtime_session_key) and not is_control_command:
            interaction_result = await self.orchestrator.handle_interaction_text(runtime_session_key, content)
            if interaction_result:
                ack = interaction_result.get("ack", "")
                submitted = bool(interaction_result.get("submitted"))
                if submitted and await self._handoff_running_reply(runtime_session_key, req_id, ack):
                    return
                if ack:
                    await self._reply_text(req_id, ack, finish=True)
                    return
            await self._reply_text(req_id, self._pending_interaction_notice(), finish=True)
            return

        pending_delegate_interaction = self._resolve_pending_brochure_delegate_interaction(
            user_id=user_id,
            session_key=session_key,
            log_context=log_context,
        )
        if pending_delegate_interaction:
            (
                _target_bot_key,
                _target_bot_config,
                target_orchestrator,
                _delegated_log_context,
                target_runtime_session_key,
            ) = pending_delegate_interaction
            target_is_control_command = False
            checker = getattr(target_orchestrator, "is_control_command", None)
            if callable(checker):
                try:
                    target_is_control_command = bool(checker(command_content))
                except Exception as e:
                    logger.warning("[Dispatcher:%s] 检查委托控制命令失败: %s", self.bot_key, e)
            if not target_is_control_command:
                interaction_result = await target_orchestrator.handle_interaction_text(
                    target_runtime_session_key,
                    content,
                )
                if interaction_result:
                    ack = interaction_result.get("ack", "")
                    submitted = bool(interaction_result.get("submitted"))
                    if submitted and await self._handoff_running_reply(runtime_session_key, req_id, ack):
                        return
                    if ack:
                        await self._reply_text(req_id, ack, finish=True)
                        return
                await self._reply_text(
                    req_id,
                    "当前画册后台任务仍在等待你的确认，请直接回复上一条提示中的答案。",
                    finish=True,
                )
                return

        if not is_control_command and await self._maybe_handoff_running_task(runtime_session_key, req_id):
            return

        if await self._maybe_resume_recent_brochure_delegate(
            req_id=req_id,
            body=body,
            user_id=user_id,
            session_key=session_key,
            runtime_session_key=runtime_session_key,
            content=content,
            log_context=log_context,
        ):
            return

        if (
            not self.orchestrator.has_pending_interaction(runtime_session_key)
            and self._is_help_menu_trigger(content)
            and await self._reply_help_menu_card(req_id)
        ):
            return

        if self._is_help_menu_trigger(content) and not is_control_command:
            specialized_help = self._build_specialized_help_reply()
            if specialized_help:
                await self._reply_text(req_id, specialized_help, finish=True)
                return

        if await self._maybe_handle_brochure_internal_delegate(
            req_id=req_id,
            body=body,
            user_id=user_id,
            session_key=session_key,
            runtime_session_key=runtime_session_key,
            content=content,
            quote_context=quote_context,
            group_project_context=group_project_context,
            log_context=log_context,
        ):
            return

        control_command_content = command_content
        if (self.config.bot_type or "").strip() == "codex_cli" and full_quote_context:
            quoted_control_message = self._compose_message_with_quote(command_content, full_quote_context)
            if parse_quoted_requirement_doc_request(quoted_control_message):
                control_command_content = quoted_control_message

        control_stream_id: Optional[str] = None
        try:
            if self._should_ack_control_command(control_command_content):
                control_stream_id = uuid.uuid4().hex[:12]
                await self._reply_stream(
                    req_id,
                    control_stream_id,
                    self._control_command_processing_ack(control_command_content),
                    finish=False,
                )

            control_reply = await self.orchestrator.handle_control_command(
                user_id=user_id,
                content=control_command_content,
                session_key=session_key,
                log_context=log_context,
            )
        except Exception as e:
            logger.error("[Dispatcher:%s] 控制命令处理失败: %s", self.bot_key, e, exc_info=True)
            error_message = _friendly_error(e)
            if control_stream_id:
                await self._reply_stream(req_id, control_stream_id, error_message, finish=True)
            else:
                await self._reply_text(req_id, error_message, finish=True)
            return

        if control_reply:
            await self._reply_control_result(req_id, control_reply, stream_id=control_stream_id)
            return

        handler = self.command_router.handlers.get(command_content) or self.command_router.handlers.get(normalized)
        if handler:
            stream_id = uuid.uuid4().hex[:12]
            try:
                msg_json, _ = handler.handle(command_content, stream_id, user_id)
                import json as _json
                msg_data = _json.loads(msg_json)
                if msg_data.get("msgtype") == "stream":
                    text_content = msg_data.get("stream", {}).get("content", "")
                elif msg_data.get("msgtype") == "template_card":
                    text_content = "模板卡片命令暂不支持，请使用其他命令。"
                else:
                    text_content = str(msg_data)
                await self._reply_stream(req_id, stream_id, text_content, finish=True)
            except Exception as e:
                logger.error("[Dispatcher:%s] 命令处理失败: %s", self.bot_key, e, exc_info=True)
                await self._reply_text(req_id, f"命令处理出错：{e}", finish=True)
            return

        stream_id = uuid.uuid4().hex[:12]
        reply_state = self._build_reply_state(req_id, stream_id, body=body)
        on_stream_delta = self._make_stream_delta_callback(
            reply_state,
            task_key=self._task_registry_key(runtime_session_key),
        )

        await self._run_with_task_registry(
            req_id,
            stream_id,
            runtime_session_key,
            self.orchestrator.handle_text_message(
                user_id=user_id,
                message=rewrite_quoted_development_request(
                    self._compose_message_with_group_project_context(
                        self._compose_message_with_quote(content, quote_context),
                        group_project_context,
                    )
                ),
                stream_id=stream_id,
                session_key=session_key,
                log_context=log_context,
                on_stream_delta=on_stream_delta,
                **self._make_orchestrator_call_kwargs(req_id),
            ),
            reply_state=reply_state,
        )

    async def _handle_image(self, req_id: str, body: dict, user_id: str, session_key: str, chattype: str):
        """处理图片消息"""
        image_info = body.get("image", {})
        image_url = image_info.get("url", "")
        aeskey = image_info.get("aeskey", "")

        if not image_url:
            return

        group_project_context = self._resolve_group_project_context(user_id, session_key, chattype)
        log_context = self._build_log_context(
            body,
            chattype,
            "image",
            **self._group_project_context_log_fields(group_project_context),
        )
        runtime_session_key = self._resolve_runtime_session_key(user_id, session_key, log_context)
        if await self._maybe_store_brochure_image_material(req_id, body, user_id, session_key, log_context):
            return
        if self.orchestrator.has_pending_interaction(runtime_session_key):
            await self._reply_text(req_id, self._pending_interaction_notice(), finish=True)
            return
        if await self._maybe_handoff_running_task(runtime_session_key, req_id):
            return

        try:
            data_uri = await ImageUtils.download_and_decrypt_to_base64(image_url, aeskey)
            content_blocks = [
                {
                    "type": "text",
                    "text": self._compose_message_with_group_project_context(
                        "[用户发送了一张图片] 请描述或分析这张图片",
                        group_project_context,
                    ),
                },
                {"type": "image_url", "image_url": {"url": data_uri}},
            ]
        except Exception as e:
            logger.error("[Dispatcher:%s] 图片下载解密失败: %s", self.bot_key, e)
            await self._reply_text(req_id, "图片处理失败，请重试。", finish=True)
            return

        stream_id = uuid.uuid4().hex[:12]
        reply_state = self._build_reply_state(req_id, stream_id, body=body)
        on_stream_delta = self._make_stream_delta_callback(
            reply_state,
            task_key=self._task_registry_key(runtime_session_key),
        )

        await self._run_with_task_registry(
            req_id, stream_id, runtime_session_key,
            self.orchestrator.handle_multimodal_message(
                user_id=user_id,
                content_blocks=content_blocks,
                stream_id=stream_id,
                session_key=session_key,
                log_context=log_context,
                on_stream_delta=on_stream_delta,
                **self._make_orchestrator_call_kwargs(req_id),
            ),
            reply_state=reply_state,
        )

    async def _handle_voice(self, req_id: str, body: dict, user_id: str, session_key: str, chattype: str):
        """处理语音消息（已转为文本）"""
        voice_content = body.get("voice", {}).get("content", "")
        if not voice_content:
            await self._reply_text(req_id, "语音识别失败，请重试或发送文字。", finish=True)
            return

        # 修改body模拟文本消息，复用文本处理
        body["text"] = {"content": voice_content}
        body["_original_msgtype"] = "voice"
        await self._handle_text(req_id, body, user_id, session_key, chattype)

    async def _handle_file(self, req_id: str, body: dict, user_id: str, session_key: str, chattype: str):
        """处理文件消息"""
        file_info = body.get("file", {})
        file_url = file_info.get("url", "")
        file_name = file_info.get("filename", "")
        aeskey = file_info.get("aeskey", "")

        if not file_url:
            return

        group_project_context = self._resolve_group_project_context(user_id, session_key, chattype)
        log_context = self._build_log_context(
            body,
            chattype,
            "file",
            **self._group_project_context_log_fields(group_project_context),
        )
        runtime_session_key = self._resolve_runtime_session_key(user_id, session_key, log_context)
        if await self._maybe_store_brochure_file_material(req_id, body, user_id, session_key, log_context):
            return
        if self.orchestrator.has_pending_interaction(runtime_session_key):
            await self._reply_text(req_id, self._pending_interaction_notice(), finish=True)
            return
        if await self._maybe_handoff_running_task(runtime_session_key, req_id):
            return

        try:
            file_bytes, header_filename = await FileUtils.download_and_decrypt(file_url, aeskey)
            if not file_name:
                file_name = header_filename or FileUtils.detect_filename_from_bytes(file_bytes)
            if not FileUtils.is_allowed(file_name):
                await self._reply_text(req_id, f"不支持的文件类型: {file_name}", finish=True)
                return
            file_data = FileUtils.encode_for_relay(file_bytes, file_name)
        except Exception as e:
            logger.error("[Dispatcher:%s] 文件下载解密失败: %s", self.bot_key, e)
            await self._reply_text(req_id, "文件处理失败，请重试。", finish=True)
            return

        stream_id = uuid.uuid4().hex[:12]
        message = self._compose_message_with_group_project_context(
            f"[用户发送了文件: {file_name}] 请分析这个文件的内容。",
            group_project_context,
        )
        log_context["file_info"] = [{"filename": file_name}]
        reply_state = self._build_reply_state(req_id, stream_id, body=body)
        on_stream_delta = self._make_stream_delta_callback(
            reply_state,
            task_key=self._task_registry_key(runtime_session_key),
        )

        await self._run_with_task_registry(
            req_id, stream_id, runtime_session_key,
            self.orchestrator.handle_file_message(
                user_id=user_id,
                message=message,
                files=[file_data],
                stream_id=stream_id,
                session_key=session_key,
                log_context=log_context,
                on_stream_delta=on_stream_delta,
                **self._make_orchestrator_call_kwargs(req_id),
            ),
            reply_state=reply_state,
        )

    async def _handle_mixed(self, req_id: str, body: dict, user_id: str, session_key: str, chattype: str):
        """处理图文混排消息"""
        mixed_data = body.get("mixed", {})
        items = mixed_data.get("msg_item") or mixed_data.get("items") or []
        if not items:
            return

        group_project_context = self._resolve_group_project_context(user_id, session_key, chattype)
        log_context = self._build_log_context(
            body,
            chattype,
            "mixed",
            **self._group_project_context_log_fields(group_project_context),
        )
        runtime_session_key = self._resolve_runtime_session_key(user_id, session_key, log_context)
        if self.orchestrator.has_pending_interaction(runtime_session_key):
            await self._reply_text(req_id, self._pending_interaction_notice(), finish=True)
            return
        if await self._maybe_handoff_running_task(runtime_session_key, req_id):
            return

        quote_context = self._extract_quote_context(body)
        content_blocks = []
        text_parts = []
        has_non_text_content = False
        for item in items:
            item_type = item.get("msgtype", "")
            if item_type == "text":
                text_value = item.get("text", {}).get("content", "")
                if text_value:
                    text_parts.append(text_value)
                    content_blocks.append({"type": "text", "text": text_value})
            elif item_type == "image":
                has_non_text_content = True
                image_url = item.get("image", {}).get("url", "")
                aeskey = item.get("image", {}).get("aeskey", "")
                if image_url and aeskey:
                    try:
                        data_uri = await ImageUtils.download_and_decrypt_to_base64(
                            image_url, aeskey, key_format="auto",
                        )
                        content_blocks.append({"type": "image_url", "image_url": {"url": data_uri}})
                    except Exception as e:
                        logger.warning("[Dispatcher:%s] 混排图片解密失败: %s", self.bot_key, e)
                        content_blocks.append({"type": "text", "text": "[图片加载失败]"})
                else:
                    content_blocks.append({"type": "text", "text": "[图片]"})

        if not content_blocks:
            return

        if text_parts and not has_non_text_content:
            text_body = dict(body)
            text_body["text"] = {"content": "\n".join(text_parts)}
            text_body["_original_msgtype"] = "mixed"
            await self._handle_text(req_id, text_body, user_id, session_key, chattype)
            return

        if group_project_context:
            content_blocks.insert(
                0,
                {
                    "type": "text",
                    "text": f"【当前群项目上下文】\n{self._format_group_project_context(group_project_context)}",
                },
            )
        if quote_context:
            content_blocks.insert(0, {"type": "text", "text": f"【引用消息】\n{quote_context}"})

        stream_id = uuid.uuid4().hex[:12]
        reply_state = self._build_reply_state(req_id, stream_id, body=body)
        on_stream_delta = self._make_stream_delta_callback(
            reply_state,
            task_key=self._task_registry_key(runtime_session_key),
        )

        await self._run_with_task_registry(
            req_id, stream_id, runtime_session_key,
            self.orchestrator.handle_multimodal_message(
                user_id=user_id,
                content_blocks=content_blocks,
                stream_id=stream_id,
                session_key=session_key,
                log_context=log_context,
                on_stream_delta=on_stream_delta,
                **self._make_orchestrator_call_kwargs(req_id),
            ),
            reply_state=reply_state,
        )

    # ---- 事件回调 ----

    async def on_event_callback(self, msg: dict):
        """处理 aibot_event_callback"""
        req_id = msg["headers"]["req_id"]
        body = msg["body"]
        event_type = body.get("event", {}).get("eventtype", "")
        user_id = body.get("from", {}).get("userid", "")

        logger.info(
            "[Dispatcher:%s] 收到事件: eventtype=%s, user=%s",
            self.bot_key, event_type, user_id
        )

        if event_type == "enter_chat":
            await self._handle_enter_chat(req_id, body, user_id)
        elif event_type == "template_card_event":
            await self._handle_template_card_event(req_id, body, user_id)
        elif event_type == "feedback_event":
            logger.info("[Dispatcher:%s] 用户反馈事件: user=%s, body=%s", self.bot_key, user_id, body)
        elif event_type == "disconnected_event":
            # 由WsClient处理，这里不应该收到
            pass
        else:
            logger.warning("[Dispatcher:%s] 未知事件类型: %s", self.bot_key, event_type)

    async def _handle_enter_chat(self, req_id: str, body: dict, user_id: str):
        """处理进入会话事件，回复欢迎语"""
        user_name = user_id
        welcome = f"你好 {user_name}！我是AI助手，有什么可以帮您的吗？"

        payload = {
            "cmd": "aibot_respond_welcome_msg",
            "headers": {"req_id": req_id},
            "body": {
                "msgtype": "text",
                "text": {"content": welcome},
            },
        }
        await self._send_reply_payload(payload)

    async def _handle_template_card_event(self, req_id: str, body: dict, user_id: str):
        """处理模板卡片点击事件"""
        event = body.get("event", {}) or {}
        task_id = event.get("task_id", "")
        session_key = self._resolve_session_key(body, user_id)
        chattype = body.get("chattype") or event.get("chattype") or "single"
        log_context = self._build_log_context(body, chattype, "template_card")
        runtime_session_key = self._resolve_runtime_session_key(user_id, session_key, log_context)

        if task_id.startswith("choice@"):
            logger.info("[Dispatcher:%s] 处理AskUserQuestion卡片点击: task_id=%s", self.bot_key, task_id)
            return

        if task_id.startswith("menu@"):
            logger.info(
                "[Dispatcher:%s] 收到帮助菜单卡片事件: task_id=%s, chatid=%s, response_code=%s, event=%s",
                self.bot_key,
                task_id,
                body.get("chatid") or event.get("chatid") or body.get("chat", {}).get("chatid", ""),
                event.get("response_code") or body.get("response_code") or "",
                event,
            )
            await self._handle_menu_card_event(req_id, body, user_id)
            return

        if task_id.startswith("codex@"):
            has_pending = self.orchestrator.has_pending_interaction(runtime_session_key)
            logger.info(
                "[Dispatcher:%s] 处理 Codex 交互卡片: task_id=%s, session_key=%s, runtime_session_key=%s, has_pending=%s, event=%s",
                self.bot_key,
                task_id,
                session_key,
                runtime_session_key,
                has_pending,
                event,
            )
            interaction_result = await self.orchestrator.handle_interaction_card(runtime_session_key, event)
            if interaction_result:
                ack = interaction_result.get("ack", "")
                submitted = bool(interaction_result.get("submitted"))
                if submitted and await self._handoff_running_reply(runtime_session_key, req_id, ack):
                    return
                if ack:
                    await self._reply_text(req_id, ack, finish=True)
            else:
                logger.warning(
                    "[Dispatcher:%s] Codex 卡片点击未产生响应: task_id=%s, runtime_session_key=%s, event=%s",
                    self.bot_key,
                    task_id,
                    runtime_session_key,
                    event,
                )
            return

        logger.info("[Dispatcher:%s] 未知卡片事件: task_id=%s", self.bot_key, task_id)

    async def _handle_menu_card_event(self, req_id: str, body: dict, user_id: str):
        event = body.get("event", {}) or {}
        if not self._supports_help_menu_card():
            await self._reply_text(req_id, "当前机器人暂不支持帮助菜单卡片。", finish=True)
            return

        extractor = getattr(self.orchestrator, "_extract_card_selected_values", None)
        if callable(extractor):
            selected_values = extractor(event)
        else:
            selected_values = self._extract_card_selected_values(event)

        if not selected_values:
            await self._reply_text(req_id, "未识别到帮助菜单选择，请重新发送：帮助", finish=True)
            return

        topic_id = selected_values[0]
        response_code = self._extract_template_card_response_code(body)
        logger.info(
            "[Dispatcher:%s] 处理帮助菜单卡片点击: topic=%s, response_code=%s, selected_values=%s, event=%s",
            self.bot_key,
            topic_id,
            response_code,
            selected_values,
            event,
        )

        template_card = self.orchestrator.build_help_topic_card(
            topic_id,
            task_id=f"menu@help@{self.bot_key}@{topic_id}",
        )

        if response_code:
            await self._reply_update_template_card(req_id, response_code, template_card)
            return

        reply_text = self.orchestrator.build_help_menu_reply(topic_id)
        await self._reply_text(req_id, reply_text, finish=True)

    # ---- 流式推送 ----

    def _make_stream_delta_callback(self, reply_state: dict, task_key: str = ""):
        """创建带节流的on_stream_delta回调"""
        state = {
            'last_pushed_text': "",
            'last_push_time': 0.0,
            'throttle_task': None,
        }
        push_lock = asyncio.Lock()

        def _mark_delivery_failed():
            if not task_key:
                return
            from src.core.task_registry import get_task_registry

            get_task_registry().touch(task_key, reply_delivery_failed=True)

        async def _maybe_send_proactive_reply(content: str) -> bool:
            response_url = str((reply_state or {}).get("response_url") or "").strip()
            if not response_url:
                return False

            sent = False
            try:
                sent = await ProactiveReplyClient.send_markdown(response_url, content)
            except Exception as e:
                logger.warning(
                    "[Dispatcher:%s] 主动回复补发失败: err=%s",
                    self.bot_key,
                    e,
                    exc_info=True,
                )
                sent = False

            if task_key:
                from src.core.task_registry import get_task_registry

                get_task_registry().touch(
                    task_key,
                    proactive_reply_attempted=True,
                    proactive_reply_sent=sent,
                )
            if sent:
                logger.info("[Dispatcher:%s] 已通过主动回复补发最终结果", self.bot_key)
            return sent

        async def on_stream_delta(accumulated_text: str, finish: bool):
            if task_key:
                from src.core.task_registry import get_task_registry

                get_task_registry().touch(
                    task_key,
                    last_preview=self._summarize_stream_preview(accumulated_text),
                )
            if finish:
                # 完成时立即推送最终内容
                if state['throttle_task'] and not state['throttle_task'].done():
                    state['throttle_task'].cancel()
                target_req_id = (reply_state or {}).get("req_id", "")
                target_stream_id = (reply_state or {}).get("stream_id", "")
                if not target_req_id or not target_stream_id:
                    logger.warning("[Dispatcher:%s] 缺少流式回复目标，跳过最终推送", self.bot_key)
                    return
                final_content = self._compose_stream_content(reply_state, accumulated_text)
                sent = await self._reply_stream(
                    target_req_id,
                    target_stream_id,
                    final_content,
                    finish=True,
                )
                if not sent:
                    _mark_delivery_failed()
                    await _maybe_send_proactive_reply(final_content)
                state['last_pushed_text'] = accumulated_text
                return

            # 节流
            now = time.monotonic()
            elapsed = now - state['last_push_time']

            if elapsed >= STREAM_THROTTLE_INTERVAL and accumulated_text != state['last_pushed_text']:
                async with push_lock:
                    target_req_id = (reply_state or {}).get("req_id", "")
                    target_stream_id = (reply_state or {}).get("stream_id", "")
                    if not target_req_id or not target_stream_id:
                        logger.warning("[Dispatcher:%s] 缺少流式回复目标，跳过增量推送", self.bot_key)
                        return
                    sent = await self._reply_stream(
                        target_req_id,
                        target_stream_id,
                        self._compose_stream_content(reply_state, accumulated_text),
                        finish=False,
                    )
                    if not sent:
                        _mark_delivery_failed()
                    state['last_pushed_text'] = accumulated_text
                    state['last_push_time'] = time.monotonic()
            elif state['throttle_task'] is None or state['throttle_task'].done():
                captured_text = accumulated_text

                async def delayed_push():
                    await asyncio.sleep(STREAM_THROTTLE_INTERVAL - elapsed)
                    async with push_lock:
                        if captured_text != state['last_pushed_text']:
                            target_req_id = (reply_state or {}).get("req_id", "")
                            target_stream_id = (reply_state or {}).get("stream_id", "")
                            if not target_req_id or not target_stream_id:
                                logger.warning("[Dispatcher:%s] 缺少流式回复目标，跳过延迟推送", self.bot_key)
                                return
                            sent = await self._reply_stream(
                                target_req_id,
                                target_stream_id,
                                self._compose_stream_content(reply_state, captured_text),
                                finish=False,
                            )
                            if not sent:
                                _mark_delivery_failed()
                            state['last_pushed_text'] = captured_text
                            state['last_push_time'] = time.monotonic()

                state['throttle_task'] = asyncio.create_task(delayed_push())

        return on_stream_delta

    # ---- 任务管理 ----

    async def _run_with_task_registry(
        self, req_id: str, stream_id: str, session_key: str, coro, reply_state: dict | None = None,
    ):
        """将 orchestrator 协程包装为 task 并注册到全局任务表，支持 stop 命令取消。

        超时后任务继续后台运行，完成时主动推送结果给用户。
        """
        from src.core.task_registry import get_task_registry

        inner_task = asyncio.create_task(coro)
        task_key = f"{self.bot_key}:{session_key}"
        get_task_registry().register(
            task_key,
            inner_task,
            stream_id,
            req_id=req_id,
            reply_state=reply_state or self._build_reply_state(req_id, stream_id),
        )

        try:
            await inner_task
        except asyncio.CancelledError:
            # 被用户 stop 命令取消，_handle_stop 已处理旧消息气泡
            logger.info("[Dispatcher:%s] 任务被用户取消: session_key=%s", self.bot_key, session_key)
        except Exception as e:
            logger.error("[Dispatcher:%s] AI 处理异常: %s", self.bot_key, e, exc_info=True)
            await self._reply_stream(req_id, stream_id, _friendly_error(e), finish=True)

    # ---- 回复辅助方法 ----

    async def _reply_text(self, req_id: str, content: str, finish: bool = True):
        """回复纯文本消息"""
        stream_id = uuid.uuid4().hex[:12]
        return await self._reply_stream(req_id, stream_id, content, finish)

    async def _reply_control_result(
        self,
        req_id: str,
        control_reply: str | dict,
        stream_id: Optional[str] = None,
    ):
        if isinstance(control_reply, dict):
            reply_type = str(control_reply.get("type") or "").strip().lower()
            if reply_type == "image":
                if stream_id:
                    await self._reply_stream(
                        req_id,
                        stream_id,
                        str(control_reply.get("content") or "操作已完成。"),
                        finish=True,
                    )
                await self._reply_image(
                    req_id,
                    str(control_reply.get("image_base64") or ""),
                    str(control_reply.get("image_md5") or ""),
                    str(control_reply.get("content") or ""),
                )
                return
            if reply_type == "text":
                if stream_id:
                    await self._reply_stream(
                        req_id,
                        stream_id,
                        str(control_reply.get("content") or ""),
                        finish=True,
                    )
                else:
                    await self._reply_text(req_id, str(control_reply.get("content") or ""), finish=True)
                return
        if stream_id:
            await self._reply_stream(req_id, stream_id, str(control_reply), finish=True)
        else:
            await self._reply_text(req_id, str(control_reply), finish=True)

    async def _reply_template_card(
        self,
        req_id: str,
        template_card: dict,
        stream_content: str = "Codex 需要你的确认，请点击下方卡片。",
        plain_card: bool = False,
    ):
        """回复模板卡片消息

        企业微信 WebSocket 机器人对纯 template_card 兼容性不稳定，
        这里统一改为 stream_with_template_card，提升实际展示成功率。
        """
        stream_id = uuid.uuid4().hex[:12]
        body = {
            "msgtype": "stream_with_template_card",
            "stream": {
                "id": stream_id,
                "finish": True,
                "content": stream_content,
            },
            "template_card": template_card,
        }
        if plain_card:
            body = {
                "msgtype": "template_card",
                "template_card": template_card,
            }
        payload = {
            "cmd": "aibot_respond_msg",
            "headers": {"req_id": req_id},
            "body": body,
        }
        return await self._send_reply_payload(payload)

    async def _reply_update_template_card(self, req_id: str, response_code: str, template_card: dict):
        payload = {
            "cmd": "aibot_respond_update_msg",
            "headers": {"req_id": req_id},
            "body": {
                "response_code": response_code,
                "template_card": template_card,
            },
        }
        return await self._send_reply_payload(payload)

    @staticmethod
    def _extract_template_card_response_code(body: dict) -> str:
        event = body.get("event", {}) or {}
        candidates = [
            event.get("response_code"),
            body.get("response_code"),
            event.get("template_card", {}).get("response_code"),
            event.get("button_selection", {}).get("response_code"),
            event.get("checkbox", {}).get("response_code"),
            event.get("multiple_select", {}).get("response_code"),
            event.get("submit_button", {}).get("response_code"),
        ]
        for value in candidates:
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    async def _reply_stream(self, req_id: str, stream_id: str, content: str, finish: bool):
        """通过WebSocket发送流式消息回复"""
        payload = {
            "cmd": "aibot_respond_msg",
            "headers": {"req_id": req_id},
            "body": {
                "msgtype": "stream",
                "stream": {
                    "id": stream_id,
                    "finish": finish,
                    "content": content,
                },
            },
        }
        return await self._send_reply_payload(payload)

    async def _reply_image(self, req_id: str, image_base64: str, image_md5: str, content: str = ""):
        stream_id = uuid.uuid4().hex[:12]
        payload = {
            "cmd": "aibot_respond_msg",
            "headers": {"req_id": req_id},
            "body": {
                "msgtype": "stream",
                "stream": {
                    "id": stream_id,
                    "finish": True,
                    "content": content,
                    "msg_item": [
                        {
                            "msgtype": "image",
                            "image": {
                                "base64": image_base64,
                                "md5": image_md5,
                            },
                        }
                    ],
                },
            },
        }
        return await self._send_reply_payload(payload)

    async def _send_reply_payload(self, payload: dict) -> bool:
        req_id = str((payload or {}).get("headers", {}).get("req_id") or "")
        cmd = str((payload or {}).get("cmd") or "")
        try:
            await self.ws.send_reply(payload)
            return True
        except Exception as e:
            logger.warning(
                "[Dispatcher:%s] 回复发送失败: cmd=%s, req_id=%s, err=%s",
                self.bot_key,
                cmd,
                req_id,
                e,
                exc_info=True,
            )
            return False

    # ---- 工具方法 ----

    def _cleanup_processed_msgids(self):
        """清理超过5分钟的已处理消息ID"""
        now = time.time()
        expired = [k for k, v in self._processed_msgids.items() if now - v > 300]
        for k in expired:
            del self._processed_msgids[k]

    @staticmethod
    def _extract_card_selected_values(event: dict) -> list[str]:
        candidates = [
            event.get("selected_items"),
            event.get("selected_item"),
            event.get("option_ids"),
            event.get("option_id"),
            event.get("selected_ids"),
            event.get("selected_id"),
            event.get("value"),
            event.get("values"),
            event.get("event_data"),
            event.get("checkbox"),
            event.get("button_selection"),
            event.get("multiple_select"),
            event.get("select_list"),
            event.get("submit_button"),
        ]

        for candidate in candidates:
            values = MessageDispatcher._extract_selected_values(candidate)
            if values:
                return values
        return []

    @staticmethod
    def _extract_selected_values(selected_items) -> list[str]:
        values: list[str] = []

        def walk(value):
            if value is None:
                return
            if isinstance(value, str):
                if value:
                    values.append(value)
                return
            if isinstance(value, list):
                for item in value:
                    walk(item)
                return
            if isinstance(value, dict):
                for key in ("id", "key", "value", "option_id", "selected_id"):
                    if key in value and isinstance(value[key], str) and value[key]:
                        values.append(value[key])
                for key in ("selected_ids", "option_ids", "values"):
                    if key in value:
                        walk(value[key])
                for nested_key, nested in value.items():
                    if nested_key in {"question_key", "task_id", "event_key", "submit_button_key"}:
                        continue
                    if isinstance(nested, (list, dict)):
                        walk(nested)

        walk(selected_items)
        deduped: list[str] = []
        for value in values:
            if value not in deduped:
                deduped.append(value)
        return deduped
