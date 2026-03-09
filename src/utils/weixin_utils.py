#!/usr/bin/env python
# coding=utf-8
"""
企业微信机器人工具类
封装消息构造、模板卡片、流式消息等功能
"""

import json
import os
import logging
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class MessageBuilder:
    """消息构造工具类"""

    @staticmethod
    def text(stream_id: str, content: str, finish: bool = True) -> str:
        """
        构造文本消息

        Args:
            stream_id: 流式消息ID
            content: 消息内容（支持Markdown）
            finish: 是否结束流式消息

        Returns:
            JSON格式的消息字符串
        """
        plain = {
            "msgtype": "stream",
            "stream": {
                "id": stream_id,
                "finish": finish,
                "content": content
            }
        }
        return json.dumps(plain, ensure_ascii=False)

    @staticmethod
    def image(stream_id: str, image_base64: str, image_md5: str, content: str = "") -> str:
        """
        构造图片消息（流式消息+图片）

        Args:
            stream_id: 流式消息ID
            image_base64: 图片的base64编码
            image_md5: 图片的MD5值
            content: 可选的文本内容

        Returns:
            JSON格式的消息字符串
        """
        plain = {
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
                            "md5": image_md5
                        }
                    }
                ]
            }
        }
        return json.dumps(plain, ensure_ascii=False)

    @staticmethod
    def stream_with_card(
        stream_id: str,
        content: str,
        finish: bool = True,
        template_card: Optional[Dict] = None
    ) -> str:
        """
        构造流式消息+模板卡片

        Args:
            stream_id: 流式消息ID
            content: 流式消息内容
            finish: 是否结束流式消息
            template_card: 模板卡片内容（可选）

        Returns:
            JSON格式的消息字符串
        """
        plain = {
            "msgtype": "stream_with_template_card",
            "stream": {
                "id": stream_id,
                "finish": finish,
                "content": content
            }
        }

        # 只在提供了template_card时才添加
        if template_card:
            plain["template_card"] = template_card

        return json.dumps(plain, ensure_ascii=False)

    @staticmethod
    def template_card(template_card: Dict) -> str:
        """
        构造纯模板卡片消息

        Args:
            template_card: 模板卡片内容

        Returns:
            JSON格式的消息字符串
        """
        plain = {
            "msgtype": "template_card",
            "template_card": template_card
        }
        return json.dumps(plain, ensure_ascii=False)


