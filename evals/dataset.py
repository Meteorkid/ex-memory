"""Golden Dataset 与评测语料的数据结构、加载与校验。

语料消息格式与 parsers 输出保持一致（sender/content/timestamp/is_target），
可直接喂给 memory.chunker.Chunker.chunk_messages。
消息级 ground truth 通过 msg_id（语料内全局序号）标注。
"""

import json
from dataclasses import dataclass
from pathlib import Path

EVAL_DATA_DIR = Path(__file__).resolve().parent / "data"
CORPUS_PATH = EVAL_DATA_DIR / "corpus.jsonl"
GOLDEN_PATH = EVAL_DATA_DIR / "golden.jsonl"

REQUIRED_MSG_KEYS = {"msg_id", "sender", "content", "timestamp", "is_target"}
REQUIRED_GOLDEN_KEYS = {"qid", "query", "category", "fact", "gold_msg_ids"}


@dataclass(frozen=True)
class GoldenItem:
    """一条评测查询：query 模拟用户向数字镜像提问，gold_msg_ids 是应被检索到的消息。"""

    qid: str
    query: str
    category: str
    fact: str
    gold_msg_ids: frozenset[int]


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        raise FileNotFoundError(
            f"评测数据不存在: {path}（先运行 python -m evals.build_corpus 生成）"
        )
    rows = []
    with path.open(encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as e:
                raise ValueError(f"{path.name} 第 {lineno} 行不是合法 JSON: {e}") from e
    return rows


def load_corpus(path: Path = CORPUS_PATH) -> list[dict]:
    """加载评测语料并校验：msg_id 连续、timestamp 唯一且递增（chunk→消息映射依赖）。"""
    messages = _load_jsonl(path)
    if not messages:
        raise ValueError(f"{path.name} 为空")

    seen_ts = set()
    prev_ts = ""
    for i, msg in enumerate(messages):
        missing = REQUIRED_MSG_KEYS - msg.keys()
        if missing:
            raise ValueError(f"语料第 {i} 条缺少字段: {missing}")
        if msg["msg_id"] != i:
            raise ValueError(f"语料第 {i} 条 msg_id={msg['msg_id']} 不连续")
        if not str(msg["content"]).strip():
            raise ValueError(f"语料第 {i} 条 content 为空")
        ts = msg["timestamp"]
        if ts in seen_ts:
            raise ValueError(f"语料第 {i} 条 timestamp 重复: {ts}")
        if ts < prev_ts:
            raise ValueError(f"语料第 {i} 条 timestamp 未递增: {ts}")
        seen_ts.add(ts)
        prev_ts = ts
    return messages


def load_golden(
    path: Path = GOLDEN_PATH, corpus: list[dict] | None = None
) -> list[GoldenItem]:
    """加载 Golden Dataset；传入 corpus 时校验 gold_msg_ids 指向 target 的消息。"""
    rows = _load_jsonl(path)
    if not rows:
        raise ValueError(f"{path.name} 为空")

    items = []
    seen_qids = set()
    for i, row in enumerate(rows):
        missing = REQUIRED_GOLDEN_KEYS - row.keys()
        if missing:
            raise ValueError(f"golden 第 {i} 条缺少字段: {missing}")
        qid = row["qid"]
        if qid in seen_qids:
            raise ValueError(f"golden 第 {i} 条 qid 重复: {qid}")
        seen_qids.add(qid)
        gold_ids = row["gold_msg_ids"]
        if not gold_ids:
            raise ValueError(f"golden {qid} 的 gold_msg_ids 为空")

        if corpus is not None:
            for mid in gold_ids:
                if not (0 <= mid < len(corpus)):
                    raise ValueError(f"golden {qid} 引用越界消息 msg_id={mid}")
                if not corpus[mid]["is_target"]:
                    # 引擎只检索 target 原话，黄金消息必须是 target 说的
                    raise ValueError(f"golden {qid} 的 msg_id={mid} 不是 target 消息")

        items.append(
            GoldenItem(
                qid=qid,
                query=str(row["query"]).strip(),
                category=row["category"],
                fact=row["fact"],
                gold_msg_ids=frozenset(gold_ids),
            )
        )
    return items
