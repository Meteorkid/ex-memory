"""SQLCipher 快照解密：密钥只通过 stdin 传递。"""

from __future__ import annotations

import os
import re
import sqlite3
import subprocess
from pathlib import Path
from typing import Callable

from local_helper.wechat_macos.key_extractor import WeChatKeyPair


class DatabaseDecryptionError(RuntimeError):
    """数据库解密或完整性验证失败。"""


def decrypt_database(
    *,
    encrypted_db: Path,
    output_db: Path,
    key_pair: WeChatKeyPair,
    sqlcipher_binary: Path,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> Path:
    if encrypted_db.is_symlink() or sqlcipher_binary.is_symlink():
        raise DatabaseDecryptionError("加密数据库和 SQLCipher 不能是符号链接")
    encrypted_db = encrypted_db.resolve(strict=True)
    sqlcipher_binary = sqlcipher_binary.resolve(strict=True)
    if not encrypted_db.is_file():
        raise DatabaseDecryptionError("加密数据库必须是普通文件")
    if not sqlcipher_binary.is_file() or not os.access(sqlcipher_binary, os.X_OK):
        raise DatabaseDecryptionError("内置 SQLCipher 不存在或不可执行")
    if output_db.exists() or output_db.is_symlink():
        raise DatabaseDecryptionError("拒绝覆盖已存在的解密数据库")
    output_db.parent.mkdir(parents=True, exist_ok=True)
    output_resolved = output_db.parent.resolve() / output_db.name
    quoted_output = str(output_resolved).replace("'", "''")
    raw_key = key_pair.key_hex + key_pair.salt_hex
    if not re.fullmatch(r"[0-9a-f]{96}", raw_key):
        raise DatabaseDecryptionError("密钥格式无效")

    sql = "\n".join(
        (
            f'PRAGMA key = "x\'{raw_key}\'";',
            f"ATTACH DATABASE '{quoted_output}' AS plaintext KEY '';",
            "SELECT sqlcipher_export('plaintext');",
            "DETACH DATABASE plaintext;",
        )
    )
    try:
        result = runner(
            [str(sqlcipher_binary), str(encrypted_db)],
            input=sql,
            check=False,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        output_resolved.unlink(missing_ok=True)
        raise DatabaseDecryptionError("SQLCipher 执行失败") from exc
    if result.returncode != 0 or _contains_sqlcipher_error(result.stderr):
        output_resolved.unlink(missing_ok=True)
        raise DatabaseDecryptionError("SQLCipher 无法解密数据库，密钥或 schema 参数可能不匹配")
    if not output_resolved.is_file() or output_resolved.stat().st_size == 0:
        output_resolved.unlink(missing_ok=True)
        raise DatabaseDecryptionError("SQLCipher 未生成有效的解密数据库")
    verify_sqlite_master(output_resolved)
    verify_plain_sqlite(output_resolved)
    return output_resolved


def _contains_sqlcipher_error(stderr: str) -> bool:
    return bool(re.search(r"file is not a database|encrypted|out of memory|error", stderr, re.IGNORECASE))


def verify_plain_sqlite(path: Path) -> None:
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
            schema_rows = connection.execute(
                "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL"
            ).fetchall()
            has_virtual_tables = any(
                str(row[0]).lstrip().upper().startswith("CREATE VIRTUAL TABLE") for row in schema_rows
            )
            try:
                result = connection.execute("PRAGMA integrity_check").fetchone()
            except sqlite3.DatabaseError:
                if has_virtual_tables:
                    return
                raise
    except sqlite3.DatabaseError as exc:
        path.unlink(missing_ok=True)
        raise DatabaseDecryptionError("解密数据库无法通过 SQLite 验证") from exc
    if not result or result[0] != "ok":
        path.unlink(missing_ok=True)
        raise DatabaseDecryptionError("解密数据库完整性检查失败")


def verify_sqlite_master(path: Path) -> None:
    """逐库执行只读 sqlite_master 查询，确认 schema 可访问。"""
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
            connection.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
    except sqlite3.DatabaseError as exc:
        path.unlink(missing_ok=True)
        raise DatabaseDecryptionError("解密数据库无法读取 SQLite sqlite_master") from exc
