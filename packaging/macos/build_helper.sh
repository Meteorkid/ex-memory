#!/bin/bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
SITE_ORIGIN="${SITE_ORIGIN:-}"
RELEASE_VERSION="${RELEASE_VERSION:-$(cd "$PROJECT_ROOT" && "$PYTHON_BIN" -c 'from local_helper import __version__; print(__version__)')}"
SQLCIPHER_BIN="${SQLCIPHER_BIN:-/opt/homebrew/opt/sqlcipher/bin/sqlcipher}"
OUTPUT_ROOT="${OUTPUT_ROOT:-$PROJECT_ROOT/dist/local-helper/$RELEASE_VERSION}"

HTTPS_ORIGIN_PATTERN='^https://[^/]+(:[0-9]+)?$'
LOCAL_ORIGIN_PATTERN='^http://(127\.0\.0\.1|localhost)(:[0-9]+)?$'
if [[ ! "$SITE_ORIGIN" =~ $HTTPS_ORIGIN_PATTERN ]] && \
   [[ "${LOCAL_TEST_BUILD:-0}" != "1" || ! "$SITE_ORIGIN" =~ $LOCAL_ORIGIN_PATTERN ]]; then
    echo "SITE_ORIGIN 必须是精确的 HTTPS Origin，例如 https://memory.example.com" >&2
    exit 2
fi
if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "该构建脚本只能在 macOS 上运行" >&2
    exit 2
fi
if [[ "$(uname -m)" != "arm64" ]]; then
    echo "当前发布只支持在 Apple Silicon（arm64）Mac 上构建" >&2
    exit 2
fi
if ! "$PYTHON_BIN" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
    echo "打包 Python 必须为 3.10 或更高版本：$PYTHON_BIN" >&2
    exit 2
fi
if ! "$PYTHON_BIN" -m PyInstaller --version >/dev/null 2>&1; then
    echo "指定 Python 缺少 PyInstaller：$PYTHON_BIN" >&2
    exit 2
fi
if ! /usr/bin/xcrun --find lldb >/dev/null 2>&1; then
    echo "未找到系统 LLDB，请先安装 Xcode Command Line Tools" >&2
    exit 2
fi
if [[ ! -x "$SQLCIPHER_BIN" ]]; then
    echo "未找到 SQLCipher：$SQLCIPHER_BIN" >&2
    exit 2
fi
if ! command -v dylibbundler >/dev/null 2>&1; then
    echo "缺少 dylibbundler，请先执行 brew install dylibbundler" >&2
    exit 2
fi
if [[ -e "$OUTPUT_ROOT" ]]; then
    echo "输出目录已存在，拒绝覆盖：$OUTPUT_ROOT" >&2
    exit 2
fi

BUILD_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/ex-memory-helper-build.XXXXXX")"
trap 'rm -rf "$BUILD_ROOT"' EXIT
mkdir -p "$BUILD_ROOT/runtime/local_helper/bin" "$BUILD_ROOT/runtime/local_helper/Frameworks" "$OUTPUT_ROOT"
printf '%s\n' "$SITE_ORIGIN" > "$BUILD_ROOT/release-origin.txt"

cp "$SQLCIPHER_BIN" "$BUILD_ROOT/runtime/local_helper/bin/sqlcipher"
dylibbundler \
    -od \
    -b \
    -x "$BUILD_ROOT/runtime/local_helper/bin/sqlcipher" \
    -d "$BUILD_ROOT/runtime/local_helper/Frameworks" \
    -p '@executable_path/../Frameworks/'

cd "$PROJECT_ROOT"
"$PYTHON_BIN" -m PyInstaller \
    --noconfirm \
    --clean \
    --windowed \
    --name "ex-memory 微信导出助手" \
    --osx-bundle-identifier "com.meteorkid.exmemory.wechat-helper" \
    --distpath "$BUILD_ROOT/dist" \
    --workpath "$BUILD_ROOT/work" \
    --specpath "$BUILD_ROOT/spec" \
    --add-data "$BUILD_ROOT/release-origin.txt:." \
    --add-binary "$PROJECT_ROOT/local_helper/wechat_macos/lldb_capture_launcher.sh:local_helper/wechat_macos" \
    --add-data "$PROJECT_ROOT/local_helper/wechat_macos/lldb_key_capture.py:local_helper/wechat_macos" \
    --add-binary "$BUILD_ROOT/runtime/local_helper/bin/sqlcipher:local_helper/bin" \
    --add-binary "$BUILD_ROOT/runtime/local_helper/Frameworks:local_helper/Frameworks" \
    --collect-submodules uvicorn \
    --collect-submodules zstandard \
    local_helper/main.py

APP="$BUILD_ROOT/dist/ex-memory 微信导出助手.app"
PLIST_VERSION="${RELEASE_VERSION%%-*}"
/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $PLIST_VERSION" "$APP/Contents/Info.plist"
# ad-hoc 签名不需要付费证书，也不等于 Apple Developer ID 签名或公证。
codesign --force --deep --sign - "$APP"
codesign --verify --deep --strict "$APP"

ARCH="$(uname -m)"
DMG_NAME="ex-memory-wechat-helper-${RELEASE_VERSION}-macos-${ARCH}.dmg"
hdiutil create \
    -volname "ex-memory 微信导出助手" \
    -srcfolder "$APP" \
    -format UDZO \
    -ov \
    "$OUTPUT_ROOT/$DMG_NAME"

shasum -a 256 "$OUTPUT_ROOT/$DMG_NAME" > "$OUTPUT_ROOT/$DMG_NAME.sha256"
"$PYTHON_BIN" "$PROJECT_ROOT/packaging/macos/write_manifest.py" \
    --artifact "$OUTPUT_ROOT/$DMG_NAME" \
    --version "$RELEASE_VERSION" \
    --architecture "$ARCH" \
    --site-origin "$SITE_ORIGIN" \
    --output "$OUTPUT_ROOT/build-manifest.json"

echo "构建完成：$OUTPUT_ROOT"
