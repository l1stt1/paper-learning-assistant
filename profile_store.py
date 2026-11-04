"""Resolve complete libraries and hold an operating-system lock while using them."""
from __future__ import annotations

import errno
import json
import os
from pathlib import Path
import re
import sys
import uuid


class ProfileInUseError(RuntimeError):
    """Another process already holds this library open."""


def default_profile_root():
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return (base / "PaperLearningAssistant" / "default").resolve()
    if sys.platform == "darwin":
        return (Path.home() / "Library" / "Application Support" / "PaperLearningAssistant" / "default").resolve()
    base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return (base / "paper-learning-assistant" / "default").resolve()


def resolve_profile_root(app_root, data_root=None):
    """An explicit directory is a complete library, never an overlay on the app."""
    if data_root is not None:
        return Path(data_root).expanduser().resolve()
    app_root = Path(app_root).resolve()
    return app_root if (app_root / "articles.json").is_file() else default_profile_root()


def profile_run_path(profile_root, *components):
    """Resolve generated task output paths without accepting restored IDs as paths."""
    if not components or any(not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,120}", value)
                             for value in components):
        raise ValueError("任务标识无效，未启动处理；请检查文库记录。")
    root = Path(profile_root).resolve()
    path = root.joinpath(*components).resolve()
    if not path.is_relative_to(root) or path == root:
        raise ValueError("任务运行目录不在当前文库内，未启动处理。")
    return path


class ProfileLock:
    """A nonblocking cross-process lock; the lock file is intentionally retained."""

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.handle = None

    def acquire(self):
        if self.handle is not None:
            return self
        # Only the directory and lock file are created before acquiring the lock.
        self.root.mkdir(parents=True, exist_ok=True)
        handle = (self.root / ".profile.lock").open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise ProfileInUseError(f"此文库已在使用：{self.root}") from exc
            raise
        self.handle = handle
        return self

    def release(self):
        handle, self.handle = self.handle, None
        if handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *_):
        self.release()


def read_service_info(profile_root):
    try:
        value = json.loads((Path(profile_root) / ".service.json").read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def write_service_info(profile_root, info):
    """Write non-secret discovery information while the caller holds the lock."""
    root = Path(profile_root)
    pending = root / (".service.pending-" + uuid.uuid4().hex + ".json")
    pending.write_text(json.dumps(info, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(pending, root / ".service.json")
