# macOS 本地微信导出助手：未签名开源 Beta

## 发布边界

本地助手只监听 `127.0.0.1:17653`。公共网站只能调用健康检查、唤起和无隐私任务状态；联系人、会话、聊天正文、数据库密钥、解密快照、媒体和输出路径只对 localhost 私有页面可见。

当前发布方式固定为：

- 不购买或使用 Apple Developer ID。
- 不做 Apple notarization（公证）。
- 构建阶段使用免费的 ad-hoc 完整性签名，避免 App Bundle 内部文件被无提示替换；这不会消除 Gatekeeper 警告。
- DMG 只上传到站点自己的阿里云 HTTPS 下载地址，不上传 GitHub Release。
- 网站同时公布版本、CPU 架构、SHA-256 和构建清单。

## 构建 arm64 安装包

构建机为 Apple Silicon macOS，打包解释器必须是 Python 3.10+。先安装打包工具和 SQLCipher，并为助手建立隔离环境，避免把项目开发依赖打进安装包：

```bash
brew install sqlcipher dylibbundler
python3.12 -m venv .venv-local-helper-package
.venv-local-helper-package/bin/pip install -r requirements-local-helper.txt -r requirements-packaging.txt
```

然后把正式网站所有实际入口的精确 Origin 写入安装包并构建。多个 Origin 用逗号分隔；
例如站点同时接受根域名与 `www` 时必须全部列出，否则用户从另一个入口登录后无法连接本机助手：

```bash
SITE_ORIGINS=https://你的域名,https://www.你的域名 \
RELEASE_VERSION=0.1.0-beta.4 \
PYTHON_BIN=.venv-local-helper-package/bin/python \
bash packaging/macos/build_helper.sh
```

产物位于 `dist/local-helper/{版本}/`，包括 DMG、`.sha256` 和 `build-manifest.json`。脚本拒绝 Python 3.9、Intel 构建机、缺少 LLDB/SQLCipher/PyInstaller 的环境，也拒绝覆盖已有版本目录。

## 上传阿里云并配置网站

将三个产物上传到只允许 HTTPS 下载的静态目录。服务端环境变量示例：

```dotenv
LOCAL_WECHAT_HELPER_ENABLED=true
LOCAL_WECHAT_HELPER_VERSION=0.1.0-beta.4
LOCAL_WECHAT_HELPER_MIN_API_VERSION=1
LOCAL_WECHAT_HELPER_ARM64_URL=https://你的域名/downloads/ex-memory-wechat-helper-0.1.0-beta.4-macos-arm64.dmg
LOCAL_WECHAT_HELPER_ARM64_SHA256=构建清单中的64位sha256
```

重启服务后，`GET /api/local-helper/config` 只会展示 HTTPS 且 SHA-256 合法的安装包。不要把私钥、云存储密钥或聊天数据放进这些变量。

## 用户安装与手动放行

1. 从网站下载 DMG，对照网页公布的 SHA-256。
2. 拖入“应用程序”。首次启动如果被 Gatekeeper 阻止，在 Finder 中按住 Control 点击 App，选择“打开”，再次确认。
3. 不要关闭系统的 Gatekeeper，也不要运行来源不明的解除隔离脚本。
4. 助手只在用户主动选择已验证的微信 4.1.12 专家模式后显示 SIP 流程。

## 账号与密钥

- 每个微信账号都有独立的数据库密码，不同账号不能复用密钥。
- 助手会根据微信主进程当前打开的数据库自动选择唯一活动账号；多账号同时活动时仍需手动选择。
- 检测到未验证的微信版本时会在关闭 SIP 前停止，不会继续密钥提取流程。
- 同一账号成功提取一次后，助手会结合各数据库的 salt，为该账号全部数据库派生并验证密钥。
- 首次导入、切换账号、助手重启、微信升级、数据库重建或密钥验证失败时必须重新提取。
- 一次任务只处理用户确认的当前登录账号；其他账号需要分别登录、分别提取和分别导入。
- 提取前必须确认所选账号就是微信当前登录账号。账号不匹配时助手会拒绝继续，不会尝试其他账号目录。

## SIP 专家流程

SIP 关闭会显著降低整台 Mac 的防护，只用于无法通过正常权限读取的微信 4.x 数据库。助手不会自动修改 SIP 或 NVRAM。

1. 开始任务时必须检测到 SIP 已开启。
2. 关机前先用手机拍照保存助手页面上的完整步骤。
3. 用户按机型自行进入恢复模式：
   - Apple 芯片：完全关机，长按电源键直到出现启动选项，选择“选项 → 继续”。
   - Intel：完全关机，按下电源键后立即长按 `⌘R`，直到出现 Apple 标志或旋转地球。
4. 如有要求，选择管理员并输入登录密码；从屏幕顶部选择“实用工具 → 终端”。
5. 运行 `csrutil disable` 并按回车；出现确认提示后输入 `y`，再按提示输入管理员用户名和登录密码（密码输入时终端不显示任何字符，这是正常现象）；看到成功提示后从 Apple 菜单正常重启。
6. 在 SIP 关闭期间不要浏览网页、安装软件或运行其他不可信程序。先完全退出微信并点击开始提取，再按页面提示启动微信并登录已确认的当前账号；看到“当前账号全库密钥已验证”后再次完全退出微信。
7. 助手创建并验证解密快照，随后停止在 `awaiting_sip_enabled` 阶段，不允许读取联系人或导出 HTML。
8. 用户再次拍照保存重新开启步骤，按相同机型方式进入恢复模式，在终端运行 `csrutil enable`；如提示认证则输入管理员用户名和密码（密码不显示字符），然后正常重启。
9. 助手确认 SIP 已开启后，才开放联系人选择和完整导出。

回到系统后可运行 `csrutil status` 核对：`disabled` 表示已关闭，`enabled` 表示已开启。若 Apple 芯片关闭时提示 `Failed to create local policy`，先执行 `csrutil clear`，从 Apple 菜单重启后再次进入恢复模式重试。

恢复模式进入方式参考 [Apple Support](https://support.apple.com/en-ie/102518)，SIP 命令参考 [Apple Developer](https://developer.apple.com/documentation/security/disabling-and-enabling-system-integrity-protection)。

数据库密码和派生密钥只存在于解密工作线程内存中，不写入状态文件、日志或命令行参数。任务结束、失败、取消或助手重启后不能复用，下次导入必须重新提取。重启后恢复的是无密钥的解密快照任务。

## 输出结构

每个会话输出一个离线 HTML、`export-manifest.json` 和以下分类目录：

```text
image/      video/      voice/      file/
emoji/      music/      avatar/     icon/
thumbnail/  location/   card/       raw/
```

未知消息保留类型编号和原始载荷到 `raw/`；媒体找不到或新 schema 未适配时，清单状态为 `partial`，不会伪装成完整成功。

## 卸载

导出完成或失败后，优先点击本地页面的“删除本地任务数据”，销毁当前任务状态和解密数据库快照；已生成到下载目录的 HTML 不受影响。退出助手后，可将 App 移到废纸篓。

如果页面已无法打开，才手动删除：

```text
~/Library/Application Support/ex-memory-helper/
```

已导出的文件默认在：

```text
~/Downloads/ex-memory-wechat-exports/
```
