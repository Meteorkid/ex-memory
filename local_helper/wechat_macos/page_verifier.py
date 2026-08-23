"""SQLCipher 4 数据库首页的无内容密码学校验。"""

from __future__ import annotations

import hashlib
import hmac
import struct


SQLCIPHER4_PAGE_SIZE = 4096
SQLCIPHER4_KEY_SIZE = 32
SQLCIPHER4_SALT_SIZE = 16
SQLCIPHER4_RESERVE_SIZE = 64
SQLCIPHER4_KDF_ITERATIONS = 256_000


def derive_sqlcipher4_key(password: bytes, salt: bytes) -> bytes:
    """按微信 4.1.12 实测参数派生 SQLCipher 4 页面密钥。"""
    if len(password) != SQLCIPHER4_KEY_SIZE or len(salt) != SQLCIPHER4_SALT_SIZE:
        raise ValueError("SQLCipher 密码或 salt 长度无效")
    return hashlib.pbkdf2_hmac(
        "sha512",
        password,
        salt,
        SQLCIPHER4_KDF_ITERATIONS,
        dklen=SQLCIPHER4_KEY_SIZE,
    )


def verify_sqlcipher4_page(raw_key: bytes, page: bytes, *, page_number: int = 1) -> bool:
    """验证 raw key 是否匹配 SQLCipher 4 页面，不解密或读取正文。"""
    if len(raw_key) != SQLCIPHER4_KEY_SIZE or len(page) != SQLCIPHER4_PAGE_SIZE:
        return False
    if page_number <= 0:
        return False

    salt = page[:SQLCIPHER4_SALT_SIZE]
    encrypted_payload = page[SQLCIPHER4_SALT_SIZE:-SQLCIPHER4_RESERVE_SIZE]
    stored_mac = page[-SQLCIPHER4_RESERVE_SIZE:]
    mac_salt = bytes(value ^ 0x3A for value in salt)
    mac_key = hashlib.pbkdf2_hmac("sha512", raw_key, mac_salt, 2, dklen=SQLCIPHER4_KEY_SIZE)
    expected_mac = hmac.new(mac_key, encrypted_payload, hashlib.sha512)
    expected_mac.update(struct.pack("<I", page_number))
    return hmac.compare_digest(expected_mac.digest(), stored_mac)
