"""插件基类和元数据定义。"""

from dataclasses import dataclass, field
from typing import Optional, Any


@dataclass
class PluginMeta:
    """插件元数据。"""
    name: str
    version: str = "1.0.0"
    author: str = ""
    description: str = ""


class BasePlugin:
    """插件基类，所有插件必须继承此类。"""

    meta: PluginMeta = PluginMeta(name="unnamed")

    def on_load(self):
        """插件加载时调用。"""
        pass

    def on_unload(self):
        """插件卸载时调用。"""
        pass

    def on_message(self, message: dict) -> Optional[dict]:
        """处理用户消息，可修改或拦截消息。

        Args:
            message: {"role": "user", "content": "..."}

        Returns:
            修改后的消息，或 None 表示不修改
        """
        return None

    def on_response(self, response: dict) -> Optional[dict]:
        """处理 AI 回复，可修改回复内容。

        Args:
            response: {"role": "assistant", "content": "..."}

        Returns:
            修改后的回复，或 None 表示不修改
        """
        return None
