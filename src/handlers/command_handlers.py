#!/usr/bin/env python
# coding=utf-8
"""
命令处理器模块
负责处理所有文本命令,并返回相应的消息内容
"""

import logging
from typing import Dict
from src.utils.weixin_utils import MessageBuilder, TemplateCardBuilder

logger = logging.getLogger(__name__)


class CommandHandler:
    """命令处理器基类"""

    def handle(self, cmd: str, stream_id: str, user_id: str) -> tuple[str, None]:
        """
        处理命令

        Args:
            cmd: 命令文本
            stream_id: 流式消息ID
            user_id: 用户ID

        Returns:
            (消息JSON字符串, None) 元组
        """
        raise NotImplementedError


class HelpCommandHandler(CommandHandler):
    """帮助命令处理器"""

    def handle(self, cmd: str, stream_id: str, user_id: str) -> tuple[str, None]:
        help_text = """🤖 **ClawRelay Bot - Demo Commands**

📝 **Basic:**
• `hello` - Greeting
• `文本` - Text reply (Markdown)

🔄 **Streaming:**
• `流式` - Stream output
• `流式+思考` - Stream + thinking
• `流式+图片` - Stream + image
• `流式+卡片` - Stream + template card

🎴 **Template Cards:**
• `文本卡片` / `图文卡片` / `按钮卡片` / `投票卡片` / `表单卡片`

📊 **Other:**
• `欢迎卡片` - Welcome card
• `数据展示` - Data display card

ℹ️ `help` / `帮助` / `?` / `？` - Show this help"""
        return MessageBuilder.text(stream_id, help_text, finish=True), None


class HelloCommandHandler(CommandHandler):
    """Hello命令处理器"""

    def handle(self, cmd: str, stream_id: str, user_id: str) -> tuple[str, None]:
        reply_content = f"欢迎你 {user_id}！很高兴为你服务。"
        return MessageBuilder.text(stream_id, reply_content, finish=True), None


class TextCommandHandler(CommandHandler):
    """文本命令处理器"""

    def handle(self, cmd: str, stream_id: str, user_id: str) -> tuple[str, None]:
        reply_content = "这是一个简单的文本回复示例。\n\n支持**Markdown**格式\n- 列表项1\n- 列表项2"
        return MessageBuilder.text(stream_id, reply_content, finish=True), None


class TextCardCommandHandler(CommandHandler):
    """文本卡片命令处理器"""

    def handle(self, cmd: str, stream_id: str, user_id: str) -> tuple[str, None]:
        template_card = TemplateCardBuilder.text_notice(
            task_id=f"text_notice_{stream_id}",
            title="📢 系统通知",
            desc="这是一个文本通知模板卡片",
            icon_url="",
            source_desc="企业微信",
            emphasis_title="99+",
            emphasis_desc="待办事项",
            sub_title="点击查看详情或使用右上角菜单进行更多操作",
            quote_area={
                "type": 1,
                "url": "https://work.weixin.qq.com",
                "title": "💬 相关引用",
                "quote_text": "用户反馈：这个功能非常实用！\n开发团队：感谢您的支持和建议。"
            },
            horizontal_content=[
                {"keyname": "通知时间", "value": "2025-10-10"},
                {"keyname": "负责人", "value": "张三", "type": 0},
                {"keyname": "查看详情", "value": "点击访问", "type": 1, "url": "https://work.weixin.qq.com"}
            ],
            jump_list=[
                {"type": 1, "url": "https://work.weixin.qq.com", "title": "🔗 查看详情"},
                {"type": 3, "title": "💬 智能问答", "question": "如何使用文本卡片"}
            ],
            action_menu={
                "desc": "更多操作",
                "action_list": [
                    {"text": "接收推送", "key": "action_receive"},
                    {"text": "不再推送", "key": "action_ignore"},
                    {"text": "设置提醒", "key": "action_remind"}
                ]
            }
        )
        return MessageBuilder.template_card(template_card), None


