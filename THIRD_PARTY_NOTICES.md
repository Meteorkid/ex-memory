# Third Party Notices

`ex-memory` 本体使用 MIT License。以下第三方项目以独立组件方式接入或引用，
其授权条款不改变 `ex-memory` 本体源码的授权。

## WechatExporter

- Project: BlueMatthew/WechatExporter
- Repository: https://github.com/BlueMatthew/WechatExporter
- License: GNU General Public License v2.0 or later
- Local path: `third_party/WechatExporter`
- Integration: git submodule, used as an independently built external binary

`ex-memory` 不链接或内嵌 WechatExporter 的 C++/Objective-C++ 源码。运行时仅在用户
显式配置 `WECHAT_EXPORTER_BIN` 后，通过命令行调用用户本机编译或下载的 WechatExporter
二进制，用于从未加密 iTunes/iOS 备份导出微信聊天记录。
