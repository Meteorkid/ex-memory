"""插件系统：加载和管理扩展插件。"""

from core.plugins.base import BasePlugin, PluginMeta
from core.plugins.loader import PluginLoader

__all__ = ["BasePlugin", "PluginMeta", "PluginLoader"]
