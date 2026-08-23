import sqlite3
import subprocess
import hashlib
import hmac
import struct
from pathlib import Path

import pytest

from local_helper.wechat_macos.decryptor import (
    DatabaseDecryptionError,
    decrypt_database,
    verify_plain_sqlite,
)
from local_helper.wechat_macos.key_extractor import (
    CapturedAccountKeys,
    KeyExtractionError,
    WeChatKeyPair,
    collect_database_salts,
    derive_verified_key_pairs,
    extract_current_account_keys,
)
from local_helper.wechat_macos.lldb_key_capture import is_wechat_sqlcipher_kdf
from local_helper.wechat_macos.page_verifier import derive_sqlcipher4_key, verify_sqlcipher4_page
from local_helper.wechat_macos.sip import SIPStatus, parse_sip_status


def _executable(path: Path) -> Path:
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o700)
    return path


def test_parse_sip_status():
    assert parse_sip_status("System Integrity Protection status: enabled.") is SIPStatus.ENABLED
    assert parse_sip_status("System Integrity Protection status: disabled.") is SIPStatus.DISABLED
    assert parse_sip_status("unexpected") is SIPStatus.UNKNOWN


def test_collect_database_salts_is_deduplicated(tmp_path: Path):
    first = tmp_path / "first.db"
    second = tmp_path / "second.db"
    first.write_bytes(bytes.fromhex("11" * 16) + b"payload")
    second.write_bytes(bytes.fromhex("11" * 16) + b"other")

    assert collect_database_salts((first, second)) == ("11" * 16,)


def test_key_extraction_requires_disabled_sip(tmp_path: Path):
    launcher = _executable(tmp_path / "launcher")
    module = tmp_path / "capture.py"
    module.write_text("", encoding="utf-8")
    with pytest.raises(KeyExtractionError, match="SIP"):
        extract_current_account_keys(
            launcher=launcher,
            capture_module=module,
            databases=(),
            sip_status=SIPStatus.ENABLED,
        )


def test_key_extraction_captures_password_and_verifies_all_salts(tmp_path: Path):
    launcher = _executable(tmp_path / "launcher")
    module = tmp_path / "capture.py"
    module.write_text("", encoding="utf-8")
    password = bytes(range(32))
    database = _encrypted_page(tmp_path / "message.db", password, bytes.fromhex("11" * 16))

    def runner(*args, **kwargs):
        return subprocess.CompletedProcess(
            args[0], 0, f"noise\nWECHAT_PID 123\nWECHAT_PBKDF2_PASSWORD {password.hex()}\n", ""
        )

    captured = extract_current_account_keys(
        launcher=launcher,
        capture_module=module,
        databases=(database,),
        sip_status=SIPStatus.DISABLED,
        runner=runner,
    )

    assert isinstance(captured, CapturedAccountKeys)
    assert captured.wechat_pid == 123
    assert captured.keys == (
        WeChatKeyPair(key_hex=derive_sqlcipher4_key(password, bytes.fromhex("11" * 16)).hex(), salt_hex="11" * 16),
    )


def test_key_extraction_rejects_account_mismatch_without_leaking_password(tmp_path: Path):
    launcher = _executable(tmp_path / "launcher")
    module = tmp_path / "capture.py"
    module.write_text("", encoding="utf-8")
    password = bytes(range(32))
    database = _encrypted_page(tmp_path / "message.db", password, bytes.fromhex("11" * 16))
    wrong_password = bytes(reversed(range(32)))

    def runner(*args, **kwargs):
        output = f"WECHAT_PID 123\nWECHAT_PBKDF2_PASSWORD {wrong_password.hex()}\n"
        return subprocess.CompletedProcess(args[0], 0, output, "")

    with pytest.raises(KeyExtractionError, match="每个微信账号密钥不同") as caught:
        extract_current_account_keys(
            launcher=launcher,
            capture_module=module,
            databases=(database,),
            sip_status=SIPStatus.DISABLED,
            runner=runner,
        )
    assert wrong_password.hex() not in str(caught.value)


