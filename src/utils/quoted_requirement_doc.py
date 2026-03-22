"""
引用消息保存为需求文档的辅助工具

支持识别“引用一条消息后，发送保存/写入/生成需求文档”的命令，
默认把需求文档落到 `docs/requirements.md`。
"""

from dataclasses import dataclass
import re

from src.utils.brochure_generation import looks_like_brochure_requirement_request
from src.utils.quoted_handoff import split_structured_user_message

DEFAULT_REQUIREMENT_DOC_PATH = "docs/requirements.md"

_DEFAULT_TARGET_ALIASES = {
    "需求文档",
    "需求说明",
    "需求文件",
    "文档",
    "prd",
    "prd文档",
    "requirements",
    "requirements.md",
    "docs/requirements",
    "docs/requirements.md",
    "画册需求文档",
    "产品画册需求文档",
    "画册文档",
}
_SAVE_REQUIREMENT_DOC_COMMANDS = {
    "保存为需求文档",
    "保存成需求文档",
    "保存到需求文档",
    "写入需求文档",
    "写到需求文档",
    "生成需求文档",
    "整理成需求文档",
    "保存为文档",
    "写入文档",
    "生成文档",
    "保存为 prd",
    "保存为prd",
    "生成 prd",
    "生成prd",
    "保存为画册需求文档",
    "保存成画册需求文档",
    "写入画册需求文档",
    "生成画册需求文档",
    "保存为产品画册需求文档",
    "生成产品画册需求文档",
}
_SAVE_REQUIREMENT_DOC_RE = re.compile(
    r"^(?:(?:请|麻烦|帮我|把|将|就|那就)\s*)?"
    r"(?:(?:根据|基于|按|按照)(?:引用消息|引用内容|上面(?:的)?内容|上述内容)\s*)?"
    r"(?P<action>保存(?:为|成|到)?|写入|写到|生成|整理成|整理到|落到)\s*(?P<target>.+)?$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class QuotedRequirementDocRequest:
    target_path: str
    group_project_context: str
    quote_context: str
    current_message: str
    workflow: str = "generic"


def _normalize_target_path(value: str) -> str:
    target = str(value or "").strip().strip("`'\"")
    if not target:
        return DEFAULT_REQUIREMENT_DOC_PATH

    target = re.sub(r"^[：:，,、\s]+", "", target).strip()
    target = re.sub(r"^(?:到|为)\s+", "", target, flags=re.IGNORECASE).strip()
    lowered = target.lower()
    if lowered in _DEFAULT_TARGET_ALIASES:
        return DEFAULT_REQUIREMENT_DOC_PATH

    for alias in sorted(_DEFAULT_TARGET_ALIASES, key=len, reverse=True):
        prefix = f"{alias} "
        if lowered.startswith(prefix):
            target = target[len(prefix) :].strip()
            break

    if not target:
        return DEFAULT_REQUIREMENT_DOC_PATH

    normalized = target.replace("\\", "/").strip().lstrip("/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    if not normalized:
        return DEFAULT_REQUIREMENT_DOC_PATH

    filename = normalized.rsplit("/", 1)[-1]
    if "." not in filename:
        normalized = f"{normalized}.md"
    return normalized


def _parse_requirement_doc_target(command_text: str) -> str | None:
    normalized = re.sub(r"\s+", " ", str(command_text or "").strip())
    if not normalized:
        return None

    if normalized.lower() in _SAVE_REQUIREMENT_DOC_COMMANDS:
        return DEFAULT_REQUIREMENT_DOC_PATH

    match = _SAVE_REQUIREMENT_DOC_RE.match(normalized)
    if not match:
        return None
    return _normalize_target_path(match.group("target") or "")


def parse_quoted_requirement_doc_request(message: str) -> QuotedRequirementDocRequest | None:
    text = str(message or "").strip()
    if not text:
        return None

    group_project_context, quote_context, current_message = split_structured_user_message(text)
    command_text = current_message or text
    target_path = _parse_requirement_doc_target(command_text)
    if not target_path:
        return None

    return QuotedRequirementDocRequest(
        target_path=target_path,
        group_project_context=group_project_context,
        quote_context=quote_context,
        current_message=current_message.strip() or command_text.strip(),
        workflow="brochure" if looks_like_brochure_requirement_request(current_message or command_text) else "generic",
    )
