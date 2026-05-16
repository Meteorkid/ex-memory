# 贡献指南

感谢你对 ex-memory 的兴趣！欢迎提交 Issue、PR 和功能建议。

## 项目理念

ex-memory 致力于帮助人们以尊重、私密的方式处理与前任/已故亲友/重要他人的记忆。请在贡献时牢记：
- **隐私第一**：所有数据本地存储，不上传云端
- **尊重边界**：这不是"stalker工具"，而是正视记忆的工具
- **保持简洁**：Vanilla JS SPA + Python CLI，零前端框架依赖

## 开发环境

```bash
# 1. 克隆仓库
git clone <repo-url>
cd ex-memory

# 2. 安装依赖
make install

# 3. 配置 API Key（推荐使用 Keychain）
cp .env.example .env
# 编辑 .env 填入 API Key，或使用：
python run.py
# 然后执行 /keychain set llm <your-key>

# 4. 运行测试
make test
```

## 代码风格

- Python：遵循 PEP 8，使用 `ruff` 检查
- JavaScript：保持 vanilla JS 风格，不引入框架
- CSS：使用 `:root` CSS 自定义属性，BEM 风格类名
- 提交信息：中文（除非项目转为国际化）

```bash
# 格式化检查
make lint
```

## PR 流程

1. Fork 仓库并创建分支 `feature/xxx` 或 `fix/xxx`
2. 编写代码和测试（核心逻辑需要测试）
3. 运行 `make test` 确保通过
4. 运行 `make lint` 确保无警告
5. 提交 PR，描述清楚改了什么、为什么改

## 项目结构

```
ex-memory/
├── core/          # 核心引擎、会话管理、验证、文件工具
├── server/        # FastAPI 路由、认证、中间件
├── memory/        # ChromaDB、Embedder、Chunker
├── pipeline/      # 自动化蒸馏管线
├── parsers/       # 聊天记录解析器（微信、QQ、口述）
├── commands/      # CLI 指令模块
├── prompts/       # LLM prompt 模板
├── tests/         # 测试
├── web/           # 前端（HTML/CSS/JS）
│   └── static/
├── migrations/    # 数据库迁移脚本
└── docs/          # 文档
```

## 测试

```bash
# 运行全部测试
pytest -v

# 运行特定测试
pytest tests/test_auth.py -v

# 覆盖率报告
pytest --cov --cov-report=html
open htmlcov/index.html
```
