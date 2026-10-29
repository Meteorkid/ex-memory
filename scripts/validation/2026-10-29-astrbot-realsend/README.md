# 真机投递验证

这一轮要回答的问题很具体：**插件装上 AstrBot、挂到 Telegram / 飞书之后，
主动消息能不能真的出现在聊天窗口里。**

## 本地已验证 / 未验证

`check_send_path.py` 在真实 AstrBot 4.29.3 的类上跑通了一段链路，
它证明的是「代码调用得对」：

- umo 拆解、平台实例匹配、`#话题` 与飞书 `open_id`/`chat_id` 的选择
- 找不到平台时返回 False、非法 umo 抛 ValueError
- 平台侧抛异常会穿透出来，插件不会误 ack
- 绑定能落盘，重启后仍然生效

它证明不了的是：平台服务端认不认这个机器人。那要真凭证。
被替换掉的只有 SDK 的最终网络调用，其余都是真类。

## 在真机上按这个顺序做

### 0. 装好 AstrBot 和插件

插件目录整体拷进 AstrBot 的 `data/plugins/`，保证目录里直接是
`main.py` / `metadata.yaml` / `_conf_schema.json`（不要再套一层文件夹）。

在 WebUI 的插件页填三个必填项：

| 配置项 | 填什么 |
| --- | --- |
| `exmemory_dir` | ex-memory 项目绝对路径，含 `mcp_server/` |
| `exmemory_python` | 指向 ex-memory 自己的虚拟环境解释器，别用系统 python |
| `exmemory_token` | 下一步签发的令牌 |

### 1. 签发 MCP 令牌

```bash
cd <ex-memory>
.venv/bin/python run.py "/mcp-token issue --label astrbot"
```

令牌只显示一次，复制到上面 `exmemory_token`。之后可用
`/mcp-token list` 查看，`/mcp-token revoke --prefix <前缀>` 吊销。

### 2. 接平台

**Telegram**：找 @BotFather 拿 token，填进 AstrBot 的 Telegram 适配器配置。
本机若直连 `api.telegram.org` 不通，需要给 AstrBot 进程配代理，
否则表现为适配器起不来、日志里一直 polling 失败。

**飞书**：自建应用 → 开长连接（socket）模式 → 加 `im:message` 收发权限 →
发布版本并把应用可用范围放到自己 → 把 App ID / App Secret 填进 Lark 适配器。
飞书私聊的投递靠 open_id，群聊靠 chat_id，两者都由适配器从消息里取，
插件不参与判断。

### 3. 绑定会话并打开主动消息

在**目标聊天窗口里**依次发：

```
/exlist            # 看有哪些镜像
/exbind <镜像名>    # 把当前会话绑上去
/exproactive       # 看状态：应该是「已关闭」、待发送 0 条
/exproactive on    # 打开
```

绑定会把当前会话的 umo 一起记下来——主动消息靠它定位「发到哪」，
所以必须在最终要收消息的那个会话里执行，不要在别的窗口绑。

### 4. 造一条待发送，别干等

`proactive_tick` 只在「沉默够久 + 没超日上限 + 不在免打扰 + 健康保护没触发」
时才会生成内容。刚装好的环境很可能一小时都生成不出来，观测会变成
「什么都没发生」，白等。

用自检消息把「排队 → 取走 → 投递 → ack」这一段单独跑通（不经过大模型）：

```bash
cd <ex-memory>
EXMEMORY_MCP_TOKEN=<插件里填的那个令牌> \
  .venv/bin/python scripts/validation/2026-10-29-astrbot-realsend/seed_pending.py \
  --slug <镜像名> --text "投递自检：看到这句说明链路通了"
```

自检期间把插件的 `proactive_interval_minutes` 临时改成 `5`，
否则默认 60 分钟才轮到第一轮。测完改回 60——间隔调小只会让判定更频繁，
日上限不变，但生成调用有成本。想反复确认队列状态可以用 `--list`。

