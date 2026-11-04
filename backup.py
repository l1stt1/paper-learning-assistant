"""Offline, self-contained profile backups; restoration never replaces a file.

Archives contain private learning records and must be stored privately. Credentials,
application code and transient locks/caches are not part of the profile backup.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import sqlite3
import stat
import unicodedata
import zipfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath

from profile_store import ProfileInUseError, ProfileLock


FORMAT = "paper-learning-assistant-backup"
VERSION = 1
MANIFEST = "backup-manifest.json"
MAX_FILES = 100_000
MAX_FILE_BYTES = 2 * 1024 ** 3
MAX_TOTAL_BYTES = 20 * 1024 ** 3
MAX_SQLITE_BYTES = 256 * 1024 ** 2
MAX_JSON_BYTES = 16 * 1024 ** 2
CHUNK = 1024 ** 2
PROFILE_DIRS = {"articles", "learning", "work"}
APP_WORK_DIRS = {"explanations", "comparisons", "import-writes"}
MODEL_REVIEW_PATH = "learning/model-settings-review.json"
CODE_SUFFIXES = {".py", ".pyc", ".pyo", ".js", ".mjs", ".cjs", ".exe", ".dll", ".bat", ".cmd", ".vbs", ".ps1", ".sh"}
PRIVATE_NAMES = {"auth.json", "credentials.json", "credentials", "secrets.json", "secrets", "id_rsa", "id_ed25519", "cookies", "cookies.sqlite"}
SKIP_DIRS = {"__pycache__", "cache", ".cache", ".git", ".venv", "venv", "node_modules"}


class BackupError(ValueError):
    """A backup is invalid, unsafe, unavailable, or cannot be completed."""


def _json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _json(data, label):
    if len(data) > MAX_JSON_BYTES:
        raise BackupError(f"{label} 超过 JSON 大小上限。")
    try:
        return json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise BackupError(f"{label} 不是有效的 UTF-8 JSON。") from exc


def _safe_name(name):
    """Use one portable interpretation, including on Windows extraction targets."""
    if not isinstance(name, str) or not name or len(name) > 1000 or "\\" in name:
        raise BackupError("备份包含无效路径。")
    parts = name.split("/")
    for part in parts:
        if (not part or len(part) > 255 or part in {".", ".."} or part.endswith((".", " "))
                or any(ord(c) < 32 or c in ':<>"|?*' for c in part)
                or PureWindowsPath(part).is_reserved()
                or re.fullmatch(r"(?:con|prn|aux|nul|com[0-9]|lpt[0-9])(?:\..*)?", part, re.I)):
            raise BackupError("备份包含不安全或不兼容的路径。")
    return name


def _key(name):
    return unicodedata.normalize("NFC", name).casefold()


def _excluded(name):
    parts = PurePosixPath(name).parts
    leaf = parts[-1].casefold()
    return (any(p.casefold() in SKIP_DIRS for p in parts)
            or leaf in PRIVATE_NAMES or leaf.startswith(".env")
            or leaf in {".profile.lock", ".service.json"} or leaf.endswith((".lock", "-wal", "-shm", "-journal"))
            or PurePosixPath(leaf).suffix in CODE_SUFFIXES | {".pem", ".key", ".p12", ".pfx"})


def _profile_name(name):
    _safe_name(name)
    parts = PurePosixPath(name).parts
    if not ((len(parts) == 1 and name in {"articles.json", "relations.json"})
            or (len(parts) > 1 and parts[0] in PROFILE_DIRS)) or _excluded(name):
        raise BackupError("备份包含不属于学习库的文件或凭据/程序文件。")
    return name


def _is_link(path):
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _read_small(path, label):
    if path.stat().st_size > MAX_JSON_BYTES:
        raise BackupError(f"{label} 超过 JSON 大小上限。")
    return _json(path.read_bytes(), label)


def _article_manifest(value):
    if (not isinstance(value, dict) or not isinstance(value.get("articles"), list)
            or not isinstance(value.get("library_id"), str) or not value["library_id"]):
        raise BackupError("articles.json 缺少文章清单。")
    seen = set()
    for article in value["articles"]:
        if not isinstance(article, dict):
            raise BackupError("文章清单格式不正确。")
        aid = article.get("id")
        if not isinstance(aid, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,100}", aid) or aid in seen:
            raise BackupError("文章 ID 无效或重复。")
        seen.add(aid)
        if not isinstance(article.get("pages"), int) or isinstance(article["pages"], bool) or article["pages"] < 1:
            raise BackupError("文章 PDF 页数无效。")
        if not isinstance(article.get("assets", []), list):
            raise BackupError("文章附件清单无效。")
        for field in ("pdf", "content"):
            if not isinstance(article.get(field), str) or not article[field]:
                raise BackupError("文章缺少 PDF 或导读文件引用。")
        for asset in article.get("assets", []):
            if not isinstance(asset, dict) or not isinstance(asset.get("path"), str) or not asset["path"]:
                raise BackupError("文章附件缺少文件引用。")
    return value


def _check_model_settings(value):
    # The application only stores a credential environment-variable NAME. Refuse
    # an unexpected settings file rather than copying or printing a secret value.
    from model_backend import _validated_settings
    try:
        _validated_settings(value)
    except (ValueError, TypeError) as exc:
        raise BackupError("模型设置格式无效或包含不支持的凭据字段；未复制设置。") from exc


def _sources(root):
    manifest = copy.deepcopy(_article_manifest(_read_small(root / "articles.json", "articles.json")))
    sources = {}
    for filename in ("articles.json", "relations.json"):
        path = root / filename
        if path.exists():
            if _is_link(path) or not path.is_file():
                raise BackupError("学习库清单必须是普通文件。")
            sources[filename] = path
    for dirname in sorted(PROFILE_DIRS):
        base = root / dirname
        if not base.exists():
            continue
        if _is_link(base) or not base.is_dir():
            raise BackupError(f"学习库目录不能是链接：{base}")
        for current, folders, files in os.walk(base, followlinks=False):
            if dirname == "work" and Path(current) == base:
                # Old in-place installations also keep development copies here.
                # Only these three directories are produced by the application.
                folders[:] = [folder for folder in folders if folder in APP_WORK_DIRS]
                files = []
            for folder in list(folders):
                path = Path(current) / folder
                name = path.relative_to(root).as_posix()
                if _excluded(name):
                    folders.remove(folder)
                elif _is_link(path):
                    raise BackupError(f"学习库中有目录链接，未跟随：{path}")
            for filename in files:
                path = Path(current) / filename
                name = path.relative_to(root).as_posix()
                if _excluded(name):
                    continue
                _profile_name(name)
                if _is_link(path) or not path.is_file():
                    raise BackupError(f"学习库中有非普通文件：{path}")
                sources[name] = path

    def include(value, aid, role):
        source = (root / value).resolve(strict=True)
        if not source.is_file() or _is_link(root / value):
            raise BackupError("文章引用必须指向存在的普通文件。")
        try:
            name = source.relative_to(root).as_posix()
            _profile_name(name)
        except (ValueError, BackupError):
            suffix = source.suffix.lower()
            if _excluded(source.name):
                raise BackupError("文章引用了程序或凭据文件，未纳入备份。")
            name = f"articles/{aid}/backup-assets/{role}{suffix}"
            number = 1
            while name in sources and sources[name] != source:
                name = f"articles/{aid}/backup-assets/{role}-{number}{suffix}"
                number += 1
        _profile_name(name)
        sources[name] = source
        return name

    for article in manifest["articles"]:
        aid = article["id"]
        article["pdf"] = include(article["pdf"], aid, "reading")
        article["content"] = include(article["content"], aid, "content")
        for index, asset in enumerate(article.get("assets", []), 1):
            asset["path"] = include(asset["path"], aid, f"attachment-{index}")
    sources["articles.json"] = _json_bytes(manifest)
    if "learning/model-settings.json" in sources:
        _check_model_settings(_read_small(sources["learning/model-settings.json"], "模型设置"))
    names = [_key(name) for name in sources]
    if len(set(names)) != len(names):
        raise BackupError("学习库文件名在其他系统上会发生冲突。")
    return sources, manifest


def _sqlite_snapshot(path):
    if path.stat().st_size > MAX_SQLITE_BYTES:
        raise BackupError("单个 SQLite 超过 256 MiB，当前内存快照方式不支持；原库未修改。")
    # Read-only mode includes committed WAL records. The profile lock prevents the
    # application from opening any new transaction during this snapshot.
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as source, closing(sqlite3.connect(":memory:")) as target:
        logical_size = source.execute("PRAGMA page_count").fetchone()[0] * source.execute("PRAGMA page_size").fetchone()[0]
        if logical_size > MAX_SQLITE_BYTES:
            raise BackupError("单个 SQLite 超过 256 MiB，当前内存快照方式不支持；原库未修改。")
        source.backup(target)
        if target.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise BackupError("SQLite 一致性检查失败。")
        return target.serialize()


def _summary(metadata):
    return {key: metadata[key] for key in ("format", "version", "created_at", "article_count", "file_count", "size")}


def create_backup(profile_root, destination):
    """Back up a stopped profile to a NEW ZIP; keep a failed partial ZIP in place."""
    root = Path(profile_root).expanduser().resolve()
    target = Path(destination).expanduser().absolute()
    if not root.is_dir() or target.suffix.lower() != ".zip":
        raise BackupError("请选择现有学习库目录和新的 .zip 备份文件。")
    if target.exists() or target.is_symlink():
        raise BackupError("备份目标已存在；不会覆盖，请使用新的文件名。")
    if target.resolve().is_relative_to(root):
        raise BackupError("备份文件必须放在学习库目录之外。")
    if not target.parent.is_dir():
        raise BackupError("备份目标的父目录不存在。")
    created = False
    try:
        with ProfileLock(root):
            sources, manifest = _sources(root)
            if len(sources) > MAX_FILES:
                raise BackupError("学习库文件数量超过备份上限。")
            sizes = [len(value) if isinstance(value, bytes) else value.stat().st_size for value in sources.values()]
            if any(size > MAX_FILE_BYTES for size in sizes) or sum(sizes) > MAX_TOTAL_BYTES:
                raise BackupError("学习库超过备份大小上限（单文件 2 GiB，总计 20 GiB）。")
            metadata = {"format": FORMAT, "version": VERSION, "created_at": datetime.now(timezone.utc).isoformat(),
                        "article_count": len(manifest["articles"]), "file_count": len(sources), "size": 0, "files": []}
            with target.open("xb") as output:
                created = True
                with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
                    for name, source in sorted(sources.items()):
                        data = source if isinstance(source, bytes) else None
                        if data is None and source.suffix.lower() in {".sqlite", ".sqlite3", ".db"}:
                            data = _sqlite_snapshot(source)
                        digest, size = hashlib.sha256(), 0
                        with archive.open("profile/" + name, "w", force_zip64=True) as entry:
                            if data is not None:
                                entry.write(data)
                                digest.update(data)
                                size = len(data)
                            else:
                                with source.open("rb") as handle:
                                    for chunk in iter(lambda: handle.read(CHUNK), b""):
                                        size += len(chunk)
                                        if size > MAX_FILE_BYTES:
                                            raise BackupError("备份期间文件大小超过上限。")
                                        entry.write(chunk)
                                        digest.update(chunk)
                        metadata["size"] += size
                        if metadata["size"] > MAX_TOTAL_BYTES:
                            raise BackupError("备份期间文件总大小超过上限。")
                        metadata["files"].append({"path": name, "size": size, "sha256": digest.hexdigest()})
                    metadata_bytes = _json_bytes(metadata)
                    if len(metadata_bytes) > MAX_JSON_BYTES:
                        raise BackupError("备份索引超过大小上限。")
                    archive.writestr(MANIFEST, metadata_bytes)
            # Verify the exact published archive before reporting success.
            return {**inspect_backup(target), "archive": str(target.resolve())}
    except ProfileInUseError as exc:
        raise BackupError("学习库正在使用中；请先正常停止阅读助手，再备份。") from exc
    except Exception as exc:
        detail = str(exc) if isinstance(exc, BackupError) else type(exc).__name__
        residual = f" 不完整备份已保留：{target}" if created else ""
        raise BackupError(f"备份未完成：{detail}{residual}") from exc


def _inspect(archive):
    entries = archive.infolist()
    if len(entries) > MAX_FILES + 1:
        raise BackupError("备份文件数量超过上限。")
    names, infos, total = set(), {}, 0
    for info in entries:
        name = _safe_name(info.filename)
        key = _key(name)
        mode = (info.external_attr >> 16) & 0xFFFF
        if key in names or info.is_dir() or info.external_attr & 0x400 or (stat.S_IFMT(mode) not in {0, stat.S_IFREG}):
            raise BackupError("备份包含重复文件、目录项或链接。")
        if info.flag_bits & 1 or info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
            raise BackupError("不支持加密或此压缩格式的备份。")
        if info.file_size < 0 or info.file_size > MAX_FILE_BYTES:
            raise BackupError("备份单文件大小超过上限。")
        total += info.file_size
        if total > MAX_TOTAL_BYTES + MAX_JSON_BYTES:
            raise BackupError("备份总大小超过上限。")
        names.add(key)
        infos[name] = info
        if name != MANIFEST:
            if not name.startswith("profile/"):
                raise BackupError("备份包含不明文件。")
            _profile_name(name.removeprefix("profile/"))
    if MANIFEST not in infos or infos[MANIFEST].file_size > MAX_JSON_BYTES:
        raise BackupError("缺少备份索引或索引过大。")
    metadata = _json(archive.read(MANIFEST), MANIFEST)
    if (not isinstance(metadata, dict) or metadata.get("format") != FORMAT or metadata.get("version") != VERSION
            or type(metadata.get("version")) is not int or not isinstance(metadata.get("files"), list)
            or not isinstance(metadata.get("created_at"), str)
            or any(type(metadata.get(key)) is not int or metadata[key] < 0 for key in ("article_count", "file_count", "size"))):
        raise BackupError("备份格式或版本不受支持。")
    expected = {}
    for record in metadata["files"]:
        if not isinstance(record, dict):
            raise BackupError("备份索引条目格式无效。")
        name = _profile_name(record.get("path"))
        if name in expected or not isinstance(record.get("size"), int) or isinstance(record["size"], bool) or record["size"] < 0:
            raise BackupError("备份索引有重复或无效条目。")
        if not isinstance(record.get("sha256"), str) or not re.fullmatch("[0-9a-f]{64}", record["sha256"]):
            raise BackupError("备份完整性摘要格式无效。")
        expected[name] = record
    if set(infos) != {MANIFEST, *("profile/" + name for name in expected)}:
        raise BackupError("备份文件与索引不一致。")
    if ("articles.json" not in expected or metadata.get("file_count") != len(expected)
            or metadata.get("size") != sum(record["size"] for record in expected.values())):
        raise BackupError("备份缺少必要文件或总计不一致。")
    if metadata["size"] > MAX_TOTAL_BYTES:
        raise BackupError("备份总大小超过上限。")
    # A file must never also be the parent directory of another file.
    keys = {_key(name) for name in expected}
    for name in expected:
        if any(_key(parent.as_posix()) in keys for parent in PurePosixPath(name).parents if str(parent) != "."):
            raise BackupError("备份路径发生文件与目录冲突。")
    for name, record in expected.items():
        info = infos["profile/" + name]
        if info.file_size != record["size"]:
            raise BackupError("备份文件大小与索引不一致。")
        digest, size = hashlib.sha256(), 0
        with archive.open(info) as handle:
            for chunk in iter(lambda: handle.read(CHUNK), b""):
                digest.update(chunk)
                size += len(chunk)
                if size > record["size"]:
                    raise BackupError("备份解压大小不一致。")
        if size != record["size"] or digest.hexdigest() != record["sha256"]:
            raise BackupError("备份完整性检查失败。")
    articles_info = infos["profile/articles.json"]
    if articles_info.file_size > MAX_JSON_BYTES:
        raise BackupError("文章清单过大。")
    articles = _article_manifest(_json(archive.read(articles_info), "articles.json"))
    if metadata.get("article_count") != len(articles["articles"]):
        raise BackupError("备份文章数量不一致。")
    if "learning/model-settings.json" in expected:
        settings_info = infos["profile/learning/model-settings.json"]
        if settings_info.file_size > MAX_JSON_BYTES:
            raise BackupError("模型设置文件过大。")
        _check_model_settings(_json(archive.read(settings_info), "模型设置"))
    for article in articles["articles"]:
        refs = [article["pdf"], article["content"], *(asset["path"] for asset in article.get("assets", []))]
        if any(_profile_name(ref) not in expected for ref in refs):
            raise BackupError("备份缺少文章 PDF、导读或附件。")
        content_info = infos["profile/" + article["content"]]
        if content_info.file_size > MAX_JSON_BYTES:
            raise BackupError("文章导读过大。")
        content = _json(archive.read(content_info), "文章导读")
        if not isinstance(content, dict) or not all(isinstance(content.get(k), list) for k in ("sections", "concepts")):
            raise BackupError("文章导读格式不正确。")
    return metadata


def inspect_backup(archive):
    """Validate the entire archive without extracting or exposing record contents."""
    try:
        with zipfile.ZipFile(archive, "r") as source:
            return _summary(_inspect(source))
    except BackupError:
        raise
    except Exception as exc:
        raise BackupError(f"无法读取或验证备份：{type(exc).__name__}。") from exc


def restore_backup(archive, destination):
    """Restore only into a new or entirely empty directory; retain failed output."""
    target = Path(destination).expanduser().absolute()
    started = False
    try:
        # Resolve all existing ancestors before writing, rejecting symlink/junction
        # redirection rather than silently restoring into another directory.
        for candidate in (target, *target.parents):
            if (candidate.exists() or candidate.is_symlink()) and _is_link(candidate):
                raise BackupError("恢复目标及其父目录不能是链接。")
        if target.exists() and (not target.is_dir() or any(target.iterdir())):
            raise BackupError("恢复目标必须是新目录或完全空目录；不会覆盖原资料。")
        with zipfile.ZipFile(archive, "r") as source:
            metadata = _inspect(source)
            target.mkdir(parents=True, exist_ok=True)
            started = True
            with ProfileLock(target):
                # Recheck after obtaining the lock; a concurrent startup may have
                # populated an initially empty destination while validation ran.
                if any(path.name != ".profile.lock" for path in target.iterdir()):
                    raise BackupError("恢复目标在验证期间出现了文件，已停止。")
                for dirname in sorted(PROFILE_DIRS):
                    (target / dirname).mkdir(exist_ok=True)
                # Restored settings may select a different remote endpoint or an
                # environment-variable name. Require a fresh local confirmation,
                # even when a supplied archive claims it was already confirmed.
                with (target / MODEL_REVIEW_PATH).open("xb") as handle:
                    handle.write(_json_bytes({"requires_review": True}))
                for record in metadata["files"]:
                    if _key(record["path"]) == _key(MODEL_REVIEW_PATH):
                        continue
                    path = target.joinpath(*PurePosixPath(record["path"]).parts)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    digest, size = hashlib.sha256(), 0
                    with source.open("profile/" + record["path"]) as handle, path.open("xb") as output:
                        for chunk in iter(lambda: handle.read(CHUNK), b""):
                            size += len(chunk)
                            if size > record["size"]:
                                raise BackupError("恢复期间文件大小不一致。")
                            digest.update(chunk)
                            output.write(chunk)
                    if size != record["size"] or digest.hexdigest() != record["sha256"]:
                        raise BackupError("恢复期间文件完整性检查失败。")
            return {**_summary(metadata), "destination": str(target.resolve())}
    except Exception as exc:
        detail = str(exc) if isinstance(exc, BackupError) else type(exc).__name__
        residual = f" 已写入内容保留在：{target}；请检查后选择新的恢复目录。" if started else " 原资料未覆盖。"
        raise BackupError(f"恢复未完成：{detail}{residual}") from exc
