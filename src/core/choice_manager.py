"""
选择会话管理器

管理 AskUserQuestion 交互流程的状态：问题列表、用户答案、会话恢复信息。
支持企业微信 vote_interaction 卡片的多问题逐题展示和答案收集。

工作流程：
1. Claude 触发 AskUserQuestion → 创建 ChoiceSession
2. 首题通过 vote 卡片展示给用户
3. 用户投票 → record_answer() 记录答案、推进索引
4. 全部答完 → format_answers() 格式化答案文本
5. 通过相同 session_id 恢复 Claude 会话

作者: Claude Code
日期: 2026-03-01
"""

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# 会话过期时间（30分钟）
CHOICE_SESSION_TIMEOUT = 1800


@dataclass
class ChoiceSession:
    """选择会话状态

    Attributes:
        questions: AskUserQuestion 的问题列表
        current_index: 当前问题索引
        answers: 已收集的答案 {index: answer_text}
        relay_session_id: clawrelay session_id（恢复会话用）
        response_url: 企业微信主动回复 URL
        accumulated_text: AskUserQuestion 之前累积的文本
        stream_id: 企业微信流式消息ID
        bot_key: 机器人标识
        user_id: 用户ID
        task_id_prefix: vote 卡片的 task_id 前缀
        session_key: SessionManager 使用的会话key
        relay_url: clawrelay-api 地址
        model: 模型标识
        working_dir: 工作目录
        system_prompt: 系统提示词
        env_vars: 传递给Claude子进程的环境变量
        created_at: 创建时间戳
    """
    questions: List[dict]
    current_index: int = 0
    answers: Dict[int, str] = field(default_factory=dict)
    is_submitted: bool = False  # 防止重复提交
    relay_session_id: str = ""
    response_url: str = ""
    accumulated_text: str = ""
    stream_id: str = ""
    bot_key: str = ""
    user_id: str = ""
    task_id_prefix: str = ""
    session_key: str = ""
    relay_url: str = ""
    model: str = ""
    working_dir: str = ""
    system_prompt: str = ""
    env_vars: Dict[str, str] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)


