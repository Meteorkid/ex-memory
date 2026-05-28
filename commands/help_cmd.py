"""/help — 显示帮助。"""
from commands import register


def cmd_help(_=""):
    print("""
[可用指令]
  /create          创建新的记忆镜像
  /{名称}          进入已有镜像的对话模式
  /list            列出所有镜像
  /update {名称}   向已有镜像追加新素材
  /reflect {名称}  关系反思分析
  /backup {名称}   备份镜像版本
  /rollback {名称} {版本}  回滚到指定版本
  /let-go {名称}   删除镜像（不可逆）
  /export {名称} [html|md|json|txt]  导出 ex-memory 对话记录
  /export-wechat {备份目录} {输出目录} {账号} [会话名...]  调用外部 WechatExporter
  /keychain        管理 API Key（macOS Keychain）
  /web             启动 Web 界面（Gradio）
  /help            显示帮助
  /exit            退出
""")


register("help", cmd_help)