class TemplateCardBuilder:
    """模板卡片构造工具类"""

    @staticmethod
    def text_notice(
        task_id: str,
        title: str,
        desc: str,
        icon_url: str = "",
        source_desc: str = "",
        emphasis_title: Optional[str] = None,
        emphasis_desc: Optional[str] = None,
        sub_title: Optional[str] = None,
        quote_area: Optional[Dict] = None,
        horizontal_content: Optional[List[Dict]] = None,
        jump_list: Optional[List[Dict]] = None,
        action_menu: Optional[Dict] = None
    ) -> Dict:
        """
        构造文本通知模板卡片

        Args:
            task_id: 卡片任务ID（用于更新卡片）
            title: 主标题
            desc: 主描述
            icon_url: 来源图标URL
            source_desc: 来源描述
            emphasis_title: 强调内容标题
            emphasis_desc: 强调内容描述
            sub_title: 副标题
            quote_area: 引用区域
            horizontal_content: 横向内容列表
            jump_list: 跳转列表
            action_menu: 操作菜单

        Returns:
            模板卡片字典
        """
        card = {
            "card_type": "text_notice",
            "source": {
                "icon_url": icon_url,
                "desc": source_desc
            },
            "main_title": {
                "title": title,
                "desc": desc
            },
            "task_id": task_id
        }

        if emphasis_title and emphasis_desc:
            card["emphasis_content"] = {
                "title": emphasis_title,
                "desc": emphasis_desc
            }

        if sub_title:
            card["sub_title_text"] = sub_title

        if quote_area:
            card["quote_area"] = quote_area

        if horizontal_content:
            card["horizontal_content_list"] = horizontal_content

        if jump_list:
            card["jump_list"] = jump_list

        if action_menu:
            card["action_menu"] = action_menu

        return card

    @staticmethod
    def news_notice(
        task_id: str,
        title: str,
        desc: str,
        image_url: str,
        icon_url: str = "",
        source_desc: str = "",
        aspect_ratio: float = 1.3,
        image_text_area: Optional[Dict] = None,
        vertical_content: Optional[List[Dict]] = None,
        horizontal_content: Optional[List[Dict]] = None,
        jump_list: Optional[List[Dict]] = None,
        action_menu: Optional[Dict] = None,
        card_action: Optional[Dict] = None
    ) -> Dict:
        """
        构造图文展示模板卡片

        Args:
            task_id: 卡片任务ID
            title: 主标题
            desc: 主描述
            image_url: 卡片图片URL
            icon_url: 来源图标URL
            source_desc: 来源描述
            aspect_ratio: 图片宽高比（1.3-2.25）
            image_text_area: 左图右文区域
            vertical_content: 垂直内容列表
            horizontal_content: 横向内容列表
            jump_list: 跳转列表
            action_menu: 操作菜单
            card_action: 卡片点击行为

        Returns:
            模板卡片字典
        """
        card = {
            "card_type": "news_notice",
            "source": {
                "icon_url": icon_url,
                "desc": source_desc
            },
            "main_title": {
                "title": title,
                "desc": desc
            },
            "card_image": {
                "url": image_url,
                "aspect_ratio": aspect_ratio
            },
            "task_id": task_id
        }

        if image_text_area:
            card["image_text_area"] = image_text_area

        if vertical_content:
            card["vertical_content_list"] = vertical_content

        if horizontal_content:
            card["horizontal_content_list"] = horizontal_content

        if jump_list:
            card["jump_list"] = jump_list

        if action_menu:
            card["action_menu"] = action_menu

        if card_action:
            card["card_action"] = card_action

        return card

    @staticmethod
    def button_interaction(
        task_id: str,
        title: str,
        desc: str,
        button_list: List[Dict],
        icon_url: str = "",
        source_desc: str = "",
        button_selection: Optional[Dict] = None,
        sub_title: Optional[str] = None,
        quote_area: Optional[Dict] = None,
        horizontal_content: Optional[List[Dict]] = None,
        action_menu: Optional[Dict] = None
    ) -> Dict:
        """
        构造按钮交互模板卡片

        Args:
            task_id: 卡片任务ID
            title: 主标题
            desc: 主描述
            button_list: 按钮列表
            icon_url: 来源图标URL
            source_desc: 来源描述
            button_selection: 下拉选择器
            sub_title: 副标题
            quote_area: 引用区域
            horizontal_content: 横向内容列表
            action_menu: 操作菜单

        Returns:
            模板卡片字典
        """
        card = {
            "card_type": "button_interaction",
            "source": {
                "icon_url": icon_url,
                "desc": source_desc
            },
            "main_title": {
                "title": title,
                "desc": desc
            },
            "button_list": button_list,
            "task_id": task_id
        }

        if button_selection:
            card["button_selection"] = button_selection

        if sub_title:
            card["sub_title_text"] = sub_title

        if quote_area:
            card["quote_area"] = quote_area

        if horizontal_content:
            card["horizontal_content_list"] = horizontal_content

        if action_menu:
            card["action_menu"] = action_menu

        return card

    @staticmethod
    def vote_interaction(
        task_id: str,
        title: str,
        desc: str,
        option_list: List[Dict],
        submit_button_text: str = "提交",
        submit_button_key: str = "submit",
        icon_url: str = "",
        source_desc: str = "",
        question_key: str = "question",
        mode: int = 1
    ) -> Dict:
        """
        构造投票选择模板卡片

        Args:
            task_id: 卡片任务ID
            title: 主标题
            desc: 主描述
            option_list: 选项列表
            submit_button_text: 提交按钮文本
            submit_button_key: 提交按钮key
            icon_url: 来源图标URL
            source_desc: 来源描述
            question_key: 问题key
            mode: 选择模式（0单选，1多选）

        Returns:
            模板卡片字典
        """
        card = {
            "card_type": "vote_interaction",
            "source": {
                "icon_url": icon_url,
                "desc": source_desc
            },
            "main_title": {
                "title": title,
                "desc": desc
            },
            "checkbox": {
                "question_key": question_key,
                "option_list": option_list,
                "mode": mode,
                "disable": False
            },
            "submit_button": {
                "text": submit_button_text,
                "key": submit_button_key
            },
            "task_id": task_id
        }

        return card

    @staticmethod
    def multiple_interaction(
        task_id: str,
        title: str,
        desc: str,
        select_list: List[Dict],
        submit_button_text: str = "提交",
        submit_button_key: str = "submit",
        icon_url: str = "",
        source_desc: str = ""
    ) -> Dict:
        """
        构造多项选择模板卡片

        Args:
            task_id: 卡片任务ID
            title: 主标题
            desc: 主描述
            select_list: 下拉选择列表
            submit_button_text: 提交按钮文本
            submit_button_key: 提交按钮key
            icon_url: 来源图标URL
            source_desc: 来源描述

        Returns:
            模板卡片字典
        """
        card = {
            "card_type": "multiple_interaction",
            "source": {
                "icon_url": icon_url,
                "desc": source_desc
            },
            "main_title": {
                "title": title,
                "desc": desc
            },
            "select_list": select_list,
            "submit_button": {
                "text": submit_button_text,
                "key": submit_button_key
            },
            "task_id": task_id
        }

        return card