class ChoiceManager:
    """选择会话管理器（全局单例）

    按 {bot_key}:{user_id} 隔离会话，同一用户同时只能有一个活跃的选择会话。

    Example:
        >>> mgr = get_choice_manager()
        >>> mgr.create_session("bot1", "user1", questions=[...], ...)
        >>> mgr.has_pending_choice("bot1", "user1")
        True
        >>> result = mgr.record_answer("bot1", "user1", "Next.js")
        >>> if result["done"]:
        ...     text = mgr.format_answers("bot1", "user1")
    """

    def __init__(self):
        self._sessions: Dict[str, ChoiceSession] = {}
        self._lock = threading.Lock()
        logger.info("[ChoiceManager] 初始化完成")

    @staticmethod
    def _make_key(bot_key: str, user_id: str) -> str:
        return f"{bot_key}:{user_id}"

    def create_session(
        self,
        bot_key: str,
        user_id: str,
        questions: List[dict],
        relay_session_id: str = "",
        response_url: str = "",
        accumulated_text: str = "",
        stream_id: str = "",
        task_id_prefix: str = "",
        session_key: str = "",
        relay_url: str = "",
        model: str = "",
        working_dir: str = "",
        system_prompt: str = "",
        env_vars: dict = None,
    ) -> ChoiceSession:
        """创建选择会话

        Args:
            bot_key: 机器人标识
            user_id: 用户ID
            questions: AskUserQuestion 的问题列表
            relay_session_id: clawrelay session_id
            response_url: 企业微信主动回复 URL
            accumulated_text: 之前累积的文本
            stream_id: 流式消息ID
            task_id_prefix: vote 卡片的 task_id 前缀
            session_key: 会话key
            relay_url: clawrelay-api 地址
            model: 模型标识
            working_dir: 工作目录
            system_prompt: 系统提示词
            env_vars: 传递给Claude子进程的环境变量

        Returns:
            ChoiceSession: 新创建的会话
        """
        key = self._make_key(bot_key, user_id)
        session = ChoiceSession(
            questions=questions,
            relay_session_id=relay_session_id,
            response_url=response_url,
            accumulated_text=accumulated_text,
            stream_id=stream_id,
            bot_key=bot_key,
            user_id=user_id,
            task_id_prefix=task_id_prefix,
            session_key=session_key,
            relay_url=relay_url,
            model=model,
            working_dir=working_dir,
            system_prompt=system_prompt,
            env_vars=env_vars or {},
        )

        with self._lock:
            if key in self._sessions:
                logger.warning(f"[ChoiceManager] 覆盖已有会话: {key}")
            self._sessions[key] = session

        logger.info(
            f"[ChoiceManager] 创建选择会话: key={key}, "
            f"questions={len(questions)}, session_id={relay_session_id}"
        )
        return session

    def get_session(self, bot_key: str, user_id: str) -> Optional[ChoiceSession]:
        """获取选择会话（含过期检查）"""
        key = self._make_key(bot_key, user_id)
        with self._lock:
            session = self._sessions.get(key)
            if not session:
                return None
            # 过期检查
            if time.time() - session.created_at > CHOICE_SESSION_TIMEOUT:
                logger.warning(f"[ChoiceManager] 会话已过期: {key}")
                del self._sessions[key]
                return None
            return session

    def has_pending_choice(self, bot_key: str, user_id: str) -> bool:
        """快速检查是否有待处理的选择会话"""
        return self.get_session(bot_key, user_id) is not None

    def record_answer(
        self, bot_key: str, user_id: str, answer_text: str
    ) -> dict:
        """记录当前问题的答案并推进索引

        Args:
            bot_key: 机器人标识
            user_id: 用户ID
            answer_text: 用户的答案文本

        Returns:
            dict: {
                "done": bool,           # 是否所有问题都已回答
                "next_question": dict,   # 下一个问题（done=False 时）
                "next_index": int,       # 下一个问题索引
                "total": int,            # 总问题数
            }
        """
        key = self._make_key(bot_key, user_id)
        with self._lock:
            session = self._sessions.get(key)
            if not session:
                logger.warning(f"[ChoiceManager] 记录答案时会话不存在: {key}")
                return {"done": True, "next_question": None, "next_index": 0, "total": 0}

            # 记录当前答案
            session.answers[session.current_index] = answer_text
            session.current_index += 1

            total = len(session.questions)
            done = session.current_index >= total

            logger.info(
                f"[ChoiceManager] 记录答案: key={key}, "
                f"index={session.current_index - 1}/{total}, "
                f"answer={answer_text[:50]}, done={done}"
            )

            if done:
                return {
                    "done": True,
                    "next_question": None,
                    "next_index": session.current_index,
                    "total": total,
                }
            else:
                return {
                    "done": False,
                    "next_question": session.questions[session.current_index],
                    "next_index": session.current_index,
                    "total": total,
                }

    def format_answers(self, bot_key: str, user_id: str) -> str:
        """格式化所有答案为提交文本

        格式：
        [用户选择回答]
        1. Which framework? -> Next.js
        2. Which style? -> Tailwind CSS

        Returns:
            str: 格式化的答案文本
        """
        key = self._make_key(bot_key, user_id)
        with self._lock:
            session = self._sessions.get(key)
            if not session:
                return ""

            lines = ["[用户选择回答]"]
            for i, question in enumerate(session.questions):
                q_text = question.get("question", f"Question {i + 1}")
                answer = session.answers.get(i, "(未回答)")
                lines.append(f"{i + 1}. {q_text} -> {answer}")

            return "\n".join(lines)

    def mark_submitted(self, bot_key: str, user_id: str) -> bool:
        """原子性标记为已提交，防止重复提交

        Returns:
            bool: True=成功标记（首次提交），False=已被其他线程提交过
        """
        key = self._make_key(bot_key, user_id)
        with self._lock:
            session = self._sessions.get(key)
            if not session or session.is_submitted:
                return False
            session.is_submitted = True
            return True

    def remove_session(self, bot_key: str, user_id: str):
        """清理选择会话"""
        key = self._make_key(bot_key, user_id)
        with self._lock:
            if key in self._sessions:
                del self._sessions[key]
                logger.info(f"[ChoiceManager] 清理会话: {key}")


# 全局单例
_global_choice_manager: Optional[ChoiceManager] = None
_choice_lock = threading.Lock()


def get_choice_manager() -> ChoiceManager:
    """获取全局选择会话管理器单例"""
    global _global_choice_manager
    if _global_choice_manager is None:
        with _choice_lock:
            if _global_choice_manager is None:
                _global_choice_manager = ChoiceManager()
    return _global_choice_manager
