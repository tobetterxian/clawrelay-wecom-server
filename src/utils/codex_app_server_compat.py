"""
Codex app-server 兼容检查辅助

用于：
1. 校验当前 codex 版本生成的 schema 是否包含本项目依赖的关键协议契约
2. 对比两个 schema 目录的差异，辅助升级前评估
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List


@dataclass(frozen=True)
class SchemaContractCheck:
    relative_path: str
    pattern: str
    description: str


REQUIRED_SCHEMA_CHECKS: tuple[SchemaContractCheck, ...] = (
    SchemaContractCheck(
        relative_path="v2/ThreadStartResponse.json",
        pattern='"model": {',
        description="thread/start 返回有效 model 字段",
    ),
    SchemaContractCheck(
        relative_path="v2/ThreadStartResponse.json",
        pattern='"reasoningEffort": {',
        description="thread/start 返回有效 reasoningEffort 字段",
    ),
    SchemaContractCheck(
        relative_path="v2/ThreadResumeResponse.json",
        pattern='"reasoningEffort": {',
        description="thread/resume 返回有效 reasoningEffort 字段",
    ),
    SchemaContractCheck(
        relative_path="v2/ThreadTokenUsageUpdatedNotification.json",
        pattern='"modelContextWindow": {',
        description="thread/tokenUsage/updated 包含 modelContextWindow",
    ),
    SchemaContractCheck(
        relative_path="v2/ThreadTokenUsageUpdatedNotification.json",
        pattern='"totalTokens": {',
        description="thread/tokenUsage/updated 包含 totalTokens",
    ),
    SchemaContractCheck(
        relative_path="ServerNotification.json",
        pattern='"thread/tokenUsage/updated"',
        description="存在 thread/tokenUsage/updated 通知",
    ),
    SchemaContractCheck(
        relative_path="ServerNotification.json",
        pattern='"thread/compacted"',
        description="存在 thread/compacted 通知兼容项",
    ),
    SchemaContractCheck(
        relative_path="v2/ItemStartedNotification.json",
        pattern='"contextCompaction"',
        description="存在 contextCompaction item 类型",
    ),
    SchemaContractCheck(
        relative_path="v2/ConfigReadResponse.json",
        pattern='"model_context_window": {',
        description="config/read 返回 model_context_window",
    ),
    SchemaContractCheck(
        relative_path="v2/ConfigReadResponse.json",
        pattern='"model_auto_compact_token_limit": {',
        description="config/read 返回 model_auto_compact_token_limit",
    ),
    SchemaContractCheck(
        relative_path="ClientRequest.json",
        pattern='"config/read"',
        description="客户端支持 config/read 方法",
    ),
)


def check_schema_contract(schema_dir: str | Path) -> List[str]:
    root = Path(schema_dir).expanduser().resolve()
    failures: List[str] = []
    for check in REQUIRED_SCHEMA_CHECKS:
        path = root / check.relative_path
        if not path.exists():
            failures.append(f"{check.description}：缺少文件 {check.relative_path}")
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            failures.append(f"{check.description}：读取 {check.relative_path} 失败（{exc}）")
            continue
        if check.pattern not in content:
            failures.append(f"{check.description}：{check.relative_path} 不包含 {check.pattern}")
    return failures


def diff_schema_dirs(
    baseline_dir: str | Path,
    candidate_dir: str | Path,
) -> List[str]:
    baseline_root = Path(baseline_dir).expanduser().resolve()
    candidate_root = Path(candidate_dir).expanduser().resolve()
    relative_paths = sorted(
        {
            str(path.relative_to(baseline_root))
            for path in baseline_root.rglob("*")
            if path.is_file()
        }
        | {
            str(path.relative_to(candidate_root))
            for path in candidate_root.rglob("*")
            if path.is_file()
        }
    )

    changed: List[str] = []
    for relative_path in relative_paths:
        baseline_path = baseline_root / relative_path
        candidate_path = candidate_root / relative_path
        if not baseline_path.exists() or not candidate_path.exists():
            changed.append(relative_path)
            continue
        try:
            baseline_content = baseline_path.read_text(encoding="utf-8")
            candidate_content = candidate_path.read_text(encoding="utf-8")
        except OSError:
            changed.append(relative_path)
            continue
        if baseline_content != candidate_content:
            changed.append(relative_path)
    return changed


def summarize_changed_files(changed_files: Iterable[str], limit: int = 20) -> List[str]:
    items = [str(item or "").strip() for item in changed_files if str(item or "").strip()]
    if len(items) <= limit:
        return items
    hidden = len(items) - limit
    return [*items[:limit], f"... 以及另外 {hidden} 个文件"]
