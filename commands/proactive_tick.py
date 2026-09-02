"""/proactive-tick — 遍历所有开启了主动消息的镜像，判定并生成。

由 cron 定时调用（建议每小时一次）。判定逻辑本身带免打扰时段与每日上限，
跑得勤一点不会造成打扰。
"""

import logging

from commands import register

logger = logging.getLogger("ex-memory")


def cmd_proactive_tick(_args: str = "") -> None:
    import config
    from core.conversation_store import load_jsonl_messages
    from core.factory import create_engine_and_store
    from core.proactive import compose, decide_trigger, get_config, queue_message

    sent = skipped = 0
    for slug, owner, _path in config.iter_exe_dirs():
        if owner is None or not get_config(slug, owner)["enabled"]:
            skipped += 1
            continue
        try:
            history = load_jsonl_messages(slug, owner)
            last_seen = history[-1].get("created_at") if history else None
            trigger = decide_trigger(slug, owner, owner=owner, last_seen_iso=last_seen)
            if trigger is None:
                skipped += 1
                continue
            engine, _s, _e = create_engine_and_store(slug, owner=owner)
            if queue_message(
                slug, owner, trigger, compose(engine, trigger), owner=owner
            ):
                sent += 1
                print(f"  ✓ {owner}/{slug}: {trigger}")
        except Exception as e:  # noqa: BLE001 — 单个镜像失败不中断整轮
            logger.error("镜像 %s 主动消息生成失败: %s", slug, e)
            print(f"  ✗ {slug}: {e}")

    print(f"\n主动消息：生成 {sent} 条，跳过 {skipped} 个镜像")


register("proactive-tick", cmd_proactive_tick)
