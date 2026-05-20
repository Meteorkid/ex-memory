"""ChatSession：CLI 主循环、指令分发、轮次计数、归档触发。"""

import logging
import sys
from datetime import datetime
from pathlib import Path
from prompt_toolkit import prompt as pt_prompt

from config import get_ex_dir, ARCHIVE_THRESHOLD
from core.token_counter import TokenCounter
from core.factory import create_engine_and_store
from core.validation import validate_user_input

logger = logging.getLogger("ex-memory")


class ChatSession:
    def __init__(self, default_length=40):
        self.talk_length = default_length
        self.history = []
        self.counter = TokenCounter()
        self.engine = None
        self.running = True
        self.slug = ""
        self.turn_count = 0
        self.commands: dict[str, callable] = {}

    def register_command(self, name: str, func: callable, doc: str = ""):
        self.commands[name] = func
        func.__doc__ = doc

    def _setup(self):
        """初始化对话环境。"""
        self.slug = pt_prompt("请输入镜像名称: ").strip()
        try:
            from core.validation import validate_slug
            self.slug = validate_slug(self.slug)
        except ValueError as e:
            print(f"错误: {e}")
            sys.exit(1)

        ex_dir = get_ex_dir(self.slug)
        if not ex_dir.exists():
            print(f"错误: 镜像 [{self.slug}] 不存在。请先用 /create 创建。")
            sys.exit(1)

        self.engine, vector_store, embedder = create_engine_and_store(self.slug)
        self.vector_store = vector_store
        self.embedder = embedder
        if vector_store:
            print(f"--- 向量库已加载 ({vector_store.count()} 条记录) ---")
        else:
            print("--- 纯文本模式（无 RAG 检索） ---")

    def _process_command(self, user_input: str):
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
        if self.turn_count > 0:
            self._maybe_archive()
        print("对话已结束")
        self.running = False

    def _maybe_archive(self):
        if self.turn_count < 5:
            return
        try:
            ans = pt_prompt("是否归档本次对话？(y/n): ").strip().lower()
            if ans in ("y", "yes", "是"):
                self._archive_session()
        except (KeyboardInterrupt, EOFError):
            pass

    def _archive_session(self):
        sessions_dir = get_ex_dir(self.slug) / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        session_file = sessions_dir / f"session_{timestamp}.md"

        lines = [f"# 对话记录 — {timestamp}\n"]
        for msg in self.history:
            role = "用户" if msg["role"] == "user" else self.slug
            lines.append(f"**{role}**: {msg['content']}\n")

        session_file.write_text("\n".join(lines), encoding="utf-8")
        logger.info("对话已归档: %s", session_file.name)
        print(f"--- 对话已归档: {session_file.name} ---")

        # 生成 LLM 语义摘要
        self._generate_summary(sessions_dir, timestamp)

    def _generate_summary(self, sessions_dir: Path, timestamp: str):
        """调用 LLM 生成会话语义摘要，用于下次启动时快速恢复上下文。"""
        from config import get_llm_config, get_llm_client

        cfg = get_llm_config()
        if not cfg["api_key"]:
            return

        try:
            prompts_dir = Path(__file__).resolve().parent.parent / "prompts"
            prompt_template = (prompts_dir / "session_summary.md").read_text(encoding="utf-8")

            # 只取最近 20 轮做摘要（避免上下文超限）
            recent = self.history[-40:]
            history_text = "\n".join(
                f"{'用户' if m['role'] == 'user' else self.slug}: {m['content'][:300]}"
                for m in recent
            )

            client = get_llm_client()
            response = client.chat.completions.create(
                model=cfg["model"],
                messages=[
                    {"role": "system", "content": prompt_template},
                    {"role": "user", "content": f"请压缩以下对话为摘要：\n\n{history_text}"},
                ],
                temperature=0.3,
            )
            summary = response.choices[0].message.content

            summary_file = sessions_dir / f"session_{timestamp}_summary.md"
            summary_file.write_text(summary, encoding="utf-8")
            logger.info("会话摘要已生成: %s", summary_file.name)

            # 追加到引擎的 session_summaries（当前 session 可能还没结束，但预先加载）
            if self.engine:
                self.engine.session_summaries.append(summary)
                # Token 预算控制：过大的摘要列表弹出旧项
                from core.validation import estimate_tokens
                from config import LLM_MAX_CONTEXT_CHARS
                while (len(self.engine.session_summaries) > 5
                       and estimate_tokens("\n".join(self.engine.session_summaries)) > LLM_MAX_CONTEXT_CHARS * 0.3):
                    self.engine.session_summaries.pop(0)

            # 可选：加入向量库
            if self.vector_store and self.embedder:
                try:
                    self.vector_store.add_session_summary(summary, self.slug, self.embedder)
                except Exception:
                    logger.debug("摘要写入向量库失败（非关键）")

            # 同步更新 SKILL.md 的记忆段
            self._update_skill_memory(summary)

        except Exception as e:
            logger.warning("生成会话摘要失败（已降级，原始归档完好）: %s", e)

    def _update_skill_memory(self, new_summary: str):
        """将新摘要追加到 SKILL.md 的 PART A 末尾。"""
        from config import get_ex_dir
        skill_path = get_ex_dir(self.slug) / "SKILL.md"
        if not skill_path.exists():
            return

        try:
            content = skill_path.read_text(encoding="utf-8")
            # 在 PART A 的末尾追加摘要
            marker = "---\n\n## PART B"
            if marker in content:
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
                addition = f"\n\n### 对话摘要 ({timestamp})\n{new_summary}\n"
                content = content.replace(marker, addition + marker)
                skill_path.write_text(content, encoding="utf-8")
                logger.info("SKILL.md 已同步最新摘要")
        except Exception as e:
            logger.debug("更新 SKILL.md 摘要失败（非关键）: %s", e)

    def _chat(self, user_msg: str):
        try:
            user_msg = validate_user_input(user_msg)
        except ValueError as e:
            print(f"\n[输入错误]: {e}")
            logger.warning("输入校验失败: %s", e)
            return

        try:
            reply, _stickers, usage = self.engine.chat(user_msg, self.history)
            print(f"\n{self.slug}: {reply}")

            self.counter.update(usage)
            self.history.append({"role": "user", "content": user_msg})
            self.history.append({"role": "assistant", "content": reply})
            self.turn_count += 1

            if len(self.history) > self.talk_length * 2:
                self.history = self.history[-(self.talk_length * 2):]

            if self.turn_count >= ARCHIVE_THRESHOLD:
                self._maybe_archive()
                self.turn_count = 0

        except Exception as e:
            logger.error("对话出错: %s", e, exc_info=True)
            print(f"\n[错误]: {e}")

    def run(self):
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
                logger.error("运行异常: %s", e, exc_info=True)
                print(f"运行异常: {e}")
                break

        self.counter.display_summary()