class NewsCardCommandHandler(CommandHandler):
    """图文卡片命令处理器"""

    def handle(self, cmd: str, stream_id: str, user_id: str) -> tuple[str, None]:
        template_card = TemplateCardBuilder.news_notice(
            task_id=f"news_notice_{stream_id}",
            title="🖼️ 图文展示",
            desc="这是一个图文展示卡片",
            image_url="",
            icon_url="",
            source_desc="企业微信",
            aspect_ratio=1.3,
            image_text_area={
                "type": 1,
                "url": "https://work.weixin.qq.com",
                "title": "左图右文展示区",
                "desc": "支持图文混排的展示方式",
                "image_url": ""
            },
            vertical_content=[
                {"title": "精彩内容", "desc": "点击图片查看更多详情"},
                {"title": "功能亮点", "desc": "支持多种展示样式和交互方式"}
            ],
            horizontal_content=[
                {"keyname": "发布时间", "value": "刚刚"},
                {"keyname": "阅读量", "value": "1.2万"}
            ],
            jump_list=[
                {"type": 1, "url": "https://work.weixin.qq.com", "title": "📖 阅读全文"},
                {"type": 3, "title": "❓ 相关问题", "question": "如何使用图文卡片"}
            ],
            action_menu={
                "desc": "卡片操作",
                "action_list": [
                    {"text": "收藏", "key": "action_favorite"},
                    {"text": "分享", "key": "action_share"}
                ]
            },
            card_action={"type": 1, "url": "https://work.weixin.qq.com"}
        )
        return MessageBuilder.template_card(template_card), None


class ButtonCardCommandHandler(CommandHandler):
    """按钮卡片命令处理器"""

    def handle(self, cmd: str, stream_id: str, user_id: str) -> tuple[str, None]:
        template_card = TemplateCardBuilder.button_interaction(
            task_id=f"button_interaction_{stream_id}",
            title="🔘 请选择操作",
            desc="点击下方按钮进行操作",
            button_list=[
                {"text": "✅ 确认", "style": 1, "key": "btn_confirm"},
                {"text": "❌ 取消", "style": 2, "key": "btn_cancel"},
                {"text": "💬 咨询", "style": 3, "key": "btn_ask"}
            ],
            icon_url="",
            source_desc="企业微信",
            button_selection={
                "question_key": "role_selection",
                "title": "您的身份",
                "disable": False,
                "option_list": [
                    {"id": "admin", "text": "管理员"},
                    {"id": "user", "text": "普通用户"},
                    {"id": "guest", "text": "访客"}
                ],
                "selected_id": "user"
            },
            sub_title="请仔细确认您的选择,提交后可能无法修改",
            quote_area={
                "type": 1,
                "url": "https://work.weixin.qq.com",
                "title": "📋 温馨提示",
                "quote_text": "选择您的身份后,点击确认按钮提交\n不同身份将获得不同的权限"
            },
            horizontal_content=[
                {"keyname": "当前状态", "value": "待选择"},
                {"keyname": "截止时间", "value": "今天 18:00"},
                {"keyname": "查看规则", "value": "点击查看", "type": 1, "url": "https://work.weixin.qq.com"}
            ],
            action_menu={
                "desc": "按钮操作",
                "action_list": [{"text": "帮助", "key": "action_help"}]
            }
        )
        return MessageBuilder.template_card(template_card), None


class VoteCardCommandHandler(CommandHandler):
    """投票卡片命令处理器"""

    def handle(self, cmd: str, stream_id: str, user_id: str) -> tuple[str, None]:
        template_card = TemplateCardBuilder.vote_interaction(
            task_id=f"vote_interaction_{stream_id}",
            title="📊 团建活动投票",
            desc="请选择您想参加的活动(可多选)",
            option_list=[
                {"id": "opt1", "text": "🏃 户外拓展", "is_checked": False},
                {"id": "opt2", "text": "🍴 聚餐", "is_checked": True},
                {"id": "opt3", "text": "🎬 看电影", "is_checked": False},
                {"id": "opt4", "text": "🎮 游戏竞赛", "is_checked": False}
            ],
            submit_button_text="提交投票",
            submit_button_key="submit_vote",
            question_key="activity_vote",
            mode=1  # 多选
        )
        return MessageBuilder.template_card(template_card), None


class FormCardCommandHandler(CommandHandler):
    """表单卡片命令处理器"""

    def handle(self, cmd: str, stream_id: str, user_id: str) -> tuple[str, None]:
        template_card = TemplateCardBuilder.multiple_interaction(
            task_id=f"multiple_interaction_{stream_id}",
            title="📝 信息收集表",
            desc="请填写以下信息",
            select_list=[
                {
                    "question_key": "department",
                    "title": "所在部门",
                    "disable": False,
                    "selected_id": "tech",
                    "option_list": [
                        {"id": "tech", "text": "技术部"},
                        {"id": "product", "text": "产品部"},
                        {"id": "operate", "text": "运营部"}
                    ]
                },
                {
                    "question_key": "experience",
                    "title": "工作年限",
                    "selected_id": "1-3",
                    "option_list": [
                        {"id": "1-3", "text": "1-3年"},
                        {"id": "3-5", "text": "3-5年"},
                        {"id": "5+", "text": "5年以上"}
                    ]
                }
            ],
            submit_button_text="提交",
            submit_button_key="submit_form"
        )
        return MessageBuilder.template_card(template_card), None


