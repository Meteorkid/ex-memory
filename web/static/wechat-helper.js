(() => {
    'use strict';

    const HELPER_BASE = 'http://127.0.0.1:17653';
    const HELPER_LAUNCH_URL = 'ex-memory-helper://launch';
    const entry = document.getElementById('wechat-helper-entry');
    const panel = document.getElementById('wechat-helper-panel');
    const state = document.getElementById('wechat-helper-state');
    const launch = document.getElementById('wechat-helper-launch');
    const open = document.getElementById('wechat-helper-open');
    const skip = document.getElementById('wechat-helper-skip');
    const downloads = document.getElementById('wechat-helper-downloads');
    if (!entry || !panel || !state || !launch || !open || !skip || !downloads) return;

    let release = null;
    let pollingTimer = null;
    let activeTaskId = null;
    let localTaskId = null;
    let reopeningLocalPage = false;
    let helperCompatible = true;
    let helperDetected = false;
    let detectedArchitecture = '';

    entry.addEventListener('click', async () => {
        panel.style.display = 'block';
        const timeline = document.getElementById('moments-timeline');
        if (timeline) timeline.style.display = 'none';
        if (window.setDiscoverFeature) window.setDiscoverFeature(entry.id);
        launch.disabled = true;
        state.textContent = '正在检测本机助手，最多等待 6 秒……';
        await loadRelease();
        await detectHelper();
    });

    skip.addEventListener('click', () => {
        if (typeof window.switchTab === 'function') window.switchTab('create');
    });

    open.addEventListener('click', async (event) => {
        event.preventDefault();
        if (!localTaskId || reopeningLocalPage) return;
        reopeningLocalPage = true;
        open.setAttribute('aria-disabled', 'true');
        state.textContent = '正在重新打开本地安全页面……';
        try {
            const response = await fetchWithTimeout(
                `${HELPER_BASE}/v1/control/tasks/${encodeURIComponent(localTaskId)}/reopen`,
                { method: 'POST' },
                5000,
            );
            if (response.status === 404) {
                activeTaskId = null;
                localTaskId = null;
                open.style.display = 'none';
                launch.textContent = '重新连接助手';
                state.textContent = '本地助手已重启，原网页任务已失效。请重新连接；已有本地任务可在新页面恢复。';
                return;
            }
            if (!response.ok) throw new Error('reopen_failed');
            const task = await response.json();
            localTaskId = task.task_id;
            state.textContent = '本地安全页面已重新打开；本地导出任务保持不变。';
        } catch {
            state.textContent = '未能重新打开本地安全页面，请确认助手仍在运行后重试。';
        } finally {
            reopeningLocalPage = false;
            open.removeAttribute('aria-disabled');
        }
    });

    launch.addEventListener('click', async () => {
        if (!helperCompatible) return;
        launch.disabled = true;
        state.textContent = '正在连接本地助手……';
        try {
            if (activeTaskId) {
                state.textContent = '正在重新读取本地导出进度……';
                pollTask(activeTaskId);
                return;
            }
            if (!helperDetected) {
                window.top.location.href = HELPER_LAUNCH_URL;
                state.textContent = '正在启动已安装的本地助手……';
                if (!await waitForHelperReady()) {
                    if (!helperDetected) await renderDetectionFailure();
                    return;
                }
            }
            state.textContent = '助手已连接，正在创建本地导出任务……';
            const response = await fetchWithTimeout(`${HELPER_BASE}/v1/control/launch`, { method: 'POST' }, 5000);
            if (!response.ok) throw new Error('launch_failed');
            const task = await response.json();
            activeTaskId = task.task_id;
            localTaskId = task.task_id;
            renderLocalLink();
            state.textContent = '本地安全页面已打开；关闭后可用下方按钮重新打开。';
            launch.textContent = '刷新本地任务状态';
            pollTask(task.task_id);
        } catch {
            if (helperDetected) {
                state.textContent = '助手已连接，但创建导出任务超时。请点击“重新连接助手”；你也可以直接导入已有聊天文件。';
                launch.textContent = '重新连接助手';
            } else {
                await renderDetectionFailure();
            }
        } finally {
            launch.disabled = !helperCompatible;
        }
    });

    async function fetchWithTimeout(url, options = {}, timeoutMs = 3000) {
        const controller = new AbortController();
        const timeout = window.setTimeout(() => controller.abort(), timeoutMs);
        try {
            return await fetch(url, { ...options, signal: controller.signal });
        } finally {
            window.clearTimeout(timeout);
        }
    }

    function renderLocalLink() {
        open.href = '#';
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
        const response = await fetchWithTimeout(`${HELPER_BASE}/v1/control/health`, {
            cache: 'no-store',
        }, timeoutMs);
        if (!response.ok) throw new Error('unavailable');
        return await response.json();
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
        try {
            await fetchWithTimeout(`${HELPER_BASE}/v1/control/health`, {
                mode: 'no-cors',
                cache: 'no-store',
            }, 1000);
            return true;
        } catch {
            return false;
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
            state.textContent = '尚未连接本地助手。如果已经安装，点击“打开已安装助手”；没有安装可在下方下载。';
            launch.textContent = '打开已安装助手';
        }
        renderDownloads();
    }

    async function loadRelease() {
        if (release) return;
        try {
            const basePath = document.documentElement.dataset.basePath || '';
            const response = await fetchWithTimeout(`${basePath}/api/local-helper/config`, { cache: 'no-store' }, 2500);
            if (response.ok) release = await response.json();
        } catch {
            release = null;
        }
    }

    function renderDownloads() {
        downloads.replaceChildren();
        const title = document.createElement('strong');
        title.textContent = '首次使用，只需安装一次';
        downloads.append(title);
        const steps = document.createElement('ol');
        steps.className = 'wechat-helper-install-steps';
        for (const copy of ['下载助手并拖入“应用程序”', '首次在 Finder 中右键选择“打开”', '回到此页点击“打开已安装助手”']) {
            const item = document.createElement('li');
            item.textContent = copy;
            steps.append(item);
        }
        downloads.append(steps);
        if (!release || !release.enabled || !release.downloads.length) {
            downloads.style.display = 'block';
            const unavailable = document.createElement('p');
            unavailable.textContent = '安装包配置暂时不可用，请稍后重新检测；也可以直接导入已有聊天文件。';
            downloads.append(unavailable);
            return;
        }
        const version = document.createElement('p');
        version.className = 'wechat-helper-download-version';
        version.textContent = `下载 macOS 助手 ${release.version || 'Beta'}`;
        downloads.append(version);
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
        activeTaskId = taskId;
        let consecutiveFailures = 0;
        const poll = async () => {
            try {
                const response = await fetchWithTimeout(`${HELPER_BASE}/v1/control/tasks/${encodeURIComponent(taskId)}`, {}, 3000);
                if (!response.ok) throw new Error('status_unavailable');
                const task = await response.json();
                consecutiveFailures = 0;
                state.textContent = publicStatus(task);
                if (['success', 'partial', 'failed', 'cancelled'].includes(task.status)) {
                    activeTaskId = null;
                    launch.textContent = '开始新的本地导出';
                    launch.disabled = false;
                    return;
                }
            } catch {
                consecutiveFailures += 1;
                if (consecutiveFailures >= 3) {
                    state.textContent = '网站暂时无法读取进度，但本地导出不会中断。请在已打开的本地安全页面继续。';
                    launch.textContent = '重新读取进度';
                    launch.disabled = false;
                    return;
                }
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
