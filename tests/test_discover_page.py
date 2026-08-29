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


def test_wechat_helper_explains_denied_local_network_permission():
    helper_js = (ROOT / "web/static/wechat-helper.js").read_text(encoding="utf-8")

    assert "navigator.permissions.query({ name: 'local-network-access' })" in helper_js
    assert "浏览器已拒绝访问本机助手" in helper_js
    assert "允许“本地网络访问”" in helper_js


def test_wechat_helper_distinguishes_running_incompatible_process_from_missing_app():
    helper_js = (ROOT / "web/static/wechat-helper.js").read_text(encoding="utf-8")

    assert "mode: 'no-cors'" in helper_js
    assert "检测到本机助手正在运行" in helper_js
    assert "请完全退出助手后重新打开" in helper_js


def test_wechat_helper_launches_installed_app_before_retrying_health_check():
    helper_js = (ROOT / "web/static/wechat-helper.js").read_text(encoding="utf-8")

    assert "ex-memory-helper://launch" in helper_js
    assert "window.top.location.href = HELPER_LAUNCH_URL" in helper_js
    assert "await waitForHelperReady()" in helper_js


def test_wechat_helper_failure_never_blocks_manual_file_import():
    html = (ROOT / "web/static/index.html").read_text(encoding="utf-8")
    helper_js = (ROOT / "web/static/wechat-helper.js").read_text(encoding="utf-8")

    assert 'id="wechat-helper-skip"' in html
    assert "已有导出文件？直接导入" in html
    assert 'aria-live="polite"' in html
    assert "window.switchTab('create')" in helper_js
    assert "首次使用，只需安装一次" in helper_js


def test_wechat_helper_network_waits_always_finish_in_a_recoverable_state():
    helper_js = (ROOT / "web/static/wechat-helper.js").read_text(encoding="utf-8")

    assert "async function fetchWithTimeout" in helper_js
    assert helper_js.count("await fetch(") == 1
    assert "正在检测本机助手，最多等待 6 秒" in helper_js
    assert "本地导出不会中断" in helper_js
    assert "launch.disabled = false" in helper_js


def test_wechat_helper_progress_retry_reuses_the_existing_local_task():
    helper_js = (ROOT / "web/static/wechat-helper.js").read_text(encoding="utf-8")

    assert "let activeTaskId = null" in helper_js
    assert "if (activeTaskId)" in helper_js
    assert "pollTask(activeTaskId)" in helper_js
    assert "重新读取进度" in helper_js


def test_wechat_helper_reopens_the_local_page_with_a_fresh_ticket_for_the_same_task():
    html = (ROOT / "web/static/index.html").read_text(encoding="utf-8")
    helper_js = (ROOT / "web/static/wechat-helper.js").read_text(encoding="utf-8")

    assert "重新打开本地安全页面" in html
    assert "let localTaskId = null" in helper_js
    assert "open.addEventListener('click'" in helper_js
    assert "/reopen`" in helper_js
    assert "localTaskId = task.task_id" in helper_js


def test_frontend_initializes_after_runtime_helpers_are_declared():
    app_js = (ROOT / "web/static/app.js").read_text(encoding="utf-8")

    assert app_js.index("const PerfMonitor =") < app_js.index("// ── 初始化 ──")


def test_frontend_release_busts_cached_startup_script():
    html = (ROOT / "web/static/index.html").read_text(encoding="utf-8")
    service_worker = (ROOT / "web/static/sw.js").read_text(encoding="utf-8")

    assert 'src="static/app.js?v=20260823e"' in html
    assert 'src="static/wechat-helper.js?v=20260824c"' in html
    assert "const CACHE_VERSION = 'v14'" in service_worker


def test_frontend_request_dedup_cleanup_does_not_leak_rejections():
    app_js = (ROOT / "web/static/app.js").read_text(encoding="utf-8")

    assert "promise.finally(() => pendingRequests.delete(requestKey))" not in app_js
    assert "promise.then(clearPendingRequest, clearPendingRequest)" in app_js


def test_browser_offline_event_never_controls_persistent_banner():
    app_js = (ROOT / "web/static/app.js").read_text(encoding="utf-8")
    mark_offline = app_js.split("function markNetworkOffline()", 1)[1].split(
        "function markNetworkOnline()", 1
    )[0]

    assert "window.addEventListener('offline'" not in app_js
    assert "fetch(`${BASE_PATH}/health`" not in app_js
    assert "if (PROXY_AUTH) return;" in mark_offline


def test_successful_api_response_clears_stale_offline_state():
    app_js = (ROOT / "web/static/app.js").read_text(encoding="utf-8")
    api_wrapper = app_js.split("async function api", 1)[1].split("function logout", 1)[
        0
    ]

    assert "markNetworkOnline();" in api_wrapper


def test_api_marks_offline_only_after_network_failure_retries_are_exhausted():
    app_js = (ROOT / "web/static/app.js").read_text(encoding="utf-8")
    api_wrapper = app_js.split("async function api", 1)[1].split("function logout", 1)[
        0
    ]

    assert "const networkFailure = isNetworkFailure(e);" in api_wrapper
    assert "if (networkFailure) markNetworkOffline();" in api_wrapper
    assert api_wrapper.index("if (i < retries && networkFailure)") < api_wrapper.index(
        "if (networkFailure) markNetworkOffline();"
    )
