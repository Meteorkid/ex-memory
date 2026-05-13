"""/backup — 备份镜像版本。"""
from commands import register


def cmd_backup(slug: str):
    if not slug:
        print("用法: /backup {镜像名称}")
        return
    from core.version_manager import backup as do_backup
    try:
        version_name = do_backup(slug)
        print(f"备份成功！版本：{version_name}")
    except FileNotFoundError as e:
        print(f"备份失败: {e}")


register("backup", cmd_backup)
