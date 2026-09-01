"""/grant-admin — 授予或撤销管理员角色。

权限提升刻意只走 CLI：需要能登上服务器的人才能操作，
不给「第一个注册的用户自动成为管理员」这类便利逻辑留口子。
"""

from commands import register


def cmd_grant_admin(args: str = "") -> None:
    """用法: /grant-admin {用户名} [--revoke]"""
    parts = args.strip().split()
    if not parts:
        print("用法: /grant-admin {用户名} [--revoke]")
        return

    username = parts[0]
    role = "user" if "--revoke" in parts else "admin"

    from server.auth import set_user_role

    if set_user_role(username, role):
        action = "已撤销管理员权限" if role == "user" else "已授予管理员权限"
        print(f"{action}: {username}")
    else:
        print(f"用户不存在: {username}")


register("grant-admin", cmd_grant_admin)
