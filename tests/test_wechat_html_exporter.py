import json
import sqlite3
from pathlib import Path

from local_helper.export.html_exporter import export_conversation
from local_helper.export.media import RESOURCE_DIRECTORIES, extract_media_hints
from local_helper.wechat_macos.reader import Message, MessageKind


def message(
    *,
    local_id: int,
    content: str,
    kind: MessageKind,
    raw_type: int = 1,
) -> Message:
    return Message(
        local_id=local_id,
        create_time=1_700_000_000 + local_id,
        content=content,
        direction=0,
        raw_type=raw_type,
        app_subtype=None,
        sender_wxid="wxid_friend",
        kind=kind,
        shard="message_0.sqlite",
    )


def test_export_creates_complete_offline_structure(tmp_path: Path):
    account_root = tmp_path / "account"
    account_root.mkdir()
    result = export_conversation(
        messages=(message(local_id=1, content="hello", kind=MessageKind.TEXT),),
        output_root=tmp_path / "exports",
        account_root=account_root,
        session_wxid="wxid_friend",
        display_name="测试/好友",
        owner_wxid="wxid_owner",
        wechat_version="4.1.12",
        schema_fingerprint="a" * 64,
        exporter_version="0.1.0",
    )

    assert result.status == "complete"
    assert result.message_count == 1
    assert all(
        (result.output_dir / directory).is_dir() for directory in RESOURCE_DIRECTORIES
    )
    manifest = json.loads(result.manifest_file.read_text(encoding="utf-8"))
    assert manifest["message_count"] == 1
    assert manifest["status"] == "complete"
    html = result.html_file.read_text(encoding="utf-8")
    assert "hello" in html
    assert "https://" not in html


def test_export_copies_media_by_hash_and_marks_missing(tmp_path: Path):
    account_root = tmp_path / "account"
    source = account_root / "msg" / "video" / "2026-01" / "clip.mp4"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"video-bytes")
    result = export_conversation(
        messages=(
            message(
                local_id=1,
                content='<video path="msg/video/2026-01/clip.mp4"/>',
                kind=MessageKind.VIDEO,
                raw_type=43,
            ),
            message(
                local_id=2,
                content='<video path="missing.mp4"/>',
                kind=MessageKind.VIDEO,
                raw_type=43,
            ),
        ),
        output_root=tmp_path / "exports",
        account_root=account_root,
        session_wxid="wxid_friend",
        display_name="friend",
        owner_wxid="wxid_owner",
        wechat_version="4.1.12",
        schema_fingerprint="a" * 64,
        exporter_version="0.1.0",
    )

    media = list((result.output_dir / "video").iterdir())
    assert len(media) == 1
    assert media[0].read_bytes() == b"video-bytes"
    manifest = json.loads(result.manifest_file.read_text(encoding="utf-8"))
    assert manifest["media"]["missing"] == 1
    assert result.status == "partial"


def test_unknown_message_is_preserved_and_script_content_is_escaped(tmp_path: Path):
    account_root = tmp_path / "account"
    account_root.mkdir()
    payload = "</script><script>alert(1)</script>"
    result = export_conversation(
        messages=(
            message(
                local_id=9, content=payload, kind=MessageKind.UNKNOWN, raw_type=999
            ),
        ),
        output_root=tmp_path / "exports",
        account_root=account_root,
        session_wxid="wxid_friend",
        display_name="friend",
        owner_wxid="wxid_owner",
        wechat_version="4.1.12",
        schema_fingerprint="a" * 64,
        exporter_version="0.1.0",
    )

    html = result.html_file.read_text(encoding="utf-8")
    assert payload not in html
    assert "\\u003c/script>" in html
    assert (result.output_dir / "raw" / "message_0.sqlite-9.txt").read_text(
        encoding="utf-8"
    ) == payload
    assert result.status == "partial"


def test_extract_media_hints_deduplicates_paths():
    content = (
        '<appmsg path="msg/file/report.pdf"><filename>report.pdf</filename></appmsg>'
    )
    hints = extract_media_hints(content)
    assert "msg/file/report.pdf" in hints
    assert hints.count("report.pdf") == 1


def test_voice_is_exported_from_media_database(tmp_path: Path):
    account_root = tmp_path / "account"
    account_root.mkdir()
    media_db = tmp_path / "media_0.db"
    with sqlite3.connect(media_db) as connection:
        connection.execute("CREATE TABLE Name2Id(user_name TEXT)")
        connection.execute(
            "INSERT INTO Name2Id(rowid, user_name) VALUES (7, 'wxid_friend')"
        )
        connection.execute(
            "CREATE TABLE VoiceInfo(chat_name_id INTEGER, local_id INTEGER, svr_id INTEGER, "
            "data_index INTEGER, voice_data BLOB)"
        )
        connection.execute(
            "INSERT INTO VoiceInfo VALUES (7, 10, 99, 0, ?)",
            (b"\x02#!SILK_V3voice",),
        )
    voice_message = message(
        local_id=10, content="", kind=MessageKind.VOICE, raw_type=34
    )
    voice_message = Message(**{**voice_message.__dict__, "server_id": 99})

    result = export_conversation(
        messages=(voice_message,),
        output_root=tmp_path / "exports",
        account_root=account_root,
        session_wxid="wxid_friend",
        display_name="friend",
        owner_wxid="wxid_owner",
        wechat_version="4.1.12",
        schema_fingerprint="a" * 64,
        exporter_version="0.1.0",
        media_databases=(media_db,),
    )

    exported = next((result.output_dir / "voice").glob("*.silk"))
    assert exported.read_bytes() == b"#!SILK_V3voice"
    assert result.status == "partial"


def test_bare_media_md5_finds_file_with_extension(tmp_path: Path):
    account_root = tmp_path / "account"
    digest = "ab" * 16
    source = account_root / "msg" / "attach" / "folder" / "Img" / f"{digest}.dat"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"original-image-container")

    result = export_conversation(
        messages=(
            message(local_id=3, content=digest, kind=MessageKind.IMAGE, raw_type=3),
        ),
        output_root=tmp_path / "exports",
        account_root=account_root,
        session_wxid="wxid_friend",
        display_name="friend",
        owner_wxid="wxid_owner",
        wechat_version="4.1.12",
        schema_fingerprint="a" * 64,
        exporter_version="0.1.0",
    )

    exported = next((result.output_dir / "image").iterdir())
    assert exported.read_bytes() == b"original-image-container"
