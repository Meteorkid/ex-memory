"""前任记忆智能体 — 主入口。"""

import sys
import logging
from commands import COMMANDS
from commands.chat import cmd_chat
from config import init_app, require_privacy_consent

logger = logging.getLogger("ex-memory")


def print_banner():
    print()
    print("  ┌─────────────────────────────────────┐")
    print("  │     前任记忆智能体  ex-memory        │")
    print("  │  把一段记忆，变成可以对话的人        │")
    print("  └─────────────────────────────────────┘")
    print()
    print("  /create   创建新的记忆镜像")
    print("  /{名称}   进入已有镜像对话")
    print("  /list     列出所有镜像")
    print("  /web      启动 Gradio Web 界面")
    print("  /help     帮助")
    print("  /exit     退出")
    print()


def _dispatch(cmd: str, arg: str):
    """查表分发命令。"""
    if cmd in ("exit", "quit"):
        print("再见。")
        return "exit"

    handler = COMMANDS.get(cmd)
    if handler is not None:
        handler(arg)
    else:
        # 兜底：把命令名当作镜像 slug 进入对话
        cmd_chat(cmd)

    return None


def main():
    print_banner()
    init_app()
    require_privacy_consent()

    if len(sys.argv) > 1:
        arg = sys.argv[1]
        if arg.startswith("/"):
            parts = arg[1:].split(maxsplit=1)
            cmd = parts[0].lower()
            carg = parts[1] if len(parts) > 1 else ""
            _dispatch(cmd, carg)
        return

    while True:
        try:
            user_input = input("\n> ").strip()
            if not user_input:
                continue

            if user_input.startswith("/"):
                parts = user_input[1:].split(maxsplit=1)
                cmd = parts[0].lower()
                arg = parts[1] if len(parts) > 1 else ""

                if _dispatch(cmd, arg) == "exit":
                    break
            else:
                print("输入 /help 查看可用指令。")

        except KeyboardInterrupt:
            print("\n再见。")
            break
        except EOFError:
            print("\n再见。")
            break


if __name__ == "__main__":
    main()
