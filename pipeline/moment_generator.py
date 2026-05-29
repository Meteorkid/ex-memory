"""朋友圈生成器：基于 persona.md 生成朋友圈内容，含评论和点赞。"""

import json
import logging
import random
from datetime import datetime, timedelta
from pathlib import Path
from config import get_llm_config, get_llm_client, get_ex_dir
from core.file_utils import atomic_write_json

logger = logging.getLogger("ex-memory")
PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

# 模拟点赞用户
FAKE_LIKERS = ["小明", "阿花", "老王", "小红", "大壮", "美美"]


def generate_moment(slug: str) -> str:
    """为指定镜像生成一条朋友圈，含评论和点赞。

    Args:
        slug: 前任代号

    Returns:
        生成的朋友圈内容

    Raises:
        FileNotFoundError: 缺少 persona.md
        RuntimeError: 未配置 LLM
    """
    ex_dir = get_ex_dir(slug)
    persona_path = ex_dir / "persona.md"
    if not persona_path.exists():
        raise FileNotFoundError("缺少 persona.md")

    cfg = get_llm_config()
    if not cfg["api_key"]:
        raise RuntimeError("未配置 LLM API Key")

    persona_content = persona_path.read_text(encoding="utf-8")
    moment_prompt = (PROMPTS_DIR / "moment.md").read_text(encoding="utf-8")

    client = get_llm_client()
    response = client.chat.completions.create(
        model=cfg["model"],
        messages=[
            {"role": "system", "content": moment_prompt},
            {"role": "user", "content": f"请根据以下人格画像生成一条朋友圈：\n\n{persona_content}"},
        ],
        temperature=0.9,
    )
    content = response.choices[0].message.content or ""

    # 生成随机时间（最近 1-7 天内）
    days_ago = random.randint(0, 7)
    hours_ago = random.randint(6, 23)
    minutes_ago = random.randint(0, 59)
    post_time = datetime.now() - timedelta(days=days_ago, hours=hours_ago, minutes=minutes_ago)

    # 生成随机点赞（0-3 个）
    num_likes = random.randint(0, 3)
    likes = random.sample(FAKE_LIKERS, min(num_likes, len(FAKE_LIKERS)))

    # 生成自评论（0-1 条）
    comments = []
    if random.random() < 0.4:  # 40% 概率有自评论
        self_comments = [
            "哈哈谢谢大家",
            "😊",
            "今天心情不错",
            "晚安",
            "你们说什么呢",
            "已阅",
        ]
        comments.append({
            "author": "自己",
            "content": random.choice(self_comments),
        })

    moments_path = ex_dir / "moments.json"
    moments = json.loads(moments_path.read_text(encoding="utf-8")) if moments_path.exists() else []
    moments.append({
        "id": f"m{len(moments)+1}",
        "content": content,
        "created_at": post_time.isoformat(),
        "likes": likes,
        "comments": comments,
    })
    atomic_write_json(moments_path, moments)
    logger.info("朋友圈已生成: %s", slug)
    return content
