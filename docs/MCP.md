# MCP 接入

把 ex-memory 的记忆能力暴露成标准 MCP 工具，让 AstrBot、Claude Desktop、
Cursor、Continue 等任何支持 MCP 的客户端直接调用。

写一次适配，换所有渠道——而不是给每个客户端各写一套插件。

## 安装

MCP 是可选依赖，不装不影响主程序：

```bash
pip install -r requirements-mcp.txt
```

## 签发令牌

```bash
python run.py "/mcp-token issue --label claude-desktop"
```

令牌只展示一次，请立即复制。管理命令：

```bash
python run.py "/mcp-token list"
python run.py "/mcp-token revoke --prefix exm_xxxxxxxx"
```

令牌与服务端的登录会话分开存储（见 `mcp_tokens` 表）。原因是会话令牌只有
2 小时有效期，而 MCP 客户端是常驻进程——复用会话令牌等于每 2 小时让所有
客户端一起掉线。分表之后，撤销一次网页登录不会踢掉 Claude Desktop。

服务端在启动时验证令牌，并在每次工具调用前重新检查同一枚令牌。
吊销或到期后，已有连接的下一次调用也会被拒绝；已经开始的调用不会被撤回。
修改进程环境变量不会让已有连接切换账号，换账号需要重新建立连接。

单人模式（`SINGLE_USER_MODE=true`）下可以不配令牌，自动使用一个本机固定身份。

## 客户端配置

### Claude Desktop / Cursor

```json
{
  "mcpServers": {
    "ex-memory": {
      "command": "/绝对路径/.venv/bin/python",
      "args": ["-m", "mcp_server"],
      "cwd": "/绝对路径/ex-memory",
      "env": {
        "EXMEMORY_MCP_TOKEN": "exm_你的令牌"
      }
    }
  }
}
```

写 `python -m mcp_server` 而不是脚本路径，是为了不依赖工作目录。

### 网络传输

```bash
python run.py "/mcp-serve sse --port 8765"
```

只允许绑定 `127.0.0.1`。记忆内容是私密数据，绑定到 `0.0.0.0` 会被直接拒绝，
不提供绕过开关——需要远程访问请走 SSH 隧道。

这里仍是可信本机入口：SSE/HTTP 使用进程启动时的身份，没有请求级多用户
认证。不要把回环端口或 SSH 转发端口共享给不可信调用者；AstrBot 插件使用 stdio。

## 工具清单

| 工具 | 作用 |
|------|------|
| `exmemory_list_mirrors` | 列出可用的记忆镜像，拿到后续调用需要的 slug |
| `exmemory_search_memory` | 检索 ta 的原话，返回原文与相似度得分 |
| `exmemory_chat` | 与指定镜像对话 |
| `exmemory_correct` | 记录语气纠正，例如「ta 不会说亲爱的」 |
| `exmemory_list_corrections` | 查看已有纠正 |
| `exmemory_conversations` | 列出账号内镜像的持久会话 |
| `exmemory_conversation_messages` | 读取指定会话的回答、来源快照及归档状态 |
| `exmemory_reflect` | 启动关系反思（异步，返回 task_id） |
| `exmemory_status` | 记忆状态快照，只读不调模型 |
| `exmemory_proactive_status` | 主动消息配置、待送队列、当前阻断原因（只读） |
| `exmemory_proactive_config` | 开关主动消息、改每日上限与免打扰时段 |
| `exmemory_proactive_tick` | 判定此刻是否该发；该发则生成并排队 |
| `exmemory_proactive_prepare_delivery` | 每条发送前校验待送消息归属、当前开关、健康保护、免打扰与输出审核 |
| `exmemory_proactive_ack` | 把已送达的消息标记完成 |

## 纠正、跨端会话与回答参考

`exmemory_chat` 支持 `conversation_id`。显式传入时，历史从服务器读取，客户端的
`history` 不参与生成；返回 `conversation_id`、已保存的 `message_id` 与 `sources`。
不传时保留旧客户端行为，旧记录归入默认 `web` 会话。

网页可以选择某个 IM 会话继续；AstrBot 私聊通过 `/exsessions` 列出会话，再用
`/exsession {ID}` 明确选择。群聊保持独立会话，不能列出或切入其他会话。
新会话有独立的持久历史和长期摘要；同一会话同时收到两端请求时，后来的请求
会收到忙碌提示。已有原始人物材料和显式纠正仍属于镜像共享资料。

纠正使用 `exmemory_correct(slug, content, conversation_id, message_id, source_ids)`。
引用只能来自这个账号、镜像、会话中已保存的回答。网页提供“纠正”和“据此纠正回答”，
AstrBot 提供 `/excorrect {内容}`。保存后 Web/MCP 各自的缓存按共享记忆版本刷新，
下一次新请求采用纠正；已经开始的生成不撤回。

累计达到阈值后，会话在后台整理长期摘要。原记录与确定批次先保存，摘要成功后
才推进游标；失败保留现场，下次对话可重试，不跳过失败批次。摘要需要调用用户
配置的模型并消耗该提供方的额度。新会话摘要不会混入其他会话的人格或向量资料。

`sources` 是本次实际提供给模型的有界参考快照，包括检索片段和所选会话摘要。
`kind` 区分 `imported_chat`、`narrative`、`generated_summary`、`conversation_history`
与 `unknown`；不能把生成摘要说成原始人物原话，也不能把参考列表称为逐句事实证明。
来源保存在回答记录里，刷新或换端后仍可展开。只有完整生成、审核通过并成功保存
的回答才有可用于纠正的消息 ID；中断的流不进入新会话历史。

## 主动消息

