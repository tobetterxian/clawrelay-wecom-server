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
from typing import Optional

from config.bot_config import BotConfig
from src.core.base_orchestrator import BaseOrchestrator
from src.transport.ws_client import WsClient
from src.core.orchestrator_factory import OrchestratorFactory
from src.core.session_manager import SessionManager
from src.handlers.command_handlers import CommandRouter
from src.utils.weixin_utils import ImageUtils, FileUtils

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
    if "[CodexCLI] Process exited" in msg or "Codex app-server 进程异常退出" in msg:
        return _CODEX_CLI_EXEC_HINT
    return f"抱歉，处理出错，请稍后重试。如问题持续，请联系管理员。"

# 节流间隔(秒)
STREAM_THROTTLE_INTERVAL = 0.3


class MessageDispatcher:
    """WebSocket消息分发与回复"""

    def __init__(
        self,
        ws_client: WsClient,
        bot_config: BotConfig,
        orchestrator: Optional[BaseOrchestrator] = None,
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

    def _resolve_runtime_session_key(self, user_id: str, session_key: str, log_context: dict = None) -> str:
        return self.orchestrator.get_runtime_session_key(
            user_id=user_id,
            session_key=session_key,
            log_context=log_context or {},
        )

    def _make_orchestrator_call_kwargs(self, req_id: str) -> dict:
        if self.config.bot_type != "codex_cli":
            return {}

        async def on_interaction_request(payload: dict):
            template_card = (payload or {}).get("template_card")
            if template_card:
                logger.info(
                    "[Dispatcher:%s] Codex 交互已触发，当前通道以文字授权为主，卡片仅作兼容保留: task_id=%s",
                    self.bot_key,
                    (payload or {}).get("task_id", ""),
                )

        return {"on_interaction_request": on_interaction_request}

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
    def _is_quote_hint(cls, value: str) -> bool:
        normalized = str(value or "").strip().lower()
        return bool(normalized) and any(token in normalized for token in _QUOTE_HINT_TOKENS)

    @classmethod
    def _extract_text_fragments_from_node(cls, node, depth: int = 0) -> list[str]:
        if depth > 4 or node is None:
            return []
        if isinstance(node, str):
            cleaned = cls._clean_message_fragment(node)
            return [cleaned] if cleaned else []
        if isinstance(node, list):
            fragments: list[str] = []
            for item in node[:10]:
                fragments.extend(cls._extract_text_fragments_from_node(item, depth + 1))
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
                cleaned = cls._clean_message_fragment(value)
                if cleaned:
                    fragments.append(cleaned)
            elif isinstance(value, (dict, list)):
                fragments.extend(cls._extract_text_fragments_from_node(value, depth + 1))

        for key, value in node.items():
            normalized_key = str(key or "").strip().lower()
            if normalized_key in priority_keys or normalized_key in skip_nested_keys:
                continue
            if isinstance(value, (dict, list)):
                fragments.extend(cls._extract_text_fragments_from_node(value, depth + 1))

        deduped: list[str] = []
        seen: set[str] = set()
        for fragment in fragments:
            normalized = fragment.strip()
            if not normalized or normalized in seen:
                continue
            deduped.append(normalized)
            seen.add(normalized)
            if len(deduped) >= 3:
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
    def _compose_stream_content(reply_state: dict, content: str) -> str:
        prefix = ((reply_state or {}).get("prefix") or "").strip()
        if prefix and content:
            return f"{prefix}\n\n{content}"
        return prefix or content

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
        command_content = self._resolve_control_command_content(content, quote_context)
        normalized = command_content.strip().lower()
        log_context = self._build_log_context(body, chattype, "text")
        runtime_session_key = self._resolve_runtime_session_key(user_id, session_key, log_context)

        if normalized in ("reset", "new", "clear", "重置", "清空"):
            await self.session_manager.clear_session(self.bot_key, runtime_session_key)
            await self.orchestrator.clear_session(runtime_session_key)
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
                await self._reply_text(req_id, "⏹ 已停止当前任务。", finish=True)
            else:
                await self._reply_text(req_id, "当前没有正在运行的任务。", finish=True)
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

        if (
            not self.orchestrator.has_pending_interaction(runtime_session_key)
            and self._is_help_menu_trigger(content)
            and await self._reply_help_menu_card(req_id)
        ):
            return

        control_reply = await self.orchestrator.handle_control_command(
            user_id=user_id,
            content=command_content,
            session_key=session_key,
            log_context=log_context,
        )
        if control_reply:
            await self._reply_text(req_id, control_reply, finish=True)
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
        reply_state = {"req_id": req_id, "stream_id": stream_id, "prefix": ""}
        on_stream_delta = self._make_stream_delta_callback(reply_state)

        await self._run_with_task_registry(
            req_id,
            stream_id,
            runtime_session_key,
            self.orchestrator.handle_text_message(
                user_id=user_id,
                message=self._compose_message_with_quote(content, quote_context),
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

        log_context = self._build_log_context(body, chattype, "image")
        runtime_session_key = self._resolve_runtime_session_key(user_id, session_key, log_context)
        if self.orchestrator.has_pending_interaction(runtime_session_key):
            await self._reply_text(req_id, self._pending_interaction_notice(), finish=True)
            return

        try:
            data_uri = await ImageUtils.download_and_decrypt_to_base64(image_url, aeskey)
            content_blocks = [
                {"type": "text", "text": "[用户发送了一张图片] 请描述或分析这张图片"},
                {"type": "image_url", "image_url": {"url": data_uri}},
            ]
        except Exception as e:
            logger.error("[Dispatcher:%s] 图片下载解密失败: %s", self.bot_key, e)
            await self._reply_text(req_id, "图片处理失败，请重试。", finish=True)
            return

        stream_id = uuid.uuid4().hex[:12]
        reply_state = {"req_id": req_id, "stream_id": stream_id, "prefix": ""}
        on_stream_delta = self._make_stream_delta_callback(reply_state)

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

        log_context = self._build_log_context(body, chattype, "file")
        runtime_session_key = self._resolve_runtime_session_key(user_id, session_key, log_context)
        if self.orchestrator.has_pending_interaction(runtime_session_key):
            await self._reply_text(req_id, self._pending_interaction_notice(), finish=True)
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
        message = f"[用户发送了文件: {file_name}] 请分析这个文件的内容。"
        log_context["file_info"] = [{"filename": file_name}]
        reply_state = {"req_id": req_id, "stream_id": stream_id, "prefix": ""}
        on_stream_delta = self._make_stream_delta_callback(reply_state)

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

        log_context = self._build_log_context(body, chattype, "mixed")
        runtime_session_key = self._resolve_runtime_session_key(user_id, session_key, log_context)
        if self.orchestrator.has_pending_interaction(runtime_session_key):
            await self._reply_text(req_id, self._pending_interaction_notice(), finish=True)
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

        if quote_context:
            content_blocks.insert(0, {"type": "text", "text": f"【引用消息】\n{quote_context}"})

        stream_id = uuid.uuid4().hex[:12]
        reply_state = {"req_id": req_id, "stream_id": stream_id, "prefix": ""}
        on_stream_delta = self._make_stream_delta_callback(reply_state)

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
        await self.ws.send_reply(payload)

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

    def _make_stream_delta_callback(self, reply_state: dict):
        """创建带节流的on_stream_delta回调"""
        state = {
            'last_pushed_text': "",
            'last_push_time': 0.0,
            'throttle_task': None,
        }
        push_lock = asyncio.Lock()

        async def on_stream_delta(accumulated_text: str, finish: bool):
            if finish:
                # 完成时立即推送最终内容
                if state['throttle_task'] and not state['throttle_task'].done():
                    state['throttle_task'].cancel()
                target_req_id = (reply_state or {}).get("req_id", "")
                target_stream_id = (reply_state or {}).get("stream_id", "")
                if not target_req_id or not target_stream_id:
                    logger.warning("[Dispatcher:%s] 缺少流式回复目标，跳过最终推送", self.bot_key)
                    return
                await self._reply_stream(
                    target_req_id,
                    target_stream_id,
                    self._compose_stream_content(reply_state, accumulated_text),
                    finish=True,
                )
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
                    await self._reply_stream(
                        target_req_id,
                        target_stream_id,
                        self._compose_stream_content(reply_state, accumulated_text),
                        finish=False,
                    )
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
                            await self._reply_stream(
                                target_req_id,
                                target_stream_id,
                                self._compose_stream_content(reply_state, captured_text),
                                finish=False,
                            )
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
            reply_state=reply_state or {"req_id": req_id, "stream_id": stream_id, "prefix": ""},
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
        await self._reply_stream(req_id, stream_id, content, finish)

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
        await self.ws.send_reply(payload)

    async def _reply_update_template_card(self, req_id: str, response_code: str, template_card: dict):
        payload = {
            "cmd": "aibot_respond_update_msg",
            "headers": {"req_id": req_id},
            "body": {
                "response_code": response_code,
                "template_card": template_card,
            },
        }
        await self.ws.send_reply(payload)

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
        await self.ws.send_reply(payload)

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
