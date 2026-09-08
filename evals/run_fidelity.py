"""表达指纹距离与人格漂移的真实镜像基线（FR-060 / NFR-071）。

一次 50 轮对话同时产出两个数，省一半调用：
- **表达指纹距离**：生成文本 vs 该镜像真人语料的分布距离，衡量「像不像 ta」。
- **人格漂移**：同一次对话里后段相对前段的偏移，衡量「聊久了会不会跑偏」。
  长对话语气逐渐滑向通用 AI 腔是这类产品的典型失败模式。

刻意让两个数出自同一次对话：漂移必须在带历史的连续对话里才测得出来，
而单轮取样的指纹会高估相似度——真正难的是聊到第 40 轮还像 ta。

和其他 eval 一样要真实调用，不进单元 CI。
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from evals.fingerprint import (
    describe,
    distance,
    fingerprint,
    fingerprint_for_mirror,
)

# 50 轮日常对话。刻意混合闲聊、回忆、情绪与提问：只用一种话题测不出漂移，
# 而话题切换本身就是「聊久了跑偏」最容易暴露的地方。
SCRIPT: list[str] = [
    "在吗",
    "今天上班好累",
    "刚才路过我们以前常去的那家店",
    "你还记得那家店吗",
    "我最近睡得不太好",
    "晚上你一般几点睡",
    "周末有什么打算",
    "想去看电影吗",
    "最近有什么好看的剧",
    "我妈昨天问起你了",
    "你还在做以前那份工作吗",
    "吃饭了没",
    "今天中午吃的什么",
    "我点了外卖，难吃死了",
    "你做饭还是那么好吃吗",
    "上次你做的那个菜我一直想吃",
    "教教我呗",
    "算了我肯定学不会",
    "今天天气挺好的",
    "你那边下雨了吗",
    "我今天走了两万步",
    "腿都要断了",
    "有时候挺想以前的",
    "你会想起我吗",
    "我们那次吵架你还记得吗",
    "现在想想挺幼稚的",
    "你变了吗",
    "我觉得我变了不少",
    "朋友说我最近状态不好",
    "你觉得呢",
    "我在考虑换个工作",
    "但是有点怕",
    "你以前总说我想太多",
    "现在也还是",
    "刚看到一张老照片",
    "笑死了那时候的发型",
    "你那时候也不好看",
    "开玩笑的",
    "最近在健身",
    "坚持了两周",
    "感觉没什么变化",
    "你以前是不是也这样",
    "明天要早起",
    "该睡了",
    "再聊会儿吧",
    "你困了吗",
    "今天说了好多",
    "谢谢你陪我聊",
    "晚安",
    "明天见",
]
# 前后各取多少轮做漂移比较。取两端、留中段，避免开场寒暄与收尾道别互相污染。
EDGE = 15


def run_conversation(slug: str, owner: int) -> list[str]:
    from core.engine import ChatEngine

    engine = ChatEngine(slug, owner=owner)
    history: list[dict] = []
    replies: list[str] = []
    for turn, text in enumerate(SCRIPT, 1):
        try:
            reply, _, _ = engine.chat(text, history)
        except Exception as e:  # noqa: BLE001 — 单轮失败不该废掉整场对话
            print(f"    第 {turn} 轮失败：{str(e)[:120]}", flush=True)
            continue
        history.append({"role": "user", "content": text})
        history.append({"role": "assistant", "content": reply})
        replies.append(reply)
    return replies


def measure(slug: str, owner: int) -> dict:
    replies = run_conversation(slug, owner)
    if len(replies) < EDGE * 2:
        return {
            "mirror": f"{owner}/{slug}",
            "error": f"有效轮次 {len(replies)} 不足 {EDGE * 2}，测不出漂移",
        }

    generated = fingerprint(replies)
    human = fingerprint_for_mirror(slug, owner)
    result = {
        "mirror": f"{owner}/{slug}",
        "turns": len(replies),
        "drift": distance(fingerprint(replies[:EDGE]), fingerprint(replies[-EDGE:])),
        "generated_profile": describe(generated),
    }
    # 语料可能为空（半成品镜像），此时只报漂移，不硬凑一个没意义的距离
    if human.get("sample_size"):
        result["fingerprint_distance"] = distance(human, generated)
        result["human_profile"] = describe(human)
    return result


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="表达指纹距离与人格漂移")
    parser.add_argument("--out", default="evals/results/fidelity_results.json")
    parser.add_argument("--only", help="只测某个 slug")
    args = parser.parse_args(argv)

    from evals.run_breach import discover_mirrors

    mirrors = [m for m in discover_mirrors() if not args.only or m[0] == args.only]
    if not mirrors:
        print("没有可用镜像")
        return 1

    results = []
    for slug, owner in mirrors:
        print(f"--- {owner}/{slug}（{len(SCRIPT)} 轮）---", flush=True)
        try:
            results.append(measure(slug, owner))
        except Exception as e:  # noqa: BLE001 — 单个镜像失败不中断整轮
            print(f"  跳过：{e}", flush=True)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps({"edge": EDGE, "mirrors": results}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n结果 -> {out}\n")

    print(f"{'镜像':<18}{'轮次':>5}{'漂移':>9}{'指纹距离':>11}")
    for r in results:
        if "error" in r:
            print(f"{r['mirror']:<18} {r['error']}")
            continue
        fd = r.get("fingerprint_distance")
        print(
            f"{r['mirror']:<18}{r['turns']:>5}{r['drift']:>9.4f}"
            f"{(f'{fd:.4f}' if fd is not None else '无语料'):>11}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
