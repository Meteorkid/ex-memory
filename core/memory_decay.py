"""长期记忆衰减：管理记忆生命周期，清理过期记忆。"""

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from core.memory_scorer import calculate_importance, should_keep_memory, get_decay_info

logger = logging.getLogger("ex-memory")

# 记忆存储文件名
MEMORY_INDEX_FILE = "memory_index.json"


class MemoryIndex:
    """记忆索引：跟踪每条记忆的重要性分数和创建时间。"""

    def __init__(self, index_path: Path):
        self.index_path = index_path
        self.entries: dict[str, dict] = {}
        self._load()

    def _load(self):
        """从磁盘加载索引。"""
        if self.index_path.exists():
            try:
                data = json.loads(self.index_path.read_text(encoding="utf-8"))
                self.entries = data.get("entries", {})
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("加载记忆索引失败: %s", e)
                self.entries = {}

    def save(self):
        """保存索引到磁盘。"""
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "version": 1,
            "updated_at": datetime.now().isoformat(),
            "entries": self.entries,
        }
        self.index_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def add(self, memory_id: str, content: str, source: str = "session"):
        """添加或更新一条记忆的索引。

        Args:
            memory_id: 记忆唯一标识（通常是文件名或 hash）
            content: 记忆内容（用于计算重要度）
            source: 来源（session / correction / manual）
        """
        importance = calculate_importance(content)
        self.entries[memory_id] = {
            "importance": importance,
            "created_at": datetime.now().isoformat(),
            "source": source,
            "decay_info": get_decay_info(importance),
        }

    def remove(self, memory_id: str):
        """移除一条记忆索引。"""
        self.entries.pop(memory_id, None)

    def cleanup_expired(self) -> list[str]:
        """清理过期记忆，返回被清理的记忆 ID 列表。"""
        now = datetime.now()
        expired = []

        for mid, entry in list(self.entries.items()):
            importance = entry.get("importance", 0.0)
            created_str = entry.get("created_at")

            if not created_str:
                continue

            try:
                created = datetime.fromisoformat(created_str)
                age_days = (now - created).days
            except (ValueError, TypeError):
                continue

            if not should_keep_memory(importance, age_days):
                expired.append(mid)
                del self.entries[mid]

        if expired:
            logger.info("清理过期记忆: %d 条", len(expired))
            self.save()

        return expired

    def get_stats(self) -> dict:
        """获取记忆统计信息。"""
        now = datetime.now()
        stats = {
            "total": len(self.entries),
            "permanent": 0,
            "long": 0,
            "medium": 0,
            "short": 0,
            "expired_candidates": 0,
        }

        for entry in self.entries.values():
            level = entry.get("decay_info", {}).get("level", "short")
            stats[level] = stats.get(level, 0) + 1

            # 检查是否即将过期
            importance = entry.get("importance", 0.0)
            created_str = entry.get("created_at")
            if created_str:
                try:
                    created = datetime.fromisoformat(created_str)
                    age_days = (now - created).days
                    if not should_keep_memory(importance, age_days):
                        stats["expired_candidates"] += 1
                except (ValueError, TypeError):
                    pass

        return stats


def run_decay_cycle(ex_dir: Path) -> dict:
    """对指定镜像执行一次记忆衰减清理。

    Args:
        ex_dir: 镜像目录路径

    Returns:
        {"cleaned": int, "remaining": int, "stats": dict}
    """
    index_path = ex_dir / MEMORY_INDEX_FILE
    index = MemoryIndex(index_path)

    stats_before = index.get_stats()
    expired = index.cleanup_expired()
    stats_after = index.get_stats()

    result = {
        "cleaned": len(expired),
        "remaining": stats_after["total"],
        "stats": stats_after,
        "expired_ids": expired,
    }

    if expired:
        logger.info(
            "记忆衰减完成: 清理 %d 条，剩余 %d 条",
            len(expired), stats_after["total"],
        )

    return result
