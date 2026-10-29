"""闭环协议验收：真实服务与缓存，替换模型/检索输出和摘要生成。"""

import json
import os
from pathlib import Path
from types import SimpleNamespace
import uuid

ROOT = Path(__file__).resolve().parents[3]


def install_fixture():
    """只允许在统一 runner 创建的副本里启动。"""
    workspace = ROOT.parent
    if not (workspace / ".ex-memory-validation-workspace").is_file():
        raise RuntimeError("必须通过隔离 run_checks.py 执行此验收")
    database = Path(os.environ["LOOP_DB"]).resolve()
    exes = Path(os.environ["LOOP_EXES"]).resolve()
    if workspace not in database.parents or workspace not in exes.parents:
        raise RuntimeError("LOOP_DB 与 LOOP_EXES 必须位于本次隔离工作区")

    import config
    import core.factory
    import core.session_archive
    import server.auth as auth
    from core.engine import ChatEngine

    config.EXES_DIR = exes
    config.DATABASE_URL = ""
    config.SINGLE_USER_MODE = False
    config.REQUIRE_PHONE_VERIFICATION = False
    config.REQUIRE_AGE_CONFIRMATION = False
    auth.DB_PATH = database
    auth.DB_DIR = database.parent
    database.parent.mkdir(parents=True, exist_ok=True)
    auth.init_db()

    class FixtureEngine(ChatEngine):
        """保留真实 prompt、摘要读取和参考构造；响应为确定性诊断 JSON。"""

        def __init__(self, slug, owner):
            self.fixture_id = uuid.uuid4().hex
            super().__init__(
                slug,
                owner=owner,
                model_config={
                    "model": "offline-loop-fixture",
                    "temperature": 0,
                    "top_p": 1,
                    "frequency_penalty": 0,
                    "max_tokens": 1024,
                },
            )

        def _rag_search(self, user_input):
            return [
                {
                    "id": "offline-source-record",
                    "display_text": "合成原话：周六整理实验记录。",
                    "score": 0.95,
                    "metadata": {"source": "wechat", "dominant_speaker": "target"},
                }
            ]

        def _call_api(self, messages):
            reply = json.dumps(
                {
                    "pid": os.getpid(),
                    "engine_id": self.fixture_id,
                    "corrections": self.corrections,
                    "seen_users": [
                        m["content"] for m in messages[1:-1] if m["role"] == "user"
                    ],
                },
                ensure_ascii=False,
            )
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=reply))],
                usage=SimpleNamespace(prompt_tokens=10, completion_tokens=10),
            )

    def build_engine(slug, owner=None):
        return FixtureEngine(slug, owner), None, None

    def summarize(slug, owner, messages):
        users = [
            message["content"] for message in messages if message["role"] == "user"
        ]
        return f"合成摘要：本会话共{len(users)}轮，从{users[0]}到{users[-1]}。"

    core.factory.create_engine_and_store = build_engine
    core.session_archive._generate_conversation_summary = summarize


if __name__ == "__main__":
    install_fixture()
    from mcp_server.server import main

    main(transport="stdio")
