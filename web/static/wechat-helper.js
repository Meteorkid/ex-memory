(() => {
    'use strict';

    const HELPER_BASE = 'http://127.0.0.1:17653';
    const HELPER_LAUNCH_URL = 'ex-memory-helper://launch';
    const entry = document.getElementById('wechat-helper-entry');
    const panel = document.getElementById('wechat-helper-panel');
    const state = document.getElementById('wechat-helper-state');
    const launch = document.getElementById('wechat-helper-launch');
    const open = document.getElementById('wechat-helper-open');
    const downloads = document.getElementById('wechat-helper-downloads');
    if (!entry || !panel || !state || !launch || !open || !downloads) return;

    let release = null;
    let pollingTimer = null;
    let helperCompatible = true;
    let helperDetected = false;
    let detectedArchitecture = '';

    entry.addEventListener('click', async () => {
        panel.style.display = 'block';
        const timeline = document.getElementById('moments-timeline');
        if (timeline) timeline.style.display = 'none';
        if (window.setDiscoverFeature) window.setDiscoverFeature(entry.id);
        await loadRelease();
        await detectHelper();
    });

    launch.addEventListener('click', async () => {
        if (!helperCompatible) return;
        launch.disabled = true;
        state.textContent = '正在连接本地助手……';
        try {
            if (!helperDetected) {
                window.top.location.href = HELPER_LAUNCH_URL;
                state.textContent = '正在启动已安装的本地助手……';
                if (!await waitForHelperReady()) {
                    if (!helperDetected) await renderDetectionFailure();
                    return;
                }
            }
            const response = await fetch(`${HELPER_BASE}/v1/control/launch`, { method: 'POST' });
            if (!response.ok) throw new Error('launch_failed');
            const task = await response.json();
            renderLocalLink(task.local_url);
            state.textContent = '本地安全页面已就绪，点击下方链接继续。';
            pollTask(task.task_id);
        } catch {
            await renderDetectionFailure();
        } finally {
            launch.disabled = !helperCompatible;
        }
    });

    function renderLocalLink(url) {
        if (!url) return;
        open.href = url;
        open.style.display = '';
    }

    async function detectHelper() {
        try {
            const health = await fetchHelperHealth(1600);
            applyHelperHealth(health);
        } catch {
            await renderDetectionFailure();
        }
    }

    async function fetchHelperHealth(timeoutMs) {
        const controller = new AbortController();
        const timeout = window.setTimeout(() => controller.abort(), timeoutMs);
        try {
            const response = await fetch(`${HELPER_BASE}/v1/control/health`, {
                cache: 'no-store',
                signal: controller.signal,
            });
            if (!response.ok) throw new Error('unavailable');
            return await response.json();
        } finally {
            window.clearTimeout(timeout);
        }
    }

    function applyHelperHealth(health) {
        helperDetected = true;
        detectedArchitecture = health.architecture || '';
        const minimumApiVersion = Number(release?.min_api_version || 1);
        if (health.platform !== 'macos' || health.architecture !== 'arm64' || Number(health.api_version || 0) < minimumApiVersion) {
            helperCompatible = false;
            launch.disabled = true;
            state.textContent = `本地助手 ${health.helper_version || '未知版本'} 与当前网站不兼容，请下载最新版后重试。`;
            renderDownloads();
            return false;
        }
        helperCompatible = true;
        launch.disabled = false;
        const architecture = detectedArchitecture === 'arm64' ? 'Apple Silicon' : detectedArchitecture || '未知架构';
        state.textContent = `本地助手 ${health.helper_version}（${architecture}）已连接，可以安全启动。`;
        launch.textContent = '启动本地导出';
        return true;
    }

    async function waitForHelperReady() {
        const deadline = Date.now() + 8000;
        while (Date.now() < deadline) {
            try {
                const health = await fetchHelperHealth(800);
                return applyHelperHealth(health);
            } catch {
                await new Promise(resolve => window.setTimeout(resolve, 400));
            }
        }
        return false;
    }

    async function localNetworkPermissionState() {
        if (!navigator.permissions?.query) return 'unknown';
        try {
            const permission = await navigator.permissions.query({ name: 'local-network-access' });
            return permission.state;
        } catch {
            return 'unknown';
        }
    }

    async function probeHelperPresence() {
        const controller = new AbortController();
        const timeout = window.setTimeout(() => controller.abort(), 1000);
        try {
            await fetch(`${HELPER_BASE}/v1/control/health`, {
                mode: 'no-cors',
                cache: 'no-store',
                signal: controller.signal,
            });
            return true;
        } catch {
            return false;
        } finally {
            window.clearTimeout(timeout);
        }
    }

    async function renderDetectionFailure() {
        helperDetected = false;
        helperCompatible = true;
        launch.disabled = false;
        const permissionState = await localNetworkPermissionState();
        if (permissionState === 'denied') {
            state.textContent = '浏览器已拒绝访问本机助手。请在地址栏的网站设置中允许“本地网络访问”，然后重新检测。';
            launch.textContent = '重新检测';
        } else if (await probeHelperPresence()) {
            state.textContent = '检测到本机助手正在运行，但当前进程未授权此网站。请完全退出助手后重新打开；仍失败时请安装最新版。';
            launch.textContent = '重新检测';
        } else {
            state.textContent = '本地助手未运行。安装或启动后，再点击下方按钮。';
            launch.textContent = '重新检测并启动';
        }
        renderDownloads();
    }

    async function loadRelease() {
        if (release) return;
        try {
            const basePath = document.documentElement.dataset.basePath || '';
            const response = await fetch(`${basePath}/api/local-helper/config`, { cache: 'no-store' });
            if (response.ok) release = await response.json();
        } catch {
            release = null;
        }
    }

    function renderDownloads() {
        downloads.replaceChildren();
        if (!release || !release.enabled || !release.downloads.length) {
            downloads.style.display = 'block';
            downloads.textContent = '本站暂未发布可校验的 macOS 安装包。';
            return;
        }
        const title = document.createElement('strong');
        title.textContent = `下载 macOS 助手 ${release.version || 'Beta'}`;
        downloads.append(title);
        for (const item of release.downloads) {
            const link = document.createElement('a');
            link.className = 'wechat-helper-download';
            link.href = item.url;
            link.rel = 'noopener';
            const currentArchitecture = item.architecture === detectedArchitecture ? ' · 当前设备' : '';
            link.textContent = `${item.architecture === 'arm64' ? 'Apple Silicon（M1/M2/M3/M4）' : 'Intel Mac'}${currentArchitecture}`;
            const hash = document.createElement('span');
            hash.className = 'wechat-helper-hash';
            hash.textContent = `SHA-256: ${item.sha256}`;
            link.append(hash);
            downloads.append(link);
        }
        downloads.style.display = 'block';
    }

    function pollTask(taskId) {
        window.clearTimeout(pollingTimer);
        const poll = async () => {
            try {
                const response = await fetch(`${HELPER_BASE}/v1/control/tasks/${encodeURIComponent(taskId)}`);
                if (!response.ok) return;
                const task = await response.json();
                state.textContent = publicStatus(task);
                if (['success', 'partial', 'failed', 'cancelled'].includes(task.status)) return;
            } catch {
                return;
            }
            pollingTimer = window.setTimeout(poll, 1500);
        };
        poll();
    }

    function publicStatus(task) {
        const phases = {
            launch: '等待你在本地安全页面确认',
            awaiting_sip_disabled: '等待你手动关闭 SIP；提取前请完全退出微信',
            extracting_keys: '请启动微信并登录已确认账号；正在验证该账号全部数据库',
            awaiting_wechat_exit: '当前账号全库密钥已验证，请完全退出微信',
            decrypting: '正在逐库解密并校验 sqlite_master',
            awaiting_sip_enabled: '请立即重新开启 SIP 并重启',
            ready_to_export: 'SIP 已开启，可以选择当前账号联系人',
            exporting: '正在本机生成离线 HTML，请稍候',
            complete: '当前账号导出完成，请回到本地页面查看文件',
            partial: '当前账号已导出，部分媒体缺失，请回到本地页面查看',
            deleted: '本地任务数据已删除'
        };
        return `${phases[task.phase] || '本地任务进行中'} · ${task.progress}%`;
    }
})();