def test_key_extraction_reports_timeout_without_sensitive_output(tmp_path: Path):
    launcher = _executable(tmp_path / "launcher")
    module = tmp_path / "capture.py"
    module.write_text("", encoding="utf-8")
    database = tmp_path / "message.db"
    database.write_bytes(bytes.fromhex("11" * 16) + bytes(4080))

    def runner(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    with pytest.raises(KeyExtractionError, match="超时") as caught:
        extract_current_account_keys(
            launcher=launcher,
            capture_module=module,
            databases=(database,),
            sip_status=SIPStatus.DISABLED,
            runner=runner,
        )

    assert "11" * 16 not in str(caught.value)


def test_decrypt_database_validates_generated_sqlite(tmp_path: Path):
    encrypted = tmp_path / "encrypted.db"
    encrypted.write_bytes(b"encrypted")
    sqlcipher = _executable(tmp_path / "sqlcipher")
    output = tmp_path / "plain.db"

    def runner(command, **kwargs):
        assert "22" * 32 not in " ".join(command)
        with sqlite3.connect(output) as connection:
            connection.execute("CREATE TABLE message(id INTEGER PRIMARY KEY, content TEXT)")
        return subprocess.CompletedProcess(command, 0, "", "")

    result = decrypt_database(
        encrypted_db=encrypted,
        output_db=output,
        key_pair=WeChatKeyPair(key_hex="22" * 32, salt_hex="11" * 16),
        sqlcipher_binary=sqlcipher,
        runner=runner,
    )

    assert result == output


def test_decrypt_database_removes_invalid_output(tmp_path: Path):
    encrypted = tmp_path / "encrypted.db"
    encrypted.write_bytes(b"encrypted")
    sqlcipher = _executable(tmp_path / "sqlcipher")
    output = tmp_path / "plain.db"

    def runner(command, **kwargs):
        output.write_bytes(b"not sqlite")
        return subprocess.CompletedProcess(command, 0, "", "")

    with pytest.raises(DatabaseDecryptionError, match="SQLite"):
        decrypt_database(
            encrypted_db=encrypted,
            output_db=output,
            key_pair=WeChatKeyPair(key_hex="22" * 32, salt_hex="11" * 16),
            sqlcipher_binary=sqlcipher,
            runner=runner,
        )
    assert not output.exists()


def test_virtual_table_database_accepts_extension_specific_integrity_error(monkeypatch, tmp_path: Path):
    database = tmp_path / "fts.db"
    database.write_bytes(b"SQLite format 3\x00")

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql):
            if "sqlite_master" in sql:
                return type("Cursor", (), {"fetchall": lambda self: [("CREATE VIRTUAL TABLE docs USING wcdb_fts",)]})()
            raise sqlite3.OperationalError("SQL logic error")

    monkeypatch.setattr("local_helper.wechat_macos.decryptor.sqlite3.connect", lambda *_args, **_kwargs: FakeConnection())

    verify_plain_sqlite(database)
    assert database.exists()


def test_verify_sqlcipher4_page_accepts_matching_raw_key():
    raw_key = bytes(range(32))
    salt = bytes(range(16))
    encrypted_payload = bytes((index * 17) % 256 for index in range(4016))
    mac_salt = bytes(value ^ 0x3A for value in salt)
    mac_key = hashlib.pbkdf2_hmac("sha512", raw_key, mac_salt, 2, dklen=32)
    page_mac = hmac.new(mac_key, encrypted_payload, hashlib.sha512)
    page_mac.update(struct.pack("<I", 1))
    page = salt + encrypted_payload + page_mac.digest()

    assert len(page) == 4096
    assert verify_sqlcipher4_page(raw_key, page)


def test_verify_sqlcipher4_page_rejects_wrong_key_and_malformed_page():
    assert not verify_sqlcipher4_page(bytes(32), bytes(4096))
    assert not verify_sqlcipher4_page(bytes(31), bytes(4096))
    assert not verify_sqlcipher4_page(bytes(32), bytes(4095))


def test_derive_sqlcipher4_key_uses_wechat_4_1_12_parameters():
    password = bytes(range(32))
    salt = bytes(range(16))

    assert derive_sqlcipher4_key(password, salt) == hashlib.pbkdf2_hmac(
        "sha512", password, salt, 256_000, dklen=32
    )


def test_lldb_capture_only_accepts_observed_sqlcipher_kdf_shape():
    assert is_wechat_sqlcipher_kdf(32, 16, 256_000)
    assert not is_wechat_sqlcipher_kdf(31, 16, 256_000)
    assert not is_wechat_sqlcipher_kdf(32, 15, 256_000)
    assert not is_wechat_sqlcipher_kdf(32, 16, 2)


def test_captured_password_derives_and_verifies_every_matching_database(tmp_path: Path):
    password = bytes(range(32))
    matching = []
    for index in range(2):
        salt = bytes([index + 1]) * 16
        raw_key = derive_sqlcipher4_key(password, salt)
        encrypted_payload = bytes((offset * (index + 3)) % 256 for offset in range(4016))
        mac_salt = bytes(value ^ 0x3A for value in salt)
        mac_key = hashlib.pbkdf2_hmac("sha512", raw_key, mac_salt, 2, dklen=32)
        page_mac = hmac.new(mac_key, encrypted_payload, hashlib.sha512)
        page_mac.update(struct.pack("<I", 1))
        database = tmp_path / f"matching-{index}.db"
        database.write_bytes(salt + encrypted_payload + page_mac.digest())
        matching.append(database)

    invalid = tmp_path / "invalid.db"
    invalid.write_bytes(bytes(4096))

    pairs = derive_verified_key_pairs(password, (*matching, invalid))

    assert {pair.salt_hex for pair in pairs} == {"01" * 16, "02" * 16}


def _encrypted_page(path: Path, password: bytes, salt: bytes) -> Path:
    raw_key = derive_sqlcipher4_key(password, salt)
    encrypted_payload = bytes((offset * 7) % 256 for offset in range(4016))
    mac_salt = bytes(value ^ 0x3A for value in salt)
    mac_key = hashlib.pbkdf2_hmac("sha512", raw_key, mac_salt, 2, dklen=32)
    page_mac = hmac.new(mac_key, encrypted_payload, hashlib.sha512)
    page_mac.update(struct.pack("<I", 1))
    path.write_bytes(salt + encrypted_payload + page_mac.digest())
    return path
