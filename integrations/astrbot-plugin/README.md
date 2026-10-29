# AstrBot 插件：ExMemory 记忆体

在 QQ、Telegram、飞书、Discord 等聊天软件里，直接与 ex-memory 的记忆镜像对话。

## 它做什么

这是个**空壳插件**，不含任何记忆逻辑。它只负责：

1. 把当前会话绑定到一个 ex-memory 镜像；
2. 把消息转交给 ex-memory 的 MCP Server；
3. 把回复发回聊天窗口。

检索、人格、安全审核、计费全部由 ex-memory 侧完成。
一旦在插件里重写一遍，两边的安全闸门就会开始漂移——
「入口绕过了审核」是最难发现、后果最严重的一类问题。

## 前置条件

1. 安装 ex-memory 及其 MCP 依赖：
   ```bash
   cd /你的路径/ex-memory
   pip install -r requirements-mcp.txt
   ```
2. 签发一枚 MCP 令牌：
   ```bash
   python run.py "/mcp-token issue --label astrbot"
   ```

## 安装

把本目录放到 AstrBot 的 `data/plugins/` 下（建议作为独立仓库
`astrbot_plugin_exmemory` 克隆进去，便于后续更新）：

```bash
cd AstrBot/data/plugins
git clone https://github.com/Meteorkid/astrbot-plugin-exmemory astrbot_plugin_exmemory
```

## 配置

在 AstrBot WebUI 的插件页填写：

| 配置项 | 说明 |
|--------|------|
| `exmemory_dir` | ex-memory 项目路径 |
| `exmemory_python` | ex-memory 的虚拟环境 Python，如 `/path/.venv/bin/python` |
| `exmemory_token` | MCP 令牌 |
| `allowed_senders` | 平台实例 ID → 可信发送者 ID 列表；默认空，拒绝所有发送者 |
| `default_slug` | 默认镜像，留空则必须先绑定 |
| `allow_group` | 是否允许群聊使用，默认关闭 |
| `proactive_enabled` | 是否启用主动消息轮询，默认开启 |
| `proactive_interval_minutes` | 轮询间隔（分钟），默认 60 |

建议把 `exmemory_python` 直接指向 ex-memory 的虚拟环境，
避免插件依赖与宿主环境的 Python 包冲突。

这是一个账号的个人入口。白名单内的发送者可以使用该令牌账号有权访问的镜像，
不提供不同发送者绑定不同 Ex-Memory 账号的功能。请在插件设置中显式填写：

```json
{"tg1": ["12345"], "lark1": ["ou_example"]}
```

键是 AstrBot 机器人的**实例 ID**，不是平台类型名称；值是该平台的发送者 ID，
不是昵称。白名单控制所有命令、普通消息和主动投递。缺少身份、空白名单或
撤销授权后均拒绝访问。群开关默认关闭，群命令同样受限；开启群聊意味着群内
其他成员也能看见回复，即使他们没有触发机器人的权限。

绑定以完整 `unified_msg_origin` 隔离平台与会话，并记录授权发送者。旧绑定缺少
发送者时不会主动投递；只有能精确匹配完整会话且发送者已获授权的旧记录才可升级，
仅有裸会话 ID 的旧字符串绑定请重新 `/exbind`。

## 指令

| 指令 | 作用 |
|------|------|
| `/exlist` | 列出可用的记忆镜像 |
| `/exbind {镜像名}` | 把当前会话绑定到某个镜像 |
| `/exunbind` | 解除当前会话的绑定 |
| `/exsessions` | 在私聊中列出镜像的已保存会话 |
| `/exsession {会话ID}` | 在私聊中明确选择一个已保存会话继续 |
| `/excorrect {纠正内容}` | 明确纠正当前会话最新的已保存回答，限500字 |
| `/exsources` | 查看最新已保存回答的检索参考 |
| `/exproactive` | 查看主动消息状态（开关、免打扰、待送条数） |
| `/exproactive on` / `off` | 开启或关闭 ta 主动发消息 |

