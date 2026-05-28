# 本机微信导出向导执行文档

## 目标

在现有 Web 应用内提供“微信聊天导出”入口，让本机用户通过网页选择本机 iTunes/iOS 备份、填写微信账号和会话名，并调用独立的 WechatExporter 二进制导出 HTML/TXT/PDF 等文件。

该能力只作为本机工具开放，不作为远程 Web 服务能力开放。

## 安全边界

- 必须设置 `LOCAL_WECHAT_EXPORT_ENABLED=true` 后才启用。
- 请求来源必须是本机：`127.0.0.1`、`::1`、`localhost` 或测试客户端。
- 备份目录只从固定根目录扫描，默认：
  - `~/Library/Application Support/MobileSync/Backup/`
- 输出目录固定在项目数据目录下，默认：
  - `data/wechat_exports/{task_id}/`
- 用户不能在网页中填写任意输入目录或输出目录。
- `WECHAT_EXPORTER_BIN` 必须指向已存在且可执行的 WechatExporter 二进制。
- Web API 不暴露任意本机文件读取；只允许下载指定任务输出目录内的普通文件。

## 后端设计

新增核心模块 `core/wechat_export/`：

- `backups.py`：扫描固定 iTunes/iOS 备份目录，读取基础设备信息。
- `security.py`：判断本机请求、本机模式开关、路径边界。
- `tasks.py`：创建导出任务、后台执行、持久化状态、列出输出文件、解析下载路径。

新增 API：

- `GET /api/wechat-export/status`
  - 返回本机导出是否启用、是否本机请求、二进制是否配置、备份根目录。
- `GET /api/wechat-export/backups`
  - 返回可选备份列表。
- `POST /api/wechat-export/tasks`
  - 创建并后台执行导出任务。
- `GET /api/wechat-export/tasks/{task_id}`
  - 查询任务状态和输出文件。
- `GET /api/wechat-export/tasks/{task_id}/files/{path}`
  - 下载任务输出目录内的文件。

任务状态：

- `pending`
- `running`
- `success`
- `failed`

## 前端设计

在“发现”页增加“微信聊天导出”入口。

页面包含：

- 环境状态：是否启用、本机请求、二进制配置、备份根目录。
- 备份列表：从后端扫描结果中选择。
- 导出表单：微信账号、会话名、加载模式、是否过滤。
- 任务状态：执行中、成功、失败。
- 输出文件列表：点击下载。

## 验收标准

- 未启用 `LOCAL_WECHAT_EXPORT_ENABLED` 时，页面明确显示未启用，不能创建任务。
- 非本机请求不能扫描备份或创建任务。
- 未配置或不可执行 `WECHAT_EXPORTER_BIN` 时不能创建任务，并给出提示。
- 备份目录只来自固定根目录扫描结果。
- 输出文件只能从 `data/wechat_exports/{task_id}/` 下载。
- 前端无需终端命令即可完成：查看状态、选择备份、创建任务、下载结果。
- 测试覆盖配置缺失、路径边界、任务执行成功/失败和下载路径越界。
