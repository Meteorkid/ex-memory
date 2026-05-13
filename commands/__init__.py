"""CLI 命令注册表。

每个命令模块在导入时调用 register() 注册自身。
run.py 通过 COMMANDS 字典查表分发。
"""

COMMANDS: dict[str, callable] = {}


def register(name: str, func: callable):
    """注册一个命令处理函数。"""
    COMMANDS[name] = func


# 导入所有命令模块以触发注册
from commands import (
    create, list_cmd, help_cmd, update, reflect,
    backup, rollback, let_go, keychain, web,
)
