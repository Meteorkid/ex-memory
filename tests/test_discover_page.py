from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_discover_page_exposes_dark_feature_hub():
    html = (ROOT / "web/static/index.html").read_text(encoding="utf-8")
    discover = html.split("Tab 3: 发现", 1)[1].split("Tab 4: 我", 1)[0]

    assert 'class="discover-hero"' in discover
    assert '<button class="discover-feature-card" id="moments-entry"' in discover
    assert '<button class="discover-feature-card" id="wechat-helper-entry"' in discover
    assert "<svg" in discover
    assert 'class="wechat-helper-workspace"' in discover
    assert 'class="wechat-helper-steps"' in discover
    assert "仅在本机处理" in discover
    assert "🎞" not in discover
    assert "📦" not in discover


def test_wechat_helper_styles_use_only_dark_theme_surfaces():
    css = (ROOT / "web/static/wechat-helper.css").read_text(encoding="utf-8")

    assert "var(--bg-elevated)" in css
    assert "var(--accent)" in css
    assert "var(--wechat-green)" in css
    assert "#fff7ed" not in css
    assert "#f5f5f7" not in css
    assert "var(--card-bg, #fff)" not in css


def test_discover_feature_switch_updates_visual_and_accessible_state():
    app_js = (ROOT / "web/static/app.js").read_text(encoding="utf-8")
    helper_js = (ROOT / "web/static/wechat-helper.js").read_text(encoding="utf-8")

    assert "window.setDiscoverFeature" in app_js
    assert "classList.toggle('active'" in app_js
    assert "setAttribute('aria-expanded'" in app_js
    assert "window.setDiscoverFeature(entry.id)" in helper_js


def test_wechat_helper_entry_explains_supported_version_and_account_keys():
    html = (ROOT / "web/static/index.html").read_text(encoding="utf-8")

    assert "本机微信 4.1.12 导出" in html
    assert "仅验证微信 4.1.12" in html
    assert "每个微信账号的数据库密钥不同" in html
    assert "切换账号" in html


def test_wechat_helper_rejects_outdated_local_protocol():
    helper_js = (ROOT / "web/static/wechat-helper.js").read_text(encoding="utf-8")

    assert "release?.min_api_version" in helper_js
    assert "health.api_version" in helper_js
    assert "与当前网站不兼容" in helper_js
    assert "health.architecture" in helper_js
    assert "health.architecture !== 'arm64'" in helper_js


def test_frontend_initializes_after_runtime_helpers_are_declared():
    app_js = (ROOT / "web/static/app.js").read_text(encoding="utf-8")

    assert app_js.index("const PerfMonitor =") < app_js.index("// ── 初始化 ──")


def test_frontend_release_busts_cached_startup_script():
    html = (ROOT / "web/static/index.html").read_text(encoding="utf-8")
    service_worker = (ROOT / "web/static/sw.js").read_text(encoding="utf-8")

    assert 'src="static/app.js?v=20260823c"' in html
    assert 'src="static/wechat-helper.js?v=20260823b"' in html
    assert "const CACHE_VERSION = 'v12'" in service_worker


def test_frontend_request_dedup_cleanup_does_not_leak_rejections():
    app_js = (ROOT / "web/static/app.js").read_text(encoding="utf-8")

    assert "promise.finally(() => pendingRequests.delete(requestKey))" not in app_js
    assert "promise.then(clearPendingRequest, clearPendingRequest)" in app_js