### 5. 盯日志

```bash
python scripts/validation/2026-10-29-astrbot-realsend/watch_proactive.py \
  --log <AstrBot 数据目录>/logs/astrbot.log --minutes 60
```

它会边跑边把每条投递结果摘出来打上类别，结束后给结论。
日志路径改过 `log_file_path` 的话按实际路径传。

## 通过标准

一次成功的真机验证同时满足：

1. 聊天窗口里真的出现了自检消息的原文；
2. 日志里对应一条 `已投递 N 条主动消息 slug=<镜像>`；
3. 再跑 `seed_pending.py --list`，待送队列变空（说明 ack 生效，不会重复发）；
4. 重启 AstrBot 后 `/exproactive` 仍然显示「已开启」、绑定还在。

第 4 条不是多余的：本轮修掉的两个问题都恰好表现为「当时看着成功、重启后消失」。

## 这一轮修掉的两个问题

两个都是「桩测试永远发现不了、装进真 AstrBot 必然踩到」的类型。

**一、绑定根本没落盘。** 插件调的是 `self.context.save_config()`，
而 AstrBot 的 `Context` 上并没有这个方法——`save_config` 属于插件配置对象
`AstrBotConfig`。调用抛 `AttributeError`，被 `except Exception` 吞成一条 warning，
所以 `/exbind` 当场回「已绑定」，重启后绑定全没了。已改成走配置对象自己的
`save_config()`，拿不到时明确告警而不是假装成功。

**二、落盘了也会被裁掉。** `_conf_schema.json` 里 `bindings` 声明成
`"type": "object"`。`AstrBotConfig` 加载配置时会拿 schema 对账，
`object` 被当成固定结构（子键只能来自 `items`），于是插件写进去的会话键
被当作「多余配置」逐条删除，日志里是 `Config key removed: bindings.<会话id>`。
改成 `"type": "dict"`——AstrBot 只对这个类型保留用户自定义键。
这个坑更隐蔽：第一处修完、写入成功，重启照样丢。

## 已知限制

- **ack 判据是「平台实例存在」，不是「用户真的收到了」。** AstrBot 的
  `Context.send_message` 返回 True 只代表找到了对应平台适配器，
  `send_by_session` 本身没有回执。好在 Telegram 与飞书在投递失败时都会抛异常，
  插件据此不 ack、留待重试；但如果某个第三方适配器的 `send_by_session`
  是空实现，就会出现「ack 了但没发出去」。真机上如果发现消息丢了却显示已投递，
  先查这一条。
- **绑定以 `session_id` 为键，没有带平台 id。** 同一个 AstrBot 里挂了两个
  同类适配器（比如两个 Telegram 机器人），恰好又有相同的 chat id 时，
  两边会共用一个绑定。 telegram 是数字 chat id、飞书是 `ou_`/`oc_` 开头，
  跨平台不会撞；只有同类型多实例才需要留意。
- **群聊默认关闭。** 群里任何人都能触发，且所有人的消息都会进记忆上下文，
  要显式打开 `allow_group`。

## 离线跑 check_send_path.py

它需要一个装了 AstrBot 的解释器，别装进 ex-memory 的 `.venv`（两者依赖会打架）：

```bash
/opt/homebrew/bin/python3.12 -m venv ~/.venvs/astrbot312
~/.venvs/astrbot312/bin/pip install astrbot==4.29.3

cd <ex-memory>
~/.venvs/astrbot312/bin/python \
  scripts/validation/2026-10-29-astrbot-realsend/check_send_path.py
```

共 15 项，退出码非 0 表示有失败项。改动插件或 `_conf_schema.json` 后，
`pytest tests/test_astrbot_plugin.py` 和这个脚本都要跑——前者快、覆盖分支，
后者慢、但只有它能证明「调用的 AstrBot 接口真的长这样」。
