"""MCP 传输层：把 service.py 的能力注册成 MCP 工具。

只做三件事：解析身份、注册工具、跑传输层。
业务规则一律不在这里出现——那是 service.py 的职责。

认证的取舍：
MCP Server 默认是 stdio 进程，由客户端（Claude Desktop / Cursor / AstrBot）
直接拉起，没有 HTTP 请求、也没有登录页面可用。因此身份只能来自环境变量
EXMEMORY_MCP_TOKEN：启动时验证以尽早暴露配置错误，每次工具调用再次
验证同一枚令牌，让吊销和过期对已有连接也生效。环境变量随后变化不会
切换这个连接的身份；新身份必须重新建立连接。
"""

import logging
import os
from typing import Optional

logger = logging.getLogger("ex-memory")

TOKEN_ENV = "EXMEMORY_MCP_TOKEN"


def resolve_user_id(token: Optional[str] = None, allow_local: bool = True) -> int:
    """解析 MCP 调用者身份，返回 user_id。

    两条路径：
    1. 配了 EXMEMORY_MCP_TOKEN —— 校验令牌，多用户部署走这条；
    2. 单人模式且没配令牌 —— 映射到一个本机固定身份，省去用户
       为了在本机接个客户端还先去注册账号。

    令牌**无效**时抛错而不是静默降级到本地身份：设了令牌就说明用户
    想要的是那张身份，悄悄换成另一个账号会把对话写进错误的地方。
    """
    import config

    token = os.getenv(TOKEN_ENV, "") if token is None else token

    from server.auth import get_or_create_external_user_id, init_db

    init_db()

    if token:
        from mcp_server import tokens

        user_id = tokens.validate(token)
        if user_id is None:
            raise RuntimeError("MCP 令牌无效、已过期或已吊销，请重新签发。")
        return user_id

    if allow_local and config.SINGLE_USER_MODE:
        return get_or_create_external_user_id("mcp_local", "local")

    raise RuntimeError(
        f"未设置环境变量 {TOKEN_ENV}。请先在 ex-memory 中执行 "
        f"`python run.py /mcp-token issue --label 你的客户端名`，"
        f"再把生成的令牌填入客户端配置。"
    )


