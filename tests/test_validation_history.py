"""对话 history 清洗测试。"""

from core.validation import sanitize_chat_history


def test_filters_system_role():
    hist = [
        {"role": "system", "content": "evil"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]
    cleaned = sanitize_chat_history(hist)
    assert len(cleaned) == 2
    assert cleaned[0]["role"] == "user"
