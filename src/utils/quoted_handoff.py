"""
引用消息接力辅助工具

支持把“引用需求文档 + 短开发指令”重写为更明确的执行请求。
"""

import re

QUOTED_DEVELOPMENT_TRIGGER_RE = re.compile(
    r"^(?:(?:请|就|那就|继续)?\s*)?"
    r"(?:(?:按|按照|根据|基于|参考|照着)(?:上面|上述|这个|该|引用的)?(?:需求|文档|PRD|说明)?(?:来|去)?\s*)?"
    r"(?:(?:开始|继续|直接|先)\s*)?"
    r"(?:开发|实现|编码|落地|做|写|推进)"
    r"(?:吧|一下|下|起来)?$",
    re.IGNORECASE,
)
QUOTED_SPLIT_CURRENT_MESSAGE = "\n\n【当前消息】\n"
QUOTED_SPLIT_QUOTE_MESSAGE = "\n\n【引用消息】\n"
GROUP_PROJECT_CONTEXT_HEADER = "【当前群项目上下文】\n"
QUOTE_MESSAGE_HEADER = "【引用消息】\n"
CURRENT_MESSAGE_HEADER = "【当前消息】\n"
REWRITTEN_QUOTE_HEADER = "【引用需求文档】\n"
REWRITTEN_INTENT_HEADER = "【当前用户意图】\n"


def split_structured_user_message(message: str) -> tuple[str, str, str]:
    text = str(message or "").strip()
    if not text:
        return "", "", ""

    group_project_context = ""
    quote_context = ""
    current_message = text

    if text.startswith(GROUP_PROJECT_CONTEXT_HEADER):
        remaining = text[len(GROUP_PROJECT_CONTEXT_HEADER) :]
        split_indexes = [
            index
            for index in (
                remaining.find(QUOTED_SPLIT_QUOTE_MESSAGE),
                remaining.find(QUOTED_SPLIT_CURRENT_MESSAGE),
            )
            if index >= 0
        ]
        if split_indexes:
            cut_index = min(split_indexes)
            group_project_context = remaining[:cut_index].strip()
            text = remaining[cut_index + 2 :].strip()
        else:
            group_project_context = remaining.strip()
            text = ""

    if text.startswith(QUOTE_MESSAGE_HEADER):
        remaining = text[len(QUOTE_MESSAGE_HEADER) :]
        if QUOTED_SPLIT_CURRENT_MESSAGE in remaining:
            quote_context, current_message = remaining.split(
                QUOTED_SPLIT_CURRENT_MESSAGE,
                1,
            )
        else:
            quote_context = remaining
            current_message = ""
    elif text.startswith(CURRENT_MESSAGE_HEADER):
        current_message = text[len(CURRENT_MESSAGE_HEADER) :]
    else:
        current_message = text

    return (
        group_project_context.strip(),
        quote_context.strip(),
        current_message.strip(),
    )


def looks_like_quoted_development_handoff(current_message: str, quote_context: str) -> bool:
    text = str(current_message or "").strip()
    quote = str(quote_context or "").strip()
    if not text or not quote:
        return False
    if len(text) > 32:
        return False
    normalized = re.sub(r"\s+", "", text.lower())
    if QUOTED_DEVELOPMENT_TRIGGER_RE.match(text):
        return True
    return normalized in {
        "开发",
        "开始开发",
        "继续开发",
        "实现",
        "开始实现",
        "继续实现",
        "编码",
        "开始编码",
        "继续编码",
        "落地",
        "开始做",
        "继续做",
        "按这个开发",
        "按这个做",
        "照这个做",
    }


def rewrite_quoted_development_request(message: str) -> str:
    normalized_message = str(message or "").strip()
    if not normalized_message:
        return ""
    if REWRITTEN_QUOTE_HEADER in normalized_message and REWRITTEN_INTENT_HEADER in normalized_message:
        return normalized_message

    group_project_context, quote_context, current_message = split_structured_user_message(normalized_message)
    if not looks_like_quoted_development_handoff(current_message, quote_context):
        return normalized_message

    parts: list[str] = []
    if group_project_context:
        parts.append(f"{GROUP_PROJECT_CONTEXT_HEADER}{group_project_context}")
    parts.append(f"{REWRITTEN_QUOTE_HEADER}{quote_context}")
    parts.append(
        f"{REWRITTEN_INTENT_HEADER}"
        "用户正在引用上面的需求文档，并要求你在当前项目中直接开始开发。\n"
        f"用户原话：{current_message}"
    )
    parts.append(
        "【执行要求】\n"
        "- 先基于引用的需求文档判断本轮最小可交付内容。\n"
        "- 如果需求已经足够明确：直接开始实现，不要只停留在复述需求。\n"
        "- 如果存在关键阻塞歧义：先提出最少必要的问题。\n"
        "- 默认继续在当前项目 / 当前工作区开发；如果无法直接修改文件，要明确说明限制。"
    )
    return "\n\n".join(parts).strip()
