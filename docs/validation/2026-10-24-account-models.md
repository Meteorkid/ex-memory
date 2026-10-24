# 账号模型设置验收（2026-10-24）

## 行为

- Web 对话和 Embedding 使用当前账号单独保存的服务商、模型与 API Key，不回退站主凭据。CLI 的无账号配置保持兼容。
- 对话预设 DeepSeek、SiliconFlow、OpenAI；Embedding 预设 SiliconFlow、OpenAI。两类均可设置自定义 OpenAI 兼容公网 HTTPS 地址。
- Key 使用独立主密钥 AES-GCM 加密，绑定账号、用途与地址；响应只含配置状态和尾号。更新使用 revision 条件写入，清理旧客户端缓存。
- 自定义请求逐次验证全部 DNS 地址，固定公网 IP，保留原域名证书校验，拒绝重定向与环境代理。
- 已有索引锁定 Embedding 服务、地址与模型，允许换 Key；旧索引无法证明模型一致时拒绝混用。
- 注销移除账号配置；导出不含 Key 明文或密文。删除 Key 不删除镜像，也不会撤回已发出的请求。

## 验证

- 隔离快照 Python 回归：991 passed、29 skipped、1 deselected。仅忽略独立研究文件 `test_error_analysis.py`，仅 deselect 既有离线 holdout 的 `test_structure`；其余6项 holdout通过。29项跳过需额外 PostgreSQL/Redis 环境，账号模型设置的5项真实 PostgreSQL 用例通过。
- Node 导入、会话隔离与模型表单回归：21 passed。
- HTTP 验收：87项通过，包括跨账号、配置轮换/删除、站主 sentinel 零调用、导入和流式对话。HTTP 出口使用合成响应，未调用真实付费模型。
- 独立输送层审查：真实本地 TLS 校验 SNI、正确/错误 SAN、固定 IP、拒绝跳转及环境代理，共5项通过。
- 浏览器完成自定义两套配置、保存、示例导入、首轮回复、Key 删除/重设、刷新持久化；375px无横向溢出。密码输入框不保留保存过的 Key。

复现 Python 回归：使用 `scripts/validation/2026-10-23-first-run/sandbox.py` 创建独立快照，在安装锁定依赖的解释器下运行上述测试；不读取开发者 `.env`、Keychain 或个人运行目录。

## 发布边界

SQLite 新增迁移011，PostgreSQL 新增迁移006；先在生产数据副本验证迁移和旧版本回退，再切换。
主密钥必须一次生成、持久保存，与数据库备份分开保护。生产 Compose 显式清空站主两项 API Key。
本记录是发布前验证；实际部署版本、备份位置与线上检查另记发布记录。真实用户自带模型的可用性和产品相对直接上传的优势仍需用户验证。