class WelcomeCardCommandHandler(CommandHandler):
    """欢迎卡片命令处理器"""

    def handle(self, cmd: str, stream_id: str, user_id: str) -> tuple[str, None]:
        user_name = user_id or "朋友"
        template_card = TemplateCardBuilder.text_notice(
            task_id=f"welcome_{stream_id}",
            title=f"👋 欢迎 {user_name}",
            desc="很高兴为您服务",
            sub_title="我是您的智能助手,随时为您效劳！",
            quote_area={
                "type": 1,
                "url": "https://work.weixin.qq.com",
                "title": "💡 快速开始",
                "quote_text": "发送 help 查看所有可用命令"
            },
            jump_list=[
                {"type": 3, "title": "📖 查看帮助", "question": "help"}
            ]
        )
        return MessageBuilder.template_card(template_card), None


class DataDisplayCommandHandler(CommandHandler):
    """数据展示命令处理器"""

    def handle(self, cmd: str, stream_id: str, user_id: str) -> tuple[str, None]:
        template_card = TemplateCardBuilder.text_notice(
            task_id=f"data_display_{stream_id}",
            title="📈 今日数据概览",
            desc="实时更新",
            emphasis_title="1,234",
            emphasis_desc="今日访问量",
            horizontal_content=[
                {"keyname": "新增用户", "value": "89人"},
                {"keyname": "活跃用户", "value": "567人"},
                {"keyname": "转化率", "value": "12.5%"},
                {"keyname": "数据更新", "value": "5分钟前"}
            ],
            jump_list=[
                {"type": 1, "url": "https://work.weixin.qq.com", "title": "查看详细报表"}
            ]
        )
        return MessageBuilder.template_card(template_card), None


class DefaultCommandHandler(CommandHandler):
    """默认命令处理器"""

    def handle(self, cmd: str, stream_id: str, user_id: str) -> tuple[str, None]:
        reply_content = f"Hello World! 我收到了你的消息: {cmd}\n\n💡 发送 **help** 查看所有测试命令"
        return MessageBuilder.text(stream_id, reply_content, finish=True), None


class CommandRouter:
    """命令路由器"""

    def __init__(self):
        # 命令映射表
        self.handlers: Dict[str, CommandHandler] = {
            "help": HelpCommandHandler(),
            "帮助": HelpCommandHandler(),
            "?": HelpCommandHandler(),
            "？": HelpCommandHandler(),
            "hello": HelloCommandHandler(),
            "文本": TextCommandHandler(),
            "文本卡片": TextCardCommandHandler(),
            "图文卡片": NewsCardCommandHandler(),
            "按钮卡片": ButtonCardCommandHandler(),
            "投票卡片": VoteCardCommandHandler(),
            "表单卡片": FormCardCommandHandler(),
            "欢迎卡片": WelcomeCardCommandHandler(),
            "数据展示": DataDisplayCommandHandler(),
        }
        self.default_handler = DefaultCommandHandler()

    def register(self, handler: CommandHandler):
        """注册自定义命令处理器

        Args:
            handler: 命令处理器实例，需要有 command 属性
        """
        cmd_name = getattr(handler, 'command', None)
        if cmd_name:
            self.handlers[cmd_name] = handler
            logger.info(f"注册自定义命令: {cmd_name} -> {handler.__class__.__name__}")
        else:
            logger.warning(f"命令处理器 {handler.__class__.__name__} 缺少 command 属性，跳过注册")

    def route(self, cmd: str, stream_id: str, user_id: str) -> tuple[str, None]:
        """
        路由命令到对应的处理器

        Args:
            cmd: 命令文本
            stream_id: 流式消息ID
            user_id: 用户ID

        Returns:
            (消息JSON字符串, None) 元组
        """
        import unicodedata
        import re

        # 先去除首尾空白
        cmd_stripped = cmd.strip()
        # 移除所有不可见字符（包括零宽字符、WORD JOINER等）
        # 只保留可见字符和常规空白符（空格、换行、制表符）
        cmd_cleaned = ''.join(
            c for c in cmd_stripped
            if unicodedata.category(c)[0] != 'C' or c in '\n\r\t '
        ).strip()

        # 尝试原始命令匹配（保留大小写）
        handler = self.handlers.get(cmd_cleaned)
        # 如果没匹配上,再尝试小写匹配
        if not handler:
            cmd_lower = cmd_cleaned.lower()
            handler = self.handlers.get(cmd_lower)
        # 如果还是没匹配上,使用默认处理器
        if not handler:
            handler = self.default_handler

        logger.info("路由命令 '%s' (清理后: '%s') 到处理器 %s", cmd, cmd_cleaned, handler.__class__.__name__)
        return handler.handle(cmd, stream_id, user_id)