class ProactiveReplyClient:
    """企业微信主动回复消息客户端

    通过 response_url 向用户推送消息。
    API 文档: https://developer.work.weixin.qq.com/document/path/101138

    限制:
    - response_url 有效期 1 小时
    - 每个 response_url 仅可调用 1 次
    - 群聊中主动回复会自动引用原消息
    """

    @staticmethod
    async def send_markdown(response_url: str, content: str) -> bool:
        """发送 markdown 消息到 response_url

        Args:
            response_url: 企业微信回调中的 response_url
            content: markdown 格式的消息内容（最大 20480 字节）

        Returns:
            bool: 发送成功返回 True，失败返回 False
        """
        import aiohttp

        # 截断到 20480 字节（API 限制）
        content_bytes = content.encode('utf-8')
        if len(content_bytes) > 20480:
            content = content_bytes[:20480].decode('utf-8', errors='ignore')
            logger.warning("[主动回复] 内容超过20480字节，已截断")

        payload = {
            "msgtype": "markdown",
            "markdown": {
                "content": content
            }
        }

        try:
            client_timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=client_timeout) as session:
                async with session.post(
                    response_url,
                    json=payload,
                    headers={"Content-Type": "application/json"}
                ) as resp:
                    resp_data = await resp.json()
                    errcode = resp_data.get('errcode', -1)
                    if errcode == 0:
                        logger.info("[主动回复] 发送成功: content_len=%d", len(content))
                        return True
                    else:
                        logger.error(
                            "[主动回复] 发送失败: errcode=%s, errmsg=%s",
                            errcode, resp_data.get('errmsg', '')
                        )
                        return False
        except Exception as e:
            logger.error("[主动回复] 请求异常: %s", e)
            return False


