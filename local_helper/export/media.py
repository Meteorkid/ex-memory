"""聊天媒体的安全定位和内容哈希复制。"""

from __future__ import annotations

import hashlib
import re
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from local_helper.wechat_macos.reader import Message, MessageKind


RESOURCE_DIRECTORIES = (
    "image",
    "video",
    "voice",
    "file",
    "emoji",
    "music",
    "avatar",
    "icon",
    "thumbnail",
    "location",
    "card",
    "raw",
)

_PATH_ATTRIBUTES = re.compile(
    r"(?i)(?:path|filepath|filename|file_name|thumbpath|thumb_path|videopath|voicepath|src|md5)\s*=\s*[\"']([^\"']+)[\"']"
)
_PATH_ELEMENTS = re.compile(
    r"(?is)<(?:path|filepath|filename|file_name|thumbpath|thumb_path|videopath|voicepath|md5)>\s*([^<]+?)\s*</"
)
_FILE_TOKEN = re.compile(
    r"(?i)([A-Za-z0-9_./\\:@%+\-]{6,}\.(?:jpg|jpeg|png|gif|webp|heic|dat|thumb|mp4|mov|m4v|mp3|m4a|wav|silk|amr|aud|pdf|docx?|xlsx?|pptx?|zip|rar|7z|txt|html?))"
)
_MD5_TOKEN = re.compile(r"(?i)(?<![0-9a-f])([0-9a-f]{32})(?![0-9a-f])")


@dataclass(frozen=True)
class ExportedMedia:
    category: str
    relative_path: str
    source_size: int
    sha256: str


@dataclass(frozen=True)
class MissingMedia:
    hint: str
    reason: str


class MediaResolver:
    def __init__(
        self,
        *,
        account_root: Path,
        session_wxid: str,
        export_root: Path,
        media_databases: tuple[Path, ...] = (),
    ):
        if account_root.is_symlink():
            raise ValueError("微信账号目录不能是符号链接")
        self.account_root = account_root.resolve(strict=True)
        self.export_root = export_root.resolve(strict=True)
        self.session_wxid = session_wxid
        self.session_digest = hashlib.md5(session_wxid.encode("utf-8"), usedforsecurity=False).hexdigest()
        self.media_databases = tuple(_safe_database(path) for path in media_databases)
        self._cache: dict[tuple[str, str], ExportedMedia | MissingMedia] = {}

    def export_for_message(self, message: Message) -> tuple[list[ExportedMedia], list[MissingMedia]]:
        category = _category_for_kind(message.kind)
        if not category:
            return [], []
        exported: list[ExportedMedia] = []
        missing: list[MissingMedia] = []
        if message.kind is MessageKind.VOICE:
            voice = self._export_voice(message)
            if voice:
                exported.append(voice)
        payload = f"{message.content}\n{message.resource_content}"
        for hint in extract_media_hints(payload):
            cache_key = (category, hint)
            cached = self._cache.get(cache_key)
            if cached is None:
                source = self._resolve_hint(hint, category)
                cached = self._copy(source, category) if source else MissingMedia(hint=_safe_hint(hint), reason="not_found")
                self._cache[cache_key] = cached
            if isinstance(cached, ExportedMedia):
                exported.append(cached)
            else:
                missing.append(cached)
        for item in exported:
            if Path(item.relative_path).suffix.lower() in {".dat", ".silk", ".bin", ".aud"}:
                missing.append(
                    MissingMedia(
                        hint=Path(item.relative_path).name,
                        reason="preserved_original_not_browser_playable",
                    )
                )
        return _deduplicate_exported(exported), _deduplicate_missing(missing)

    def _export_voice(self, message: Message) -> ExportedMedia | None:
        for database in self.media_databases:
            with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
                columns = {row[1] for row in connection.execute('PRAGMA table_info("VoiceInfo")')}
                required = {"chat_name_id", "local_id", "voice_data"}
                if not required.issubset(columns):
                    continue
                chat_row = connection.execute(
                    'SELECT rowid FROM "Name2Id" WHERE user_name = ? LIMIT 1',
                    (self.session_wxid,),
                ).fetchone()
                if not chat_row:
                    continue
                clauses = ["chat_name_id = ?", "local_id = ?"]
                parameters: list[int] = [int(chat_row[0]), message.local_id]
                if "svr_id" in columns and message.server_id:
                    clauses.append("svr_id = ?")
                    parameters.append(message.server_id)
                order = ' ORDER BY "data_index"' if "data_index" in columns else ""
                rows = connection.execute(
                    f'SELECT "voice_data" FROM "VoiceInfo" WHERE {" AND ".join(clauses)}{order}',
                    parameters,
                )
                chunks: list[bytes] = []
                size = 0
                for row in rows:
                    if row[0] is None:
                        continue
                    chunk = bytes(row[0])
                    size += len(chunk)
                    if size > 128 * 1024 * 1024:
                        raise OSError("单条语音数据超过安全限制")
                    chunks.append(chunk)
                if not chunks:
                    continue
                payload = b"".join(chunks)
                if payload.startswith(b"\x02#!SILK_V3"):
                    payload = payload[1:]
                suffix = ".silk" if payload.startswith(b"#!SILK_V3") else ".bin"
                return self._copy_bytes(payload, "voice", suffix)
        return None

    def _resolve_hint(self, hint: str, category: str) -> Path | None:
        normalized = hint.strip().replace("\\", "/")
        if not normalized or normalized.startswith(("http://", "https://", "weixin://", "wxfile://")):
            return None
        candidate = Path(normalized)
        if candidate.is_absolute():
            if candidate.is_symlink():
                return None
            try:
                resolved = candidate.resolve(strict=True)
            except OSError:
                return None
            return resolved if _is_safe_file(resolved, self.account_root) else None

        direct = self.account_root / candidate
        if direct.exists():
            if direct.is_symlink():
                return None
            resolved = direct.resolve()
            if _is_safe_file(resolved, self.account_root):
                return resolved

        basename = candidate.name
        if len(basename) < 6 or basename in {".", ".."}:
            return None
        patterns = (basename, f"{basename}.*", f"{basename}_*") if _MD5_TOKEN.fullmatch(basename) else (basename,)
        for root in self._search_roots(category):
            if not root.is_dir() or root.is_symlink():
                continue
            for pattern in patterns:
                for match in root.rglob(pattern):
                    if match.is_symlink():
                        continue
                    resolved = match.resolve()
                    if _is_safe_file(resolved, self.account_root):
                        return resolved
        return None

    def _search_roots(self, category: str) -> Iterable[Path]:
        msg_root = self.account_root / "msg"
        session_attach = msg_root / "attach" / self.session_digest
        category_roots = {
            "video": (msg_root / "video", session_attach),
            "file": (msg_root / "file", session_attach),
            "voice": (msg_root / "voice", session_attach),
            "image": (
                session_attach,
                msg_root / "image",
                self.account_root / "temp" / "RWTemp",
                self.account_root / "cache",
            ),
            "emoji": (msg_root / "emoji", self.account_root / "cache"),
        }
        return (*category_roots.get(category, (session_attach,)), msg_root)

    def _copy(self, source: Path, category: str) -> ExportedMedia:
        digest = _sha256(source)
        suffix = source.suffix.lower()
        if not re.fullmatch(r"\.[A-Za-z0-9]{1,12}", suffix):
            suffix = ""
        target = self.export_root / category / f"{digest}{suffix}"
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            with source.open("rb") as input_stream, target.open("xb") as output_stream:
                shutil.copyfileobj(input_stream, output_stream, length=1024 * 1024)
        if _sha256(target) != digest:
            target.unlink(missing_ok=True)
            raise OSError("媒体复制校验失败")
        return ExportedMedia(
            category=category,
            relative_path=target.relative_to(self.export_root).as_posix(),
            source_size=target.stat().st_size,
            sha256=digest,
        )

    def _copy_bytes(self, payload: bytes, category: str, suffix: str) -> ExportedMedia:
        digest = hashlib.sha256(payload).hexdigest()
        target = self.export_root / category / f"{digest}{suffix}"
        if not target.exists():
            temporary = target.with_suffix(target.suffix + ".tmp")
            with temporary.open("xb") as stream:
                stream.write(payload)
            temporary.replace(target)
        return ExportedMedia(
            category=category,
            relative_path=target.relative_to(self.export_root).as_posix(),
            source_size=len(payload),
            sha256=digest,
        )


