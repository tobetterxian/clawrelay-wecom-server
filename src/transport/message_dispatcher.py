"""
WebSocket消息分发器

接收WsClient分发的消息回调，路由到现有handler处理，
通过WebSocket推送流式回复（500ms节流）。
"""

import asyncio
import logging
import time
import uuid

from config.bot_config import BotConfig
from src.transport.ws_client import WsClient
from src.core.claude_relay_orchestrator import ClaudeRelayOrchestrator
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


def _friendly_error(e: Exception) -> str:
    """将内部异常转为用户友好的错误提示"""
    msg = str(e)
    if "[ClaudeRelay] Connection error" in msg:
        return _RELAY_CONNECTION_HINT
    if "[ClaudeRelay] HTTP" in msg:
        return _RELAY_HTTP_ERROR_HINT
    return f"抱歉，处理出错，请稍后重试。如问题持续，请联系管理员。"

# 节流间隔(秒)
STREAM_THROTTLE_INTERVAL = 0.5


class MessageDispatcher:
    """WebSocket消息分发与回复"""

    def __init__(self, ws_client: WsClient, bot_config: BotConfig):
        self.ws = ws_client
        self.config = bot_config
        self.bot_key = bot_config.bot_key

        # 命令路由器
        self.command_router = CommandRouter()

        # Claude Relay编排器
        self.orchestrator = ClaudeRelayOrchestrator(
            bot_key=bot_config.bot_key,
            relay_url=bot_config.relay_url or "http://localhost:50009",
            working_dir=bot_config.working_dir or "",
            model=bot_config.model or "",
            system_prompt=bot_config.system_prompt or "",
            env_vars=bot_config.env_vars or None,
        )

        # 会话管理
        self.session_manager = SessionManager()

        # 加载自定义命令
        self._load_custom_commands()

        # 机器人名称（用于过滤@提及）
        self.bot_name = bot_config.name or ""

        # 消息去重集合
        self._processed_msgids: dict[str, float] = {}

        logger.info("[Dispatcher:%s] 初始化完成", self.bot_key)

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
        content = body.get("text", {}).get("content", "").strip()
        if not content:
            return

        # 过滤@机器人名称前缀
        if self.bot_name and content.startswith(f"@{self.bot_name} "):
            content = content[len(f"@{self.bot_name} "):].strip()
        if self.bot_name and content.startswith(f"@{self.bot_name}"):
            content = content[len(f"@{self.bot_name}"):].strip()

        # 检查命令
        normalized = content.strip().lower()

        # 重置会话命令
        if normalized in ("reset", "new", "clear", "重置", "清空"):
            await self.session_manager.clear_session(self.bot_key, session_key)
            await self._reply_text(req_id, "会话已重置，可以开始新的对话。", finish=True)
            return

        # 停止任务命令
        import re
        stop_msg = re.sub(r'[^\w\u4e00-\u9fff]', '', normalized)
        if stop_msg in ("stop", "停止", "暂停", "停"):
            from src.core.task_registry import get_task_registry
            cancelled = get_task_registry().cancel(f"{self.bot_key}:{session_key}")
            if cancelled:
                await self._reply_text(req_id, "已停止当前任务。", finish=True)
            else:
                await self._reply_text(req_id, "当前没有正在运行的任务。", finish=True)
            return

        # 检查内置/自定义命令
        handler = self.command_router.handlers.get(content) or self.command_router.handlers.get(normalized)
        if handler:
            stream_id = uuid.uuid4().hex[:12]
            try:
                msg_json, _ = handler.handle(content, stream_id, user_id)
                # 命令处理器返回的是 MessageBuilder 格式的 JSON 字符串
                import json as _json
                msg_data = _json.loads(msg_json)
                # 提取文本内容通过流式回复发送
                if msg_data.get("msgtype") == "stream":
                    text_content = msg_data.get("stream", {}).get("content", "")
                elif msg_data.get("msgtype") == "template_card":
                    # 模板卡片暂不支持通过流式回复，回退为文本提示
                    text_content = "模板卡片命令暂不支持，请使用其他命令。"
                else:
                    text_content = str(msg_data)
                await self._reply_stream(req_id, stream_id, text_content, finish=True)
            except Exception as e:
                logger.error("[Dispatcher:%s] 命令处理失败: %s", self.bot_key, e, exc_info=True)
                await self._reply_text(req_id, f"命令处理出错：{e}", finish=True)
            return

        # 调用AI处理，带节流流式推送
        stream_id = uuid.uuid4().hex[:12]
        log_context = {
            'chat_type': chattype,
            'chat_id': body.get('chatid', ''),
            'message_type': 'text',
        }
        on_stream_delta = self._make_stream_delta_callback(req_id, stream_id)

        try:
            await self.orchestrator.handle_text_message(
                user_id=user_id,
                message=content,
                stream_id=stream_id,
                session_key=session_key,
                log_context=log_context,
                on_stream_delta=on_stream_delta,
            )
        except Exception as e:
            logger.error("[Dispatcher:%s] 处理文本消息失败: %s", self.bot_key, e, exc_info=True)
            await self._reply_stream(req_id, stream_id, _friendly_error(e), finish=True)

    async def _handle_image(self, req_id: str, body: dict, user_id: str, session_key: str, chattype: str):
        """处理图片消息"""
        image_info = body.get("image", {})
        image_url = image_info.get("url", "")
        aeskey = image_info.get("aeskey", "")

        if not image_url:
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
        log_context = {'chat_type': chattype, 'message_type': 'image'}
        on_stream_delta = self._make_stream_delta_callback(req_id, stream_id)

        try:
            await self.orchestrator.handle_multimodal_message(
                user_id=user_id,
                content_blocks=content_blocks,
                stream_id=stream_id,
                session_key=session_key,
                log_context=log_context,
                on_stream_delta=on_stream_delta,
            )
        except Exception as e:
            logger.error("[Dispatcher:%s] 处理图片消息失败: %s", self.bot_key, e, exc_info=True)
            await self._reply_stream(req_id, stream_id, _friendly_error(e), finish=True)

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
        log_context = {'chat_type': chattype, 'message_type': 'file', 'file_info': [{'filename': file_name}]}
        on_stream_delta = self._make_stream_delta_callback(req_id, stream_id)

        try:
            await self.orchestrator.handle_file_message(
                user_id=user_id,
                message=message,
                files=[file_data],
                stream_id=stream_id,
                session_key=session_key,
                log_context=log_context,
                on_stream_delta=on_stream_delta,
            )
        except Exception as e:
            logger.error("[Dispatcher:%s] 处理文件消息失败: %s", self.bot_key, e, exc_info=True)
            await self._reply_stream(req_id, stream_id, _friendly_error(e), finish=True)

    async def _handle_mixed(self, req_id: str, body: dict, user_id: str, session_key: str, chattype: str):
        """处理图文混排消息"""
        items = body.get("mixed", {}).get("msg_item", [])
        if not items:
            return

        content_blocks = []
        for item in items:
            item_type = item.get("msgtype", "")
            if item_type == "text":
                text = item.get("text", {}).get("content", "")
                if text:
                    content_blocks.append({"type": "text", "text": text})
            elif item_type == "image":
                image_url = item.get("image", {}).get("url", "")
                aeskey = item.get("image", {}).get("aeskey", "")
                if image_url:
                    try:
                        data_uri = await ImageUtils.download_and_decrypt_to_base64(image_url, aeskey)
                        content_blocks.append({"type": "image_url", "image_url": {"url": data_uri}})
                    except Exception as e:
                        logger.warning("[Dispatcher:%s] 混排图片解密失败: %s", self.bot_key, e)
                        content_blocks.append({"type": "text", "text": "[图片处理失败]"})

        if not content_blocks:
            return

        stream_id = uuid.uuid4().hex[:12]
        log_context = {'chat_type': chattype, 'message_type': 'mixed'}
        on_stream_delta = self._make_stream_delta_callback(req_id, stream_id)

        try:
            await self.orchestrator.handle_multimodal_message(
                user_id=user_id,
                content_blocks=content_blocks,
                stream_id=stream_id,
                session_key=session_key,
                log_context=log_context,
                on_stream_delta=on_stream_delta,
            )
        except Exception as e:
            logger.error("[Dispatcher:%s] 处理混排消息失败: %s", self.bot_key, e, exc_info=True)
            await self._reply_stream(req_id, stream_id, _friendly_error(e), finish=True)

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
        task_id = body.get("event", {}).get("task_id", "")

        if task_id.startswith("choice@"):
            logger.info("[Dispatcher:%s] 处理AskUserQuestion卡片点击: task_id=%s", self.bot_key, task_id)
            # TODO: 集成choice_manager处理逻辑
        else:
            logger.info("[Dispatcher:%s] 未知卡片事件: task_id=%s", self.bot_key, task_id)

    # ---- 流式推送 ----

    def _make_stream_delta_callback(self, req_id: str, stream_id: str):
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
                await self._reply_stream(req_id, stream_id, accumulated_text, finish=True)
                state['last_pushed_text'] = accumulated_text
                return

            # 节流
            now = time.monotonic()
            elapsed = now - state['last_push_time']

            if elapsed >= STREAM_THROTTLE_INTERVAL and accumulated_text != state['last_pushed_text']:
                async with push_lock:
                    await self._reply_stream(req_id, stream_id, accumulated_text, finish=False)
                    state['last_pushed_text'] = accumulated_text
                    state['last_push_time'] = time.monotonic()
            elif state['throttle_task'] is None or state['throttle_task'].done():
                captured_text = accumulated_text

                async def delayed_push():
                    await asyncio.sleep(STREAM_THROTTLE_INTERVAL - elapsed)
                    async with push_lock:
                        if captured_text != state['last_pushed_text']:
                            await self._reply_stream(req_id, stream_id, captured_text, finish=False)
                            state['last_pushed_text'] = captured_text
                            state['last_push_time'] = time.monotonic()

                state['throttle_task'] = asyncio.create_task(delayed_push())

        return on_stream_delta

    # ---- 回复辅助方法 ----

    async def _reply_text(self, req_id: str, content: str, finish: bool = True):
        """回复纯文本消息"""
        stream_id = uuid.uuid4().hex[:12]
        await self._reply_stream(req_id, stream_id, content, finish)

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
