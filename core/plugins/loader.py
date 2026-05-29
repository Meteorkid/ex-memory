"""插件加载器：从目录加载和管理插件。"""

import importlib
import logging
from pathlib import Path
from typing import Optional

from core.plugins.base import BasePlugin

logger = logging.getLogger(__name__)


class PluginLoader:
    """从 plugins/ 目录加载插件。"""

    def __init__(self, plugins_dir: str = "plugins"):
        self.plugins_dir = Path(plugins_dir)
        self.plugins: dict[str, BasePlugin] = {}

    def discover(self) -> list[str]:
        """发现可用插件。"""
        if not self.plugins_dir.exists():
            return []

        plugins = []
        for path in self.plugins_dir.iterdir():
            if path.is_dir() and (path / "__init__.py").exists():
                plugins.append(path.name)
            elif path.suffix == ".py" and path.name.startswith("plugin_"):
                plugins.append(path.stem)
        return plugins

    def load(self, name: str) -> Optional[BasePlugin]:
        """加载指定插件。"""
        if name in self.plugins:
            return self.plugins[name]

        try:
            # 尝试从 plugins 目录导入
            module = importlib.import_module(f"plugins.{name}")
            plugin_class = getattr(module, "Plugin", None)
            if plugin_class and issubclass(plugin_class, BasePlugin):
                plugin = plugin_class()
                plugin.on_load()
                self.plugins[name] = plugin
                logger.info("Loaded plugin: %s", name)
                return plugin
        except Exception as e:
            logger.error("Failed to load plugin %s: %s", name, e)
        return None

    def load_all(self) -> int:
        """加载所有发现的插件。"""
        count = 0
        for name in self.discover():
            if self.load(name):
                count += 1
        return count

    def unload(self, name: str):
        """卸载指定插件。"""
        if name in self.plugins:
            self.plugins[name].on_unload()
            del self.plugins[name]
            logger.info("Unloaded plugin: %s", name)

    def run_on_message(self, message: dict) -> dict:
        """让所有插件处理消息。"""
        for plugin in self.plugins.values():
            result = plugin.on_message(message)
            if result is not None:
                message = result
        return message

    def run_on_response(self, response: dict) -> dict:
        """让所有插件处理回复。"""
        for plugin in self.plugins.values():
            result = plugin.on_response(response)
            if result is not None:
                response = result
        return response