def build_server(user_id: int, *, token: Optional[str] = None):
    """构造 MCP Server 实例并注册全部工具。"""
    import config
    from mcp.server.fastmcp import FastMCP
    from mcp_server import service, tokens
    from mcp_server.service import ServiceError

    token = os.getenv(TOKEN_ENV, "") if token is None else token
    if resolve_user_id(token=token) != user_id:
        raise RuntimeError("MCP 令牌与启动用户身份不匹配，请重新建立连接。")

    mcp = FastMCP("ex-memory")

    def _guard(fn, *args, **kwargs):
        """先复核连接身份，再执行业务；失效令牌不能回退到单人模式。"""
        if token:
            authorized = tokens.validate(token) == user_id
        else:
            # 无令牌身份已在构造时限定为单人模式的固定本机账号。
            authorized = config.SINGLE_USER_MODE
        if not authorized:
            raise RuntimeError(
                "[authentication_required] MCP 令牌无效、已过期或已吊销，"
                "请重新签发并建立连接。"
            )
        try:
            return fn(*args, **kwargs)
        except ServiceError as e:
            raise RuntimeError(f"[{e.code}] {e.message}") from None

    @mcp.tool()
    def exmemory_list_mirrors() -> dict:
        """列出当前账号可用的全部记忆镜像。

        返回每个镜像的 slug 与展示名。后续所有工具都需要 slug，
        不确定有哪些镜像时先调用这个。
        """
        return _guard(service.list_mirrors, user_id)

    @mcp.tool()
    def exmemory_search_memory(
        slug: str,
        query: str,
        top_k: int = 5,
        target_only: bool = True,
    ) -> dict:
        """从记忆库里检索 ta 说过的原话。

        用于在回复前找到 ta 在类似情境下的真实表达，作为语气锚点。
        target_only 为 true 时只召回 ta 的话（默认），false 时包含自己的话。
        返回每条片段的原文与相似度得分 score（0~1，越高越相关）。
        """
        return _guard(service.search_memory, user_id, slug, query, top_k, target_only)

    @mcp.tool()
    def exmemory_chat(
        slug: str,
        message: str,
        history: Optional[list] = None,
        conversation_id: Optional[str] = None,
    ) -> dict:
        """与指定的记忆镜像对话。

        传入 slug 与消息，返回 ta 的回复。
        若被安全闸门拦截，返回 reply 为空串且 blocked 为 true，
        并附带 notice 说明原因——此时不要自行编造回复。
        """
        return _guard(service.chat, user_id, slug, message, history, conversation_id)

    @mcp.tool()
    def exmemory_correct(
        slug: str,
        content: str,
        conversation_id: str = "web",
        message_id: Optional[str] = None,
        source_ids: Optional[list[str]] = None,
    ) -> dict:
        """记录一条语气纠正，例如「ta 不会说亲爱的」。

        纠正会在后续对话中获得最高优先级，用于修正记忆体偏离本人表达习惯的地方。
        同一主题重复纠正会被合并计数，不必担心重复提交。
        """
        return _guard(
            service.correct,
            user_id,
            slug,
            content,
            conversation_id,
            message_id,
            source_ids,
        )

    @mcp.tool()
    def exmemory_conversations(slug: str) -> dict:
        """列出本账号镜像的持久会话，供网页与 IM 明确选择同一会话继续。"""
        return _guard(service.conversations, user_id, slug)

    @mcp.tool()
    def exmemory_conversation_messages(slug: str, conversation_id: str) -> dict:
        """读取指定会话的消息、来源快照和归档状态。"""
        return _guard(service.conversation_messages, user_id, slug, conversation_id)

    @mcp.tool()
    def exmemory_list_corrections(slug: str) -> dict:
        """列出某个镜像上已经记录的全部纠正。"""
        return _guard(service.list_corrections, user_id, slug)

    @mcp.tool()
    def exmemory_reflect(slug: str) -> dict:
        """启动一次关系反思分析（异步）。

        返回 task_id，分析在后台执行，完成后写入镜像的 reflections。
        不要在调用后阻塞等待结果。
        """
        return _guard(service.reflect, user_id, slug)

    @mcp.tool()
    def exmemory_status(slug: str, include_corrections: bool = False) -> dict:
        """查看镜像的记忆状态快照。

        包含对话条数、情绪记忆、关系状态。只读，不会调用大模型，
        可以放心频繁查询。
        """
        return _guard(service.status, user_id, slug, include_corrections)

    # ── 主动消息 ──
    #
    # 生成（tick）和投递分开，推送失败只重试投递，不再重复生成和扣额度。
    # prepare_delivery 在每次发送前复核当前限制与内容，status 保持只读。

    @mcp.tool()
    def exmemory_proactive_status(slug: str) -> dict:
        """查看主动消息的开关、免打扰时段、待送队列与当前阻断原因。

        只读，不调用大模型，适合高频轮询。blocked_reason 非空表示
        即使满足触发条件也不会推送（冷静期 / 达当日时长上限 / 近期危机事件）。
        """
        return _guard(service.proactive_status, user_id, slug)

    @mcp.tool()
    def exmemory_proactive_config(
        slug: str,
        enabled: Optional[bool] = None,
        max_per_day: Optional[int] = None,
        quiet_start: Optional[int] = None,
        quiet_end: Optional[int] = None,
    ) -> dict:
        """修改主动消息配置。未传的字段保持原值。

        默认关闭——主动消息是打扰，必须由用户明确开启。
        max_per_day 取 0~10，quiet_start / quiet_end 取 0~23（免打扰区间，可跨午夜）。
        """
        return _guard(
            service.proactive_config,
            user_id,
            slug,
            enabled,
            max_per_day,
            quiet_start,
            quiet_end,
        )

    @mcp.tool()
    def exmemory_proactive_tick(slug: str) -> dict:
        """判定此刻是否该主动发消息；该发则生成并排队。

        会调用大模型，是按成本计费的调用。客户端应按低频节奏调用
        （建议每小时一次）；判定自带的免打扰与每日上限会拦住过量生成。
        返回 sent 为 false 时 reason 字段说明原因，不要自行编造一条消息。
        """
        return _guard(service.proactive_tick, user_id, slug)

    @mcp.tool()
    def exmemory_proactive_prepare_delivery(slug: str, message_id: int) -> dict:
        """发送一条待送消息前，复核当前投递限制和输出审核。

        只发送 deliverable 为 true 时返回的 content；拒绝时保留队列，
        不发送也不 ack。此步骤不生成消息或再次扣取模型额度。
        """
        return _guard(service.proactive_prepare_delivery, user_id, slug, message_id)

    @mcp.tool()
    def exmemory_proactive_ack(message_ids: list[int]) -> dict:
        """把已成功发到他手机/聊天软件的消息标记为已送达。

        投递成功后必须调用，否则同一条消息会在下次轮询时被重复发送。
        """
        return _guard(service.proactive_ack, user_id, message_ids)

    return mcp


def main(transport: str = "stdio", host: str = "127.0.0.1", port: int = 8765):
    """启动 MCP Server。

    默认 stdio，且网络传输默认只监听 127.0.0.1——
    记忆内容是最私密的个人数据，绝不能因为一个配置失误就暴露到局域网。
    """
    # 绑定地址的校验排在身份解析之前：配置错误应当在最便宜的一步就失败，
    # 而不是先建好引擎、连上数据库，最后才发现绑不上去
    if transport == "stdio":
        pass
    elif transport in ("sse", "streamable-http"):
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise RuntimeError(
                f"只允许监听本机回环地址，拒绝绑定到 {host}："
                f"记忆内容是最私密的个人数据，绑到公网或局域网等于直接公开。"
                f"确需远程访问请走 SSH 隧道。"
            )
    else:
        raise RuntimeError(f"不支持的传输方式: {transport}")

    token = os.getenv(TOKEN_ENV, "")
    user_id = resolve_user_id(token=token)
    mcp = build_server(user_id, token=token)

    if transport == "stdio":
        mcp.run(transport="stdio")
        return

    mcp.settings.host = host
    mcp.settings.port = port
    mcp.run(transport=transport)
