"""表情贴纸选择器 — 基于回复文本的情绪分析匹配贴纸。"""

import random
import re
from typing import Optional

# ── 贴纸库 ──

STICKERS = {
    # 😄 开心类
    "hehe":       {"emoji": "😄", "label": "嘿嘿", "emotion": "happy"},
    "haha":       {"emoji": "😂", "label": "哈哈", "emotion": "happy"},
    "xiaosi":     {"emoji": "🤣", "label": "笑死", "emotion": "happy"},
    "touxiao":    {"emoji": "😆", "label": "偷笑", "emotion": "happy"},
    "dexiao":     {"emoji": "😊", "label": "得意的笑", "emotion": "happy"},
    "kaixin":     {"emoji": "😃", "label": "开心", "emotion": "happy"},
    # 🥺 撒娇/可爱类
    "sa_jiao":    {"emoji": "🥺", "label": "撒娇", "emotion": "cute"},
    "mua":        {"emoji": "😚", "label": "亲亲", "emotion": "cute"},
    "tiaopi":     {"emoji": "😋", "label": "调皮", "emotion": "cute"},
    "keai":       {"emoji": "🫣", "label": "可爱", "emotion": "cute"},
    "weiqu":      {"emoji": "🥹", "label": "委屈", "emotion": "cute"},
    # 😢 难过类
    "nanguo":     {"emoji": "😢", "label": "难过", "emotion": "sad"},
    "ku":         {"emoji": "😭", "label": "大哭", "emotion": "sad"},
    "shangxin":   {"emoji": "😞", "label": "伤心", "emotion": "sad"},
    # 😤 生气类
    "shengqi":    {"emoji": "😤", "label": "生气", "emotion": "angry"},
    "fennu":      {"emoji": "😡", "label": "愤怒", "emotion": "angry"},
    # 😏 调皮类
    "xieyan":     {"emoji": "😏", "label": "邪魅一笑", "emotion": "playful"},
    "huaixiao":   {"emoji": "🤪", "label": "坏笑", "emotion": "playful"},
    "doubi":      {"emoji": "😜", "label": "逗比", "emotion": "playful"},
    # 😳 害羞/尴尬类
    "haixiu":     {"emoji": "😳", "label": "害羞", "emotion": "shy"},
    "gangga":     {"emoji": "😅", "label": "尴尬", "emotion": "shy"},
    "baiyan":     {"emoji": "🙄", "label": "白眼", "emotion": "dismissive"},
    "wuyu":       {"emoji": "😒", "label": "无语", "emotion": "dismissive"},
    # 😱 惊讶类
    "jingya":     {"emoji": "😱", "label": "惊讶", "emotion": "surprised"},
    "cijing":     {"emoji": "😨", "label": "吃惊", "emotion": "surprised"},
    # 动作/手势类
    "like":       {"emoji": "👍", "label": "赞", "emotion": "gesture"},
    "guzhang":    {"emoji": "👏", "label": "鼓掌", "emotion": "gesture"},
    "hejiu":      {"emoji": "🍻", "label": "干杯", "emotion": "gesture"},
    "aini":       {"emoji": "❤️", "label": "爱你", "emotion": "love"},
    "xin":        {"emoji": "💕", "label": "心心", "emotion": "love"},
}

# ── 图片贴纸映射（与 sticker_manager 扫描 ID 一致）──

_BUILTIN_LABELS = {
    "smile": "微笑", "laugh": "大笑", "celebrate": "庆祝", "thumbsup": "点赞",
    "cry": "大哭", "sad": "难过", "tear": "流泪",
    "angry": "生气", "rage": "暴怒", "hmph": "哼",
    "heart-eyes": "爱心眼", "blush": "害羞", "wink": "眨眼", "puppy-eyes": "卖萌",
    "tongue": "吐舌", "smirk": "坏笑", "silly": "搞怪",
    "heart": "爱心", "kiss": "亲亲", "hug": "抱抱",
}


def _build_image_stickers() -> dict:
    from core.sticker_manager import _scan_builtin
    result = {}
    for s in _scan_builtin():
        stem = s["id"].split("_", 2)[-1] if s["id"].startswith("builtin_") else s.get("label", "")
        result[s["id"]] = {
            "label": _BUILTIN_LABELS.get(stem, stem.replace("-", " ")),
            "emotion": s.get("category", "happy"),
            "type": s.get("type", "image"),
        }
    return result


IMAGE_STICKERS: dict = _build_image_stickers()

# ── 情绪关键词映射 ──

