"""聊天记录切片：将消息列表切分为 chunks，判定 dominant_speaker。"""

import re
import hashlib
from collections import Counter
from config import CHUNK_TURNS, CHUNK_OVERLAP


def _split_sentences(text: str) -> list[str]:
    """按中英文句子边界切分文本。"""
    # 匹配中英文句号、问号、感叹号、分号后的断点
    parts = re.split(r'(?<=[。！？；\.\!\?\;])\s*', text)
    return [p for p in parts if p.strip()]


class Chunker:
    def chunk_messages(
        self,
        messages: list[dict],
        source: str,
        chat_id: str,
        chunk_turns: int = CHUNK_TURNS,
        overlap_turns: int = CHUNK_OVERLAP,
    ) -> list[dict]:
        """将消息列表按轮次窗口切片。

        Args:
            messages: 标准化消息列表，每条含 sender, content, timestamp
            source: 数据来源标识 (wechat, qq, oral, ...)
            chat_id: 会话标识
            chunk_turns: 每个 chunk 包含的消息数
            overlap_turns: chunk 之间的重叠消息数

        Returns:
            chunk 列表，每个含 id, text_for_embedding, display_text, metadata
        """
        if not messages:
            return []

        chunks = []
        step = chunk_turns - overlap_turns
        if step <= 0:
            step = 1

        for i in range(0, len(messages), step):
            window = messages[i : i + chunk_turns]
            if not window:
                break

            # 判定 dominant_speaker
            speaker_counts = Counter()
            speaker_chars = Counter()
            for msg in window:
                sender = msg.get("sender", "unknown")
                speaker_counts[sender] += 1
                speaker_chars[sender] += len(msg.get("content", ""))

            # 判定 dominant_speaker：优先用 is_target 标记
            target_count = sum(1 for msg in window if msg.get("is_target"))
            dominant = "target" if target_count > len(window) / 2 else speaker_counts.most_common(1)[0][0]

            # 构建文本
            display_lines = []
            embedding_lines = []
            for msg in window:
                sender = msg.get("sender", "unknown")
                content = msg.get("content", "").strip()
                if not content:
                    continue
                ts = msg.get("timestamp", "")
                display_lines.append(f"[{ts}] {sender}: {content}")
                embedding_lines.append(f"{sender}: {content}")

            if not display_lines:
                continue

            display_text = "\n".join(display_lines)
            text_for_embedding = "\n".join(embedding_lines)

            # 生成唯一 ID
            chunk_id = hashlib.md5(
                f"{source}_{chat_id}_{i}_{display_text[:100]}".encode()
            ).hexdigest()

            start_ts = window[0].get("timestamp", "")
            end_ts = window[-1].get("timestamp", "")

            chunks.append({
                "id": chunk_id,
                "text_for_embedding": text_for_embedding,
                "display_text": display_text,
                "metadata": {
                    "source": source,
                    "chat_id": chat_id,
                    "dominant_speaker": dominant,
                    "start_ts": start_ts,
                    "end_ts": end_ts,
                    "turn_count": len(window),
                },
            })

        return chunks

    def chunk_text(
        self,
        text: str,
        source: str,
        chunk_chars: int = 800,
        overlap_chars: int = 80,
    ) -> list[dict]:
        """纯文本按句子/段落边界切片，避免截断语义。无边界时回退到字符窗口。"""
        if not text.strip():
            return []

        # 按段落分割，再按句子边界合并
        paragraphs = text.split("\n")
        chunks = []
        current = ""

        for para in paragraphs:
            para = para.strip()
            if not para:
                continue

            # 如果当前段落本身超过 chunk_chars，按句子切分
            if len(para) > chunk_chars:
                if current.strip():
                    chunks.append(current.strip())
                    current = ""
                sentences = _split_sentences(para)
                # 如果没有句子边界（纯连续文本），回退到字符窗口
                if len(sentences) <= 1:
                    for i in range(0, len(para), chunk_chars - overlap_chars):
                        segment = para[i:i + chunk_chars].strip()
                        if segment:
                            chunks.append(segment)
                    continue
                for sent in sentences:
                    if len(current) + len(sent) > chunk_chars and current.strip():
                        chunks.append(current.strip())
                        current = current[-overlap_chars:] if overlap_chars else ""
                    current += sent
            elif len(current) + len(para) + 1 > chunk_chars:
                if current.strip():
                    chunks.append(current.strip())
                current = current[-overlap_chars:] + "\n" + para if overlap_chars else para
            else:
                current += "\n" + para if current else para

        if current.strip():
            chunks.append(current.strip())

        # 构建标准 chunk 结构
        result = []
        for i, segment in enumerate(chunks):
            chunk_id = hashlib.md5(
                f"{source}_text_{i}_{segment[:50]}".encode()
            ).hexdigest()
            result.append({
                "id": chunk_id,
                "text_for_embedding": segment,
                "display_text": segment,
                "metadata": {
                    "source": source,
                    "dominant_speaker": "narrative",
                    "chunk_index": i,
                },
            })

        return result