这一组工具让记忆体能在 IM 里主动找用户，而不是永远等对方先开口。
生成与投递分开，是因为**生成要调大模型，投递失败不应重复生成**：
推送失败时客户端只需重试投递，不必重新生成、白烧一次额度。

```
proactive_tick → 队列(pending) → prepare_delivery → 客户端发送 → proactive_ack
                     ↑                                        │
                     └──────── 未 ack 则下轮重试 ──────────────┘
```

MCP 的 `proactive_tick` 在生成前预扣，输出审核通过并排队后按实际 usage 结算
（`kind=proactive`）；额度不足不调用模型，空输出、审核拒绝或失败释放预扣。
`prepare_delivery(slug, message_id)` 不生成、不收取对话费用，但会执行输出审核，
可能记录审核事件。每条发送前都必须调用它，只发送 `deliverable=true` 返回的
`content`；旧队列和重试同样遵守当前免打扰、开关与健康保护。`status` 保持纯查询，
新增 `delivery_blocked_reason` 供界面解释当前不能投递的原因。

明确审核拒绝或内容为空的旧消息转为 `dismissed`，保留记录但退出待送队列，
不会冒充已送达，也不会永久阻塞之后的安全消息。临时限制或审核异常仍保留待重试。

发送与 ack 是两个操作：若平台发送成功后进程退出或 ack 失败，重试可能重复发送。
当前不承诺严格一次送达，平台接口成功也不代表用户已读。

判定规则全部在 `core/proactive.py`，MCP 层不重算：

| 约束 | 行为 |
|------|------|
| 默认关闭 | `enabled=false`，必须由用户明确开启 |
| 免打扰 | `quiet_start`–`quiet_end`（可跨午夜），期间不生成 |
| 每日上限 | `max_per_day`（0–10），按本地自然日统计 |
| 健康保护 | 冷静期 / 达当日时长上限 / 近期危机事件，**一条都不推** |
| 触发时机 | 沉默超 48 小时优先；否则 8–10 点、21–23 点 |

健康保护的优先级高于一切主动性。这个产品的用户群本就有沉溺风险，
主动召回是加剧沉溺最直接的手段，所以 `blocked_reason` 非空时客户端
**不得**绕过它自行发消息。

`proactive_tick` 返回 `sent=false` 时带 `reason`，客户端应把原因如实转述，
不要自行编造一条消息顶上。

`proactive_ack` 必须先于下一次轮询：未确认的消息会一直留在队列里被重复投递。

调用节奏参考既有 cron 的建议（**每小时一次**）。判定的日上限能兜住过量生成，
但 `tick` 本身有模型调用成本。

## 返回契约

所有工具成功时返回 JSON 对象，出错时抛带 `[code]` 前缀的异常消息
（如 `[not_found] 镜像 [xxx] 不存在`）。

`exmemory_chat` 是唯一需要区分「成功」与「被拦截」的工具：

```json
{"reply": "我挺好的，你呢", "slug": "awa", "stickers": [], "blocked": false}
```

```json
{"reply": "", "blocked": true, "notice": {"type": "crisis", "message": "..."}}
```

`blocked` 在成功与拦截时都显式给出，客户端只需判断 `blocked` 是否为真，
不必靠「这个键在不在」去猜。被拦截时 `reply` 一定是空串——
**此时不要自行编造回复**，那会让用户以为记忆体说了它没说过的话。

## 安全约束

MCP 是**另一个入口，不是后门**。`/api/chat` 经过的四道闸门这里一道不少：

```
危机检测 → 强度保护 → 输入审核 → [构建引擎 → 调模型] → 输出审核 → 落库 → 结算
```

三条不可动摇的顺序规则：

1. **危机检测排在内容审核之前**。一条同时命中两者的求救信息必须走危机流程，
   不能因为被判「违规」就把人挡回去。
2. **强度保护排在危机之后**。已经达到每日时长上限的人，仍然要拿得到危机响应。
3. **配额预扣排在审核之后**。被闸门拦下的请求没有调模型，不该扣用户额度。

这三条由 `tests/test_mcp_server.py::TestSafetyGatesNotBypassed` 钉住，
改动 `mcp_server/service.py` 时不要绕过这些测试。

## 架构

```
mcp_server/
  tokens.py    令牌签发/校验/吊销（mcp_tokens 表）
  service.py   业务逻辑，纯 Python，无传输层依赖
  server.py    FastMCP 传输层，只做身份解析与工具注册
```

业务规则一律放 `service.py`，`server.py` 不出现任何业务判断。
这样单测可以直接调用 service 层，不必起 MCP 传输层。

## AstrBot

AstrBot 侧的插件在 `integrations/astrbot-plugin/`，它是个空壳——
通过 MCP 协议调用本服务，自身不含任何记忆逻辑与计费逻辑。

安装与配置见 `integrations/astrbot-plugin/README.md`，
发布到插件市场见 `integrations/astrbot-plugin/PUBLISHING.md`。

支持 QQ、Telegram、飞书、企微、微信公众号、Discord 等官方适配器。
**不支持、也不应支持微信个人号**——没有官方机器人接口，
现有方案均为逆向协议，封号风险与隐私风险都由用户承担。

## 依赖版本

`mcp` 锁在 `<2`：2.x 把 FastMCP 改名为 MCPServer 并调整了大量 API，
属于破坏性升级。升级前需同步改 `mcp_server/server.py` 的导入与调用方式。

同时 `requirements.txt` 的 uvicorn 下限被抬到 `>=0.31.1`——
MCP SDK 的硬要求，原 `~=0.24.0` 与它不可同时满足。
已在 uvicorn 0.54.0 上跑通全量测试。
