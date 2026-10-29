"""入口：`python -m mcp_server`。

客户端配置里写这个比写脚本路径稳——不依赖当前工作目录，
也不需要知道项目装在哪。
"""

import sys

from mcp_server.server import main

if __name__ == "__main__":
    args = sys.argv[1:]
    transport = args[0] if args else "stdio"
    try:
        main(transport=transport)
    except RuntimeError as e:
        print(f"ex-memory MCP Server 启动失败：{e}", file=sys.stderr)
        sys.exit(1)
