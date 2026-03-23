"""
画册机器人内部委托辅助工具

用于识别“做完整画册 / 生成并回传预览”这类请求，
并把它们拆成：
1. 画册机器人负责生成方案
2. 后台委托 Codex CLI 负责真正落地
"""

from __future__ import annotations

from dataclasses import dataclass
import re

from src.utils.quoted_handoff import split_structured_user_message

GROUP_PROJECT_CONTEXT_HEADER = "【当前群项目上下文】\n"
REWRITTEN_QUOTE_HEADER = "【引用资料】\n"
BROCHURE_DELEGATE_TASK_HEADER = "【画册落地前置任务】\n"

_BROCHURE_KEYWORDS = ("画册", "宣传页", "宣传册", "brochure", "h5画册", "h5宣传页")
_FULL_FLOW_TOKENS = ("完整", "全部", "全套", "一条龙", "做完", "全做完", "整套", "全流程")


@dataclass(frozen=True)
class BrochureDelegateRequest:
    mode: str
    planning_needed: bool
    final_control_command: str
    original_message: str
    current_message: str


def _normalize(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "").strip().lower())


def _contains_brochure_keyword(normalized: str) -> bool:
    return any(keyword in normalized for keyword in _BROCHURE_KEYWORDS)


def parse_brochure_delegate_request(message: str) -> BrochureDelegateRequest | None:
    normalized_message = str(message or "").strip()
    if not normalized_message:
        return None

    _group_project_context, _quote_context, current_message = split_structured_user_message(normalized_message)
    effective_message = current_message or normalized_message
    normalized = _normalize(effective_message)
    if not normalized or not _contains_brochure_keyword(normalized):
        return None

    direct_prefix_map = {
        "回传画册图片": ("回传画册图片", "发送画册图片", "回传画册预览图", "预览画册"),
        "导出画册PDF": ("导出画册pdf",),
        "导出画册PPT": ("导出画册ppt",),
        "发布画册": ("发布画册", "一键发布画册", "部署画册"),
    }
    for target_command, prefixes in direct_prefix_map.items():
        if any(normalized.startswith(prefix) for prefix in prefixes):
            return BrochureDelegateRequest(
                mode="direct_control",
                planning_needed=False,
                final_control_command=effective_message,
                original_message=normalized_message,
                current_message=effective_message,
            )

    explicit_full_flow = (
        normalized.startswith("生成画册并")
        or normalized.startswith("做画册并")
        or normalized.startswith("做完整画册")
        or normalized.startswith("生成完整画册")
        or normalized.startswith("完成画册")
    )
    contains_full_flow_token = any(token in normalized for token in _FULL_FLOW_TOKENS)
    if not explicit_full_flow and not contains_full_flow_token:
        return None

    final_control_command = "回传画册图片"
    if "导出画册ppt" in normalized or ("导出" in normalized and "ppt" in normalized):
        final_control_command = "导出画册PPT"
    elif "导出画册pdf" in normalized or ("导出" in normalized and "pdf" in normalized):
        final_control_command = "导出画册PDF"
    elif "发布画册" in normalized or "部署画册" in normalized:
        final_control_command = "发布画册"
    elif "预览" in normalized or "回传" in normalized or "图片" in normalized:
        final_control_command = "回传画册图片"

    return BrochureDelegateRequest(
        mode="full_flow",
        planning_needed=True,
        final_control_command=final_control_command,
        original_message=normalized_message,
        current_message=effective_message,
    )


def build_brochure_delegate_planning_prompt(message: str) -> str:
    normalized_message = str(message or "").strip()
    if not normalized_message:
        return ""

    group_project_context, quote_context, current_message = split_structured_user_message(normalized_message)
    effective_message = current_message or normalized_message

    parts: list[str] = []
    if group_project_context:
        parts.append(f"{GROUP_PROJECT_CONTEXT_HEADER}{group_project_context}")
    if quote_context:
        parts.append(f"{REWRITTEN_QUOTE_HEADER}{quote_context}")
    parts.append(
        f"{BROCHURE_DELEGATE_TASK_HEADER}"
        "用户希望你先产出一份可直接交给 Codex CLI 落地执行的产品画册需求文档。\n"
        "系统随后会自动把这份需求保存到当前项目，并生成 HTML/H5 画册。\n"
        f"用户原话：{effective_message}"
    )
    parts.append(
        "【输出要求】\n"
        "- 直接输出“可落地的画册需求文档正文”，不要输出寒暄、解释流程或额外前后缀。\n"
        "- 覆盖：画册定位、目标受众、目录结构、每页标题、每页核心文案、配图建议、风格建议。\n"
        "- 如果信息不足，可以做合理假设，但要写清楚待确认项。\n"
        "- 输出应适合后续直接保存为 `docs/requirements.md`。\n"
        "- 不要只给大纲，尽量给到每页可直接落地的文案摘要。"
    )
    return "\n\n".join(parts).strip()