class FileUtils:
    """文件处理工具类

    支持企业微信加密文件的下载解密，复用 ImageUtils 相同的 AES-256-CBC 解密逻辑。
    用于正向文件传输：用户发文件 -> 下载解密 -> 发给 clawrelay。
    """

    ALLOWED_EXTENSIONS = {
        '.txt', '.md', '.csv', '.json', '.xml', '.yaml', '.yml',
        '.py', '.js', '.ts', '.go', '.java', '.html', '.css', '.sql', '.sh', '.log',
        '.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx',
        '.zip',
    }
    MAX_FILE_SIZE = int(os.getenv('WEIXIN_MAX_FILE_SIZE', str(20 * 1024 * 1024)))

    @staticmethod
    async def download_and_decrypt(url: str, aes_key: str, timeout: int = 30, key_format: str = "auto") -> tuple:
        """下载企业微信加密文件并解密为原始字节

        Args:
            url: 企业微信文件下载 URL（加密的，5分钟内有效）
            aes_key: 消息中的 aeskey（用于 AES-256-CBC 解密）
            timeout: 下载超时时间（秒）
            key_format: 密钥格式（"base64"/"hex"/"auto"）

        Returns:
            (file_bytes, detected_filename): 解密后的字节数据和从响应头检测的文件名

        Raises:
            Exception: 下载失败、解密失败或文件超限时抛出
        """
        from Crypto.Cipher import AES
        import aiohttp
        import re

        # 1. 异步下载加密文件
        detected_filename = ''
        client_timeout = aiohttp.ClientTimeout(total=timeout)
        async with aiohttp.ClientSession(timeout=client_timeout) as session:
            async with session.get(url) as response:
                if response.status != 200:
                    raise Exception(f"下载文件失败，状态码: {response.status}")
                encrypted_data = await response.read()

                # 尝试从 Content-Disposition 提取文件名
                content_disp = response.headers.get('Content-Disposition', '')
                if content_disp:
                    m = re.search(r"filename\*=(?:UTF-8''|utf-8'')(.+?)(?:;|$)", content_disp)
                    if m:
                        from urllib.parse import unquote
                        detected_filename = unquote(m.group(1).strip())
                    else:
                        m = re.search(r'filename="?([^";]+)"?', content_disp)
                        if m:
                            detected_filename = m.group(1).strip()

        logger.info(
            f"[FileUtils] 下载加密文件完成: size={len(encrypted_data)} bytes, "
            f"detected_filename={detected_filename or '(none)'}"
        )

        # 2. AES-256-CBC 解密
        key = ImageUtils._resolve_aes_key(aes_key, key_format)
        iv = key[:16]
        cipher = AES.new(key, AES.MODE_CBC, iv)
        decrypted = cipher.decrypt(encrypted_data)

        # 3. 去除 PKCS#7 填充
        pad_len = decrypted[-1]
        if pad_len < 1 or pad_len > 32:
            raise Exception(f"PKCS#7 填充无效: pad_len={pad_len}")
        file_data = decrypted[:-pad_len]

        # 4. 校验文件大小
        if len(file_data) > FileUtils.MAX_FILE_SIZE:
            raise Exception(
                f"文件大小超限: {len(file_data)} bytes > {FileUtils.MAX_FILE_SIZE} bytes"
            )

        logger.info(f"[FileUtils] 文件解密成功: decrypted_size={len(file_data)} bytes")
        return file_data, detected_filename

    @staticmethod
    def detect_filename_from_bytes(file_data: bytes, fallback: str = "file.bin") -> str:
        """通过文件魔数检测文件类型并生成文件名"""
        signatures = [
            (b'%PDF', '.pdf'),
            (b'\xd0\xcf\x11\xe0', '.xls'),
            (b'PK\x03\x04', None),
            (b'\x89PNG', '.png'),
            (b'\xff\xd8\xff', '.jpg'),
        ]

        for sig, ext in signatures:
            if file_data[:len(sig)] == sig:
                if ext is not None:
                    return f"file{ext}"
                try:
                    import zipfile
                    import io
                    with zipfile.ZipFile(io.BytesIO(file_data)) as zf:
                        names = zf.namelist()
                        if any(n.startswith('xl/') for n in names):
                            return "file.xlsx"
                        elif any(n.startswith('word/') for n in names):
                            return "file.docx"
                        elif any(n.startswith('ppt/') for n in names):
                            return "file.pptx"
                        return "file.zip"
                except Exception:
                    return "file.zip"

        try:
            file_data[:1024].decode('utf-8')
            return "file.txt"
        except (UnicodeDecodeError, ValueError):
            pass

        return fallback

    @staticmethod
    def is_allowed(filename: str) -> bool:
        """检查文件扩展名是否在白名单中"""
        _, ext = os.path.splitext(filename.lower())
        return ext in FileUtils.ALLOWED_EXTENSIONS

    # 扩展名 -> MIME 类型映射
    EXT_TO_MIME = {
        '.txt': 'text/plain', '.md': 'text/markdown', '.csv': 'text/csv',
        '.json': 'application/json', '.xml': 'application/xml',
        '.yaml': 'application/x-yaml', '.yml': 'application/x-yaml',
        '.py': 'text/x-python', '.js': 'text/javascript', '.ts': 'text/typescript',
        '.go': 'text/x-go', '.java': 'text/x-java', '.html': 'text/html',
        '.css': 'text/css', '.sql': 'text/x-sql', '.sh': 'text/x-shellscript',
        '.log': 'text/plain',
        '.pdf': 'application/pdf',
        '.doc': 'application/msword',
        '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        '.xls': 'application/vnd.ms-excel',
        '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        '.ppt': 'application/vnd.ms-powerpoint',
        '.pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
        '.zip': 'application/zip',
    }

    @staticmethod
    def encode_for_relay(file_bytes: bytes, filename: str) -> dict:
        """编码为 OpenAI content part 格式（file_url 类型）

        Args:
            file_bytes: 文件原始字节
            filename: 文件名

        Returns:
            dict: {"type": "file_url", "file_url": {"url": "data:mime;base64,...", "filename": "..."}}
        """
        import base64
        _, ext = os.path.splitext(filename.lower())
        mime = FileUtils.EXT_TO_MIME.get(ext, 'application/octet-stream')
        b64 = base64.b64encode(file_bytes).decode('utf-8')
        return {
            "type": "file_url",
            "file_url": {
                "url": f"data:{mime};base64,{b64}",
                "filename": filename,
            },
        }


