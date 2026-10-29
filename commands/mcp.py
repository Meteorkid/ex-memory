"""/mcp-token 与 /mcp-serve — MCP 接入的命令行入口。

/mcp-token issue  签发一枚令牌（明文只展示一次）
/mcp-token list   列出已有令牌
/mcp-token revoke 吊销某枚令牌
/mcp-serve        启动 MCP Server（默认 stdio）

令牌签发刻意只走 CLI：它需要能登上这台机器的人才能操作，
不给「任何能调 API 的人都能给自己签一枚长期令牌」留口子。
"""

from commands import register

USAGE_TOKEN = """用法:
  /mcp-token issue  [--label 名称] [--days 365]
  /mcp-token list
  /mcp-token revoke --prefix exm_xxxxxxxx"""

USAGE_SERVE = """用法:
  /mcp-serve [stdio|sse] [--host 127.0.0.1] [--port 8765]"""


def _parse_flags(args: str) -> tuple[list[str], dict[str, str]]:
    """把 `issue --label astrbot --days 30` 解析成位置参数与开关。"""
    parts = args.split()
    positional: list[str] = []
    flags: dict[str, str] = {}
    i = 0
    while i < len(parts):
        part = parts[i]
        if part.startswith("--"):
            key = part[2:]
            if i + 1 < len(parts) and not parts[i + 1].startswith("--"):
                flags[key] = parts[i + 1]
                i += 2
            else:
                flags[key] = "true"
                i += 1
        else:
            positional.append(part)
            i += 1
    return positional, flags


def _local_user_id() -> int:
    """取本机固定身份。与 server.resolve_user_id 的单人模式路径一致。"""
    from server.auth import get_or_create_external_user_id, init_db

    init_db()
    return get_or_create_external_user_id("mcp_local", "local")


def cmd_mcp_token(args: str = "") -> None:
    positional, flags = _parse_flags(args)
    action = positional[0] if positional else ""

    if action not in ("issue", "list", "revoke"):
        print(USAGE_TOKEN)
        return

    from mcp_server import tokens

    user_id = _local_user_id()

    if action == "issue":
        label = flags.get("label", "")
        try:
            days = int(flags.get("days", "365"))
        except ValueError:
            print("--days 必须是整数")
            return
        token = tokens.issue(user_id, label=label, ttl_days=days)
        print("\n  令牌已签发。请立即复制保存，关闭窗口后无法再次查看：\n")
        print(f"    {token}\n")
        print(f"  前缀    {token[:12]}（吊销时使用）")
        print(f"  用途    {label or '(未命名)'}")
        print(f"  有效期  {days} 天" if days > 0 else "  有效期  永不过期")
        print("\n  把它填进客户端配置的环境变量 EXMEMORY_MCP_TOKEN。\n")
        return

    if action == "list":
        rows = tokens.list_tokens(user_id)
        if not rows:
            print("  还没有签发任何令牌。用 `/mcp-token issue` 创建一枚。")
            return
        print(f"\n  共 {len(rows)} 枚令牌：\n")
        for row in rows:
            state = "已吊销" if row.get("revoked_at") else "有效"
            used = row.get("last_used_at") or "从未使用"
            print(f"    {row['prefix']}  [{state}]  {row.get('label') or '(未命名)'}")
            print(f"        创建 {row.get('created_at')}   最近使用 {used}")
        print()
        return

    prefix = flags.get("prefix", "")
    if not prefix:
        print("  吊销需要指定 --prefix，可先用 `/mcp-token list` 查看。")
        return
    count = tokens.revoke(prefix, user_id=user_id)
    if count:
        print(f"  已吊销 {count} 枚令牌。客户端下次调用将被拒绝。")
    else:
        print("  没有匹配的令牌，或它已经被吊销过了。")


def cmd_mcp_serve(args: str = "") -> None:
    positional, flags = _parse_flags(args)
    transport = positional[0] if positional else "stdio"

    if transport not in ("stdio", "sse", "streamable-http"):
        print(USAGE_SERVE)
        return

    try:
        from mcp_server.server import main

        main(
            transport=transport,
            host=flags.get("host", "127.0.0.1"),
            port=int(flags.get("port", "8765")),
        )
    except RuntimeError as e:
        print(f"  启动失败：{e}")


register("mcp-token", cmd_mcp_token)
register("mcp-serve", cmd_mcp_serve)
