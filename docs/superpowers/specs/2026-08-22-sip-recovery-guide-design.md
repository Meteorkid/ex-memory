# SIP 恢复模式操作引导设计

## 目标

在 macOS 本地微信导出专家流程中提供可以在关机前拍照保存的 SIP 操作说明，同时覆盖 Apple 芯片和 Intel Mac。引导必须分别出现在关闭 SIP 和重新开启 SIP 阶段，且不自动修改系统安全设置。

## 展示方式

- 采用始终可见的步骤卡，不使用弹窗或外部帮助页。
- 卡片顶部醒目提示：“接下来需要关机操作，请先用手机拍照保存本页步骤。”
- 桌面端并列展示 Apple 芯片与 Intel 两套步骤，窄屏改为上下排列。
- 命令使用高对比度代码块，并明确区分 `csrutil disable` 与 `csrutil enable`。
- 关闭与开启阶段使用相同布局，避免用户第二次进入恢复模式时重新理解页面。

## 终端示意图

- 在 `csrutil disable` 和 `csrutil enable` 命令框下方分别按芯片类型展示内嵌 SVG 终端示意图。
- 关闭阶段（`csrutil disable`）的 Apple 芯片示意图展示完整交互：执行命令、输入 `y` 确认、输入管理员用户名、在密码不回显的情况下完成认证、出现成功提示和新的提示符。
- 开启阶段（`csrutil enable`）的 Apple 芯片示意图展示命令与成功提示；如该 macOS 版本提示认证，则按屏幕提示输入用户名和密码，密码不回显。
- 示例用户名固定使用 `your_username`，并明确说明用户必须替换为自己的 Mac 管理员用户名；页面不得读取或展示真实用户名。
- 密码提示后不绘制圆点、星号或密码字符，以还原终端的无回显行为；图内和图下均注明“输入密码时屏幕不会显示任何字符，这是正常现象”。
- Intel Mac 示意图展示命令和常见成功提示，并说明不同机型与 macOS 版本可能额外要求确认或认证，应以恢复模式终端的实际提示为准。
- 关闭示意图显示常见成功提示：`Successfully disabled System Integrity Protection. Please restart the machine for the changes to take effect.`
- 开启示意图显示常见成功提示：`Successfully enabled System Integrity Protection. Please restart the machine for the changes to take effect.`
- 图下注明：“终端示意图；不同 macOS 版本的提示文字和窗口外观可能略有差异。”
- SVG 直接内嵌在离线页面中，不依赖网络、外部图片或额外安装包资源；提供中文 `aria-label` 供辅助技术识别。

## 关闭 SIP 步骤

### Apple 芯片

1. 保存工作并将 Mac 完全关机。
2. 长按电源键，直到出现启动选项。
3. 选择“选项”，点击“继续”；如有要求，选择管理员并输入登录密码。
4. 在屏幕顶部选择“实用工具 → 终端”。
5. 执行 `csrutil disable` 并按回车。
6. 出现确认提示后输入 `y` 并按回车。
7. 按提示输入 Mac 管理员用户名并按回车。
8. 输入该管理员的登录密码并按回车；终端不会显示输入的字符，这是正常现象。
9. 看到关闭成功提示后，从 Apple 菜单正常重启。

### Intel Mac

1. 保存工作并将 Mac 完全关机。
2. 按下电源键后立即长按 `⌘R`，直到出现 Apple 标志或旋转地球。
3. 如有要求，选择管理员并输入登录密码。
4. 在屏幕顶部选择“实用工具 → 终端”。
5. 执行 `csrutil disable` 并按回车。
6. 如终端要求确认或认证，按屏幕提示完成；输入密码时不会显示字符。
7. 看到关闭成功提示后，从 Apple 菜单正常重启。

回到正常系统后，用户先启动并登录微信，再点击“我已关闭 SIP，开始提取和解密”。助手必须检测到 SIP 已关闭，否则拒绝继续。

## 重新开启 SIP 步骤

Apple 芯片和 Intel Mac 使用与关闭阶段相同的恢复模式进入方式，在恢复模式终端执行 `csrutil enable`。Apple 芯片按终端提示输入 `y`、管理员用户名和登录密码；如果当前 macOS 没有显示其中某项提示，则直接继续。Intel Mac 如出现确认或认证提示，也按屏幕提示完成。输入密码时终端不会显示字符。看到开启成功提示后从 Apple 菜单正常重启。回到系统后点击“我已重新开启 SIP，继续导出”，助手必须检测到 SIP 已开启，才允许读取聊天和生成 HTML。

## 验证 SIP 状态（可选）

回到正常系统后，可在“终端”执行 `csrutil status` 核对：显示 `System Integrity Protection status: disabled` 表示已关闭，`enabled` 表示已开启。此步骤可选，助手在后续流程中也会自动检测 SIP 状态。

## 特殊情况处理

- Apple 芯片若关闭时提示 `Failed to create local policy`，先执行 `csrutil clear` 清空现有配置，然后从 Apple 菜单重启；再次进入恢复模式后重新执行 `csrutil disable`。
- 页面同时展示 `csrutil status` 核对命令与 `csrutil clear` 排错命令；助手不自动执行任何命令。

## 安全提示

- 关闭 SIP 会显著降低整台 Mac 的保护能力。
- SIP 关闭期间不要浏览网页、安装软件或运行不可信程序。
- 重新开启 SIP 不能撤销关闭期间已经发生的系统修改、持久化或数据泄露。
- 助手不自动执行 `csrutil`、不修改 NVRAM，也不代替用户输入管理员密码。

## 实现范围

- 修改 localhost 私有导出页的 SIP 阶段说明和相关暗色主题样式。
- 保持现有工作流状态、API、安全校验和按钮行为不变。
- 不增加联网依赖；所有步骤随 `.app` 离线提供。
- 同步更新 macOS Beta 文档中的双机型步骤。

## 验证

- 页面响应包含拍照提示、Apple 芯片步骤、Intel `⌘R` 步骤及关闭、开启命令。
- 页面响应包含关闭与开启两组分芯片终端示意图、两条已执行命令及对应的常见成功提示。
- Apple 芯片示意图和正文均包含 `y` 确认、示例管理员用户名和密码无回显提醒，且不包含真实用户名或密码字符。
- 页面正文包含 `csrutil status` 核对命令与 `Failed to create local policy` → `csrutil clear` 排错说明。
- 原有 SIP 状态机测试继续通过。
- 运行本地助手安全测试、Ruff 和差异检查。
- 重新构建本机测试版 `.app`，校验签名并确认助手健康检查通过。

## 依据

- Apple Support：<https://support.apple.com/en-ie/102518>
- Apple Developer：<https://developer.apple.com/documentation/security/disabling-and-enabling-system-integrity-protection>
