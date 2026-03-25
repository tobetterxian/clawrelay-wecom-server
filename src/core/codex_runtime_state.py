from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class CodexRuntimePendingState:
    kind: str
    title: str = ""
    description: str = ""
    action_hint: str = ""


@dataclass
class CodexRuntimeState:
    lifecycle_line: str = "🤖 Codex 正在处理..."
    response_text: str = ""
    commentary_text: str = ""
    runtime_status_line: str = ""
    context_line: str = ""
    static_lines: list[str] = field(default_factory=list)
    notice_lines: dict[str, str] = field(default_factory=dict)
    detail_lines: list[str] = field(default_factory=list)
    pending: Optional[CodexRuntimePendingState] = None

    def add_static_line(self, content: str) -> None:
        normalized = str(content or "").strip()
        if normalized:
            self.static_lines.append(normalized)

    def set_runtime_status_line(self, content: str) -> None:
        self.runtime_status_line = str(content or "").strip()

    def set_context_line(self, content: str) -> None:
        self.context_line = str(content or "").strip()

    def upsert_notice(self, key: str, content: str) -> None:
        normalized_key = str(key or "").strip()
        normalized_content = str(content or "").strip()
        if not normalized_key:
            return
        if normalized_content:
            self.notice_lines[normalized_key] = normalized_content
            return
        self.notice_lines.pop(normalized_key, None)

    def append_detail_line(self, content: str) -> None:
        normalized_content = str(content or "").strip()
        if not normalized_content:
            return
        if self.detail_lines:
            last_line = str(self.detail_lines[-1] or "").strip()
            base_last, count_last = self._split_repeat_suffix(last_line)
            base_new, _count_new = self._split_repeat_suffix(normalized_content)
            if base_last == base_new:
                repeated_count = count_last + 1
                self.detail_lines[-1] = (
                    f"{base_new} ×{repeated_count}"
                    if repeated_count > 1
                    else base_new
                )
                return
        self.detail_lines.append(normalized_content)

    def append_commentary_text(self, content: str, *, is_new_message: bool = False) -> None:
        normalized = str(content or "")
        if not normalized:
            return
        if is_new_message and self.commentary_text.strip():
            self.commentary_text += "\n\n"
        self.commentary_text += normalized

    def clear_commentary_text(self) -> None:
        self.commentary_text = ""

    def append_response_text(self, content: str, *, is_new_message: bool = False) -> None:
        normalized = str(content or "")
        if not normalized:
            return
        if is_new_message and self.response_text.strip():
            self.response_text += "\n\n"
        self.response_text += normalized

    def set_response_text(self, content: str) -> None:
        self.response_text = str(content or "").strip()

    def visible_text(self, override_text: Optional[str] = None) -> str:
        if override_text is not None:
            return str(override_text or "")
        if self.response_text.strip():
            return self.response_text
        return self.commentary_text

    def set_pending(self, pending: Optional[CodexRuntimePendingState]) -> None:
        self.pending = pending

    def clear_pending(self) -> None:
        self.pending = None

    def render_lines(self) -> list[str]:
        lines: list[str] = [self.lifecycle_line]
        if self.runtime_status_line:
            lines.append(self.runtime_status_line)
        lines.extend(self.static_lines)
        lines.extend(self.notice_lines.values())
        if self.context_line:
            lines.append(self.context_line)
        lines.extend(self.detail_lines)
        return [str(item).strip() for item in lines if str(item or "").strip()]

    def to_registry_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {}
        visible_text = self.visible_text().strip()
        if visible_text:
            payload["runtime_visible_text"] = visible_text
        if self.response_text.strip():
            payload["runtime_response_text"] = self.response_text.strip()
        if self.commentary_text.strip():
            payload["runtime_commentary_text"] = self.commentary_text.strip()
        if self.detail_lines:
            payload["runtime_last_detail_line"] = str(self.detail_lines[-1] or "").strip()
        if self.runtime_status_line:
            payload["runtime_status_line"] = self.runtime_status_line
        if self.context_line:
            payload["runtime_context_line"] = self.context_line
        if self.pending:
            payload["runtime_pending_kind"] = self.pending.kind
            payload["runtime_pending_title"] = self.pending.title
            payload["runtime_pending_desc"] = self.pending.description
            payload["runtime_pending_action_hint"] = self.pending.action_hint
        else:
            payload["runtime_pending_kind"] = ""
            payload["runtime_pending_title"] = ""
            payload["runtime_pending_desc"] = ""
            payload["runtime_pending_action_hint"] = ""
        return payload

    @staticmethod
    def _split_repeat_suffix(line: str) -> tuple[str, int]:
        value = str(line or "").strip()
        marker = " ×"
        if marker not in value:
            return value, 1
        base, suffix = value.rsplit(marker, 1)
        try:
            count = max(int(suffix), 1)
        except ValueError:
            return value, 1
        return base.strip(), count
