"""
产品画册生成辅助工具

把“生成画册 / 做产品画册”这类短请求改写成更明确的执行说明，
便于 Codex 在当前项目里稳定产出 HTML/H5 画册标准文件。
"""

import re

from src.utils.quoted_handoff import split_structured_user_message

GROUP_PROJECT_CONTEXT_HEADER = "【当前群项目上下文】\n"
REWRITTEN_QUOTE_HEADER = "【引用需求文档】\n"
BROCHURE_TASK_HEADER = "【画册生成任务】\n"
BROCHURE_OUTPUTS_HEADER = "【标准产物】\n"

BROCHURE_TRIGGER_RE = re.compile(
    r"^(?:(?:请|麻烦|帮我|就|那就|继续)\s*)?"
    r"(?:(?:根据|基于|按|按照|参考)(?:上面|上述|这个|该|引用的|当前)?"
    r"(?:需求|文档|PRD|说明|requirements(?:\.md)?|docs/requirements(?:\.md)?)?"
    r"(?:来|去)?\s*)?"
    r"(?:(?:开始|继续|直接|先)\s*)?"
    r"(?:(?:生成|做|制作|产出|输出|设计|落地)\s*)?"
    r"(?:(?:产品)?画册|宣传页|宣传册|brochure|h5画册|h5宣传页)"
    r"(?:吧|一下|下|起来)?$",
    re.IGNORECASE,
)

BROCHURE_HINT_KEYWORDS = (
    "画册",
    "宣传页",
    "宣传册",
    "brochure",
)


def looks_like_brochure_generation_request(current_message: str) -> bool:
    text = str(current_message or "").strip()
    if not text:
        return False
    if len(text) > 80:
        return False
    normalized = re.sub(r"\s+", "", text.lower())
    if BROCHURE_TRIGGER_RE.match(text):
        return True
    return normalized in {
        "生成画册",
        "开始生成画册",
        "做画册",
        "开始做画册",
        "生成产品画册",
        "开始做产品画册",
        "做产品画册",
        "生成宣传页",
        "开始生成宣传页",
    }


def looks_like_brochure_requirement_request(current_message: str) -> bool:
    normalized = re.sub(r"\s+", "", str(current_message or "").strip().lower())
    if not normalized:
        return False
    return any(keyword in normalized for keyword in BROCHURE_HINT_KEYWORDS)


def rewrite_brochure_generation_request(message: str) -> str:
    normalized_message = str(message or "").strip()
    if not normalized_message:
        return ""
    if BROCHURE_TASK_HEADER in normalized_message and BROCHURE_OUTPUTS_HEADER in normalized_message:
        return normalized_message

    group_project_context, quote_context, current_message = split_structured_user_message(normalized_message)
    effective_message = current_message or normalized_message
    if not looks_like_brochure_generation_request(effective_message):
        return normalized_message

    parts: list[str] = []
    if group_project_context:
        parts.append(f"{GROUP_PROJECT_CONTEXT_HEADER}{group_project_context}")
    if quote_context:
        parts.append(f"{REWRITTEN_QUOTE_HEADER}{quote_context}")
    parts.append(
        f"{BROCHURE_TASK_HEADER}"
        "用户希望你在当前项目中生成一版可预览的 HTML/H5 产品画册。\n"
        f"用户原话：{effective_message}"
    )
    parts.append(
        f"{BROCHURE_OUTPUTS_HEADER}"
        "- `docs/brochure-outline.md`：画册目录、每页目标与文案摘要。\n"
        "- `docs/image-prompts.md`：每页配图建议、出图提示词、缺失素材说明。\n"
        "- `docs/source-materials/`：用户上传的产品参数文档、说明书、表格等原始资料。\n"
        "- `docs/brochure-source-materials.json`：用户上传资料清单与路径索引。\n"
        "- `brochure/index.html`：可直接预览的 HTML 画册入口。\n"
        "- `brochure/styles.css`：画册样式文件。\n"
        "- `brochure/assets/`：图片、图标、占位素材。"
    )
    parts.append(
        "【执行要求】\n"
        "- 默认优先读取 `docs/requirements.md`；如果当前消息引用了需求文档，也要一并使用。\n"
        "- 如果存在 `docs/brochure-source-materials.json`，优先阅读其中记录的图片素材和参数文档，并按路径打开原文件。\n"
        "- 如果存在 `docs/brochure-assets.json`，优先使用其中的素材 URL、标签和说明来安排封面图与内页配图。\n"
        "- 先做可运行、可预览的 V1，不要只停留在策划说明。\n"
        "- 画册默认采用 HTML/H5 形式，兼顾移动端和 PC 端。\n"
        "- 如素材不足，可先用可替换占位内容，但要在 `docs/image-prompts.md` 中写清楚待补素材。\n"
        "- 如果需求存在关键缺失，只提出最少必要问题；否则直接开始产出文件。"
    )
    return "\n\n".join(parts).strip()
