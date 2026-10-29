"""回答参考是每轮实际输入的快照，不是共享引擎上的最近一次检索。"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Barrier
from types import SimpleNamespace

import pytest


def completion(text="回答"):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))], usage=None
    )


def stream_chunk(text):
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=SimpleNamespace(content=text))], usage=None
    )


@pytest.fixture
def engine(monkeypatch):
    from core.engine import ChatEngine
    from core.mirror_store import mirror_store

    store = mirror_store("sources", 1)
    store.write_text("SKILL.md", "# 原始人格\n共同资料\n---\n\n## PART B\n规则")
    store.write_json(
        "meta.json", {"owner_user_id": 1, "creation_source": "chat_import"}
    )
    engine = ChatEngine(
        "sources",
        owner=1,
        model_config={
            "model": "synthetic",
            "temperature": 0,
            "top_p": 1,
            "frequency_penalty": 0,
            "max_tokens": 100,
        },
    )
    monkeypatch.setattr(engine, "_call_api", lambda messages: completion())
    monkeypatch.setattr(
        engine, "_call_stream", lambda messages: iter([stream_chunk("回答")])
    )
    monkeypatch.setattr(
        "core.wallet_manager.detect_redpacket_trigger", lambda *args: None
    )
    return engine


def hit(text="[2026-10-29] 小林: 周六交报告", score=0.9, source="wechat", **metadata):
    return {
        "id": "chunk-123",
        "display_text": text,
        "score": score,
        "metadata": {
            "source": source,
            "dominant_speaker": "target",
            "start_ts": "2026-10-29",
            "end_ts": "2026-10-29",
            **metadata,
        },
    }


def test_sources_match_prompt_and_keep_legacy_chat_contract(engine, monkeypatch):
    requests = []
    searches = []
    monkeypatch.setattr(
        engine, "_rag_search", lambda query: searches.append(query) or [hit()]
    )
    monkeypatch.setattr(
        engine, "_call_api", lambda messages: requests.append(messages) or completion()
    )
    reply, stickers, usage, sources = engine.chat_with_sources("什么时候", [])
    assert reply == "回答"
    assert len(searches) == 1
    assert len(sources) == 1
    assert sources[0]["text"] in requests[0][0]["content"]
    assert sources[0]["kind"] == "imported_chat"
    assert sources[0]["speaker"] == "target"
    assert sources[0]["start_ts"] == "2026-10-29"
    assert len(engine.chat("旧入口", [])) == 3


def test_sources_only_include_accepted_nonempty_finite_results(engine, monkeypatch):
    from config import RAG_THRESHOLD

    monkeypatch.setattr(
        engine,
        "_rag_search",
        lambda query: [
            hit("有效参考"),
            hit("低分", score=RAG_THRESHOLD),
            hit("", score=0.99),
            hit("NaN", score=float("nan")),
            hit("无限", score=float("inf")),
            hit("非法分数", score="oops"),
        ],
    )
    sources = engine.chat_with_sources("问题", [])[3]
    assert [item["text"] for item in sources] == ["有效参考"]


def test_reference_metadata_is_whitelisted_and_text_is_bounded(engine, monkeypatch):
    requests = []
    monkeypatch.setattr(
        engine,
        "_rag_search",
        lambda query: [
            hit("长" * 20000, secret_key="never-return", chat_id="private-chat")
            for _ in range(50)
        ],
    )
    monkeypatch.setattr(
        engine, "_call_api", lambda messages: requests.append(messages) or completion()
    )
    sources = engine.chat_with_sources("问题", [])[3]
    assert 0 < len(sources) <= 8
    assert sum(len(item["text"]) for item in sources) <= 12000
    assert all(len(item["text"]) <= 1500 for item in sources)
    assert "never-return" not in str(sources)
    assert "private-chat" not in str(sources)
    assert all(item["text"] in requests[0][0]["content"] for item in sources)


def test_source_ids_are_stable_scoped_and_content_sensitive(engine, monkeypatch):
    current = [hit()]
    monkeypatch.setattr(engine, "_rag_search", lambda query: current)
    first = engine.chat_with_sources("问题", [])[3][0]["id"]
    assert engine.chat_with_sources("重试", [])[3][0]["id"] == first
    engine.owner = 2
    assert engine.chat_with_sources("其他账号", [])[3][0]["id"] != first
    engine.owner = 1
    current[0] = hit("同ID的新原文")
    assert engine.chat_with_sources("修改后", [])[3][0]["id"] != first


def test_legacy_results_without_ids_have_deterministic_references(engine, monkeypatch):
    raw = hit()
    raw.pop("id")
    monkeypatch.setattr(engine, "_rag_search", lambda query: [raw])
    one = engine.chat_with_sources("一", [])[3][0]["id"]
    assert one and engine.chat_with_sources("二", [])[3][0]["id"] == one


@pytest.mark.parametrize(
    "source,speaker,kind",
    [
        ("session_summary", "session", "generated_summary"),
        ("oral", "narrative", "narrative"),
        ("unclassified", "unknown", "unknown"),
    ],
)
def test_non_chat_material_is_not_labelled_as_original_chat(
    engine, monkeypatch, source, speaker, kind
):
    monkeypatch.setattr(
        engine,
        "_rag_search",
        lambda query: [hit(source=source, dominant_speaker=speaker)],
    )
    assert engine.chat_with_sources("问题", [])[3][0]["kind"] == kind


def test_legacy_session_summary_is_referenced_as_generated_material(engine):
    engine.session_summaries = ["模型生成的旧摘要"]
    sources = engine.chat_with_sources("问题", [])[3]
    assert any(
        s["text"] == "模型生成的旧摘要" and s["kind"] == "generated_summary"
        for s in sources
    )


def test_summary_removed_by_prompt_budget_has_no_reference(engine, monkeypatch):
    engine.session_summaries = ["最近摘要", "应被裁掉的旧摘要"]
    monkeypatch.setattr("core.engine.LLM_MAX_CONTEXT_CHARS", 1)
    sources = engine.chat_with_sources("问题", [])[3]
    assert [s["text"] for s in sources] == ["最近摘要"]


def test_selected_conversation_excludes_legacy_global_memories(engine, monkeypatch):
    import core.session_archive as archive

    engine.session_summaries = ["旧网页私聊秘密"]
    engine.skill_content = (
        "# 原始人格\n共同资料\n\n### 对话摘要 (2026-10-29 12:00)\n"
        "旧网页自动摘要秘密\n---\n\n## PART B\n共同规则"
    )
    monkeypatch.setattr(
        archive,
        "load_conversation_summaries",
        lambda slug, owner, conversation_id: [
            {"id": "summary-group", "text": "当前群聊摘要", "kind": "generated_summary"}
        ],
        raising=False,
    )
    requests = []
    monkeypatch.setattr(
        engine, "_call_api", lambda messages: requests.append(messages) or completion()
    )
    sources = engine.chat_with_sources("问题", [], conversation_id="im_group")[3]
    prompt = requests[0][0]["content"]
    assert "旧网页" not in prompt
    assert "当前群聊摘要" in prompt
    assert "共同资料" in prompt and "共同规则" in prompt
    assert [s["text"] for s in sources] == ["当前群聊摘要"]
    assert "旧网页" in engine.skill_content


def test_web_conversation_can_still_use_legacy_summary(engine, monkeypatch):
    import core.session_archive as archive

    engine.session_summaries = ["旧网页摘要"]
    monkeypatch.setattr(
        archive, "load_conversation_summaries", lambda *args: [], raising=False
    )
    sources = engine.chat_with_sources("问题", [], conversation_id="web")[3]
    assert [source["text"] for source in sources] == ["旧网页摘要"]


def test_real_scoped_archive_is_used_only_by_selected_conversation(engine, monkeypatch):
    from core import session_archive
    from core.conversation_store import append_turn

    append_turn("sources", 1, "群里讨论下雨", "生成回复", conversation_id="im_group")
    monkeypatch.setattr(
        session_archive,
        "_generate_conversation_summary",
        lambda *args: "群聊的生成摘要",
    )
    assert session_archive.maybe_archive(
        "sources", owner=1, conversation_id="im_group", threshold=1
    )
    requests = []
    monkeypatch.setattr(
        engine, "_call_api", lambda messages: requests.append(messages) or completion()
    )
    references = engine.chat_with_sources("继续", [], conversation_id="im_group")[3]
    assert any(
        source["text"] == "群聊的生成摘要" and source["kind"] == "generated_summary"
        for source in references
    )
    assert "群聊的生成摘要" in requests[-1][0]["content"]
    assert engine.chat_with_sources("私聊", [], conversation_id="im_private")[3] == []
    assert "群聊的生成摘要" not in requests[-1][0]["content"]


def test_new_conversation_reads_only_its_summary_and_keeps_latest_first(
    engine, monkeypatch
):
    import core.session_archive as archive

    calls = []
    engine.session_summaries = ["全局网页摘要"]

    def selected(slug, owner, conversation_id):
        calls.append((slug, owner, conversation_id))
        return [
            {"id": "1", "text": "当前会话较早摘要"},
            {"id": "2", "text": "当前会话最新摘要"},
        ]

    monkeypatch.setattr(archive, "load_conversation_summaries", selected, raising=False)
    sources = engine.chat_with_sources("问题", [], conversation_id="im_private")[3]
    assert calls == [("sources", 1, "im_private")]
    assert [source["text"] for source in sources] == [
        "当前会话最新摘要",
        "当前会话较早摘要",
    ]


def test_reference_redaction_is_identical_in_model_input(engine, monkeypatch):
    requests = []
    monkeypatch.setattr(
        engine, "_rag_search", lambda query: [hit("联系方式 contact@example.com")]
    )
    monkeypatch.setattr(
        engine, "_call_api", lambda messages: requests.append(messages) or completion()
    )
    source = engine.chat_with_sources("问题", [])[3][0]
    assert "contact@example.com" not in source["text"]
    assert source["text"] in requests[0][0]["content"]


def test_legacy_raw_session_is_not_labelled_as_original_chat(engine):
    from core.engine import ChatEngine

    engine.store.write_text("sessions/session_20261029_120000.md", "用户:你好\nAI:你好")
    fresh = ChatEngine("sources", owner=1, model_config=engine._llm_config)
    source = fresh._summary_references(None)[0]
    assert source["kind"] == "conversation_history"


def test_shared_engine_concurrent_calls_never_exchange_sources(engine, monkeypatch):
    barrier = Barrier(2)
    monkeypatch.setattr(engine, "_rag_search", lambda query: [hit(f"原话:{query}")])

    def respond(messages):
        barrier.wait(timeout=5)
        return completion(messages[-1]["content"])

    monkeypatch.setattr(engine, "_call_api", respond)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(lambda q: engine.chat_with_sources(q, []), ["甲", "乙"])
        )
    for reply, _stickers, _usage, sources in results:
        assert [s["text"] for s in sources] == [f"原话:{reply}"]


def test_interleaved_streams_have_request_local_sources(engine, monkeypatch):
    monkeypatch.setattr(engine, "_rag_search", lambda query: [hit(f"原话:{query}")])
    streams = [engine.chat_stream_with_sources(q, []) for q in ["甲", "乙"]]
    assert next(streams[0])["type"] == "text"
    assert next(streams[1])["type"] == "text"
    for query, stream in zip(["甲", "乙"], streams):
        events = list(stream)
        assert events[-1]["type"] == "sources"
        assert [s["text"] for s in events[-1]["sources"]] == [f"原话:{query}"]
    assert all(event["type"] != "sources" for event in engine.chat_stream("旧入口", []))


def test_failed_stream_does_not_publish_sources(engine, monkeypatch):
    monkeypatch.setattr(engine, "_rag_search", lambda query: [hit()])

    def broken(messages):
        yield stream_chunk("部分文本")
        raise RuntimeError("synthetic broken stream")

    monkeypatch.setattr(engine, "_call_stream", broken)
    stream = engine.chat_stream_with_sources("问题", [])
    assert next(stream)["type"] == "text"
    with pytest.raises(RuntimeError, match="broken stream"):
        list(stream)


def test_chroma_search_keeps_the_stored_chunk_identifier():
    from memory.vector_store import VectorStore

    store = VectorStore.__new__(VectorStore)
    store.collection = SimpleNamespace(
        query=lambda **kwargs: {
            "ids": [["original-chunk-id"]],
            "documents": [["embedding text"]],
            "metadatas": [[{"display_text": "原话"}]],
            "distances": [[0.1]],
        }
    )
    results = store.search("查询", SimpleNamespace(embed_one=lambda query: [1.0]))
    assert results[0]["id"] == "original-chunk-id"
    assert results[0]["display_text"] == "原话"


def test_pgvector_search_keeps_the_stored_chunk_identifier(monkeypatch):
    from memory.pgvector_store import PgVectorStore

    store = PgVectorStore("", "scoped_collection")
    queries = []

    class Connection:
        def execute(self, sql, params):
            queries.append((sql, params))
            return SimpleNamespace(
                fetchall=lambda: [
                    {
                        "id": "original-pg-chunk",
                        "display_text": "原话",
                        "document": "embedding",
                        "metadata": {"source": "wechat"},
                        "score": 0.9,
                    }
                ]
            )

    @contextmanager
    def connection():
        yield Connection()

    monkeypatch.setattr(store, "_conn", connection)
    results = store.search("查询", SimpleNamespace(embed_one=lambda query: [1.0]))
    assert results[0]["id"] == "original-pg-chunk"
    assert "SELECT id," in queries[0][0]
    assert "scoped_collection" in queries[0][1]
