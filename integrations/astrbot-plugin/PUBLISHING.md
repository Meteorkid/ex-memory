# 发布到 AstrBot 插件市场

## 计费边界（最重要）

**插件本体永远免费，且不包含任何付费逻辑。**

这不是姿态，是结构性约束：插件跑在用户自己的 AstrBot 进程里，
任何写在插件里的计费判断都能被改一行代码绕过。
能真正收上钱的地方只有服务端——ex-memory 侧的 `billing_gate` / `reserve` / `settle`
在每次对话前预扣额度，MCP 这条路径走的是同一套代码，不因为入口不同而放宽。

因此三方关系是：

| 角色 | 负责 |
|------|------|
| 插件 | 只做消息转发与会话绑定，零业务、零计费 |
| MCP 令牌 | 授权载体。吊销后已有连接的下一次工具调用也会被拒绝，不撤回已开始的调用 |
| ex-memory 服务端 | 配额、计费、安全审核、人格与检索 |

**额度耗尽时的表现**：`reserve()` 抛 `QuotaBlocked`，MCP 返回 `blocked: true`，
插件把提示原样转达给用户。插件不需要、也不应该自己判断余额。

## 发布流程

1. 生成独立仓库内容（插件源码在本仓库的 `integrations/` 下，
   但市场要求它自己是独立 GitHub 仓库）：

   ```bash
   python scripts/package_astrbot_plugin.py
   ```

2. 在 GitHub 新建仓库 `astrbot_plugin_exmemory`（全小写、无空格），
   把 `dist/astrbot_plugin_exmemory/` 的内容推上去。

3. 到 <https://cloud.astrbot.app/publish> 提交发布，需要 AstrBot Cloud 账号。

## 发布前检查

- [ ] `metadata.yaml` 的 `name` / `version` / `repo` 已改成你自己的
- [ ] `repo` 指向真实仓库，否则用户在市场里无法更新插件
- [ ] 已放一张 256x256 的 `logo.png`（可选但强烈建议）
- [ ] 压缩包小于 16MB——超过会被 CI 直接拒绝
- [ ] 仓库内不含 `__pycache__`、`.git`、`.venv`
- [ ] `requirements.txt` 只列真正需要的依赖（目前只有 `mcp`）

## 平台合规

AstrBot 官方适配器支持 QQ、Telegram、飞书、企微、微信公众号、Discord 等。
**不要接微信个人号**。微信个人号没有官方机器人接口，市面上的方案都是逆向协议，
封号风险由用户承担，且把最私密的聊天记录交给逆向客户端并不合适。

`metadata.yaml` 的 `support_platforms` 只列官方维护的适配器，
避免出现「声明支持但实际靠第三方客户端」的误导。

## 版本约束

`astrbot_version: ">=4.16,<5"`。AstrBot 目前迭代很快（4.29.x），
把上限锁在 5 是防止将来大版本改了插件 API 却仍被市场推荐给用户。
升级大版本时先跑一遍 `tests/test_astrbot_plugin.py` 并核对官方迁移文档。
