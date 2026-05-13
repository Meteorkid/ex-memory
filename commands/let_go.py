"""/let-go — 删除镜像（不可逆）。"""
import shutil
import logging
from config import get_ex_dir
from commands import register

logger = logging.getLogger("ex-memory")


def cmd_let_go(slug: str):
    if not slug:
        print("用法: /let-go {镜像名称}")
        return

    ex_dir = get_ex_dir(slug)
    if not ex_dir.exists():
        print(f"镜像 [{slug}] 不存在。")
        return

    confirm = input(f"确认删除 [{slug}]？这是不可逆操作。(输入 yes 确认): ").strip()
    if confirm != "yes":
        print("已取消。")
        return

    shutil.rmtree(ex_dir)
    logger.info("镜像 %s 已删除", slug)
    print(f"\n镜像 [{slug}] 已删除。")


register("let-go", cmd_let_go)
