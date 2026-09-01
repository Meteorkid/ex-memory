"""ChatSession：CLI 主循环、指令分发、轮次计数、归档触发。"""

from typing import Callable
import json
import logging
import sys
from datetime import datetime
from prompt_toolkit import prompt as pt_prompt

from config import get_ex_dir, ARCHIVE_THRESHOLD
from core.token_counter import TokenCounter
from core.factory import create_engine_and_store
from core.validation import validate_user_input

logger = logging.getLogger("ex-memory")

# 关系阶段定义
RELATIONSHIP_STAGES = {
    "dating": "热恋期",
    "conflicted": "磨合期",
    "broken": "分手期",
    "healing": "治愈期",
}


class ChatSession:
    def __init__(self, default_length=40):
        self.talk_length = default_length
        self.history = []
        self.counter = TokenCounter()
        self.engine = None
        self.running = True
        self.slug = ""
        self.turn_count = 0
        self.commands: dict[str, Callable[[str], None]] = {}
        self.relationship_stage = "dating"  # 默认热恋期

    def register_command(self, name: str, func: Callable[[str], None], doc: str = ""):
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

        # 加载关系阶段
        self._load_stage()
        stage_label = RELATIONSHIP_STAGES.get(self.relationship_stage, "未知")
        print(f"--- 关系阶段: {stage_label} ({self.relationship_stage}) ---")

    def _load_stage(self):
        """从 meta.json 加载关系阶段。"""
        meta_path = get_ex_dir(self.slug) / "meta.json"
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                stage = meta.get("relationship_stage", "dating")
                if stage in RELATIONSHIP_STAGES:
                    self.relationship_stage = stage
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("加载 meta.json 失败: %s", e)

    def _save_stage(self):
        """保存关系阶段到 meta.json。"""
        meta_path = get_ex_dir(self.slug) / "meta.json"
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                meta = {}
        else:
            meta = {}

        meta["relationship_stage"] = self.relationship_stage
        meta["updated_at"] = datetime.now().isoformat()

        meta_path.parent.mkdir(parents=True, exist_ok=True)
        meta_path.write_text(
            json.dumps(meta, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def do_stage(self, arg: str):
        """设置关系阶段。用法: /stage {dating|conflicted|broken|healing}"""
        stage = arg.strip().lower()
        if not stage:
            current_label = RELATIONSHIP_STAGES.get(self.relationship_stage, "未知")
            print(f"\n当前关系阶段: {current_label} ({self.relationship_stage})")
            print("可用阶段:")
            for key, label in RELATIONSHIP_STAGES.items():
                marker = " ← 当前" if key == self.relationship_stage else ""
                print(f"  {key} - {label}{marker}")
            return

        if stage not in RELATIONSHIP_STAGES:
            print(f"未知阶段: {stage}")
            print("可用阶段: " + ", ".join(RELATIONSHIP_STAGES.keys()))
            return

        old_stage = self.relationship_stage
        self.relationship_stage = stage
        self._save_stage()

        # 同步更新引擎的阶段
        if self.engine:
            self.engine.relationship_stage = stage

        stage_label = RELATIONSHIP_STAGES[stage]
        print(
            f"--- 关系阶段已切换: {RELATIONSHIP_STAGES[old_stage]} → {stage_label} ---"
        )

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
        elif cmd_name == "stage":
            self.do_stage(arg)
        elif cmd_name == "help":
            self.do_help()
        else:
            print(f"未知指令: /{cmd_name}。输入 /help 查看列表。")

    def do_help(self, _=""):
        print("\n[可用指令]")
        print("  /help    - 显示帮助")
        print("  /clear   - 清空对话上下文")
        print("  /status  - 查看 Token 使用情况")
        print("  /stage   - 查看/设置关系阶段")
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
        """归档逻辑已提取到 core/session_archive，Web 与 CLI 共用。"""
        from core.session_archive import archive_session

        result = archive_session(
            self.slug,
            self.history,
            vector_store=self.vector_store,
            embedder=self.embedder,
            engine=self.engine,
        )
        if result:
            print(f"--- 对话已归档: {result['session_file']} ---")

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
                self.history = self.history[-(self.talk_length * 2) :]

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
