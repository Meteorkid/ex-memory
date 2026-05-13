"""调用 LLM 生成 persona.md（含 9 场景原话样本抽取）。"""

from pathlib import Path
from config import get_llm_config, get_llm_client, get_ex_dir

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

# 9 个场景及其检索 query
SPEECH_SCENES = [
    ("打招呼/开场", "哈喽 hi 在吗 你好 hey"),
    ("日常问候", "吃了吗 在干嘛 今天怎么样"),
    ("开心/分享", "哈哈哈 好开心 太棒了 笑死"),
    ("冷淡/不满", "哦 随便 你说了算 行吧"),
    ("撒娇/委屈", "哼 你都不理我 委屈 呜呜"),
    ("生气/争吵", "你什么意思 说了多少次 烦不烦"),
    ("吃醋/占有欲", "你跟谁 你是不是 又是谁"),
    ("感谢/认可", "谢谢 你真好 辛苦了 有你真好"),
    ("告别/晚安", "拜拜 晚安 下次聊 先睡了"),
]


def build_persona(
    slug: str,
    materials_summary: str,
    vector_store=None,
    embedder=None,
) -> str:
    """生成 persona.md 内容。

    Args:
        slug: 前任代号
        materials_summary: 原材料摘要（来自解析器的统计 + 用户口述）
        vector_store: 向量库实例（用于抽取原话样本）
        embedder: embedding 实例

    Returns:
        persona.md 的完整内容
    """
    cfg = get_llm_config()
    client = get_llm_client()

    # 读取 prompt 模板
    analyzer_prompt = (PROMPTS_DIR / "persona_analyzer.md").read_text(encoding="utf-8")
    builder_prompt = (PROMPTS_DIR / "persona_builder.md").read_text(encoding="utf-8")

    # Step 1: 分析性格特征
    analysis_response = client.chat.completions.create(
        model=cfg["model"],
        messages=[
            {"role": "system", "content": analyzer_prompt},
            {"role": "user", "content": f"请分析以下原材料，提取性格特征和行为模式：\n\n{materials_summary}"},
        ],
        temperature=0.7,
    )
    analysis = analysis_response.choices[0].message.content

    # Step 2: 抽取 9 场景原话样本
    speech_samples = ""
    if vector_store and embedder:
        samples = []
        for scene_name, query in SPEECH_SCENES:
            results = vector_store.search_target_only(query, embedder, top_k=8)
            picked = []
            seen = set()
            for r in results:
                text = r.get("display_text", "").strip()
                if text and len(text) > 2 and text not in seen:
                    seen.add(text)
                    picked.append(text)
                if len(picked) >= 3:
                    break
            if picked:
                samples.append(f"### {scene_name}")
                for p in picked:
                    samples.append(f"- {p}")
        if samples:
            speech_samples = "\n".join(samples)

    # Step 3: 生成完整 persona.md
    user_content = f"""请根据以下分析结果生成 persona.md：

## 性格分析
{analysis}

## 原话样本（从聊天记录中提取的真实原话）
{speech_samples if speech_samples else "（无原话样本，请基于分析结果推断）"}

请按照 persona_builder.md 的 5 层结构输出完整的 persona.md。"""

    build_response = client.chat.completions.create(
        model=cfg["model"],
        messages=[
            {"role": "system", "content": builder_prompt},
            {"role": "user", "content": user_content},
        ],
        temperature=0.7,
    )

    return build_response.choices[0].message.content
