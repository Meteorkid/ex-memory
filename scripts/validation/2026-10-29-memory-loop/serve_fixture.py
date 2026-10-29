"""通过隔离runner启动真实HTTP应用；仅替换生成模型，用于记忆闭环UI验收。"""

from types import SimpleNamespace
import sys

import config
import uvicorn

from core import corrections, session_archive
from core.conversation_store import append_turn
from core.mirror_store import mirror_store
import core.factory
import server.auth as auth
from server import routes
from server.app import create_app

config.METEOR_STORE_SSO_ENABLED = False
config.SINGLE_USER_MODE = False
config.REQUIRE_PHONE_VERIFICATION = False
config.REQUIRE_AGE_CONFIRMATION = False
auth.register_user("loopcheck", "offline-only-pass")
uid = auth.validate_token(auth.login_user("loopcheck", "offline-only-pass"))
store = mirror_store("rain", uid)
store.write_json(
    "meta.json",
    {
        "owner_user_id": uid,
        "name": "小雨（合成验收）",
        "display_name": "小雨（合成验收）",
        "state": "ready",
    },
)
store.write_text("SKILL.md", "合成测试人物，不代表真实人物。")
SOURCE = {
    "id": "rain_reference",
    "text": "下雨的时候，我喜欢坐在窗边看书。",
    "source": "wechat",
    "speaker": "target",
    "start_ts": "2026-10-01 15:00",
    "end_ts": "2026-10-01 15:01",
    "kind": "imported_chat",
    "score": 0.92,
}
append_turn(
    "rain",
    uid,
    "这是从IM开始的合成对话",
    "我记得，我们聊过下雨和看书。",
    source="mcp",
    conversation_id="im_fixture",
    sources=[SOURCE],
    conversation_label="IM 私聊（合成）",
)


class FixtureEngine:
    vector_store = None
    embedder = None
    style_profile = None
    model = "offline-fixture"

    def __init__(self, owner):
        self.correction = corrections.prompt_section("rain", owner)

    def chat_with_sources(self, message, history, conversation_id=None):
        reply = "我喜欢下雨时在窗边看书。"
        if self.correction:
            reply = "已采用你保存的纠正：" + corrections.load("rain", uid)[0]["content"]
        return (
            reply,
            [],
            SimpleNamespace(prompt_tokens=10, completion_tokens=5),
            [SOURCE],
        )

    def chat(self, message, history):
        return self.chat_with_sources(message, history)[:3]

    def chat_stream_with_sources(self, message, history, conversation_id=None):
        reply, _, _, sources = self.chat_with_sources(message, history, conversation_id)
        yield {"type": "text", "content": reply}
        yield {"type": "usage", "prompt_tokens": 10, "completion_tokens": 5}
        yield {"type": "sources", "sources": sources}


core.factory.create_engine_and_store = lambda slug, owner=None: (
    FixtureEngine(owner),
    None,
    None,
)
session_archive._generate_conversation_summary = lambda slug, owner, messages: (
    "这是本会话的合成摘要：用户与记忆体讨论了下雨和读书，AI回复不是原始人物事实。"
)


def archive(slug, vector_store, embedder, owner, conversation_id=None):
    if conversation_id:
        session_archive.maybe_archive(
            slug, owner=owner, conversation_id=conversation_id, threshold=2
        )


routes._run_session_archive = archive
print(
    "本机合成验收；账号 loopcheck / offline-only-pass",
    flush=True,
)
uvicorn.run(
    create_app(),
    host="127.0.0.1",
    port=int(sys.argv[1]) if len(sys.argv) > 1 else 8876,
    log_level="warning",
)
