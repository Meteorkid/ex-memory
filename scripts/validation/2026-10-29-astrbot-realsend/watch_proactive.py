#!/usr/bin/env python3
"""盯一小时日志，看主动消息到底有没有真的发出去。

check_send_path.py 证明的是「代码路径对不对」，证明不了「真机上跑起来对不对」。
这个脚本补的就是后半段：装上 AstrBot、绑定会话、打开主动消息之后，
用它盯住日志，把每一条投递结果摘出来，最后给一个结论。

用法
    python watch_proactive.py --log data/logs/astrbot.log --minutes 60

日志路径在 AstrBot 里默认是 <data>/logs/astrbot.log，
如果改过 log_file_path，把改后的路径传进来即可。
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

# 插件里每条结局对应的日志原文（改动插件文案时这里要一起改）
MARKERS = {
    "delivered": re.compile(r"已投递 (\d+) 条主动消息 slug=(\S+)"),
    "no_session": re.compile(r"平台未找到目标会话，主动消息留待重试 slug=(\S+)"),
    "send_failed": re.compile(r"发送主动消息失败 slug=(\S+): (.*)"),
    "blocked": re.compile(r"健康保护生效，跳过主动消息 slug=(\S+)"),
    "round_error": re.compile(r"主动消息轮询出错，稍后重试: (.*)"),
    "mcp_failed": re.compile(r"ex-memory 调用失败: (.*)"),
    "bind_ok": re.compile(r"已把当前会话绑定到 \[(\S+)\]"),
    "config_unsaved": re.compile(r"当前配置对象不支持保存"),
}

TS = re.compile(r"^\[(\d{2}:\d{2}:\d{2})")


@dataclass
class Summary:
    hits: dict[str, int] = field(default_factory=dict)
    delivered_total: int = 0
    slugs: set[str] = field(default_factory=set)
    errors: list[str] = field(default_factory=list)

    def bump(self, kind: str):
        self.hits[kind] = self.hits.get(kind, 0) + 1


def classify(line: str, summary: Summary) -> str | None:
    """返回这条日志的类别；不相关就返回 None。"""
    for kind, pattern in MARKERS.items():
        match = pattern.search(line)
        if not match:
            continue

        summary.bump(kind)
        if kind == "delivered":
            summary.delivered_total += int(match.group(1))
            summary.slugs.add(match.group(2))
        elif kind in {"send_failed", "round_error", "mcp_failed"}:
            summary.errors.append(line.strip())
            if match.groups():
                summary.slugs.add(match.group(1))
        elif kind in {"no_session", "blocked"}:
            summary.slugs.add(match.group(1))
        return kind
    return None


def verdict(summary: Summary, seconds: int, saw_plugin: bool) -> tuple[bool, str]:
    """给一句人话结论。"""
    if not saw_plugin:
        return False, (
            "整段日志里没有任何一条本插件的记录。"
            "先确认插件已启用（WebUI 插件页）、exmemory_dir 与令牌已填，"
            "然后发一条消息看有没有「已连接 ex-memory MCP Server」之外的动静。"
        )

    if summary.delivered_total > 0:
        extra = ""
        if summary.hits.get("send_failed"):
            extra = f"，另有 {summary.hits['send_failed']} 次发送失败（见上面时间点）"
        return True, f"真机投递成功，共 {summary.delivered_total} 条{extra}。"

    if summary.hits.get("send_failed"):
        return False, "有投递尝试但都失败了，看上面每条的错误原因。"

    if summary.hits.get("no_session"):
        return False, (
            "平台没找到目标会话。通常是绑定时记录的 umo 对应的那个会话已失效——"
            "比如私聊对方删了机器人、群聊机器人被踢出，或换过机器人实例。"
            "重新在该会话里发一次 /exbind 即可刷新地址。"
        )

    if summary.hits.get("blocked"):
        return (
            True,
            "健康保护生效，主动消息被有意拦下了。这条路径本来就该什么都不发，"
            "不算故障；想验证投递请换到非免打扰时段，或先关掉健康保护观察一次。",
        )

    if summary.hits.get("round_error"):
        return False, "轮询本身报错了，先修这个再看投递。"

    return False, (
        f"{seconds // 60} 分钟内没有出现任何一轮投递记录。"
        "常见原因：服务端主动消息开关没开（/exproactive on 只是打开它）、"
        "待发送队列为空（ta 这一轮没生成新消息）、"
        "或轮询间隔大于观测时长（默认 60 分钟，第一轮要等满一个间隔才跑）。"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="观察 AstrBot 主动消息投递")
    parser.add_argument(
        "--log",
        default="data/logs/astrbot.log",
        help="AstrBot 日志文件，默认 data/logs/astrbot.log",
    )
    parser.add_argument("--minutes", type=float, default=60, help="观测时长（分钟）")
    parser.add_argument(
        "--from-start",
        action="store_true",
        help="从头读而不是只看新写入的行",
    )
    args = parser.parse_args()

    path = Path(args.log).expanduser()
    if not path.exists():
        print(f"找不到日志：{path}")
        print("确认 AstrBot 已启动过，或用 --log 指定实际路径。")
        return 2

    summary = Summary()
    saw_plugin = False
    deadline = time.monotonic() + args.minutes * 60

    print(f"观测 {args.minutes:g} 分钟：{path}")
    print("期间可以去做别的事，结束后会打印结论。\n")

    with path.open("r", encoding="utf-8", errors="replace") as fh:
        if not args.from_start:
            fh.seek(0, 2)
        buffer = ""
        while time.monotonic() < deadline:
            chunk = fh.readline()
            if not chunk:
                time.sleep(1.0)
                continue
            if not chunk.endswith("\n"):
                buffer = chunk
                continue
            line = buffer + chunk
            buffer = ""

            if "astrbot-plugin.main" in line:
                saw_plugin = True

            kind = classify(line, summary)
            if kind:
                stamp = TS.match(line)
                when = stamp.group(1) if stamp else "--:--:--"
                print(f"[{when}] {kind:12s} {line.strip()[:150]}")

    print()
    print("—" * 60)
    print(f"投递成功 {summary.delivered_total} 条，涉及镜像：{', '.join(sorted(summary.slugs)) or '无'}")
    if summary.hits:
        detail = "，".join(f"{k}={v}" for k, v in sorted(summary.hits.items()))
        print(f"明细：{detail}")
    if summary.errors:
        print("\n错误原文：")
        for line in summary.errors[:10]:
            print(f"  {line[:200]}")

    ok, why = verdict(summary, int(args.minutes * 60), saw_plugin)
    print()
    print(("结论：通过。 " if ok else "结论：需要排查。 ") + why)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
