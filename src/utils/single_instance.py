"""
Single-instance process guard.

Prevents multiple local processes from starting the same WeCom server at the
same time, which would otherwise cause identical bot subscriptions to kick each
other offline and trigger repeated reconnect loops.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Optional

logger = logging.getLogger(__name__)

if os.name == "nt":
    import msvcrt
else:
    import fcntl


class SingleInstanceError(RuntimeError):
    """Raised when another local process already holds the instance lock."""


class SingleInstanceLock:
    """Cross-platform non-blocking single-process lock."""

    def __init__(self, lock_path: Path, app_name: str):
        self.lock_path = Path(lock_path)
        self.app_name = app_name
        self._file: Optional[BinaryIO] = None
        self._released = True

    def __enter__(self) -> "SingleInstanceLock":
        return self.acquire()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()

    def acquire(self) -> "SingleInstanceLock":
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_lock_file()

        lock_file = open(self.lock_path, "r+b")
        try:
            self._acquire_lock(lock_file)
        except OSError as exc:
            holder = self._read_holder()
            try:
                lock_file.close()
            except OSError:
                pass
            detail = f"{self.app_name} 已在运行"
            if holder:
                pid = holder.get("pid")
                started_at = holder.get("started_at")
                extras = []
                if pid:
                    extras.append(f"pid={pid}")
                if started_at:
                    extras.append(f"started_at={started_at}")
                if extras:
                    detail += f"（{', '.join(extras)}）"
            detail += "；请先停止已有实例，避免同一批 bot 连接互相挤下线。"
            raise SingleInstanceError(detail) from exc

        self._file = lock_file
        self._released = False
        self._write_metadata()
        atexit.register(self.release)
        logger.info("[SingleInstance] 已获取单实例锁: %s", self.lock_path)
        return self

    def release(self) -> None:
        if self._released or not self._file:
            return

        try:
            self._clear_metadata()
            self._release_lock(self._file)
        except OSError as exc:
            logger.warning("[SingleInstance] 释放锁失败: %s", exc)
        finally:
            try:
                self._file.close()
            finally:
                self._file = None
                self._released = True

    def _ensure_lock_file(self) -> None:
        with open(self.lock_path, "a+b") as seed_file:
            seed_file.seek(0, os.SEEK_END)
            if seed_file.tell() == 0:
                seed_file.write(b" ")
                seed_file.flush()
                os.fsync(seed_file.fileno())

    def _acquire_lock(self, lock_file: BinaryIO) -> None:
        if os.name == "nt":
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _release_lock(self, lock_file: BinaryIO) -> None:
        if os.name == "nt":
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _write_metadata(self) -> None:
        if not self._file:
            return

        payload = {
            "pid": os.getpid(),
            "started_at": datetime.now(timezone.utc).isoformat(),
            "cwd": os.getcwd(),
        }
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        if os.name == "nt":
            self._file.seek(1)
            self._file.truncate(1)
            self._file.write(encoded)
        else:
            self._file.seek(0)
            self._file.truncate()
            self._file.write(encoded)

        self._file.flush()
        os.fsync(self._file.fileno())

    def _clear_metadata(self) -> None:
        if not self._file:
            return

        self._file.seek(0)
        self._file.truncate()
        self._file.flush()
        os.fsync(self._file.fileno())

    def _read_holder(self) -> Optional[dict]:
        try:
            raw = self.lock_path.read_bytes()
        except OSError:
            return None

        if os.name == "nt" and raw.startswith(b" "):
            raw = raw[1:]

        if not raw:
            return None

        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