绑定后直接发消息即可，不必再加前缀。

每个 IM 入口默认按完整会话与发送者生成稳定的持久会话 ID，服务端再按账号和镜像隔离。
重启后仍使用同一个 ID，从服务器继续已保存的历史；插件内存中的有界历史仅用于旧接口兼容，
新接口以服务器历史为准。插件不会把聊天原文写入配置文件。

网页可以选择这条 IM 会话继续，私聊也可先 `/exsessions`、再 `/exsession 会话ID`
明确接着某条网页会话聊。选择结果随绑定保存，解绑或重新 `/exbind` 后回到该入口的默认会话。
群聊不能列出或切入其他会话，始终按群来源与发送者保持独立。

`/excorrect` 会读取当前会话最新的已保存回答，再提交用户明确给出的纠正。服务端保存成功后，
后续回答读取新的记忆版本；尚未落库的回答不能被引用纠正。`/exsources` 读取随回答保存的参考快照，
区分原始聊天、口述材料、生成摘要和历史人机对话；这些是模型当时使用的参考，不能当成逐句事实证明。

## 主动消息

ta 可以在聊天软件里主动找你，而不是永远等你先开口。

**默认关闭**，需要 `/exproactive on` 开启。开关与限制都存在服务端，
插件只负责投递——判定交给 `core/proactive.py`，避免两套规则漂移。

服务端有四道限制，任何一条命中都不会推送：

- 默认关闭，必须显式开启
- 免打扰时段（默认 23:00–08:00）
- 每日上限（`max_per_day`，默认 2）
- 健康保护：冷静期、达当日时长上限、近期危机事件

健康保护优先于一切主动性，插件侧不会绕过它。

每条消息发送前调用服务端 `proactive_prepare_delivery`，重新检查当前限制并审核
待送内容。因此白天排队、夜间重试的消息也受免打扰约束。发送失败不 ack；
发送成功后 ack 失败仍可能造成重试重复，不承诺严格一次投递。

轮询间隔默认 60 分钟，与 ex-memory 的 `/proactive-tick` cron 建议一致。
调小只会让判定更频繁（日上限不变），但生成要调大模型，有成本。

**发到哪里**：绑定会把当前会话的 `unified_msg_origin` 一起记下来，
主动消息靠它定位收件人。所以要**在最终想收到消息的那个聊天窗口里**
执行 `/exbind`，在别的窗口绑会导致主动消息发到那个窗口去。

**平台限制**：主动消息走 AstrBot 的 `send_message`，需要平台适配器实现
`send_by_session`。AstrBot 自带的 18 个适配器都实现了，其中 Telegram 与飞书
已在真实 AstrBot 上验证过投递路径。注意 `send_message` 返回 True 只代表
「找到了对应平台的适配器实例」，不代表对方真的收到了——Telegram、飞书在
投递失败时会抛异常，插件据此不 ack、留待下一轮重试；若某个第三方适配器的
`send_by_session` 是空实现，会出现「显示已投递但没发出去」。

## 验收

改完插件或配置后，两件事都要做：

```bash
# 快：分支与决策逻辑
pytest tests/test_astrbot_plugin.py

# 慢但关键：在真实 AstrBot 的类上验证投递链路
~/.venvs/astrbot312/bin/python \
  scripts/validation/2026-10-29-astrbot-realsend/check_send_path.py
```

真机验收（真凭证、真聊天窗口）的完整步骤见
`scripts/validation/2026-10-29-astrbot-realsend/README.md`。

## 群聊

群聊默认关闭。开启后仅白名单内的发送者可以触发记忆体回复，各发送者使用独立持久会话。
同群其他成员仍能看见回复，因此应只在适合公开该镜像内容的群中开启。

## 隐私

插件到本机 ex-memory 进程这一段经 MCP stdio 通信。聊天平台与用户选定的远程
模型提供方仍可能处理消息；不要把 stdio 的本机通信边界理解为全链路不出本机。