class ImageUtils:
    """图片处理工具类"""

    @staticmethod
    def _resolve_aes_key(key_str: str, key_format: str = "base64") -> bytes:
        """将密钥字符串转换为 32 字节 AES 密钥

        Args:
            key_str: 密钥字符串
            key_format: 密钥格式
                - "base64": Webhook 模式的 EncodingAESKey（43字符 Base64）
                - "hex": WebSocket 模式的独立 aeskey（64字符 hex）
                - "auto": 自动检测格式（优先尝试 hex，失败则 base64）

        Returns:
            32 字节的 AES 密钥
        """
        import base64

        if key_format == "auto":
            try:
                key = bytes.fromhex(key_str)
                if len(key) == 32:
                    return key
            except ValueError:
                pass
            return base64.b64decode(key_str + "=")
        elif key_format == "hex":
            return bytes.fromhex(key_str)
        else:
            return base64.b64decode(key_str + "=")

    @staticmethod
    async def download_and_decrypt_to_base64(
        url: str,
        aes_key: str,
        timeout: int = 10,
        key_format: str = "auto",
    ) -> str:
        """下载企业微信加密图片并解密为 base64 data URI

        Args:
            url: 企业微信图片下载 URL
            aes_key: 消息中的 aeskey（用于 AES-256-CBC 解密）
            timeout: 下载超时时间（秒）
            key_format: 密钥格式（"base64"/"hex"/"auto"）

        Returns:
            data URI 字符串，如 "data:image/jpeg;base64,/9j/4AAQ..."

        Raises:
            Exception: 下载失败或解密失败时抛出
        """
        import base64
        from Crypto.Cipher import AES
        import aiohttp

        # 1. 异步下载加密图片
        client_timeout = aiohttp.ClientTimeout(total=timeout)
        async with aiohttp.ClientSession(timeout=client_timeout) as session:
            async with session.get(url) as response:
                if response.status != 200:
                    raise Exception(f"下载图片失败，状态码: {response.status}")
                encrypted_data = await response.read()

        logger.info(f"[ImageUtils] 下载加密图片完成: size={len(encrypted_data)} bytes")

        # 2. AES-256-CBC 解密
        key = ImageUtils._resolve_aes_key(aes_key, key_format)
        iv = key[:16]
        cipher = AES.new(key, AES.MODE_CBC, iv)
        decrypted = cipher.decrypt(encrypted_data)

        # 3. 去除 PKCS#7 填充
        pad_len = decrypted[-1]
        if pad_len < 1 or pad_len > 32:
            raise Exception(f"PKCS#7 填充无效: pad_len={pad_len}")
        image_data = decrypted[:-pad_len]

        # 4. 检测图片类型
        if image_data[:8] == b'\x89PNG\r\n\x1a\n':
            media_type = "image/png"
        elif image_data[:2] == b'\xff\xd8':
            media_type = "image/jpeg"
        elif image_data[:4] == b'GIF8':
            media_type = "image/gif"
        elif image_data[:4] == b'RIFF' and image_data[8:12] == b'WEBP':
            media_type = "image/webp"
        else:
            media_type = "image/jpeg"  # 默认 JPEG

        # 5. 编码为 data URI
        b64 = base64.b64encode(image_data).decode('utf-8')
        data_uri = f"data:{media_type};base64,{b64}"

        logger.info(
            f"[ImageUtils] 图片解密成功: type={media_type}, "
            f"decrypted_size={len(image_data)} bytes"
        )
        return data_uri

    @staticmethod
    def download_and_encode(url: str, timeout: int = 5) -> tuple[str, str]:
        """
        下载图片并转换为base64和MD5

        Args:
            url: 图片URL
            timeout: 超时时间（秒）

        Returns:
            (base64编码, MD5值) 元组

        Raises:
            Exception: 下载失败时抛出异常
        """
        import base64
        import hashlib
        import requests

        response = requests.get(url, timeout=timeout)
        if response.status_code != 200:
            raise Exception(f"下载图片失败，状态码: {response.status_code}")

        image_data = response.content
        image_base64 = base64.b64encode(image_data).decode('utf-8')
        image_md5 = hashlib.md5(image_data).hexdigest()

        return (image_base64, image_md5)

    @staticmethod
    def get_fallback_image() -> tuple[str, str]:
        """
        获取降级图片（1x1透明PNG）

        Returns:
            (base64编码, MD5值) 元组
        """
        # 1x1透明PNG
        fallback_base64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
        fallback_md5 = "68b329da9893e34099c7d8ad5cb9c940"
        return (fallback_base64, fallback_md5)
