"""
工作区初始化模式定义

集中维护项目/工作区初始化模式、兼容别名与展示文案。
"""

from typing import Optional

WORKSPACE_INIT_EMPTY = "empty"
WORKSPACE_INIT_GIT_REMOTE = "git_remote"
WORKSPACE_INIT_LEGACY_COPY = "legacy_copy"
DEFAULT_WORKSPACE_INIT_MODE = WORKSPACE_INIT_EMPTY

WORKSPACE_INIT_MODE_ALIASES = {
    "": DEFAULT_WORKSPACE_INIT_MODE,
    "blank": WORKSPACE_INIT_EMPTY,
    "empty": WORKSPACE_INIT_EMPTY,
    "git": WORKSPACE_INIT_GIT_REMOTE,
    "git_remote": WORKSPACE_INIT_GIT_REMOTE,
    "remote": WORKSPACE_INIT_GIT_REMOTE,
    "repo": WORKSPACE_INIT_GIT_REMOTE,
    "legacy": WORKSPACE_INIT_LEGACY_COPY,
    "legacy_copy": WORKSPACE_INIT_LEGACY_COPY,
    "copy": WORKSPACE_INIT_LEGACY_COPY,
    "local_path": WORKSPACE_INIT_LEGACY_COPY,
}

WORKSPACE_INIT_MODE_LABELS = {
    WORKSPACE_INIT_EMPTY: "空工作区",
    WORKSPACE_INIT_GIT_REMOTE: "远程 Git 仓库",
    WORKSPACE_INIT_LEGACY_COPY: "兼容复制",
}


def normalize_workspace_init_mode(
    mode: str,
    fallback: str = DEFAULT_WORKSPACE_INIT_MODE,
) -> str:
    """标准化工作区初始化模式，兼容历史别名。"""
    normalized_fallback = fallback or DEFAULT_WORKSPACE_INIT_MODE
    key = str(mode or "").strip().lower()
    return WORKSPACE_INIT_MODE_ALIASES.get(key, normalized_fallback)


def infer_project_workspace_init_mode(
    project: Optional[dict],
    fallback: str = DEFAULT_WORKSPACE_INIT_MODE,
) -> str:
    """从项目元数据推断工作区初始化模式，兼容历史字段。"""
    if not project:
        return normalize_workspace_init_mode(fallback, fallback=fallback)

    explicit_mode = str(project.get("workspace_init_mode", "")).strip()
    if explicit_mode:
        return normalize_workspace_init_mode(explicit_mode, fallback=fallback)

    source_type = str(project.get("source_type", "")).strip().lower()
    if source_type in {"git", WORKSPACE_INIT_GIT_REMOTE} or project.get("git_remote_url"):
        return WORKSPACE_INIT_GIT_REMOTE
    if source_type in {"local_path", "copy", WORKSPACE_INIT_LEGACY_COPY}:
        return WORKSPACE_INIT_LEGACY_COPY

    source_path = str(project.get("source_path", "")).strip()
    if source_path and source_type != "empty":
        return WORKSPACE_INIT_LEGACY_COPY

    return normalize_workspace_init_mode(fallback, fallback=fallback)


def workspace_init_mode_label(mode: str) -> str:
    """返回工作区初始化模式的中文展示文案。"""
    normalized = normalize_workspace_init_mode(mode)
    return WORKSPACE_INIT_MODE_LABELS.get(normalized, normalized)


def project_source_summary(project: Optional[dict]) -> str:
    """返回项目初始化来源的可读描述。"""
    mode = infer_project_workspace_init_mode(project)
    if mode == WORKSPACE_INIT_GIT_REMOTE:
        return str((project or {}).get("git_remote_url", "")).strip() or "(未配置远程仓库)"
    if mode == WORKSPACE_INIT_LEGACY_COPY:
        return (
            str((project or {}).get("source_path", "")).strip()
            or str((project or {}).get("repo_path", "")).strip()
            or "(未配置复制源目录)"
        )
    return "(空工作区)"
