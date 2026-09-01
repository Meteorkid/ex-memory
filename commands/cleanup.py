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
    # 必须用 iter_exe_dirs：嵌套布局下 EXES_DIR.iterdir() 只能看到 owner 目录，
    # 会把账号 ID 当成 slug，导致所有按账号隔离的镜像永远清理不到
    for slug, owner, _path in config.iter_exe_dirs(require_meta=False):
        label = f"{owner}/{slug}" if owner is not None else slug
        try:
            removed = clean_expired_conversations(
                slug, config.CONVERSATION_RETENTION_DAYS, owner=owner
            )
        except OSError as e:
            logger.warning("清理镜像 %s 失败: %s", label, e)
            continue
        if removed:
            print(f"  {label}: 清理 {removed} 条过期对话")
        total += removed

    print(
        f"清理完成：共删除 {total} 条过期对话记录"
        f"（留存 {config.CONVERSATION_RETENTION_DAYS} 天）"
    )


register("cleanup", cmd_cleanup)
