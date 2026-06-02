# ex-memory 项目配置

## 项目概述
ex-memory 是一个数字镜像系统，通过分析聊天记录生成 AI 对话体。技术栈：Python 3.10+ / FastAPI / Gradio / ChromaDB。

## 开发规范

### 语言
- 代码变量名、方法名、类名使用英文
- 代码注释使用中文
- commit 信息使用中文

### Python 规范
- Python 3.10+，使用类型提示
- 异常优先具体捕获，不静默吞异常
- 外部输入需校验
- 测试框架：pytest

### 前端规范
- CSS class 名称不可重命名（app.js 动态引用 94 个 class）
- 不修改 position 布局关键属性
- 保持暗色模式双机制（prefers-color-scheme + data-theme）

## 常用命令

```bash
# 开发
make dev                    # 启动开发服务器
make test                   # 运行测试
make lint                   # 代码检查

# Docker
make docker-build           # 构建镜像
make docker-up              # 启动容器

# 代码质量
ruff check .                # lint
ruff format --check .       # 格式检查
pytest tests/ -v            # 运行测试
```

## 项目结构

```
ex-memory/
├── core/                   # 核心模块
│   ├── engine.py           # 对话引擎
│   ├── session.py          # 会话管理
│   ├── emotion_tracker.py  # 情感分析
│   ├── emotional_memory.py # 情感记忆
│   ├── personalization.py  # 个性化
│   └── plugins/            # 插件系统
├── server/                 # Web 服务
│   ├── app.py              # FastAPI 应用
│   ├── routes.py           # API 路由
│   └── auth.py             # 认证
├── web/                    # 前端
│   ├── static/
│   │   ├── app.js          # 前端逻辑
│   │   ├── style.css       # 样式
│   │   └── index.html      # 入口
│   └── app.py              # Gradio 界面
├── prompts/                # Prompt 模板
├── parsers/                # 解析器
├── tests/                  # 测试
└── data/                   # 运行时数据
```

## 注意事项

- 修改 CSS 时检查大括号平衡
- 修改 JS 时运行 `node --check` 验证语法
- 新增 API 端点需添加认证（`Depends(require_auth)`）
- 测试文件放在 `tests/` 目录，以 `test_` 开头
