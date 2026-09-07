"""纠正记录的结构化沉淀（FR-070 / D-19）。

原实现是**无上限追加的流水账**：每次纠正拼一段到 corrections.md，全量注入
prompt。长期使用后它会一直涨，挤占上下文并推高每轮成本——而 prompt 里标着
「优先级最高」，被挤掉的反而是最该保留的东西。

改为结构化条目 + 上限 + 去重：
- 每条纠正是一个带时间与要点的条目，不是自由文本；
- 同一主题的重复纠正合并计数而不是各占一条——用户反复纠正同一件事，说明
  它更重要，而不是应该占更多篇幅；
- 超过上限时淘汰最旧且只被纠正过一次的，保留高频条目。
"""

import json
import logging
import re
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("ex-memory")

CORRECTIONS_FILE = "corrections.json"
LEGACY_FILE = "corrections.md"
MAX_ENTRIES = 40
MAX_IN_PROMPT = 20


def _store(slug: str, owner: Optional[int] = None):
    from core.mirror_store import mirror_store

    return mirror_store(slug, owner)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# 去重时要剥掉的符号：标点、空白、各类引号与括号。
# 「ta 不会说亲爱的」和「ta 不会说「亲爱的」。」是同一件事，
# 不归一化就会各占一条，重复纠正反而变成占更多篇幅。
_NOISE = re.compile(r"[\s，。！？、；：,.!?;:「」『』“”‘’\"'()（）\[\]【】]+")


def _normalize(text: str) -> str:
    """归一化用于去重：去标点、空白与引号，只看实质内容。"""
    return _NOISE.sub("", text)[:60]


def load(slug: str, owner: Optional[int] = None) -> list[dict]:
    store = _store(slug, owner)
    if not store.exists(CORRECTIONS_FILE):
        return _migrate_legacy(slug, owner)
    try:
        data = store.read_json(CORRECTIONS_FILE)
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("纠正记录读取失败 slug=%s: %s", slug, e)
        return []


def _migrate_legacy(slug: str, owner: Optional[int] = None) -> list[dict]:
    """把旧的 corrections.md 流水账切成结构化条目。

    存量镜像不该因为改了存储格式就丢掉已有的纠正——那是人物画像准确性的
    核心数据。
    """
    store = _store(slug, owner)
    if not store.exists(LEGACY_FILE):
        return []
    try:
        text = store.read_text(LEGACY_FILE)
    except OSError:
        return []

    blocks = re.split(r"### Correction #\d+", text)[1:]
    if not blocks:
        # 旧文件未必带 ### Correction # 表头（手工编辑过的就没有）。
        # 认不出格式时整份当作一条保留——宁可格式糙，也不能把人物画像的
        # 核心数据丢掉。
        stripped = text.strip()
        blocks = [stripped] if stripped else []

    entries = []
    for block in blocks:
        content = block.strip().strip("-").strip()
        if not content or content == "# 纠正记录":
            continue
        entries.append(
            {
                "content": content[:500],
                "key": _normalize(content),
                "count": 1,
                "created_at": _now(),
                "updated_at": _now(),
            }
        )
    if entries:
        logger.info("从旧格式迁移了 %d 条纠正记录 slug=%s", len(entries), slug)
        save(slug, entries[-MAX_ENTRIES:], owner)
    return entries[-MAX_ENTRIES:]


def save(slug: str, entries: list[dict], owner: Optional[int] = None) -> None:
    _store(slug, owner).write_json(CORRECTIONS_FILE, entries)


def add(slug: str, content: str, owner: Optional[int] = None) -> dict:
    """加入一条纠正。同主题的合并计数而不是各占一条。"""
    content = (content or "").strip()
    if not content:
        raise ValueError("纠正内容不能为空")

    entries = load(slug, owner)
    key = _normalize(content)
    for entry in entries:
        if entry.get("key") == key:
            # 反复纠正同一件事说明它更重要，而不是该占更多篇幅
            entry["count"] = int(entry.get("count", 1)) + 1
            entry["updated_at"] = _now()
            entry["content"] = content[:500]
            save(slug, entries, owner)
            return entry

    entry = {
        "content": content[:500],
        "key": key,
        "count": 1,
        "created_at": _now(),
        "updated_at": _now(),
    }
    entries.append(entry)
    save(slug, _prune(entries), owner)
    return entry


def _prune(entries: list[dict]) -> list[dict]:
    """超过上限时淘汰最旧且只被纠正过一次的，保留高频条目。"""
    if len(entries) <= MAX_ENTRIES:
        return entries
    # 先按「被纠正次数」降序、再按更新时间降序，截断尾部
    ranked = sorted(
        entries,
        key=lambda e: (int(e.get("count", 1)), e.get("updated_at", "")),
        reverse=True,
    )
    kept = ranked[:MAX_ENTRIES]
    dropped = len(entries) - len(kept)
    if dropped:
        logger.info("纠正记录超过上限，淘汰 %d 条低频旧条目", dropped)
    # 恢复时间顺序，让 prompt 里读起来仍是编年的
    return sorted(kept, key=lambda e: e.get("created_at", ""))


def prompt_section(slug: str, owner: Optional[int] = None) -> str:
    """纠正记录的 prompt 片段。有上限，不会无限增长。"""
    entries = load(slug, owner)
    if not entries:
        return ""
    ranked = sorted(
        entries,
        key=lambda e: (int(e.get("count", 1)), e.get("updated_at", "")),
        reverse=True,
    )[:MAX_IN_PROMPT]
    ranked = sorted(ranked, key=lambda e: e.get("created_at", ""))

    lines = ["\n---\n## 用户纠正记录（优先级最高）"]
    for entry in ranked:
        count = int(entry.get("count", 1))
        emphasis = f"（被纠正过 {count} 次，尤其注意）" if count > 1 else ""
        lines.append(f"- {entry['content']}{emphasis}")
    return "\n".join(lines) + "\n"


def as_eval_cases(slug: str, owner: Optional[int] = None) -> list[dict]:
    """把纠正转成评测用例（FR-070）。

    纠正收敛率要能测，前提是纠正本身是结构化的——流水账做不到这一点。
    """
    return [
        {
            "id": f"correction_{i}",
            "expectation": entry["content"],
            "times_corrected": int(entry.get("count", 1)),
        }
        for i, entry in enumerate(load(slug, owner))
    ]
