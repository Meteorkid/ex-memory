"""/rollback — 回滚到指定版本。"""
from commands import register


def cmd_rollback(args: str):
    parts = args.split()
    if len(parts) < 2:
        print("用法: /rollback {镜像名称} {版本号}")
        return

    slug, version = parts[0], parts[1]
    from core.version_manager import rollback as do_rollback, list_versions
    try:
        do_rollback(slug, version)
        print(f"已回滚 [{slug}] 到版本 {version}")
    except FileNotFoundError as e:
        print(f"回滚失败: {e}")
        versions = list_versions(slug)
        if versions:
            print("可用版本：")
            for v in versions:
                print(v)


register("rollback", cmd_rollback)
