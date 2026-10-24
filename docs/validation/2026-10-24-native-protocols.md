# 原生模型协议适配（2026-10-24）

## 范围与配置

在现有账号模型配置上新增 Anthropic Messages 和 Gemini GenerateContent；Gemini 同时提供原生
Embedding。固定服务商增加 `anthropic`、`gemini`；自定义服务增加 `custom_anthropic`、
`custom_gemini`，旧 `custom` 继续表示 OpenAI 兼容协议。

协议由保存的服务商值推导，不新增数据库列、不改写旧行或密文。服务商值已参与 Key 的 AES-GCM
认证数据，因此切换协议必须重新填 Key 或明确删除；已有 Embedding 索引不能改协议/地址/模型。

## 调用契约

- 上层仍使用 `chat.completions.create` 的现有最小返回结构；适配器转换鉴权头、system、对话历史、
  文本返回、SSE 和 token 用量，无需给每条画像/记忆/对话流程重复写分支。
- Anthropic 基础地址追加 `/messages`，Key 放 `x-api-key`，显式固定 `anthropic-version: 2023-06-01`；
  使用绑定 workspace 的 Key。system 独立传入，max_tokens 缺省4096，不转发旧采样参数。
- Gemini 基础地址追加 `/models/{model}:generateContent` 或 `:streamGenerateContent?alt=sse`；
  Key 仅放 `x-goog-api-key`，不进入 URL。Gemini 模型 ID 独立校验，不能拼接任意方法路径。
- Gemini 批量向量使用 `batchEmbedContents`，每条文本对应一个请求。固定1024维、有限数值、非零并
  归一化；与现有 pgvector 的1024维列一致。输入既可能是资料也可能是问题，不猜测 taskType。
- 只支持现有产品需要的文本对话与文本向量。工具调用、图像输入与独立工作区 header 不在本轮范围。

## 失败和安全边界

所有原生请求共用既有公网HTTPS、DNS检查、固定IP连接、TLS证书检查和禁止跳转的输送层。
HTTP错误及流中错误不回显服务商正文、Key或聊天内容。原生流拒绝、输出截断、非正常结束、空可见内容
使用 `IncompleteModelResponse`；路由丢弃半截 assistant 与贴纸、释放预扣、不结算或触发归档。
已下发给浏览器的内容以错误提示结束。既有一般中断/客户端取消的历史行为保持其回归约束。

## 验证策略

配置测试覆盖新服务商、协议切换、Key隔离与索引锁；适配器测试执行真实HTTP请求构造与SSE解析，
验证鉴权、版本、正文转换、分片、结束标记、usage、拒绝和错误响应；HTTP验收只替换最终供应商出口，
保留账号配置、适配器、导入、Chroma和对话流程。不得将合成响应验收描述为真实供应商推理验收。

发布前验证：隔离后端回归1075通过、29跳过、1排除；跳过依赖额外PG/Redis环境，排除项为既有独立
研究的holdout结构检查，相关代码本轮未动。前端Node29通过。新增协议HTTP验收119项通过；两条链路为
自定义Anthropic对话＋自定义Gemini向量、Gemini对话＋Gemini向量。旧OpenAI流程87项复测通过。
独立复核验证16MiB流总量与1MiB行/事件限制、错误事件冲突、晚到安全拦截及取消后的连接释放。
浏览器桌面与375px手机确认选择协议后地址框/提示正确，保存后Key仅返回尾号，刷新保留配置且无横向溢出。

## 官方依据

- [Anthropic Messages](https://platform.claude.com/docs/en/api/messages/create)
- [Anthropic streaming](https://platform.claude.com/docs/en/build-with-claude/streaming)
- [Anthropic authentication](https://platform.claude.com/docs/en/manage-claude/authentication#select-a-workspace)
- [Gemini API](https://ai.google.dev/api/)
- [Gemini embeddings](https://ai.google.dev/api/embeddings)
- [Gemini embedding dimensions](https://ai.google.dev/gemini-api/docs/embeddings#controlling-embedding-size)
