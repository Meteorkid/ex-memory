"""示例插件：统计对话字数。"""

from core.plugins.base import BasePlugin, PluginMeta


class Plugin(BasePlugin):
    meta = PluginMeta(
        name="word-count",
        version="1.0.0",
        author="ex-memory",
        description="统计对话总字数",
    )

    def __init__(self):
        self.total_chars = 0

    def on_message(self, message: dict) -> None:
        content = message.get("content", "")
        self.total_chars += len(content)
        return None

    def get_stats(self) -> dict:
        return {"total_chars": self.total_chars}
