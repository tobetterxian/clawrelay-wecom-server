"""
Canva 画册自动填充映射辅助工具

把项目内的需求文档、画册大纲和素材清单整理成适合 Canva Brand Template
Autofill API 的字段绑定计划。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any, Dict, List, Optional

from src.utils.brochure_asset_manifest import load_brochure_asset_manifest

DEFAULT_ASSET_SEARCH_DIRS = ("brochure/assets", "assets", "uploads", "images")
SUPPORTED_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}


@dataclass(frozen=True)
class CanvaAutofillPlan:
    design_title: str
    bindings: Dict[str, Dict[str, Any]]
    dataset_field_count: int
    text_field_count: int
    image_field_count: int


def build_canva_autofill_plan(
    workspace_path: str,
    dataset: Dict[str, Any],
    *,
    project_name: str = "",
    design_title: str = "",
    asset_manifest: Optional[Dict[str, Any]] = None,
) -> CanvaAutofillPlan:
    workspace_root = Path(workspace_path).expanduser().resolve()
    normalized_dataset = dataset if isinstance(dataset, dict) else {}
    context = _build_workspace_context(workspace_root, project_name=project_name, design_title=design_title)
    assets = _load_asset_candidates(workspace_root, asset_manifest=asset_manifest)

    bindings: Dict[str, Dict[str, Any]] = {}
    used_asset_paths: set[str] = set()
    text_field_count = 0
    image_field_count = 0

    for field_name, field_meta in normalized_dataset.items():
        field_type = str((field_meta or {}).get("type") or "").strip().lower()
        if field_type == "text":
            text_value = _select_text_for_field(str(field_name), context)
            if text_value:
                bindings[str(field_name)] = {
                    "type": "text",
                    "text": text_value,
                }
                text_field_count += 1
            continue
        if field_type == "image":
            asset = _select_asset_for_field(str(field_name), assets, used_asset_paths)
            if asset:
                bindings[str(field_name)] = {
                    "type": "image",
                    "source_file": asset.get("source_file", ""),
                    "tags": list(asset.get("tags") or []),
                    "notes": str(asset.get("notes") or "").strip(),
                }
                source_file = str(asset.get("source_file") or "").strip()
                if source_file:
                    used_asset_paths.add(source_file)
                image_field_count += 1

    return CanvaAutofillPlan(
        design_title=context["design_title"],
        bindings=bindings,
        dataset_field_count=len(normalized_dataset),
        text_field_count=text_field_count,
        image_field_count=image_field_count,
    )


def _build_workspace_context(
    workspace_root: Path,
    *,
    project_name: str = "",
    design_title: str = "",
) -> Dict[str, Any]:
    requirement_text = _read_first_existing_text(
        workspace_root,
        ("docs/requirements.md", "requirements.md"),
    )
    outline_text = _read_first_existing_text(
        workspace_root,
        ("docs/brochure-outline.md", "brochure-outline.md"),
    )
    combined_text = "\n\n".join(part for part in (outline_text, requirement_text) if part).strip()
    sections = _extract_sections(outline_text or requirement_text or combined_text)
    highlights = _extract_highlights(outline_text or requirement_text or combined_text)
    intro_paragraphs = _extract_paragraphs(requirement_text or combined_text)
    outline_paragraphs = _extract_paragraphs(outline_text)

    inferred_title = (
        str(design_title or "").strip()
        or _extract_first_heading(requirement_text)
        or _extract_first_heading(outline_text)
        or str(project_name or "").strip()
        or workspace_root.name
    )
    subtitle = (
        _first_non_empty(
            _truncate_paragraph(intro_paragraphs[0], 80) if intro_paragraphs else "",
            _truncate_paragraph(outline_paragraphs[0], 80) if outline_paragraphs else "",
            _truncate_paragraph(combined_text, 80),
        )
        or f"{inferred_title} 宣传资料"
    )
    summary = _first_non_empty(
        _truncate_paragraph(intro_paragraphs[0], 180) if intro_paragraphs else "",
        _truncate_paragraph(combined_text, 180),
    )
    detail_blocks = [_truncate_paragraph(section.get("body", ""), 320) for section in sections if section.get("body")]
    title_blocks = [str(section.get("title") or "").strip() for section in sections if section.get("title")]
    generic_blocks = [item for item in (detail_blocks + highlights + title_blocks) if item]
    if not generic_blocks and summary:
        generic_blocks = [summary]

    return {
        "design_title": inferred_title,
        "subtitle": subtitle,
        "summary": summary,
        "sections": sections,
        "section_titles": title_blocks,
        "section_bodies": detail_blocks,
        "highlights": highlights,
        "generic_blocks": generic_blocks,
        "contact_line": "欢迎联系获取完整方案、报价与落地支持。",
        "website_line": "更多资料可在正式官网、产品页或销售资料包中查看。",
        "counters": {},
    }


def _load_asset_candidates(
    workspace_root: Path,
    *,
    asset_manifest: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    manifest_payload = asset_manifest
    if manifest_payload is None:
        manifest_payload = load_brochure_asset_manifest(str(workspace_root))

    results: List[Dict[str, Any]] = []
    seen_paths: set[str] = set()
    manifest_assets = list((manifest_payload or {}).get("assets") or [])
    for item in manifest_assets:
        source_file = str((item or {}).get("source_file") or "").strip().replace("\\", "/")
        if not source_file or source_file in seen_paths:
            continue
        local_path = (workspace_root / source_file).resolve()
        if not local_path.exists() or not local_path.is_file():
            continue
        results.append(
            {
                "source_file": source_file,
                "tags": _normalize_token_list(list((item or {}).get("tags") or [])),
                "notes": str((item or {}).get("notes") or "").strip(),
            }
        )
        seen_paths.add(source_file)

    for relative_dir in DEFAULT_ASSET_SEARCH_DIRS:
        search_root = (workspace_root / relative_dir).resolve()
        if not search_root.exists() or not search_root.is_dir():
            continue
        try:
            search_root.relative_to(workspace_root)
        except ValueError:
            continue
        for path in sorted(search_root.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in SUPPORTED_IMAGE_EXTENSIONS:
                continue
            source_file = path.relative_to(workspace_root).as_posix()
            if source_file in seen_paths:
                continue
            results.append(
                {
                    "source_file": source_file,
                    "tags": _normalize_token_list(_tokenize(path.stem)),
                    "notes": "",
                }
            )
            seen_paths.add(source_file)

    return results


def _select_text_for_field(field_name: str, context: Dict[str, Any]) -> str:
    normalized = _normalize_key(field_name)
    counters = context["counters"]

    if any(token in normalized for token in ("title", "headline", "hero", "cover", "name")):
        if any(token in normalized for token in ("page", "section", "chapter", "card", "slide")):
            return _cycle_text(context["section_titles"], counters, "section_title", fallback=context["design_title"])
        return _truncate_paragraph(context["design_title"], 80)

    if any(token in normalized for token in ("subtitle", "tagline", "slogan", "subheading")):
        return _truncate_paragraph(context["subtitle"], 120)

    if any(token in normalized for token in ("summary", "overview", "intro", "description", "desc")):
        return _truncate_paragraph(context["summary"], 220)

    if any(token in normalized for token in ("highlight", "feature", "benefit", "value", "selling", "advantage")):
        return _cycle_text(context["highlights"], counters, "highlight", fallback=context["summary"])

    if any(token in normalized for token in ("cta", "button", "action")):
        return "立即咨询，获取完整产品资料"

    if any(token in normalized for token in ("phone", "mobile", "contact", "wechat", "email")):
        return context["contact_line"]

    if any(token in normalized for token in ("website", "site", "url", "link")):
        return context["website_line"]

    if any(token in normalized for token in ("page", "section", "chapter", "slide", "card")):
        if any(token in normalized for token in ("body", "content", "copy", "text", "paragraph", "desc")):
            return _cycle_text(context["section_bodies"], counters, "section_body", fallback=context["summary"])
        return _cycle_text(context["section_titles"], counters, "section_title", fallback=context["design_title"])

    if any(token in normalized for token in ("body", "content", "copy", "text", "paragraph", "message")):
        return _cycle_text(context["generic_blocks"], counters, "generic", fallback=context["summary"])

    return _cycle_text(context["generic_blocks"], counters, "generic", fallback=context["summary"])


def _select_asset_for_field(
    field_name: str,
    assets: List[Dict[str, Any]],
    used_asset_paths: set[str],
) -> Optional[Dict[str, Any]]:
    if not assets:
        return None

    normalized_field = _normalize_key(field_name)
    best_asset: Optional[Dict[str, Any]] = None
    best_score = -1
    fallback_asset: Optional[Dict[str, Any]] = None

    for asset in assets:
        source_file = str(asset.get("source_file") or "").strip()
        if not source_file:
            continue
        if fallback_asset is None and source_file not in used_asset_paths:
            fallback_asset = asset

        tags = _normalize_token_list(list(asset.get("tags") or []))
        tags.extend(_tokenize(source_file))
        notes = str(asset.get("notes") or "").strip()
        tags.extend(_tokenize(notes))
        score = _score_asset_match(normalized_field, tags)
        if source_file in used_asset_paths:
            score -= 1
        if score > best_score:
            best_score = score
            best_asset = asset

    if best_asset and best_score > 0:
        return best_asset
    return fallback_asset or best_asset


def _score_asset_match(field_name: str, tags: List[str]) -> int:
    score = 0
    normalized_tags = set(_normalize_token_list(tags))

    def has_any(*tokens: str) -> bool:
        return any(token in field_name for token in tokens)

    if has_any("cover", "hero", "banner"):
        score += 3 if {"cover", "hero", "banner"} & normalized_tags else 0
    if has_any("logo", "brand"):
        score += 3 if {"logo", "brand"} & normalized_tags else 0
    if has_any("product", "device", "item", "solution"):
        score += 2 if {"product", "device", "solution", "detail"} & normalized_tags else 0
    if has_any("team", "founder", "person"):
        score += 2 if {"team", "founder", "portrait", "person"} & normalized_tags else 0
    if has_any("case", "customer", "client", "project"):
        score += 2 if {"case", "customer", "client", "project"} & normalized_tags else 0
    if has_any("scene", "usage", "application"):
        score += 2 if {"scene", "usage", "application"} & normalized_tags else 0

    field_tokens = set(_tokenize(field_name))
    score += len(field_tokens & normalized_tags)
    return score


def _read_first_existing_text(workspace_root: Path, relative_paths: tuple[str, ...]) -> str:
    for relative_path in relative_paths:
        candidate = (workspace_root / relative_path).resolve()
        try:
            candidate.relative_to(workspace_root)
        except ValueError:
            continue
        if candidate.exists() and candidate.is_file():
            try:
                return candidate.read_text(encoding="utf-8").strip()
            except Exception:
                continue
    return ""


def _extract_first_heading(text: str) -> str:
    for line in str(text or "").splitlines():
        match = re.match(r"^\s*#{1,6}\s+(.+?)\s*$", line)
        if match:
            return match.group(1).strip()
    return ""


def _extract_sections(text: str) -> List[Dict[str, str]]:
    lines = str(text or "").splitlines()
    if not lines:
        return []

    sections: List[Dict[str, str]] = []
    current_title = ""
    current_body_lines: List[str] = []

    def push_current() -> None:
        body = _clean_markdown_text("\n".join(current_body_lines))
        if current_title or body:
            sections.append(
                {
                    "title": _clean_markdown_text(current_title),
                    "body": body,
                }
            )

    for line in lines:
        heading_match = re.match(r"^\s*#{1,6}\s+(.+?)\s*$", line)
        if heading_match:
            if current_title or current_body_lines:
                push_current()
            current_title = heading_match.group(1).strip()
            current_body_lines = []
            continue
        current_body_lines.append(line)

    if current_title or current_body_lines:
        push_current()

    if sections:
        return [section for section in sections if section.get("title") or section.get("body")]

    paragraphs = _extract_paragraphs(text)
    return [
        {
            "title": f"第 {index + 1} 部分",
            "body": _truncate_paragraph(paragraph, 320),
        }
        for index, paragraph in enumerate(paragraphs[:8])
        if paragraph
    ]


def _extract_paragraphs(text: str) -> List[str]:
    cleaned = _clean_markdown_text(text)
    if not cleaned:
        return []
    parts = [part.strip() for part in re.split(r"\n{2,}", cleaned) if part.strip()]
    return parts


def _extract_highlights(text: str, max_items: int = 8) -> List[str]:
    highlights: List[str] = []
    for line in str(text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if re.match(r"^[-*+]\s+", stripped):
            cleaned = _truncate_paragraph(re.sub(r"^[-*+]\s+", "", stripped), 120)
            if cleaned:
                highlights.append(cleaned)
        if len(highlights) >= max_items:
            break

    if highlights:
        return highlights[:max_items]

    paragraphs = _extract_paragraphs(text)
    return [_truncate_paragraph(item, 120) for item in paragraphs[:max_items] if item]


def _clean_markdown_text(text: str) -> str:
    normalized_lines: List[str] = []
    for raw_line in str(text or "").splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            normalized_lines.append("")
            continue
        line = re.sub(r"^\s*#{1,6}\s*", "", line)
        line = re.sub(r"^\s*[-*+]\s+", "", line)
        line = re.sub(r"^\s*\d+[.)]\s+", "", line)
        line = line.replace("`", "")
        line = re.sub(r"\[(.*?)\]\((.*?)\)", r"\1", line)
        normalized_lines.append(line.strip())
    cleaned = "\n".join(normalized_lines)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _truncate_paragraph(text: str, limit: int) -> str:
    normalized = re.sub(r"\s+", " ", str(text or "").strip())
    if len(normalized) <= limit:
        return normalized
    shortened = normalized[: max(0, limit - 1)].rstrip(" ，,.;；。")
    return f"{shortened}…"


def _cycle_text(items: List[str], counters: Dict[str, int], key: str, *, fallback: str = "") -> str:
    valid_items = [str(item or "").strip() for item in items if str(item or "").strip()]
    if not valid_items:
        return str(fallback or "").strip()
    current_index = counters.get(key, 0)
    selected = valid_items[current_index % len(valid_items)]
    counters[key] = current_index + 1
    return selected


def _normalize_key(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower())
    return normalized.strip("_")


def _normalize_token_list(values: List[str]) -> List[str]:
    results: List[str] = []
    seen: set[str] = set()
    for value in values:
        for token in _tokenize(value):
            if token and token not in seen:
                seen.add(token)
                results.append(token)
    return results


def _tokenize(value: str) -> List[str]:
    normalized = re.sub(r"([a-z])([A-Z])", r"\1 \2", str(value or ""))
    parts = re.split(r"[^a-zA-Z0-9\u4e00-\u9fff]+", normalized.lower())
    return [part for part in parts if part]


def _first_non_empty(*values: str) -> str:
    for value in values:
        if str(value or "").strip():
            return str(value).strip()
    return ""