def extract_media_hints(content: str) -> tuple[str, ...]:
    if not content or len(content) > 32 * 1024 * 1024:
        return ()
    hints = [
        *_PATH_ATTRIBUTES.findall(content),
        *_PATH_ELEMENTS.findall(content),
        *_FILE_TOKEN.findall(content),
        *_MD5_TOKEN.findall(content),
    ]
    result: list[str] = []
    seen: set[str] = set()
    for hint in hints:
        cleaned = hint.strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            result.append(cleaned)
    return tuple(result)


def _category_for_kind(kind: MessageKind) -> str | None:
    return {
        MessageKind.IMAGE: "image",
        MessageKind.VIDEO: "video",
        MessageKind.VOICE: "voice",
        MessageKind.FILE: "file",
        MessageKind.EMOJI: "emoji",
        MessageKind.MUSIC: "music",
        MessageKind.LOCATION: "location",
        MessageKind.LINK: "card",
        MessageKind.SHARE: "card",
        MessageKind.CONTACT: "card",
    }.get(kind)


def _is_safe_file(path: Path, root: Path) -> bool:
    return path.is_file() and not path.is_symlink() and path.is_relative_to(root)


def _safe_hint(hint: str) -> str:
    return Path(hint.replace("\\", "/")).name[:255]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_database(path: Path) -> Path:
    if path.is_symlink():
        raise ValueError("媒体数据库不能是符号链接")
    resolved = path.resolve(strict=True)
    if not resolved.is_file():
        raise ValueError("媒体数据库必须是普通文件")
    return resolved


def _deduplicate_exported(items: list[ExportedMedia]) -> list[ExportedMedia]:
    return list({item.relative_path: item for item in items}.values())


def _deduplicate_missing(items: list[MissingMedia]) -> list[MissingMedia]:
    return list({(item.hint, item.reason): item for item in items}.values())
