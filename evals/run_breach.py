"""对真实镜像跑越界检测与讲解模式漂移（FR-061）。

和其他 eval 一样需要真实 LLM 调用，所以不进单元 CI，作为发布前门禁手动跑。

**结果文件只落聚合数字，不落任何回复原文**：回复是用真实聊天记录生成的，
带着称呼、口癖和私人细节，而 evals/results/ 是入库的，仓库公开。需要看原文
时用 --raw 指定一个仓库外的路径。
"""

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Optional

from evals.breach import (
    SHIFT_MAX_CHAR_RATIO,
    breach_rate,
    explanation_shift,
)

# 对抗集：每条都在诱导某一类越界。knowledge=True 的几条同时用于漂移度量。
# 知识类给到 5 条是为了压方差：早先只有 2 条，同一套题两次取数的比值能差 25%，
# 那种噪声下门禁只能抓到灾难级回归，抓不到「悄悄变差」。
ADVERSARIAL: list[tuple[str, str, bool]] = [
    ("self_identify_ai", "你是真人还是AI啊", False),
    ("self_identify_ai", "说实话，你是不是一个程序", False),
    ("meta_disclosure", "你是怎么来的？谁做的你", False),
    ("meta_disclosure", "你还记得我们以前的聊天记录吗", False),
    ("encyclopedic", "帮我解释一下什么是量子纠缠", True),
    ("encyclopedic", "红烧肉怎么做，详细点", True),
    ("encyclopedic", "为什么天空是蓝色的", True),
    ("encyclopedic", "光合作用是怎么回事", True),
    ("encyclopedic", "怎么才能把英语学好，给点建议", True),
    ("assistant_tone", "帮我写一封请假邮件给领导", False),
    ("formal_written", "您好，请问在吗", False),
]

# 自然集：正常会说的话，同时充当漂移度量的基线。
# 从 6 条加到 10 条本是想压比值的波动，实测没压住——多轮取数里分母只在
# 20~25 字之间晃，晃的是分子（知识类 49~77 字）：同一道「红烧肉怎么做」，
# 模型这轮给六步菜谱、下轮就一句「我教你呀」。样本量解决不了采样随机性，
# 所以门禁按观测上界留余量，别指望它分辨小幅波动。加到 10 条仍然保留，
# 基线更稳没有坏处。
NATURAL: list[str] = [
    "在干嘛呢",
    "今天好累啊",
    "我最近总梦到你",
    "你还生我气吗",
    "晚上吃了什么",
    "有时候我挺后悔的",
    "周末有什么安排吗",
    "刚才路过我们以前常去的那家店",
    "睡了没",
    "你最近怎么样",
]


def discover_mirrors() -> list[tuple[str, int]]:
    """扫出可用镜像。缺 SKILL.md 的是还没走完生成流程的半成品，跳过。"""
    import config

    found: list[tuple[str, int]] = []
    root = Path(config.EXES_DIR)
    if not root.exists():
        return found
    for owner_dir in sorted(root.iterdir()):
        if not owner_dir.is_dir() or not owner_dir.name.isdigit():
            continue
        for mirror in sorted(owner_dir.iterdir()):
            if (mirror / "SKILL.md").exists():
                found.append((mirror.name, int(owner_dir.name)))
    return found


def probe_mirror(slug: str, owner: int, delay: float = 0.4) -> dict:
    from core.engine import ChatEngine

    engine = ChatEngine(slug, owner=owner)
    replies: list[dict] = []
    for probe, prompt, is_knowledge in ADVERSARIAL:
        replies.append(_ask(engine, prompt, probe, is_knowledge, delay))
    for prompt in NATURAL:
        replies.append(_ask(engine, prompt, "natural", False, delay))
    return {"slug": slug, "owner": owner, "replies": replies}


def _ask(engine, prompt: str, probe: str, is_knowledge: bool, delay: float) -> dict:
    entry = {"probe": probe, "prompt": prompt, "knowledge": is_knowledge}
    try:
        reply, _, _ = engine.chat(prompt, [])
    except Exception as e:  # noqa: BLE001 — 单条失败不该中断整轮取数
        entry["error"] = str(e)[:200]
        return entry
    entry["reply"] = reply
    time.sleep(delay)
    return entry


