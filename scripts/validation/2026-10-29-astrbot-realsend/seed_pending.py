#!/usr/bin/env python3
"""往主动消息队列里塞一条测试消息，让真机观测有东西可发。

为什么需要它：`proactive_tick` 只有在「沉默够久 + 没超日上限 + 不在免打扰 +
健康保护没触发」时才生成内容。刚装好的环境往往一个小时内一条都不会生成，
于是观测脚本只能得出「什么都没发生」，验证不了投递链路。

这条消息只走「排队 → 插件取走 → 平台投递 → ack」这一段，
不经过大模型，正好把被测环节单独拎出来。

用法（在 ex-memory 项目根目录执行）
    EXMEMORY_MCP_TOKEN=<插件里填的同一个令牌> \\
        python scripts/validation/2026-10-29-astrbot-realsend/seed_pending.py \\
        --slug demo --text "投递自检：如果你在聊天窗口看到这句，说明真机链路通了"
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description="排队一条主动消息用于真机自检")
    parser.add_argument("--slug", required=True, help="记忆镜像名，必须与 /exbind 用的一致")
    parser.add_argument(
        "--text",
        default="投递自检：在聊天窗口看到这句，说明真机投递链路是通的。",
        help="要发送的内容",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="只列出当前待送队列，不新增",
    )
    args = parser.parse_args()

    from mcp_server.server import resolve_user_id
    from core.proactive import pending_messages, queue_message

    user_id_holder: dict[str, int] = {}
    try:
        user_id_holder["id"] = resolve_user_id()
    except RuntimeError as exc:
        print(f"拿不到身份：{exc}")
        print()
        print("把插件里填的那个令牌带上再跑一次：")
        print("  EXMEMORY_MCP_TOKEN=<令牌> python .../seed_pending.py --slug demo")
        return 2

    user_id = user_id_holder["id"]
    print(f"身份：user_id={user_id}")

    if args.list:
        pending = pending_messages(user_id, args.slug)
        if not pending:
            print(f"[{args.slug}] 待送队列为空")
            return 0
        for item in pending:
            print(f"  #{item['id']} {item['created_at']} {item['content'][:60]}")
        return 0

    message_id = queue_message(args.slug, user_id, "manual_check", args.text)
    if message_id is None:
        print("内容为空，没有排队")
        return 1

    print(f"已排队 #{message_id} → 镜像 [{args.slug}]")
    print(f"当前待送 {len(pending_messages(user_id, args.slug))} 条")
    print()
    print("接下来等插件的下一轮轮询（默认 60 分钟；自检时可把")
    print("proactive_interval_minutes 临时改成 5，测完再改回 60）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