EMOTION_PATTERNS = {
    "happy":      [r"(?i)(哈哈|嘿嘿|笑死|好笑|搞笑|笑|开心|高兴|快乐|逗|有趣|好玩)"],
    "cute":       [r"(?i)(撒娇|求求|好不好嘛|想你|要抱抱|呜呜|好嘛|人家|亲亲|mua|乖)"],
    "sad":        [r"(?i)(难过|伤心|哭|泪|委屈|心疼|不好受|难受)"],
    "angry":      [r"(?i)(生气|气死|烦|讨厌|滚|混蛋|无语|火大)"],
    "playful":    [r"(?i)(嘿嘿嘿|坏笑|邪魅|略略略|略|逗|皮|笨蛋|傻)"],
    "shy":        [r"(?i)(害羞|不好意思|尴尬|脸红|羞羞|别说了)"],
    "surprised":  [r"(?i)(天哪|天啊|真的假的|不是吧|不会吧|震惊|吓|wc|卧槽|我去|啊[？！])"],
    "dismissive": [r"(?i)(随便|算了|行吧|呵呵|哦|嗯|好哦|不了|白眼|无语)"],
    "love":       [r"(?i)(爱你|喜欢|想你了|宝贝|亲爱的|❤|心)"],
}


def select_stickers(reply_text: str, max_stickers: int = 2) -> list[str]:
    """根据回复文本选择最匹配的贴纸 ID 列表。优先返回图片贴纸。"""
    if not reply_text.strip():
        return []

    # 统计每种情绪命中
    emotion_scores: dict[str, float] = {}
    for emotion, patterns in EMOTION_PATTERNS.items():
        score = sum(len(re.findall(p, reply_text)) for p in patterns)
        if score > 0:
            emotion_scores[emotion] = score

    if not emotion_scores:
        return []

    # 按得分排序，取前 max_stickers 种情绪
    sorted_emotions = sorted(emotion_scores.items(), key=lambda x: x[1], reverse=True)
    result: list[str] = []
    used_labels: set[str] = set()

    for emotion, _ in sorted_emotions:
        # 优先选图片贴纸
        image_candidates = [sid for sid, s in IMAGE_STICKERS.items()
                            if s["emotion"] == emotion and s["label"] not in used_labels]
        if image_candidates:
            chosen = random.choice(image_candidates)
            result.append(chosen)
            used_labels.add(IMAGE_STICKERS[chosen]["label"])
        else:
            # 降级到 emoji 贴纸
            emoji_candidates = [sid for sid, s in STICKERS.items()
                                if s["emotion"] == emotion and s["label"] not in used_labels]
            if emoji_candidates:
                chosen = random.choice(emoji_candidates)
                result.append(chosen)
                used_labels.add(STICKERS[chosen]["label"])
        if len(result) >= max_stickers:
            break

    return result


# ── 贴纸的 SVG data URI 生成（纯 CSS + emoji，无需外部资源）──

STICKER_SVG_CACHE: dict[str, str] = {}


def sticker_svg(sticker_id: str) -> str:
    """生成贴纸的 SVG data URI。黄底圆角 + 大 emoji。"""
    if sticker_id not in STICKERS:
        return ""
    if sticker_id in STICKER_SVG_CACHE:
        return STICKER_SVG_CACHE[sticker_id]

    emoji = STICKERS[sticker_id]["emoji"]
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="72" height="72" viewBox="0 0 72 72">
  <rect width="72" height="72" rx="16" fill="#FEE9B0"/>
  <text x="36" y="50" text-anchor="middle" font-size="40">{
    emoji.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")}</text>
</svg>'''
    import base64
    data_uri = "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()
    STICKER_SVG_CACHE[sticker_id] = data_uri
    return data_uri


def get_all_stickers() -> list[dict]:
    """返回所有 emoji 贴纸信息，供前端渲染表情面板。"""
    result = []
    for sid, info in STICKERS.items():
        result.append({
            "id": sid,
            "type": "emoji",
            "emoji": info["emoji"],
            "label": info["label"],
            "emotion": info["emotion"],
            "svg": sticker_svg(sid),
        })
    return result


def get_image_sticker_info(sticker_id: str) -> Optional[dict]:
    """获取图片贴纸的详细信息（含 URL）。"""
    from core.sticker_manager import get_sticker
    sticker = get_sticker(sticker_id)
    if sticker and sticker.get("source") == "builtin":
        info = IMAGE_STICKERS.get(sticker_id, {})
        return {
            "id": sticker_id,
            "type": sticker.get("type", info.get("type", "image")),
            "url": sticker["url"],
            "label": info.get("label", sticker.get("label", "")),
            "category": info.get("emotion", sticker.get("category", "")),
        }
    return None


def is_image_sticker(sticker_id: str) -> bool:
    """判断是否为图片贴纸。"""
    return sticker_id in IMAGE_STICKERS
