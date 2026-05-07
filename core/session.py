"""ChatSession：CLI 主循环、指令分发、轮次计数、归档触发。"""

import os
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from prompt_toolkit import prompt as pt_prompt

from config import get_ex_dir, ensure_ex_dirs, EXES_DIR, ARCHIVE_THRESHOLD, get_collection_name
from core.token_counter import TokenCounter


class ChatSession:
    def __init__(self, default_length=40):
        self.talk_length = default_length
        self.history = []
        self.counter = TokenCounter()
        self.engine = None
        self.running = True
        self.slug = ""
        self.turn_count = 0

        # 指令注册表
        self.commands: dict[str, callable] = {}

    def register_command(self, name: str, func: callable, doc: str = ""):
        """注册一个斜杠指令。"""
        self.commands[name] = func
        func.__doc__ = doc

    def _setup(self):
        """初始化对话环境。"""
        self.slug = pt_prompt("请输入镜像名称: ").strip()
        if not self.slug:
            print("错误: 镜像名称不能为空")
            sys.exit(1)

        ex_dir = get_ex_dir(self.slug)
        if not ex_dir.exists():
            print(f"错误: 镜像 [{self.slug}] 不存在。请先用 /create 创建。")
            sys.exit(1)

        # 延迟导入避免循环依赖
        from core.engine import ChatEngine
        from memory.vector_store import VectorStore
        from memory.embedder import Embedder
        from config import get_embedding_config

        embedder = None
        vector_store = None
        chroma_dir = ex_dir / "chroma_db"
        if chroma_dir.exists() and any(chroma_dir.iterdir()):
            try:
                emb_cfg = get_embedding_config()
                embedder = Embedder(
                    api_key=emb_cfg["api_key"],
                    base_url=emb_cfg["base_url"],
                    model=emb_cfg["model"],
                )
                vector_store = VectorStore(
                    persist_dir=str(chroma_dir),
                    collection_name=get_collection_name(self.slug),
                )
                print(f"--- 向量库已加载 ({vector_store.count()} 条记录) ---")
            except Exception as e:
                print(f"--- 向量库加载失败: {e}，将以纯文本模式运行 ---")

        self.engine = ChatEngine(
            slug=self.slug, vector_store=vector_store, embedder=embedder
        )

    def _process_command(self, user_input: str):
        """解析并执行斜杠指令。"""
        parts = user_input[1:].split(maxsplit=1)
        cmd_name = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""

        if cmd_name in self.commands:
            self.commands[cmd_name](arg)
        elif cmd_name in ("exit", "quit"):
            self.do_exit()
        elif cmd_name == "clear":
            self.history = []
            print("--- 对话历史已清空 ---")
        elif cmd_name == "help":
            self.do_help()
        else:
            print(f"未知指令: /{cmd_name}。输入 /help 查看列表。")

    def do_help(self, _=""):
        """显示所有可用指令。"""
        print("\n[可用指令]")
        print("  /help    - 显示帮助")
        print("  /clear   - 清空对话上下文")
        print("  /status  - 查看 Token 使用情况")
        print("  /exit    - 退出对话")
        for name, func in self.commands.items():
            doc = func.__doc__ or "无描述"
            print(f"  /{name} - {doc}")
        print()

    def do_exit(self, _=""):
        """退出程序。"""
        # 检查是否需要归档
        if self.turn_count > 0:
            self._maybe_archive()
        print("对话已结束")
        self.running = False

    def _maybe_archive(self):
        """询问是否归档本次对话。"""
        if self.turn_count < 5:
            return
        try:
            ans = pt_prompt("是否归档本次对话？(y/n): ").strip().lower()
            if ans in ("y", "yes", "是"):
                self._archive_session()
        except (KeyboardInterrupt, EOFError):
            pass

    def _archive_session(self):
        """将对话历史压缩为摘要并保存。"""
        sessions_dir = get_ex_dir(self.slug) / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        session_file = sessions_dir / f"session_{timestamp}.md"

        # 简单保存对话原文（后续可改为 LLM 压缩摘要）
        lines = [f"# 对话记录 — {timestamp}\n"]
        for msg in self.history:
            role = "用户" if msg["role"] == "user" else self.slug
            lines.append(f"**{role}**: {msg['content']}\n")

        session_file.write_text("\n".join(lines), encoding="utf-8")
        print(f"--- 对话已归档: {session_file.name} ---")

    def _chat(self, user_msg: str):
        """处理聊天对话。"""
        try:
            reply, usage = self.engine.chat(user_msg, self.history)
            print(f"\n{self.slug}: {reply}")

            self.counter.update(usage)
            self.history.append({"role": "user", "content": user_msg})
            self.history.append({"role": "assistant", "content": reply})
            self.turn_count += 1

            # 维持对话长度
            if len(self.history) > self.talk_length * 2:
                self.history = self.history[-(self.talk_length * 2) :]

            # 归档触发
            if self.turn_count >= ARCHIVE_THRESHOLD:
                self._maybe_archive()
                self.turn_count = 0

        except Exception as e:
            print(f"\n[错误]: {e}")

    def run(self):
        """主循环。"""
        self._setup()

        while self.running:
            try:
                user_input = pt_prompt("\n我: ").strip()
                if not user_input:
                    continue

                if user_input.startswith("/"):
                    self._process_command(user_input)
                else:
                    self._chat(user_input)

            except KeyboardInterrupt:
                self.do_exit()
            except EOFError:
                self.do_exit()
            except Exception as e:
                print(f"运行异常: {e}")
                break

        self.counter.display_summary()


def clear_screen():
    os.system("cls" if os.name == "nt" else "clear")
