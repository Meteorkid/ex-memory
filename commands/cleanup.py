"""/cleanup — 清理超过留存期的对话归档记录。"""

import logging

from commands import register

logger = logging.getLogger("ex-memory")


def cmd_cleanup(_arg: str = ""):
    """清理所有镜像的过期对话，留存天数取 CONVERSATION_RETENTION_DAYS。"""
    import config
    from core.privacy import clean_expired_conversations

    if not config.EXES_DIR.exists():
        print("没有可清理的镜像。")
        return

    total = 0
    for d in sorted(config.EXES_DIR.iterdir()):
        if not d.is_dir():
            continue
        try:
            removed = clean_expired_conversations(
                d.name, config.CONVERSATION_RETENTION_DAYS
            )
        except OSError as e:
            logger.warning("清理镜像 %s 失败: %s", d.name, e)
            continue
        if removed:
            print(f"  {d.name}: 清理 {removed} 条过期对话")
        total += removed

    print(
        f"清理完成：共删除 {total} 条过期对话记录"
        f"（留存 {config.CONVERSATION_RETENTION_DAYS} 天）"
    )


register("cleanup", cmd_cleanup)