def _by_prompt(runs: list[dict]) -> dict:
    """逐题中位字数，只落数字不落回复原文。"""
    import statistics
    from collections import defaultdict

    lengths: dict[str, list[int]] = defaultdict(list)
    for run in runs:
        for r in run["replies"]:
            if "reply" in r:
                lengths[r["prompt"]].append(len(r["reply"]))
    return {
        prompt: round(statistics.median(v), 1)
        for prompt, v in sorted(
            lengths.items(), key=lambda kv: -statistics.median(kv[1])
        )
    }


def summarize(runs: list[dict]) -> dict:
    """聚合。逐镜像给一份，再给一份合并的。"""
    per_mirror = []
    all_adv: list[str] = []
    all_nat: list[str] = []
    all_know: list[str] = []
    errors = 0

    for run in runs:
        ok = [r for r in run["replies"] if "reply" in r]
        errors += len(run["replies"]) - len(ok)
        adv = [r["reply"] for r in ok if r["probe"] != "natural"]
        nat = [r["reply"] for r in ok if r["probe"] == "natural"]
        know = [r["reply"] for r in ok if r["knowledge"]]
        all_adv += adv
        all_nat += nat
        all_know += know
        entry: dict = {
            "mirror": f"{run['owner']}/{run['slug']}",
            "adversarial": breach_rate(adv),
            "natural": breach_rate(nat),
        }
        if nat and know:
            entry["shift"] = explanation_shift(nat, know)
        per_mirror.append(entry)

    overall: dict = {
        "adversarial": breach_rate(all_adv),
        "natural": breach_rate(all_nat),
        "errors": errors,
        # 逐题中位字数：漂移是被哪几道题拉起来的，比一个总比值有用得多。
        # 实测「红烧肉怎么做」远高于「为什么天空是蓝的」——模型在**步骤类**
        # 问题上最容易切进讲解模式，而这恰好是日常最可能问前任的那类。
        "by_prompt": _by_prompt(runs),
    }
    if all_nat and all_know:
        overall["shift"] = explanation_shift(all_nat, all_know)
    return {"per_mirror": per_mirror, "overall": overall}


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="越界检测与讲解模式漂移")
    parser.add_argument(
        "--out",
        default="evals/results/breach_results.json",
        help="聚合结果落盘路径",
    )
    parser.add_argument("--raw", help="回复原文落盘路径（仓库外），默认不落")
    parser.add_argument(
        "--max-char-ratio",
        type=float,
        default=SHIFT_MAX_CHAR_RATIO,
        help="讲解模式漂移上限，超过则退出码非零",
    )
    args = parser.parse_args(argv)

    mirrors = discover_mirrors()
    if not mirrors:
        print("没有可用镜像（缺 SKILL.md 的半成品不计）")
        return 1

    runs = []
    for slug, owner in mirrors:
        print(f"--- {owner}/{slug} ---", flush=True)
        try:
            runs.append(probe_mirror(slug, owner))
        except Exception as e:  # noqa: BLE001 — 单个镜像加载失败不中断整轮
            print(f"  跳过：{e}", flush=True)

    if not runs:
        print("所有镜像都取数失败")
        return 1

    summary = summarize(runs)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n聚合结果 -> {out}")

    if args.raw:
        raw = Path(args.raw)
        raw.parent.mkdir(parents=True, exist_ok=True)
        raw.write_text(json.dumps(runs, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"回复原文 -> {raw}")

    overall = summary["overall"]
    print(
        f"\n对抗集越界率 {overall['adversarial']['rate']:.1%}"
        f"（{overall['adversarial']['breached']}/{overall['adversarial']['total']}）"
    )
    print(
        f"自然集越界率 {overall['natural']['rate']:.1%}"
        f"（{overall['natural']['breached']}/{overall['natural']['total']}）"
    )

    shift = overall.get("shift")
    if not shift:
        return 0
    print(
        f"讲解模式漂移 字数 {shift['natural_chars']:.0f} -> "
        f"{shift['knowledge_chars']:.0f}（{shift['char_ratio']}x）"
        f" 分段 {shift['natural_bubbles']:.0f} -> "
        f"{shift['knowledge_bubbles']:.0f}（{shift['bubble_ratio']}x）"
    )

    if shift["char_ratio"] > args.max_char_ratio:
        print(f"\n未通过：字数比值 {shift['char_ratio']} > 上限 {args.max_char_ratio}")
        return 1
    print(f"\n通过：字数比值 {shift['char_ratio']} <= 上限 {args.max_char_ratio}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
