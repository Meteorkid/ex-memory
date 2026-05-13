"""QQ 解析器测试。"""
import tempfile
from pathlib import Path
import pytest

from parsers.qq_parser import parse, detect_qq_format, _normalize


class TestDetectQqFormat:
    def test_mht_extension(self, tmp_path):
        f = tmp_path / "chat.mht"
        f.write_text("MIME-Version: 1.0")
        assert detect_qq_format(str(f)) == "mht"

    def test_mhtml_extension(self, tmp_path):
        f = tmp_path / "chat.mhtml"
        f.write_text("MIME-Version: 1.0")
        assert detect_qq_format(str(f)) == "mht"

    def test_txt_format_a(self, tmp_path):
        content = "2024-01-15 20:30:45 张三(12345)\n你好啊\n\n"
        f = tmp_path / "chat.txt"
        f.write_text(content, encoding="utf-8")
        fmt = detect_qq_format(str(f))
        assert fmt in ("qq_txt_a", "qq_txt_b")  # A 优先

    def test_txt_format_c(self, tmp_path):
        content = "张三(12345) 2024-01-15 20:30:45\n你好啊\n\n"
        f = tmp_path / "chat.txt"
        f.write_text(content, encoding="utf-8")
        fmt = detect_qq_format(str(f))
        assert fmt == "qq_txt_c"

    def test_plaintext_fallback(self, tmp_path):
        f = tmp_path / "chat.txt"
        f.write_text("这是一段普通文本", encoding="utf-8")
        assert detect_qq_format(str(f)) == "plaintext"


class TestParseTxtA:
    """测试格式 A（时间戳 + 昵称 + QQ号）。"""

    def test_basic_parse(self, tmp_path):
        content = (
            "2024-01-15 20:30:45 张三(12345)\n"
            "你好啊\n\n"
            "2024-01-15 20:31:00 李四(67890)\n"
            "嗨，最近怎么样？\n\n"
        )
        f = tmp_path / "chat.txt"
        f.write_text(content, encoding="utf-8")

        messages = parse(str(f), target_name="张三")
        assert len(messages) == 2
        assert messages[0]["sender"] == "张三"
        assert messages[0]["content"] == "你好啊"
        assert messages[0]["is_target"] is True
        assert messages[1]["sender"] == "李四"
        assert messages[1]["is_target"] is False

    def test_multiline_message(self, tmp_path):
        content = (
            "2024-01-15 20:30:45 张三(12345)\n"
            "第一行\n"
            "第二行\n"
            "第三行\n\n"
        )
        f = tmp_path / "chat.txt"
        f.write_text(content, encoding="utf-8")

        messages = parse(str(f), target_name="张三")
        assert len(messages) == 1
        assert messages[0]["content"] == "第一行\n第二行\n第三行"

    def test_empty_file(self, tmp_path):
        f = tmp_path / "chat.txt"
        f.write_text("", encoding="utf-8")
        messages = parse(str(f))
        assert messages == []


class TestParseTxtB:
    """测试格式 B（时间戳 + 昵称，无 QQ 号）。"""

    def test_basic_parse(self, tmp_path):
        content = (
            "2024-01-15 20:30:45 张三\n"
            "你好\n\n"
            "2024-01-15 20:31:00 李四\n"
            "嗯嗯\n\n"
        )
        f = tmp_path / "chat.txt"
        f.write_text(content, encoding="utf-8")

        messages = parse(str(f), target_name="张三")
        assert len(messages) == 2
        assert messages[0]["is_target"] is True
        assert messages[1]["is_target"] is False


class TestParseTxtC:
    """测试格式 C（发送者名(QQ号) 时间戳）。"""

    def test_basic_parse(self, tmp_path):
        content = (
            "张三(12345) 2024-01-15 20:30:45\n"
            "你好啊\n\n"
            "李四(67890) 2024-01-15 20:31:00\n"
            "嗨\n\n"
        )
        f = tmp_path / "chat.txt"
        f.write_text(content, encoding="utf-8")

        messages = parse(str(f), target_name="张三")
        assert len(messages) == 2
        assert messages[0]["sender"] == "张三"
        assert messages[0]["content"] == "你好啊"
        assert messages[0]["is_target"] is True
        assert messages[1]["sender"] == "李四"
        assert messages[1]["content"] == "嗨"
        assert messages[1]["is_target"] is False


class TestNormalize:
    def test_target_name_matching(self):
        raw = [
            {"timestamp": "2024-01-01 10:00:00", "sender": "张三", "content": "你好"},
            {"timestamp": "2024-01-01 10:01:00", "sender": "我", "content": "嗨"},
        ]
        result = _normalize(raw, target_name="张三")
        assert len(result) == 2
        assert result[0]["is_target"] is True
        assert result[1]["is_target"] is False

    def test_no_target_name(self):
        raw = [
            {"timestamp": "2024-01-01 10:00:00", "sender": "张三", "content": "你好"},
            {"timestamp": "2024-01-01 10:01:00", "sender": "我", "content": "嗨"},
        ]
        result = _normalize(raw, target_name="")
        assert result[0]["is_target"] is True  # 非"我"都是 target
        assert result[1]["is_target"] is False

    def test_empty_content_filtered(self):
        raw = [
            {"timestamp": "2024-01-01 10:00:00", "sender": "张三", "content": ""},
            {"timestamp": "2024-01-01 10:01:00", "sender": "张三", "content": "有内容"},
        ]
        result = _normalize(raw, target_name="张三")
        assert len(result) == 1
        assert result[0]["content"] == "有内容"

    def test_partial_name_matching(self):
        raw = [
            {"timestamp": "2024-01-01 10:00:00", "sender": "小张三", "content": "你好"},
        ]
        result = _normalize(raw, target_name="张三")
        assert result[0]["is_target"] is True
