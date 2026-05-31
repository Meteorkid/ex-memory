/* ex-memory 微信模拟器 SPA */

const API = '/api';
let token = localStorage.getItem('ex-memory-token') || '';
let currentSlug = '';
let currentName = '';
let tabHistory = [];
let speechRecognition = null;

// ── 情感健康：使用时长追踪 ──
let loginTime = Date.now();
let lastReminderTime = 0;
const REMINDER_INTERVAL = 30 * 60 * 1000; // 30 分钟

// ── Toast 通知系统 ──
function ensureToastContainer() {
    let c = document.querySelector('.toast-container');
    if (!c) {
        c = document.createElement('div');
        c.className = 'toast-container';
        c.setAttribute('role', 'status');
        c.setAttribute('aria-live', 'polite');
        document.body.appendChild(c);
    }
    return c;
}

function showToast(message, type = 'info', duration = 3000) {
    const container = ensureToastContainer();
    const icons = { success: '✓', error: '✗', info: 'ℹ', warning: '⚠' };
    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    toast.innerHTML = `<span class="toast-icon">${icons[type] || icons.info}</span><span>${escHtml(String(message))}</span>`;
    container.appendChild(toast);

    setTimeout(() => {
        toast.classList.add('toast-exit');
        toast.addEventListener('animationend', () => toast.remove());
    }, duration);
}

// 全局错误处理
window.onerror = function(msg, src, line, col, err) {
    showToast('发生了一个错误，请刷新页面重试', 'error');
    console.error('[Global Error]', msg, src, line, col, err);
};

window.addEventListener('unhandledrejection', function(e) {
    showToast('操作失败，请稍后重试', 'error');
    console.error('[Unhandled Rejection]', e.reason);
});

// 离线检测
window.addEventListener('offline', () => {
    showToast('网络连接已断开', 'warning', 5000);
    const banner = $('offline-banner');
    if (banner) banner.classList.add('visible');
});
window.addEventListener('online', () => {
    showToast('网络已恢复', 'success');
    const banner = $('offline-banner');
    if (banner) banner.classList.remove('visible');
    flushOfflineQueue();
});

// 初始检查离线状态
if (!navigator.onLine) {
    const banner = $('offline-banner');
    if (banner) banner.classList.add('visible');
}

// ── 快捷引用 ──
const $ = id => document.getElementById(id);

function authHeaders(json = true) {
    const h = {};
    if (json) h['Content-Type'] = 'application/json';
    if (token) h['Authorization'] = `Bearer ${token}`;
    return h;
}

function safeStickerUrl(url) {
    if (!url || typeof url !== 'string') return '';
    if (url.startsWith('/static/')) return url;
    return '';
}

async function parseChatStream(res, msgsEl) {
    if (res.status === 401) { logout(); return null; }
    if (!res.ok) {
        const d = await res.json().catch(() => ({}));
        const detail = d.detail;
        const msg = typeof detail === 'string' ? detail : '请求失败';
        throw new Error(msg);
    }

    const replyRow = chatBubble('assistant', '');
    msgsEl.appendChild(replyRow);
    const assistantDiv = replyRow.querySelector('.msg');

    // 打字延迟：模拟真人打字节奏
    const typeQueue = [];
    let isTyping = false;
    function typeNext() {
        if (typeQueue.length === 0) { isTyping = false; return; }
        isTyping = true;
        const text = typeQueue.shift();
        assistantDiv.textContent += text;
        msgsEl.scrollTop = msgsEl.scrollHeight;
        // 随机延迟 30-80ms，模拟打字节奏
        const delay = 30 + Math.random() * 50;
        setTimeout(typeNext, delay);
    }
    function queueType(text) {
        typeQueue.push(text);
        if (!isTyping) typeNext();
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    while (true) {
        const {done, value} = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, {stream: true});
        const lines = buffer.split('\n');
        buffer = lines.pop() || '';

        for (const line of lines) {
            if (!line.startsWith('data: ')) continue;
            const d = line.slice(6);
            if (d === '[DONE]') continue;
            try {
                const item = JSON.parse(d);
                if (item.error) {
                    assistantDiv.textContent = item.error;
                    replyRow.className = 'msg-row sys';
                } else if (item.type === 'text' && item.content) {
                    queueType(item.content);
                } else if (item.type === 'sticker' && item.id) {
                    msgsEl.appendChild(stickerMsg(item.id));
                } else if (item.type === 'red_packet') {
                    msgsEl.appendChild(redPacketBubble(item));
                } else if (item.type === 'transfer') {
                    msgsEl.appendChild(transferBubble(item));
                }
            } catch (e) { /* skip malformed SSE */ }
        }
    }

    // 等待打字队列完成
    while (typeQueue.length > 0 || isTyping) {
        await new Promise(r => setTimeout(r, 50));
    }

    if (!assistantDiv.textContent.trim() && replyRow.parentNode) {
        replyRow.remove();
        return null;
    }
    if (currentSlug) {
        const text = assistantDiv.textContent.trim();
        storeLastMessage(currentSlug, text);
        // 桌面通知
        showDesktopNotification(currentName || currentSlug, text.slice(0, 60), currentSlug);
    }
    return assistantDiv;
}

// ── 主题管理 ──
function initTheme() {
    const saved = localStorage.getItem('ex-memory-theme') || 'auto';
    applyTheme(saved);
    // 延迟绑定，因为 select 元素可能还未渲染
    setTimeout(() => {
        const select = $('theme-select');
        if (select) {
            select.value = saved;
            select.addEventListener('change', () => {
                const theme = select.value;
                localStorage.setItem('ex-memory-theme', theme);
                applyTheme(theme);
            });
        }
    }, 100);
}

function applyTheme(theme) {
    const root = document.documentElement;
    if (theme === 'auto') {
        root.removeAttribute('data-theme');
    } else {
        root.setAttribute('data-theme', theme);
    }
    // 更新 theme-color meta 标签
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) {
        meta.content = theme === 'dark' ? '#000000' : '#F2F2F7';
    }
}

// ── 平台检测（响应窗口大小变化）──
let isDesktop = window.matchMedia('(min-width: 769px)').matches;
let desktopTab = 'chat';
let currentTab = 'chat-list';

window.matchMedia('(min-width: 769px)').addEventListener('change', e => {
    const wasDesktop = isDesktop;
    isDesktop = e.matches;
    if (wasDesktop !== isDesktop) {
        // 平台切换时重新初始化布局
        if (isDesktop) {
            $('page-main').style.display = 'flex';
            setDesktopMode('chat');
        } else {
            $('page-main').style.display = 'block';
            switchTab('chat-list');
        }
    }
}); // chat | contacts | discover | me

// ── 初始化 ──
initTheme();
if (token) { showMain(); } else { showAuth(); }
updateStatusTime();
setInterval(updateStatusTime, 30000);
initDesktopLayout();
initDesktopSidebar();
try { initVoiceToggle(); } catch(e) { console.warn('Voice init skipped:', e.message); }
initSearch();
registerSW();
requestNotificationPermission();

// ── 情感健康：使用时长提醒（每 10 分钟检查一次）──
setInterval(checkUsageReminder, 10 * 60 * 1000);

function checkUsageReminder() {
    if (!token || !currentSlug) return;
    const now = Date.now();
    if (now - loginTime > REMINDER_INTERVAL && now - lastReminderTime > REMINDER_INTERVAL) {
        lastReminderTime = now;
        showMindfulReminder();
    }
}

function showMindfulReminder() {
    // 在聊天中插入系统消息，而非弹窗打断
    const msgsEl = $('chat-msgs');
    if (!msgsEl || msgsEl.style.display === 'none') return;
    fetchHealthTip().then(tip => {
        const row = document.createElement('div');
        row.className = 'msg-row sys';
        row.innerHTML = `<div class="msg mindful-msg">🌿 ${escHtml(tip)}</div>`;
        msgsEl.appendChild(row);
        msgsEl.scrollTop = msgsEl.scrollHeight;
    });
    loginTime = Date.now(); // 重置计时
}

async function fetchHealthTip() {
    // 深夜个性化提示
    const hour = new Date().getHours();
    const tips = {
        lateNight: [
            '这么晚了还在想 ta 吗？早点休息 🌙',
            '夜深了，放下手机，闭上眼睛休息吧',
            '深夜的情绪总是特别浓，但明天会更好',
            '又失眠了？深呼吸三次，感受当下的平静',
        ],
        earlyMorning: [
            '新的一天，好好生活 🌅',
            '早安，今天也要加油哦',
            '新的一天开始了，做点让自己开心的事',
        ],
        daytime: [
            '适当休息一下，看看窗外的风景',
            '深呼吸三次，感受当下的平静',
            '和真实的朋友聊聊天，感受真实的温暖',
            '运动 10 分钟，让身体和心情都好起来',
        ],
    };

    let pool = tips.daytime;
    if (hour >= 0 && hour < 6) pool = tips.lateNight;
    else if (hour >= 6 && hour < 9) pool = tips.earlyMorning;

    try {
        const data = await api('GET', '/user/health/check');
        if (data.health_tip) return data.health_tip;
    } catch(e) {}

    return pool[Math.floor(Math.random() * pool.length)];
}

// ── 正念引导：退出时的温暖告别 ──
window.addEventListener('beforeunload', (e) => {
    if (!token) return;
    const msg = '随时回来找我 🌙';
    e.returnValue = msg;
    return msg;
});

// ── 情感健康贴士：加载到聊天列表底部 ──
async function loadHealthTip() {
    const container = $('health-tip-container');
    if (!container) return;
    try {
        const tip = await fetchHealthTip();
        container.innerHTML = `<div class="health-tip">${escHtml(tip)}</div>`;
    } catch(e) {
        container.innerHTML = `<div class="health-tip">深呼吸三次，感受当下的平静。</div>`;
    }
}

function updateStatusTime() {
    const now = new Date();
    const el = $('status-time');
    if (el) el.textContent = now.getHours().toString().padStart(2,'0') + ':' + now.getMinutes().toString().padStart(2,'0');
}

function initDesktopLayout() {
    if (!isDesktop) return;
    setDesktopMode('chat');
}

function setDesktopMode(mode) {
    desktopTab = mode;
    const content = $('phone-content');
    if (!content) return;
    // 清除所有模式类
    content.className = 'phone-content ' + mode + '-mode';

    // 隐藏所有 tab 页
    const tabs = ['tab-chat-list','tab-chat-detail','tab-contacts',
                  'tab-discover','tab-me','tab-wallet','tab-create'];
    tabs.forEach(id => {
        const el = $(id);
        if (el) el.style.display = 'none';
    });

    // 更新标题
    const titles = { chat: 'ex-memory', contacts: '通讯录', discover: '发现', me: '我' };
    $('nav-title').textContent = titles[mode] || 'ex-memory';
    $('titlebar-app-name').textContent = titles[mode] || 'ex-memory';
    $('nav-back-btn').style.display = 'none';
    $('nav-action-btn').style.display = 'none';

    switch (mode) {
        case 'chat':
            $('tab-chat-list').style.display = 'block';
            if (currentSlug) {
                $('tab-chat-detail').style.display = 'flex';
                $('nav-title').textContent = currentName || currentSlug;
                $('titlebar-app-name').textContent = currentName || currentSlug;
                $('nav-action-btn').style.display = 'block';
                // 高亮当前联系人
                document.querySelectorAll('.contact-item').forEach(c => {
                    c.classList.toggle('selected', c.dataset.slug === currentSlug);
                });
            } else {
                $('tab-chat-detail').style.display = 'none';
            }
            loadContactList();
            break;
        case 'contacts':
            $('tab-contacts').style.display = 'block';
            if (currentSlug) showContactProfile();
            break;
        case 'discover':
            $('tab-discover').style.display = 'block';
            break;
        case 'me':
            $('tab-me').style.display = 'block';
            $('me-username').textContent = localStorage.getItem('ex-memory-username') || '我';
            break;
    }
}

// ── 桌面端侧边栏导航 ──
function initDesktopSidebar() {
    if (!isDesktop) return;
    document.querySelectorAll('.sidebar-icon').forEach(icon => {
        icon.addEventListener('click', () => {
            const tab = icon.dataset.desktopTab;
            if (!tab) return;
            // 更新高亮
            document.querySelectorAll('.sidebar-icon').forEach(i => i.classList.remove('active'));
            icon.classList.add('active');
            // 切换到对应模式
            setDesktopMode(tab);
            // 如果是非聊天模式，保留当前聊天状态但不显示
            if (tab !== 'chat' && currentSlug) {
                // 聊天保持在后台，切换回来时恢复
            }
        });
        // 键盘支持：Enter/Space 激活
        icon.addEventListener('keydown', e => {
            if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault();
                icon.click();
            }
        });
    });
}

// ── 离线消息队列 ──
const OFFLINE_QUEUE_KEY = 'ex-memory-offline-queue';

function getOfflineQueue() {
    try { return JSON.parse(localStorage.getItem(OFFLINE_QUEUE_KEY) || '[]'); }
    catch { return []; }
}

function saveOfflineQueue(queue) {
    localStorage.setItem(OFFLINE_QUEUE_KEY, JSON.stringify(queue));
}

function enqueueOfflineMessage(slug, message, stickerId) {
    const queue = getOfflineQueue();
    queue.push({ slug, message, stickerId, timestamp: Date.now() });
    saveOfflineQueue(queue);
    showToast('消息已离线保存，恢复网络后自动发送', 'info', 3000);
}

async function flushOfflineQueue() {
    const queue = getOfflineQueue();
    if (!queue.length) return;
    saveOfflineQueue([]);
    showToast(`正在发送 ${queue.length} 条离线消息…`, 'info', 3000);
    for (const item of queue) {
        try {
            await fetch(`${API}/chat/stream`, {
                method: 'POST',
                headers: authHeaders(),
                body: JSON.stringify({ slug: item.slug, message: item.message || '', sticker_id: item.stickerId, history: [] }),
            });
        } catch { /* 单条失败不阻塞后续 */ }
    }
    showToast('离线消息发送完成', 'success');
}

// ── Service Worker ──
function registerSW() {
    if ('serviceWorker' in navigator) {
        navigator.serviceWorker.register('/static/sw.js').then(reg => {
            // 检查更新
            reg.addEventListener('updatefound', () => {
                const newWorker = reg.installing;
                if (newWorker) {
                    newWorker.addEventListener('statechange', () => {
                        if (newWorker.state === 'activated') {
                            showToast('应用已更新，刷新页面获取最新版本', 'info', 5000);
                        }
                    });
                }
            });
        }).catch(err => {
            console.warn('[SW] 注册失败:', err);
        });
    }
}

// ── 推送通知 ──
function requestNotificationPermission() {
    if (!('Notification' in window)) return;
    if (Notification.permission === 'default') {
        Notification.requestPermission();
    }
}

function showDesktopNotification(title, body, slug) {
    if (!('Notification' in window) || Notification.permission !== 'granted') return;
    const notif = new Notification(title, { body, icon: '/static/icon-192.svg', tag: slug || 'ex-memory' });
    notif.onclick = () => {
        window.focus();
        if (slug) enterChat(slug, slug);
        notif.close();
    };
}

// ═══════════════════════════════════════
// 认证页
// ═══════════════════════════════════════

function showAuth() {
    $('page-auth').style.display = 'flex';
    $('page-main').style.display = 'none';
    showLoginForm();
}

function showMain() {
    $('page-auth').style.display = 'none';
    if (isDesktop) {
        $('page-main').style.display = 'flex';
    } else {
        $('page-main').style.display = 'block';
    }
    loadContactList();
    loadStickers();
    if (isDesktop) {
        setDesktopMode('chat');
    } else {
        // 初始显示聊天列表，无需动画
        const el = $('tab-chat-list');
        el.style.display = 'block';
        el.classList.add('visible');
        document.querySelectorAll('.tabbar-btn').forEach(b => {
            b.classList.toggle('active', b.dataset.tab === 'chat-list');
        });
    }
}

function showLoginForm() {
    $('form-login').style.display = 'flex';
    $('form-register').style.display = 'none';
    $('tab-login-btn').classList.add('active');
    $('tab-register-btn').classList.remove('active');
    $('login-error').textContent = '';
}

function showRegForm() {
    $('form-login').style.display = 'none';
    $('form-register').style.display = 'flex';
    $('tab-register-btn').classList.add('active');
    $('tab-login-btn').classList.remove('active');
    $('reg-error').textContent = '';
}

$('tab-login-btn').onclick = showLoginForm;
$('tab-register-btn').onclick = showRegForm;

// ── 密码切换 ──
document.querySelectorAll('.pwd-toggle').forEach(btn => {
    btn.addEventListener('click', () => {
        const input = document.getElementById(btn.dataset.target);
        if (!input) return;
        const isPwd = input.type === 'password';
        input.type = isPwd ? 'text' : 'password';
        btn.textContent = isPwd ? '🙈' : '👁';
    });
});

// ── 登录 ──
async function doLogin() {
    const u = $('login-username').value.trim();
    const p = $('login-password').value;
    const err = $('login-error'), btn = $('login-submit');
    if (!u || !p) { err.textContent = '请填写用户名和密码'; return; }
    btn.textContent = '登录中…'; btn.disabled = true; err.textContent = '';
    try {
        const res = await fetch(`${API}/auth/login`, {
            method: 'POST', headers: {'Content-Type':'application/json'},
            body: JSON.stringify({username:u, password:p}),
        });
        const data = await res.json();
        if (res.ok) {
            token = data.token;
            localStorage.setItem('ex-memory-token', token);
            localStorage.setItem('ex-memory-username', u);
            showMain();
        } else { err.textContent = data.detail || '登录失败'; }
    } catch(e) { err.textContent = '网络错误'; }
    finally { btn.textContent = '登录'; btn.disabled = false; }
}
$('login-submit').onclick = doLogin;
$('login-username').onkeydown = e => { if(e.key==='Enter') $('login-password').focus(); };
$('login-password').onkeydown = e => { if(e.key==='Enter') doLogin(); };

// ── 注册 ──
async function doRegister() {
    const u = $('reg-username').value.trim();
    const p = $('reg-password').value, p2 = $('reg-password2').value;
    const err = $('reg-error'), btn = $('reg-submit');
    if (u.length < 2) { err.textContent = '用户名至少2个字符'; return; }
    if (p.length < 6) { err.textContent = '密码至少6个字符'; return; }
    if (p !== p2) { err.textContent = '两次密码输入不一致'; return; }
    btn.textContent = '注册中…'; btn.disabled = true; err.textContent = '';
    try {
        const res = await fetch(`${API}/auth/register`, {
            method: 'POST', headers: {'Content-Type':'application/json'},
            body: JSON.stringify({username:u, password:p}),
        });
        if (!res.ok) { const d = await res.json(); err.textContent = d.detail || '注册失败'; return; }
        const loginRes = await fetch(`${API}/auth/login`, {
            method: 'POST', headers: {'Content-Type':'application/json'},
            body: JSON.stringify({username:u, password:p}),
        });
        const loginData = await loginRes.json();
        if (loginRes.ok) {
            token = loginData.token;
            localStorage.setItem('ex-memory-token', token);
            localStorage.setItem('ex-memory-username', u);
            showMain();
        } else { err.textContent = '注册成功但自动登录失败'; showLoginForm(); }
    } catch(e) { err.textContent = '网络错误'; }
    finally { btn.textContent = '注册'; btn.disabled = false; }
}
$('reg-submit').onclick = doRegister;
$('reg-username').onkeydown = e => { if(e.key==='Enter') $('reg-password').focus(); };
$('reg-password').onkeydown = e => { if(e.key==='Enter') $('reg-password2').focus(); };
$('reg-password2').onkeydown = e => { if(e.key==='Enter') doRegister(); };

// ═══════════════════════════════════════
// API 封装
// ═══════════════════════════════════════

async function api(method, path, body) {
    if (!navigator.onLine) {
        showToast('网络连接已断开，请检查网络', 'warning');
        throw new Error('网络连接已断开');
    }
    const headers = {};
    if (token) headers['Authorization'] = `Bearer ${token}`;
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    const res = await fetch(`${API}${path}`, {
        method, headers,
        body: body !== undefined ? JSON.stringify(body) : undefined,
    });
    if (res.status === 401) { logout(); throw new Error('登录已过期'); }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `请求失败 (${res.status})`);
    return data;
}

function logout() {
    if (token) {
        fetch(`${API}/auth/logout`, {
            method: 'POST', headers: {'Content-Type':'application/json'},
            body: JSON.stringify({token}),
        }).catch(()=>{});
    }
    token = ''; currentSlug = ''; currentName = '';
    localStorage.removeItem('ex-memory-token');
    showAuth();
}

// ═══════════════════════════════════════
// 移动端 Tab 导航
// ═══════════════════════════════════════

function switchTab(tab) {
    currentTab = tab;
    tabHistory.push(tab);
    if (tabHistory.length > 10) tabHistory.shift();

    // 桌面端：使用模式切换
    if (isDesktop) {
        const modeMap = {
            'chat-list': 'chat', 'chat-detail': 'chat',
            'contacts': 'contacts', 'discover': 'discover',
            'me': 'me', 'create': 'me', 'wallet': 'me'
        };
        const mode = modeMap[tab] || 'chat';
        // 更新侧边栏高亮
        document.querySelectorAll('.sidebar-icon').forEach(i => {
            const isActive = i.dataset.desktopTab === mode;
            i.classList.toggle('active', isActive);
            i.setAttribute('aria-selected', isActive);
        });
        setDesktopMode(mode);
        // 如果切换到钱包或创建，显示对应页面
        if (tab === 'wallet') {
            $('tab-me').style.display = 'none';
            $('tab-wallet').style.display = 'block';
            $('nav-title').textContent = '钱包';
            $('titlebar-app-name').textContent = '钱包';
            $('nav-back-btn').style.display = 'block';
            loadWallet();
        } else if (tab === 'create') {
            $('tab-me').style.display = 'none';
            $('tab-create').style.display = 'block';
            $('nav-title').textContent = '创建镜像';
            $('titlebar-app-name').textContent = '创建镜像';
            $('nav-back-btn').style.display = 'block';
        }
        return;
    }

    // 移动端：单页面切换（带过渡动画）
    const isBack = tabHistory.length > 1 && tabHistory[tabHistory.length - 2] === tab;
    const allPages = document.querySelectorAll('.tab-page');
    const currentPage = document.querySelector('.tab-page.visible');

    allPages.forEach(p => {
        p.classList.remove('visible', 'slide-back');
    });

    const targetEl = document.getElementById('tab-' + tab);
    if (targetEl) {
        targetEl.style.display = 'block';
        // 触发重排后添加动画类
        requestAnimationFrame(() => {
            if (isBack) targetEl.classList.add('slide-back');
            requestAnimationFrame(() => {
                targetEl.classList.add('visible');
                // 隐藏其他页面（延迟以允许过渡完成）
                setTimeout(() => {
                    allPages.forEach(p => {
                        if (p !== targetEl) p.style.display = 'none';
                    });
                }, 250);
            });
        });
    }

    // 更新底部 tab 高亮
    const rootTabs = { 'chat-list': 'chat-list', 'chat-detail': 'chat-list',
                        contacts:'contacts', discover:'discover', me:'me', create:'me', wallet:'me' };
    const activeRoot = rootTabs[tab] || 'chat-list';
    document.querySelectorAll('.tabbar-btn').forEach(b => {
        const isActive = b.dataset.tab === activeRoot;
        b.classList.toggle('active', isActive);
        b.setAttribute('aria-selected', isActive);
    });

    // 更新导航栏
    updateNavbar(tab);

    // 加载数据
    if (tab === 'chat-list') loadContactList();
    if (tab === 'contacts' && currentSlug) showContactProfile();
    if (tab === 'me') $('me-username').textContent = localStorage.getItem('ex-memory-username') || '我';
    if (tab === 'wallet') loadWallet();
}

function updateNavbar(tab) {
    const back = $('nav-back-btn'), title = $('nav-title'), action = $('nav-action-btn');
    back.style.display = 'none'; action.style.display = 'none';

    const titles = {
        'chat-list': 'ex-memory',
        'chat-detail': currentName || currentSlug || '聊天',
        'contacts': '通讯录',
        'discover': '发现',
        'me': '我',
        'create': '创建镜像',
        'wallet': '钱包',
    };
    title.textContent = titles[tab] || 'ex-memory';

    if (tab === 'chat-detail') {
        back.style.display = 'block';
        action.style.display = 'block';
    }
    if (tab === 'create') {
        back.style.display = 'block';
    }
}

$('nav-back-btn').onclick = () => {
    if (currentTab === 'chat-detail') {
        switchTab('chat-list');
    } else if (currentTab === 'create') {
        switchTab('me');
    } else if (currentTab === 'wallet') {
        switchTab('me');
    } else {
        // 从历史中返回
        if (tabHistory.length > 1) {
            tabHistory.pop();
            const prev = tabHistory.pop();
            switchTab(prev);
        }
    }
};

$('nav-action-btn').onclick = () => {
    if ((currentTab === 'chat-detail' || (isDesktop && desktopTab === 'chat')) && currentSlug) {
        showChatActions();
    }
};

// 移动端底部按钮点击
document.querySelectorAll('.tabbar-btn').forEach(btn => {
    btn.addEventListener('click', () => {
        const tab = btn.dataset.tab;
        if (tab === 'chat-list') {
            if (currentSlug && currentTab === 'chat-list') {
                enterChat(currentSlug, currentName);
                return;
            }
        }
        switchTab(tab);
    });
});

// ═══════════════════════════════════════
// 聊天操作菜单
// ═══════════════════════════════════════

// ── Action Sheet / Confirm 工具函数 ──
function showActionSheet(options) {
    // options: [{label, danger?, onClick}]
    const overlay = $('action-sheet-overlay');
    const optionsEl = $('action-sheet-options');
    optionsEl.innerHTML = '';
    options.forEach(opt => {
        const btn = document.createElement('button');
        btn.className = 'action-sheet-option' + (opt.danger ? ' danger' : '');
        btn.textContent = opt.label;
        btn.onclick = () => { overlay.style.display = 'none'; opt.onClick(); };
        optionsEl.appendChild(btn);
    });
    overlay.style.display = 'flex';
    $('action-sheet-cancel').onclick = () => { overlay.style.display = 'none'; };
    overlay.onclick = (e) => { if (e.target === overlay) overlay.style.display = 'none'; };
}

function showConfirm(title, message, onConfirm, danger = false) {
    $('confirm-title').textContent = title;
    $('confirm-message').textContent = message;
    const overlay = $('confirm-overlay');
    overlay.style.display = 'flex';
    $('confirm-ok-btn').className = 'confirm-btn' + (danger ? ' danger' : '');
    $('confirm-ok-btn').onclick = () => { overlay.style.display = 'none'; onConfirm(); };
    $('confirm-cancel-btn').onclick = () => { overlay.style.display = 'none'; };
    overlay.onclick = (e) => { if (e.target === overlay) overlay.style.display = 'none'; };
}

function showChatActions() {
    showActionSheet([
        { label: '清空对话', onClick: () => { $('chat-msgs').innerHTML = ''; } },
        { label: '删除镜像', danger: true, onClick: () => {
            showConfirm(
                '删除镜像',
                `确定删除 [${currentSlug}]？此操作不可恢复。`,
                () => {
                    api('DELETE', `/exes/${currentSlug}`, {confirm:true}).then(() => {
                        currentSlug = ''; currentName = '';
                        switchTab('chat-list');
                    }).catch(e => showToast('删除失败: ' + e.message, 'error'));
                },
                true
            );
        }},
    ]);
}

// ═══════════════════════════════════════
// 联系人列表
// ═══════════════════════════════════════

async function loadContactList() {
    const el = $('contact-list');
    // 加载微光骨架
    el.innerHTML = Array.from({length:3}, () => `
        <div class="skeleton-row">
            <div class="skeleton-avatar"></div>
            <div class="skeleton-body">
                <div class="skeleton-line long"></div>
                <div class="skeleton-line short"></div>
            </div>
        </div>
    `).join('');
    try {
        const exes = await api('GET', '/exes');
        if (!exes.length) {
            el.innerHTML = `
                <div class="onboarding-card">
                    <div class="onboarding-icon">💬</div>
                    <h3 class="onboarding-title">创建你的第一个镜像</h3>
                    <p class="onboarding-desc">导入聊天记录，让 TA 重新和你对话</p>
                    <button class="btn onboarding-btn" onclick="switchTab('create')">立即创建</button>
                </div>`;
            return;
        }
        el.innerHTML = exes.map(e => {
            const lastMsg = getLastMessage(e.slug);
            const isOnline = Math.random() > 0.6; // 模拟在线状态
            return `
            <div class="contact-item" data-slug="${escHtml(e.slug)}" data-name="${escHtml(e.name)}">
                <div class="contact-avatar" style="background:${avatarColor(e.slug)}">
                    ${(e.name || e.slug)[0]}
                    <span class="online-dot ${isOnline ? 'online' : ''}"></span>
                </div>
                <div class="contact-info">
                    <div class="contact-name">${escHtml(e.name)}</div>
                    <div class="contact-preview">
                        ${lastMsg ? escHtml(lastMsg) : '<span class="preview-source">[点击开始对话]</span>'}
                    </div>
                </div>
                <div class="contact-meta">
                    <div class="contact-time">${(e.created_at || '').slice(0,10)}</div>
                </div>
            </div>
        `}).join('');

        el.querySelectorAll('.contact-item').forEach(item => {
            item.addEventListener('click', () => {
                enterChat(item.dataset.slug, item.dataset.name);
            });
        });

        // 桌面端恢复高亮
        if (isDesktop && currentSlug) {
            document.querySelectorAll('.contact-item').forEach(c => {
                c.classList.toggle('selected', c.dataset.slug === currentSlug);
            });
        }

        // 情感健康贴士
        let tipEl = $('health-tip-container');
        if (!tipEl) {
            tipEl = document.createElement('div');
            tipEl.id = 'health-tip-container';
            el.appendChild(tipEl);
        }
        loadHealthTip();
    } catch(e) {
        if (token) el.innerHTML = '<p class="list-empty">加载失败: ' + escHtml(e.message) + '</p>';
    }
}

function initSearch() {
    const input = $('search-input');
    if (!input) return;
    input.addEventListener('input', () => {
        const q = input.value.trim().toLowerCase();
        document.querySelectorAll('.contact-item').forEach(item => {
            const name = (item.dataset.name || '').toLowerCase();
            const slug = (item.dataset.slug || '').toLowerCase();
            item.style.display = (!q || name.includes(q) || slug.includes(q)) ? '' : 'none';
        });
        // 无匹配提示
        const visible = document.querySelectorAll('.contact-item[style=""]').length;
        const existing = document.querySelector('.search-no-result');
        if (!q) {
            if (existing) existing.remove();
        } else if (visible === 0 && !existing) {
            const hint = document.createElement('p');
            hint.className = 'list-empty search-no-result';
            hint.textContent = '没有匹配的镜像';
            $('contact-list').appendChild(hint);
        } else if (visible > 0 && existing) {
            existing.remove();
        }
    });
}

function enterChat(slug, name) {
    currentSlug = slug;
    currentName = name;
    lastMsgTime = 0;
    $('chat-msgs').innerHTML = '';
    $('chat-msgs').style.display = 'flex';
    $('chat-empty-hint').style.display = 'none';
    $('chat-loading').style.display = 'none';
    $('msg-input').disabled = false;
    $('msg-send').disabled = false;
    $('msg-input').value = '';
    $('msg-input').placeholder = '和 ' + name + ' 说点什么...';

    if (isDesktop) {
        // 确保在聊天模式
        if (desktopTab !== 'chat') {
            document.querySelectorAll('.sidebar-icon').forEach(i => {
                i.classList.toggle('active', i.dataset.desktopTab === 'chat');
            });
        }
        setDesktopMode('chat');
        $('tab-chat-detail').style.display = 'flex';
        $('nav-title').textContent = name || slug;
        $('titlebar-app-name').textContent = name || slug;
        $('nav-action-btn').style.display = 'block';
        // 高亮联系人
        document.querySelectorAll('.contact-item').forEach(c => {
            c.classList.toggle('selected', c.dataset.slug === slug);
        });
    } else {
        switchTab('chat-detail');
    }
    setTimeout(() => $('msg-input').focus(), 300);
}

// ═══════════════════════════════════════
// 表情面板
// ═══════════════════════════════════════

let allStickers = [];
let stickerCategory = 'all';

async function loadStickers() {
    try {
        const data = await api('GET', '/stickers');
        allStickers = data.stickers || [];
        renderStickerGrid();
    } catch(e) { /* 静默失败 */ }
}

function renderStickerGrid() {
    const grid = $('sticker-grid');
    if (!grid) return;
    let filtered;
    if (stickerCategory === 'all') {
        filtered = allStickers;
    } else if (stickerCategory === 'emoji') {
        filtered = allStickers.filter(s => s.type === 'emoji');
    } else if (stickerCategory === 'image') {
        filtered = allStickers.filter(s => s.type === 'image' || s.type === 'gif');
    } else if (stickerCategory === 'custom') {
        filtered = allStickers.filter(s => s.source === 'custom');
    } else {
        filtered = allStickers.filter(s => s.emotion === stickerCategory || s.category === stickerCategory);
    }
    grid.innerHTML = filtered.map(s => {
        if (s.type === 'emoji') {
            return `<div class="sticker-item" data-id="${escHtml(s.id)}" data-type="emoji" title="${escHtml(s.label)}">${s.emoji || ''}</div>`;
        }
        const url = safeStickerUrl(s.url);
        return `<div class="sticker-item" data-id="${escHtml(s.id)}" data-type="${escHtml(s.type)}" title="${escHtml(s.label)}"><img src="${escHtml(url)}" alt="${escHtml(s.label)}" class="sticker-thumb" loading="lazy"></div>`;
    }).join('');

    grid.querySelectorAll('.sticker-item').forEach(item => {
        item.addEventListener('click', () => {
            const stickerId = item.dataset.id;
            sendStickerMessage(stickerId);
        });
    });
}

async function sendStickerMessage(stickerId) {
    if (!currentSlug) return;
    const panel = $('sticker-panel');
    panel.style.display = 'none';
    $('chat-msgs').classList.remove('has-panel');
    $('sticker-toggle-btn').textContent = '😊';

    const msgsEl = $('chat-msgs');
    if ($('chat-empty-hint').style.display !== 'none') {
        $('chat-empty-hint').style.display = 'none';
        msgsEl.style.display = 'flex';
    }

    // 用户侧显示贴纸
    const sticker = allStickers.find(s => s.id === stickerId);
    const userRow = document.createElement('div');
    userRow.className = 'msg-row user';
    const userBubble = document.createElement('div');
    userBubble.className = 'msg sticker';
    if (sticker && sticker.type === 'emoji') {
        userBubble.innerHTML = `<span class="sticker-img" style="font-size:60px;line-height:1.2;display:block;">${sticker.emoji || ''}</span>`;
    } else if (sticker && (sticker.type === 'image' || sticker.type === 'gif')) {
        const url = safeStickerUrl(sticker.url);
        userBubble.innerHTML = `<img src="${escHtml(url)}" alt="${escHtml(sticker.label)}" class="sticker-bubble-img">`;
    } else {
        userBubble.textContent = `[贴纸]`;
    }
    userRow.appendChild(userBubble);
    maybeAddTimestamp(msgsEl);
    msgsEl.appendChild(userRow);
    msgsEl.scrollTop = msgsEl.scrollHeight;

    // 打字指示器
    const typingRow = typingIndicator();
    msgsEl.appendChild(typingRow);
    msgsEl.scrollTop = msgsEl.scrollHeight;

    const hist = buildHistory(msgsEl);
    try {
        const resp = await fetch(`${API}/chat/stream`, {
            method: 'POST',
            headers: authHeaders(),
            body: JSON.stringify({ slug: currentSlug, message: '', sticker_id: stickerId, history: hist.slice(-50) }),
        });
        typingRow.remove();
        await parseChatStream(resp, msgsEl);
    } catch(e) {
        typingRow.remove();
        msgsEl.appendChild(chatBubble('assistant', '消息发送失败，请重试'));
        msgsEl.scrollTop = msgsEl.scrollHeight;
    }
}

// 贴纸上传
$('sticker-upload-btn').addEventListener('click', () => {
    $('sticker-file-input').click();
});

$('sticker-file-input').addEventListener('change', async (e) => {
    const file = e.target.files[0];
    if (!file) return;
    const formData = new FormData();
    formData.append('file', file);
    formData.append('label', file.name.replace(/\.[^.]+$/, ''));
    formData.append('category', 'custom');
    try {
        const resp = await fetch('/api/stickers/upload', {
            method: 'POST',
            headers: authHeaders(false),
            body: formData,
        });
        if (!resp.ok) {
            const err = await resp.json();
            showToast(err.detail || '上传失败', 'error');
            return;
        }
        showToast('贴纸上传成功', 'success');
        await loadStickers();
    } catch(e) {
        showToast('上传失败，请检查网络', 'error');
    }
    e.target.value = '';
});

$('sticker-toggle-btn').addEventListener('click', () => {
    const panel = $('sticker-panel');
    const visible = panel.style.display !== 'none';
    panel.style.display = visible ? 'none' : 'flex';
    $('chat-msgs').classList.toggle('has-panel', !visible);
    $('sticker-toggle-btn').textContent = visible ? '😊' : '⌨';
    setTimeout(() => { $('chat-msgs').scrollTop = $('chat-msgs').scrollHeight; }, 100);
});

document.querySelectorAll('.sticker-tab').forEach(tab => {
    tab.addEventListener('click', () => {
        document.querySelectorAll('.sticker-tab').forEach(t => t.classList.remove('active'));
        tab.classList.add('active');
        stickerCategory = tab.dataset.category;
        renderStickerGrid();
    });
});

// ═══════════════════════════════════════
// 发送消息 (流式)
// ═══════════════════════════════════════

async function sendMessage() {
    const input = $('msg-input'), btn = $('msg-send');
    const msg = input.value.trim();
    if (!msg || !currentSlug) return;

    input.value = ''; input.disabled = true; btn.disabled = true;
    const msgsEl = $('chat-msgs');
    // 首次消息：隐藏空状态，显示消息区
    if ($('chat-empty-hint').style.display !== 'none') {
        $('chat-empty-hint').style.display = 'none';
        msgsEl.style.display = 'flex';
    }

    maybeAddTimestamp(msgsEl);
    const userRow = chatBubble('user', msg);
    msgsEl.appendChild(userRow);
    storeLastMessage(currentSlug, msg);

    // 添加已读标记（初始为已发送）
    const readStatus = document.createElement('div');
    readStatus.className = 'msg-read-status';
    readStatus.textContent = '已发送';
    userRow.appendChild(readStatus);

    // 离线时排队消息
    if (!navigator.onLine) {
        enqueueOfflineMessage(currentSlug, msg);
        msgsEl.appendChild(sysMsg('消息已保存，将在恢复网络后发送'));
        input.disabled = false; btn.disabled = false;
        input.focus();
        msgsEl.scrollTop = msgsEl.scrollHeight;
        return;
    }

    // 打字指示器
    const typingRow = typingIndicator();
    msgsEl.appendChild(typingRow);
    msgsEl.scrollTop = msgsEl.scrollHeight;

    // 导航栏显示「对方正在输入...」
    const navTitle = $('nav-title');
    const origTitle = navTitle ? navTitle.textContent : '';
    if (navTitle) navTitle.textContent = '对方正在输入...';

    // 1秒后更新为「已送达」
    setTimeout(() => {
        readStatus.textContent = '已送达';
    }, 1000);

    // 构建历史
    const hist = buildHistory(msgsEl);

    try {
        const res = await fetch(`${API}/chat/stream`, {
            method: 'POST',
            headers: authHeaders(),
            body: JSON.stringify({slug: currentSlug, message: msg, history: hist.slice(-50)}),
        });

        // 模拟对方"看到消息后思考"的延迟（1-3秒）
        await new Promise(r => setTimeout(r, 1000 + Math.random() * 2000));
        typingRow.remove();
        if (navTitle) navTitle.textContent = origTitle;
        await parseChatStream(res, msgsEl);

        // 更新已读状态
        readStatus.textContent = '已读';
        readStatus.classList.add('read');

        // 添加重新生成按钮到最后一条助手消息
        addRegenerateButton(msgsEl);
    } catch(e) {
        typingRow.remove();
        if (navTitle) navTitle.textContent = origTitle;
        msgsEl.appendChild(sysMsg(e.message));
    }

    input.disabled = false; btn.disabled = false;
    input.focus();
    msgsEl.scrollTop = msgsEl.scrollHeight;
}

function sysMsg(text) {
    const row = document.createElement('div');
    row.className = 'msg-row sys';
    const div = document.createElement('div');
    div.className = 'msg';
    div.textContent = text;
    row.appendChild(div);
    return row;
}

function buildHistory(msgsEl) {
    const hist = [];
    const rows = msgsEl.querySelectorAll('.msg-row');
    let u = null;
    rows.forEach(row => {
        if (row.classList.contains('sys') || row.classList.contains('sticker')) return;
        const msgEl = row.querySelector('.msg');
        if (!msgEl) return;
        if (row.classList.contains('user')) { u = msgEl.textContent; }
        else if (row.classList.contains('assistant') && u) {
            hist.push({role:'user', content:u});
            hist.push({role:'assistant', content:msgEl.textContent});
            u = null;
        }
    });
    return hist;
}

function storeLastMessage(slug, text) {
    const key = 'ex-lastmsg-' + slug;
    const preview = text.replace(/\n/g, ' ').slice(0, 50);
    localStorage.setItem(key, preview);
}

function getLastMessage(slug) {
    return localStorage.getItem('ex-lastmsg-' + slug) || '';
}

function addRegenerateButton(msgsEl) {
    // 移除旧的重新生成按钮
    const oldBtn = msgsEl.querySelector('.regenerate-btn');
    if (oldBtn) oldBtn.remove();

    // 找到最后一条助手消息
    const rows = msgsEl.querySelectorAll('.msg-row.assistant');
    if (rows.length === 0) return;
    const lastRow = rows[rows.length - 1];

    const btn = document.createElement('button');
    btn.className = 'regenerate-btn';
    btn.textContent = '🔄 重新生成';
    btn.onclick = async () => {
        btn.disabled = true;
        btn.textContent = '生成中…';
        // 删除最后一条助手消息
        lastRow.remove();
        // 重新发送最后一条用户消息
        const userRows = msgsEl.querySelectorAll('.msg-row.user');
        if (userRows.length > 0) {
            const lastUserMsg = userRows[userRows.length - 1].querySelector('.msg').textContent;
            $('msg-input').value = lastUserMsg;
            await sendMessage();
        }
    };
    lastRow.appendChild(btn);
}

function chatBubble(role, text) {
    const row = document.createElement('div');
    row.className = 'msg-row ' + role;
    row.dataset.timestamp = Date.now();

    if (role === 'assistant' && currentName) {
        const avatar = document.createElement('div');
        avatar.className = 'msg-avatar';
        avatar.style.background = avatarColor(currentSlug);
        avatar.textContent = (currentName || currentSlug)[0];
        row.appendChild(avatar);
    }

    const div = document.createElement('div');
    div.className = 'msg ' + role;
    div.textContent = text;
    row.appendChild(div);

    // 长按/右键菜单
    div.addEventListener('contextmenu', (e) => {
        e.preventDefault();
        showMsgMenu(row, e);
    });
    // 移动端长按
    let longPressTimer;
    div.addEventListener('touchstart', (e) => {
        longPressTimer = setTimeout(() => {
            showMsgMenu(row, e.touches[0]);
        }, 500);
    }, {passive: true});
    div.addEventListener('touchend', () => clearTimeout(longPressTimer));
    div.addEventListener('touchmove', () => clearTimeout(longPressTimer));

    return row;
}

// ── 消息操作菜单 ──
let currentMenuRow = null;
let quoteMode = false;

function showMsgMenu(row, e) {
    currentMenuRow = row;
    const isUser = row.classList.contains('user');
    const isSys = row.classList.contains('sys');
    if (isSys) return;

    const msgEl = row.querySelector('.msg');
    const text = msgEl ? msgEl.textContent : '';

    // 创建菜单
    let menu = $('msg-action-menu');
    if (!menu) {
        menu = document.createElement('div');
        menu.id = 'msg-action-menu';
        menu.className = 'msg-action-menu';
        menu.style.display = 'none';
        document.body.appendChild(menu);
    }

    const options = [
        {label: '复制', action: () => { navigator.clipboard.writeText(text); showToast('已复制', 'success'); }},
        {label: '引用', action: () => startQuote(text) },
    ];
    if (isUser) {
        const age = Date.now() - parseInt(row.dataset.timestamp || '0');
        if (age < 2 * 60 * 1000) { // 2 分钟内可撤回
            options.push({label: '撤回', action: () => recallMessage(row), danger: true});
        }
    }

    menu.innerHTML = options.map(o =>
        `<button class="msg-menu-item${o.danger ? ' danger' : ''}">${o.label}</button>`
    ).join('');
    menu.querySelectorAll('.msg-menu-item').forEach((btn, i) => {
        btn.onclick = () => { menu.style.display = 'none'; options[i].action(); };
    });

    // 定位菜单
    const x = Math.min(e.clientX || 100, window.innerWidth - 140);
    const y = Math.min(e.clientY || 100, window.innerHeight - 160);
    menu.style.left = x + 'px';
    menu.style.top = y + 'px';
    menu.style.display = 'flex';
}

// 点击其他地方关闭菜单
document.addEventListener('click', () => {
    const menu = $('msg-action-menu');
    if (menu) menu.style.display = 'none';
});

function startQuote(text) {
    const input = $('msg-input');
    const preview = text.replace(/\n/g, ' ').slice(0, 50);
    input.value = `> ${preview}${text.length > 50 ? '...' : ''}\n`;
    input.focus();
    quoteMode = true;
}

function recallMessage(row) {
    const msgEl = row.querySelector('.msg');
    if (msgEl) {
        msgEl.textContent = '你撤回了一条消息';
        msgEl.className = 'msg sys recalled-msg';
        row.className = 'msg-row sys';
    }
    showToast('消息已撤回', 'info');
}

function typingIndicator() {
    const row = document.createElement('div');
    row.className = 'msg-row assistant';
    if (currentName) {
        const avatar = document.createElement('div');
        avatar.className = 'msg-avatar';
        avatar.style.background = avatarColor(currentSlug);
        avatar.textContent = (currentName || currentSlug)[0];
        row.appendChild(avatar);
    }
    const typing = document.createElement('div');
    typing.className = 'typing-indicator';
    typing.innerHTML = '<span class="dot"></span><span class="dot"></span><span class="dot"></span>';
    row.appendChild(typing);
    return row;
}

function avatarColor(slug) {
    const colors = ['#07C160','#FA9C3C','#576B95','#FA5151','#1485EE','#E67E22'];
    let hash = 0;
    for (let i = 0; i < (slug||'a').length; i++) hash = slug.charCodeAt(i) + ((hash << 5) - hash);
    return colors[Math.abs(hash) % colors.length];
}

function stickerMsg(stickerId) {
    const row = document.createElement('div');
    row.className = 'msg-row assistant sticker';
    if (currentName) {
        const avatar = document.createElement('div');
        avatar.className = 'msg-avatar';
        avatar.style.background = avatarColor(currentSlug);
        avatar.textContent = (currentName || currentSlug)[0];
        row.appendChild(avatar);
    }
    const div = document.createElement('div');
    div.className = 'msg sticker';
    const sticker = allStickers.find(s => s.id === stickerId);
    if (sticker && (sticker.type === 'image' || sticker.type === 'gif')) {
        const url = safeStickerUrl(sticker.url);
        div.innerHTML = `<img src="${escHtml(url)}" alt="${escHtml(sticker.label)}" class="sticker-bubble-img">`;
    } else {
        const emojiMap = {};
        allStickers.forEach(s => { if (s.emoji) emojiMap[s.id] = s.emoji; });
        const emoji = (sticker && sticker.emoji) || emojiMap[stickerId] || '😊';
        div.innerHTML = `<span class="sticker-img" style="font-size:60px;line-height:1.2;display:block;">${emoji}</span>`;
    }
    row.appendChild(div);
    return row;
}

$('msg-send').addEventListener('click', sendMessage);
$('msg-input').addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
});

// ═══════════════════════════════════════
// 通讯录
// ═══════════════════════════════════════

function showContactProfile() {
    const el = $('contact-profile');
    if (!currentSlug) {
        el.innerHTML = '<p class="list-empty">请先在聊天列表选择镜像</p>';
        return;
    }
    api('GET', '/exes').then(exes => {
        const exe = exes.find(e => e.slug === currentSlug);
        if (!exe) { el.innerHTML = '<p class="list-empty">镜像不存在</p>'; return; }
        el.innerHTML = `
            <div class="profile-card">
                <div class="profile-avatar">${(exe.name || exe.slug)[0]}</div>
                <div class="profile-name">${escHtml(exe.name)}</div>
                <div class="profile-id">ID: ${escHtml(exe.slug)}</div>
                <div class="profile-signature">${escHtml(exe.state === 'completed' ? '这个人还活在数字世界里' : '构建中...')}</div>
                <button class="profile-moments-btn" onclick="viewMoments()">朋友圈</button>
            </div>
        `;
    }).catch(() => {
        el.innerHTML = '<p class="list-empty">加载失败</p>';
    });
}

// ═══════════════════════════════════════
// 朋友圈
// ═══════════════════════════════════════

let viewingMoments = false;

$('moments-entry').addEventListener('click', () => {
    viewingMoments = !viewingMoments;
    $('moments-timeline').style.display = viewingMoments ? 'block' : 'none';
    if (viewingMoments) loadMoments();
});

function viewMoments() {
    if (isDesktop) {
        // 桌面端切换到发现模式
        document.querySelectorAll('.sidebar-icon').forEach(i => {
            i.classList.toggle('active', i.dataset.desktopTab === 'discover');
        });
        setDesktopMode('discover');
    } else {
        switchTab('discover');
    }
    viewingMoments = true;
    $('moments-timeline').style.display = 'block';
    loadMoments();
}

async function loadMoments() {
    const el = $('moments-list');
    if (!currentSlug) {
        el.innerHTML = '<p style="color:#888;text-align:center;padding:40px 0;">请先选择一个镜像</p>';
        return;
    }
    try {
        const data = await api('GET', `/exes/${currentSlug}/moments`);
        const moments = data.moments || [];
        if (!moments.length) {
            el.innerHTML = '<p style="color:#888;text-align:center;padding:40px 0;">ta 还没有发朋友圈</p>';
            return;
        }
        el.innerHTML = moments.map(m => `
            <div class="moment-item">
                <div class="moment-avatar">${(currentName || currentSlug)[0]}</div>
                <div class="moment-body">
                    <div class="moment-name">${escHtml(currentName || currentSlug)}</div>
                    <div class="moment-content">${escHtml(m.content)}</div>
                    <div class="moment-time">${escHtml(m.created_at || '')}</div>
                </div>
            </div>
        `).join('');
    } catch(e) {
        el.innerHTML = '<p style="color:#888;text-align:center;padding:40px 0;">朋友圈暂不可用</p>';
    }
}

// ═══════════════════════════════════════
// 钱包入口
// ═══════════════════════════════════════

$('wallet-entry').addEventListener('click', () => {
    if (!currentSlug) { showToast('请先在聊天中选择一个镜像', 'warning'); return; }
    switchTab('wallet');
});

$('create-entry').addEventListener('click', () => {
    switchTab('create');
});

$('logout-entry').addEventListener('click', () => {
    if (confirm('确定退出登录？')) logout();
});

// ═══════════════════════════════════════
// 关系阶段管理
// ═══════════════════════════════════════

const STAGE_LABELS = {
    dating: '热恋期 💕',
    conflicted: '磨合期 ⚡',
    broken: '分手期 💔',
    healing: '治愈期 🌱',
};

async function loadStage() {
    if (!currentSlug) return;
    try {
        const data = await api('GET', `/exes/${currentSlug}/stage`);
        updateStageUI(data.stage);
    } catch(e) { /* ignore */ }
}

function updateStageUI(stage) {
    const el = $('stage-indicator');
    if (el) el.textContent = STAGE_LABELS[stage] || stage;
}

async function setStage(stage) {
    if (!currentSlug) return;
    try {
        await api('PUT', `/exes/${currentSlug}/stage?stage=${stage}`);
        updateStageUI(stage);
        showToast(`关系阶段已切换为「${STAGE_LABELS[stage]}」`, 'success');
    } catch(e) {
        showToast('切换失败: ' + e.message, 'error');
    }
}

async function checkStageSuggestion() {
    if (!currentSlug) return;
    try {
        const data = await api('GET', `/exes/${currentSlug}/stage/suggest`);
        if (data.suggestion && data.suggestion !== data.current_stage) {
            // 在聊天中显示建议
            const msgsEl = $('chat-msgs');
            if (msgsEl && msgsEl.style.display !== 'none') {
                const row = document.createElement('div');
                row.className = 'msg-row sys';
                row.innerHTML = `<div class="msg stage-suggestion">💡 ${escHtml(data.reason)}，要切换到「${STAGE_LABELS[data.suggestion]}」吗？ <button class="stage-accept-btn" onclick="setStage('${data.suggestion}')">切换</button></div>`;
                msgsEl.appendChild(row);
                msgsEl.scrollTop = msgsEl.scrollHeight;
            }
        }
    } catch(e) { /* ignore */ }
}

// ═══════════════════════════════════════
// 对话搜索
// ═══════════════════════════════════════

async function searchMessages(query) {
    if (!currentSlug || !query.trim()) return [];
    try {
        const data = await api('GET', `/exes/${currentSlug}/messages/search?q=${encodeURIComponent(query)}`);
        return data.results || [];
    } catch(e) {
        return [];
    }
}

// ═══════════════════════════════════════
// 对话截图与分享
// ═══════════════════════════════════════

async function generateScreenshot() {
    const msgsEl = $('chat-msgs');
    if (!msgsEl || msgsEl.style.display === 'none') {
        showToast('请先打开对话', 'warning');
        return;
    }

    showToast('正在生成截图...', 'info');

    try {
        // 动态加载 html2canvas
        if (!window.html2canvas) {
            const script = document.createElement('script');
            script.src = 'https://cdn.jsdelivr.net/npm/html2canvas@1.4.1/dist/html2canvas.min.js';
            document.head.appendChild(script);
            await new Promise((resolve, reject) => {
                script.onload = resolve;
                script.onerror = reject;
            });
        }

        const canvas = await window.html2canvas(msgsEl, {
            backgroundColor: '#0a0a12',
            scale: 2,
            useCORS: true,
        });

        // 下载截图
        const link = document.createElement('a');
        link.download = `ex-memory-${currentSlug || 'chat'}-${Date.now()}.png`;
        link.href = canvas.toDataURL('image/png');
        link.click();

        showToast('截图已保存', 'success');
    } catch(e) {
        showToast('截图失败: ' + e.message, 'error');
    }
}

async function generateShareLink() {
    if (!currentSlug) {
        showToast('请先选择一个镜像', 'warning');
        return;
    }

    try {
        const data = await api('POST', `/exes/${currentSlug}/share`);
        if (data.url) {
            // 复制链接
            await navigator.clipboard.writeText(data.url);
            showToast('分享链接已复制', 'success');
        }
    } catch(e) {
        showToast('生成分享链接失败', 'error');
    }
}

// ═══════════════════════════════════════
// 对话统计
// ═══════════════════════════════════════

async function loadStats() {
    if (!currentSlug) {
        showToast('请先选择一个镜像', 'warning');
        return;
    }
    try {
        const [stats, emotion] = await Promise.all([
            api('GET', `/exes/${currentSlug}/stats`),
            api('GET', `/exes/${currentSlug}/emotion`).catch(() => ({analysis: {overall: {score: 0}}, curve: []})),
        ]);

        const el = $('chat-msgs');
        el.style.display = 'block';
        el.innerHTML = `
            <div class="stats-container">
                <h3 class="stats-title">对话统计</h3>
                <div class="stats-grid">
                    <div class="stat-card">
                        <div class="stat-value">${stats.total_messages}</div>
                        <div class="stat-label">总消息数</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-value">${stats.user_messages}</div>
                        <div class="stat-label">你发送的</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-value">${stats.assistant_messages}</div>
                        <div class="stat-label">TA 回复的</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-value">${Math.round((emotion.analysis?.overall?.score || 0) * 100)}%</div>
                        <div class="stat-label">情感指数</div>
                    </div>
                </div>

                <!-- 情感曲线 -->
                <h4 class="stats-subtitle">情感曲线</h4>
                <div class="emotion-curve-container">
                    <canvas id="emotion-canvas" width="400" height="120"></canvas>
                </div>

                <!-- 活跃时段 -->
                <h4 class="stats-subtitle">活跃时段</h4>
                <div class="stats-periods">
                    ${Object.entries(stats.time_periods || {}).map(([period, count]) => `
                        <div class="period-bar">
                            <span class="period-label">${period}</span>
                            <div class="period-fill" style="width: ${Math.min(count * 5, 100)}%"></div>
                            <span class="period-count">${count}</span>
                        </div>
                    `).join('')}
                </div>
            </div>`;

        // 绘制情感曲线
        drawEmotionCurve(emotion.curve || []);
        $('chat-empty-hint').style.display = 'none';
    } catch(e) {
        showToast('加载统计失败: ' + e.message, 'error');
    }
}

function drawEmotionCurve(curve) {
    const canvas = $('emotion-canvas');
    if (!canvas || curve.length === 0) return;
    const ctx = canvas.getContext('2d');
    const w = canvas.width, h = canvas.height;
    const padding = 20;

    ctx.clearRect(0, 0, w, h);

    // 背景网格
    ctx.strokeStyle = 'rgba(124, 108, 255, 0.08)';
    ctx.lineWidth = 1;
    for (let y = padding; y <= h - padding; y += 30) {
        ctx.beginPath();
        ctx.moveTo(padding, y);
        ctx.lineTo(w - padding, y);
        ctx.stroke();
    }

    // 中线（0 分）
    ctx.strokeStyle = 'rgba(124, 108, 255, 0.2)';
    ctx.beginPath();
    ctx.moveTo(padding, h / 2);
    ctx.lineTo(w - padding, h / 2);
    ctx.stroke();

    // 情感曲线
    const stepX = (w - 2 * padding) / Math.max(curve.length - 1, 1);
    ctx.strokeStyle = '#7c6cff';
    ctx.lineWidth = 2;
    ctx.beginPath();
    curve.forEach((point, i) => {
        const x = padding + i * stepX;
        const y = h / 2 - point.score * (h / 2 - padding) * 0.8;
        if (i === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
    });
    ctx.stroke();

    // 填充区域
    ctx.lineTo(padding + (curve.length - 1) * stepX, h / 2);
    ctx.lineTo(padding, h / 2);
    ctx.closePath();
    const gradient = ctx.createLinearGradient(0, 0, 0, h);
    gradient.addColorStop(0, 'rgba(124, 108, 255, 0.15)');
    gradient.addColorStop(0.5, 'rgba(124, 108, 255, 0.02)');
    gradient.addColorStop(1, 'rgba(124, 108, 255, 0.15)');
    ctx.fillStyle = gradient;
    ctx.fill();

    // 标签
    ctx.fillStyle = 'rgba(122, 116, 144, 0.6)';
    ctx.font = '10px -apple-system';
    ctx.textAlign = 'right';
    ctx.fillText('正面', padding - 4, padding + 4);
    ctx.fillText('负面', padding - 4, h - padding + 4);
}

// ═══════════════════════════════════════
// 创建镜像
// ═══════════════════════════════════════

$('create-submit').addEventListener('click', async () => {
    const name = $('create-name').value.trim();
    const basic = $('create-basic').value.trim();
    const pers  = $('create-personality').value.trim();
    const fileInput = $('create-file');
    const result = $('create-result');
    const btn = $('create-submit');
    if (!name) { result.textContent = '请输入代号'; result.style.color = 'var(--danger)'; return; }

    const slug = name.toLowerCase().replace(/\s+/g, '_');
    const hasFile = fileInput.files.length > 0;
    result.textContent = '正在创建…'; result.style.color = '#888';
    btn.textContent = '创建中…'; btn.disabled = true;

    try {
        await api('POST', '/exes', {name, slug, answers: [basic, pers]});
        let importMsg = '';
        if (hasFile) {
            const file = fileInput.files[0];
            result.textContent = '正在导入聊天记录…';
            const formData = new FormData();
            formData.append('file', file);
            if (name) formData.append('target_name', name);
            const headers = {};
            if (token) headers['Authorization'] = `Bearer ${token}`;
            const importRes = await fetch(`${API}/exes/${slug}/import`, {method:'POST', headers, body: formData});
            if (importRes.status === 401) { logout(); return; }
            const importData = await importRes.json().catch(()=>({}));
            importMsg = importRes.ok ? (' ' + importData.message) : ' 但导入失败';
        }
        result.textContent = `镜像 [${name}] 创建成功！${importMsg}`;
        result.style.color = 'var(--wechat-green)';
        $('create-name').value = ''; $('create-basic').value = '';
        $('create-personality').value = ''; fileInput.value = '';
        $('create-file-name').textContent = '未选择文件';
        loadContactList();
    } catch(e) {
        result.textContent = '创建失败: ' + e.message;
        result.style.color = 'var(--danger)';
    } finally {
        btn.textContent = '创建镜像'; btn.disabled = false;
    }
});

$('create-file').addEventListener('change', () => {
    const f = $('create-file').files[0];
    $('create-file-name').textContent = f ? f.name : '未选择文件';
});

// ═══════════════════════════════════════
// 红包系统
// ═══════════════════════════════════════

function _wrapAssistantMsg(innerDiv) {
    const row = document.createElement('div');
    row.className = 'msg-row assistant';
    if (currentName) {
        const avatar = document.createElement('div');
        avatar.className = 'msg-avatar';
        avatar.style.background = avatarColor(currentSlug);
        avatar.textContent = (currentName || currentSlug)[0];
        row.appendChild(avatar);
    }
    row.appendChild(innerDiv);
    return row;
}

function redPacketBubble(rp) {
    const div = document.createElement('div');
    div.className = 'msg redpacket';
    div.dataset.rpId = rp.id;
    div.innerHTML = `
        <div class="rp-bubble">
            <div class="rp-bubble-icon">🧧</div>
            <div class="rp-bubble-info">
                <div class="rp-bubble-note">${escHtml(rp.note)}</div>
                <div class="rp-bubble-hint">红包</div>
            </div>
        </div>
    `;
    div.addEventListener('click', () => openRedPacketOverlay(rp));
    return _wrapAssistantMsg(div);
}

let currentOpenRpId = null;

function openRedPacketOverlay(rp) {
    currentOpenRpId = rp.id;
    $('rp-sender').textContent = currentName || currentSlug;
    $('rp-note').textContent = rp.note;
    $('rp-amount').textContent = '¥ ' + rp.amount.toFixed(2);
    $('rp-done-note').textContent = rp.note;

    // 重置状态
    $('redpacket-bottom').style.display = 'flex';
    $('redpacket-result').style.display = 'none';
    $('rp-open-btn').classList.remove('opened');
    $('coins-container').innerHTML = '';

    $('redpacket-overlay').style.display = 'flex';
    $('rp-open-btn').focus();
}

$('rp-open-btn').addEventListener('click', async function() {
    const rpId = currentOpenRpId;
    if (!rpId) return;

    // 旋转按钮动画
    this.classList.add('opened');
    this.textContent = '...';

    // 撒金币
    spawnCoins();

    try {
        const data = await api('POST', `/exes/${currentSlug}/redpacket/${rpId}/open`);
        $('rp-amount').textContent = '¥ ' + data.amount.toFixed(2);
        $('rp-done-note').textContent = data.note;

        // 更新气泡状态
        const bubble = document.querySelector(`.msg-row .msg.redpacket[data-rp-id="${rpId}"]`);
        if (bubble) {
            bubble.querySelector('.rp-bubble-note').textContent = '已领取';
            bubble.querySelector('.rp-bubble-hint').textContent = '红包已打开';
            bubble.style.opacity = '0.6';
        }
    } catch(e) {
        $('rp-amount').textContent = '领取失败';
        $('rp-done-note').textContent = e.message;
    }

    setTimeout(() => {
        $('redpacket-bottom').style.display = 'none';
        $('redpacket-result').style.display = 'block';
    }, 800);
});

$('rp-close-btn').addEventListener('click', closeRedPacketOverlay);

function closeRedPacketOverlay() {
    $('redpacket-overlay').style.display = 'none';
    if (currentSlug) loadWallet();
    $('msg-input').focus();
}

// Escape 键关闭红包弹窗
document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && $('redpacket-overlay').style.display !== 'none') {
        closeRedPacketOverlay();
    }
});

function spawnCoins() {
    const container = $('coins-container');
    const emojis = ['🪙', '✨', '💛', '⭐', '💰'];
    for (let i = 0; i < 18; i++) {
        const coin = document.createElement('span');
        coin.className = 'coin';
        coin.textContent = emojis[i % emojis.length];
        coin.style.setProperty('--dx', (Math.random() * 200 - 100) + 'px');
        coin.style.setProperty('--dy', (Math.random() * -160 - 40) + 'px');
        coin.style.animationDelay = (Math.random() * 0.3) + 's';
        coin.style.animationDuration = (1 + Math.random() * 0.6) + 's';
        container.appendChild(coin);
    }
    setTimeout(() => { container.innerHTML = ''; }, 1500);
}

// ═══════════════════════════════════════
// 转账系统
// ═══════════════════════════════════════

function transferBubble(tx) {
    const div = document.createElement('div');
    div.className = 'msg transfer';
    div.dataset.txId = tx.id;
    const isIncoming = tx.direction === 'ta_to_me';
    div.innerHTML = `
        <div class="tx-bubble">
            <div class="tx-bubble-icon">💸</div>
            <div class="tx-bubble-info">
                <div class="tx-bubble-note">${escHtml(tx.note || '转账')}</div>
                <div class="tx-bubble-status">${isIncoming ? '转账给你' : '向你转账'} · 待收款</div>
                <div class="tx-bubble-amount">¥ ${tx.amount.toFixed(2)}</div>
            </div>
        </div>
    `;
    div.addEventListener('click', async () => {
        if (!confirm(`确认收款 ¥${tx.amount.toFixed(2)}？`)) return;
        try {
            await api('POST', `/exes/${currentSlug}/transfer/${tx.id}/confirm`, {action:'receive'});
            div.querySelector('.tx-bubble-status').textContent = '已收款';
            div.style.opacity = '0.6';
            loadWallet();
        } catch(e) { showToast('收款失败: ' + e.message, 'error'); }
    });
    return _wrapAssistantMsg(div);
}

// ═══════════════════════════════════════
// 钱包页
// ═══════════════════════════════════════

async function loadWallet() {
    if (!currentSlug) return;
    try {
        const data = await api('GET', `/exes/${currentSlug}/wallet`);
        $('wallet-balance').textContent = '¥ ' + data.balance.toFixed(2);

        const txList = $('wallet-tx-list');
        const txs = data.transactions || [];
        if (!txs.length) {
            txList.innerHTML = '<p class="list-empty">暂无交易记录</p>';
        } else {
            txList.innerHTML = txs.slice().reverse().map(tx => {
                const isIncome = tx.type.includes('received');
                return `
                    <div class="wallet-tx-item">
                        <div class="wallet-tx-icon ${isIncome ? 'income' : 'outgo'}">
                            ${isIncome ? '↓' : '↑'}
                        </div>
                        <div class="wallet-tx-info">
                            <div class="wallet-tx-name">${txTypeLabel(tx.type)}</div>
                            <div class="wallet-tx-time">${(tx.time || '').slice(0,16)}</div>
                        </div>
                        <div class="wallet-tx-amount ${isIncome ? 'income' : 'outgo'}">
                            ${isIncome ? '+' : '-'}¥ ${tx.amount.toFixed(2)}
                        </div>
                    </div>
                `;
            }).join('');
        }
    } catch(e) {
        $('wallet-tx-list').innerHTML = '<p class="list-empty">加载失败</p>';
    }
}

function txTypeLabel(type) {
    const map = {
        'red_packet_received': '收到红包',
        'red_packet_sent': '发出红包',
        'transfer_received': '收到转账',
        'transfer_sent': '转出',
    };
    return map[type] || type;
}

$('wallet-redpacket-btn').addEventListener('click', async () => {
    if (!currentSlug) return;
    try {
        const data = await api('GET', `/exes/${currentSlug}/wallet`);
        const txs = (data.transactions || []).filter(t => t.type.includes('red_packet'));
        renderFilteredTx(txs);
    } catch(e) {}
});

$('wallet-transfer-btn').addEventListener('click', async () => {
    if (!currentSlug) return;
    try {
        const data = await api('GET', `/exes/${currentSlug}/wallet`);
        const txs = (data.transactions || []).filter(t => t.type.includes('transfer'));
        renderFilteredTx(txs);
    } catch(e) {}
});

function renderFilteredTx(txs) {
    const txList = $('wallet-tx-list');
    if (!txs.length) {
        txList.innerHTML = '<p class="list-empty">暂无相关记录</p>';
        return;
    }
    txList.innerHTML = txs.slice().reverse().map(tx => {
        const isIncome = tx.type.includes('received');
        return `
            <div class="wallet-tx-item">
                <div class="wallet-tx-icon ${isIncome ? 'income' : 'outgo'}">${isIncome ? '↓' : '↑'}</div>
                <div class="wallet-tx-info">
                    <div class="wallet-tx-name">${txTypeLabel(tx.type)}</div>
                    <div class="wallet-tx-time">${(tx.time || '').slice(0,16)}</div>
                </div>
                <div class="wallet-tx-amount ${isIncome ? 'income' : 'outgo'}">
                    ${isIncome ? '+' : '-'}¥ ${tx.amount.toFixed(2)}
                </div>
            </div>
        `;
    }).join('');
}

// ═══════════════════════════════════════
// 工具
// ═══════════════════════════════════════

function escHtml(s) {
    return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// ═══════════════════════════════════════
// 语音消息
// ═══════════════════════════════════════

let isVoiceMode = false;

function initVoiceToggle() {
    // 检查浏览器是否支持 Web Speech API
    const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (SpeechRecognition) {
        try {
            speechRecognition = new SpeechRecognition();
            speechRecognition.lang = 'zh-CN';
            speechRecognition.interimResults = false;
            speechRecognition.maxAlternatives = 1;
        } catch(e) { /* 浏览器不支持语音识别 */ }
    }

    const voiceBtn = $('voice-toggle-btn');
    if (!voiceBtn) return;
    voiceBtn.addEventListener('click', () => {
        isVoiceMode = !isVoiceMode;
        if (isVoiceMode) {
            $('voice-toggle-btn').textContent = '⌨';
            $('msg-input').style.display = 'none';
            $('voice-record-btn').style.display = 'flex';
        } else {
            $('voice-toggle-btn').textContent = '🎤';
            $('msg-input').style.display = '';
            $('voice-record-btn').style.display = 'none';
        }
    });

    const recordBtn = $('voice-record-btn');
    let recordTimer, recordDuration, voiceRecording = false;
    let mediaRecorder = null;
    let audioChunks = [];

    recordBtn.addEventListener('touchstart', startVoiceRecord, {passive: false});
    recordBtn.addEventListener('mousedown', e => {
        if (e.sourceCapabilities && e.sourceCapabilities.firesTouchEvents) return;
        startVoiceRecord(e);
    });
    recordBtn.addEventListener('touchend', stopVoiceRecord);
    recordBtn.addEventListener('mouseup', stopVoiceRecord);
    recordBtn.addEventListener('mouseleave', stopVoiceRecord);

    async function startVoiceRecord(e) {
        if (!isVoiceMode || voiceRecording) return;
        e.preventDefault();
        voiceRecording = true;
        audioChunks = [];
        recordBtn.classList.add('recording');
        recordBtn.innerHTML = '<span class="voice-recording-dot"></span> 松开 发送';
        recordDuration = 0;
        recordTimer = setInterval(() => { recordDuration++; updateRecordTimer(); }, 1000);

        // 启动 MediaRecorder 录制真实音频
        try {
            const stream = await navigator.mediaDevices.getUserMedia({audio: true});
            mediaRecorder = new MediaRecorder(stream, {mimeType: 'audio/webm;codecs=opus'});
            mediaRecorder.ondataavailable = (e) => {
                if (e.data.size > 0) audioChunks.push(e.data);
            };
            mediaRecorder.start();
        } catch(err) {
            console.warn('MediaRecorder not available:', err);
        }

        // 启动语音识别
        if (speechRecognition) {
            try { speechRecognition.start(); } catch(e) {}
        }
    }

    function updateRecordTimer() {
        const min = Math.floor(recordDuration / 60);
        const sec = recordDuration % 60;
        const timeStr = `${min}:${sec.toString().padStart(2, '0')}`;
        recordBtn.innerHTML = `<span class="voice-recording-dot"></span> ${timeStr} 松开 发送`;
    }

    function stopVoiceRecord(e) {
        if (!voiceRecording) return;
        e.preventDefault();
        voiceRecording = false;
        recordBtn.classList.remove('recording');
        recordBtn.innerHTML = '<span class="voice-recording-dot"></span> 按住 说话';
        clearInterval(recordTimer);

        // 停止 MediaRecorder
        if (mediaRecorder && mediaRecorder.state !== 'inactive') {
            mediaRecorder.stop();
            mediaRecorder.stream.getTracks().forEach(t => t.stop());
        }

        // 停止语音识别
        if (speechRecognition) {
            try { speechRecognition.stop(); } catch(e) {}
        }

        if (recordDuration > 0 && currentSlug) {
            const dur = Math.min(recordDuration, 60);
            const msgsEl = $('chat-msgs');
            msgsEl.appendChild(voiceBubble('user', dur));
            msgsEl.scrollTop = msgsEl.scrollHeight;
            // 等待语音识别结果，超时 800ms 后发送
            const waitAndSend = () => {
                const sttText = speechRecognition?._lastResult || '';
                sendVoiceMessage(dur, sttText);
                if (speechRecognition) speechRecognition._lastResult = '';
            };
            if (speechRecognition && speechRecognition._lastResult) {
                waitAndSend();
            } else {
                setTimeout(waitAndSend, 800);
            }
        }
    }

    // 缓存语音识别结果
    if (speechRecognition) {
        speechRecognition.onresult = (event) => {
            const result = event.results[event.results.length - 1];
            if (result.isFinal) {
                speechRecognition._lastResult = result[0].transcript;
            }
        };
        speechRecognition.onerror = () => {};
    }

    // ── 加号按钮：展开更多功能 ──
    const plusBtn = $('plus-btn');
    const plusPanel = $('plus-panel');
    if (plusBtn && plusPanel) {
        plusBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            const visible = plusPanel.style.display !== 'none';
            plusPanel.style.display = visible ? 'none' : 'flex';
        });
        document.addEventListener('click', () => {
            plusPanel.style.display = 'none';
        });
    }

    // ── 图片上传 ──
    const imageInput = $('image-file-input');
    if (imageInput) {
        imageInput.addEventListener('change', async (e) => {
            const file = e.target.files[0];
            if (!file || !currentSlug) return;
            const msgsEl = $('chat-msgs');
            if (!msgsEl) return;

            // 显示图片预览
            const reader = new FileReader();
            reader.onload = async (ev) => {
                const row = document.createElement('div');
                row.className = 'msg-row user';
                row.dataset.timestamp = Date.now();
                row.innerHTML = `<div class="msg user image-msg"><img src="${ev.target.result}" class="msg-image" alt="图片"></div>`;
                msgsEl.appendChild(row);
                msgsEl.scrollTop = msgsEl.scrollHeight;

                // 添加已读标记
                const readStatus = document.createElement('div');
                readStatus.className = 'msg-read-status';
                readStatus.textContent = '已发送';
                row.appendChild(readStatus);

                // 发送图片消息
                try {
                    const formData = new FormData();
                    formData.append('file', file);
                    const resp = await fetch(`${API}/chat`, {
                        method: 'POST',
                        headers: authHeaders(false),
                        body: JSON.stringify({slug: currentSlug, message: '[图片]'}),
                    });
                    readStatus.textContent = '已读';
                    readStatus.classList.add('read');
                } catch(err) {
                    readStatus.textContent = '发送失败';
                }
            };
            reader.readAsDataURL(file);
            imageInput.value = '';
        });
    }
}

function voiceBubble(role, duration) {
    const row = document.createElement('div');
    row.className = 'msg-row ' + role;
    const div = document.createElement('div');
    div.className = 'msg voice';
    div.innerHTML = `
        <span class="voice-icon">🔊</span>
        <span class="voice-wave">${'<span class="bar"></span>'.repeat(7)}</span>
        <span class="voice-dur">${duration}″</span>
    `;
    div.addEventListener('click', () => {
        div.classList.toggle('playing');
        div.classList.toggle('paused');
    });
    row.appendChild(div);
    return row;
}

async function sendVoiceMessage(duration, sttText) {
    const msgsEl = $('chat-msgs');
    if ($('chat-empty-hint').style.display !== 'none') {
        $('chat-empty-hint').style.display = 'none';
        msgsEl.style.display = 'flex';
    }
    const typingRow = typingIndicator();
    msgsEl.appendChild(typingRow);
    msgsEl.scrollTop = msgsEl.scrollHeight;

    // 有 STT 结果则发送文字，否则发送语音标记
    const message = sttText ? sttText : `[语音消息 ${duration}秒]`;

    try {
        const hist = buildHistory(msgsEl);
        const headers = {'Content-Type': 'application/json'};
        if (token) headers['Authorization'] = `Bearer ${token}`;

        const res = await fetch(`${API}/chat/stream`, {
            method: 'POST', headers,
            body: JSON.stringify({slug: currentSlug, message, history: hist.slice(-50)}),
        });
        if (res.status === 401) { logout(); return; }

        typingRow.remove();
        const replyRow = chatBubble('assistant', '');
        msgsEl.appendChild(replyRow);
        const assistantDiv = replyRow.querySelector('.msg');

        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        while (true) {
            const {done, value} = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, {stream: true});
            const lines = buffer.split('\n');
            buffer = lines.pop() || '';
            for (const line of lines) {
                if (!line.startsWith('data: ')) continue;
                const d = line.slice(6);
                if (d === '[DONE]') continue;
                try {
                    const item = JSON.parse(d);
                    if (item.error) { assistantDiv.textContent = item.error; replyRow.className = 'msg-row sys'; }
                    else if (item.type === 'text' && item.content) { assistantDiv.textContent += item.content; }
                    else if (item.type === 'sticker' && item.id) { msgsEl.appendChild(stickerMsg(item.id)); }
                    else if (item.type === 'red_packet') { msgsEl.appendChild(redPacketBubble(item)); }
                } catch(e) {}
            }
            msgsEl.scrollTop = msgsEl.scrollHeight;
        }
    } catch(e) {
        typingRow.remove();
    }
    msgsEl.scrollTop = msgsEl.scrollHeight;
}

// ═══════════════════════════════════════
// 消息时间戳
// ═══════════════════════════════════════

const CHAT_TIME_GAP = 5 * 60 * 1000;
let lastMsgTime = 0;

function maybeAddTimestamp(msgsEl) {
    const now = Date.now();
    if (now - lastMsgTime > CHAT_TIME_GAP) {
        const timeDiv = document.createElement('div');
        timeDiv.className = 'msg-time';
        timeDiv.textContent = formatTime(now);
        msgsEl.appendChild(timeDiv);
    }
    lastMsgTime = now;
}

function formatTime(ts) {
    const d = new Date(ts);
    const now = new Date();
    const h = d.getHours().toString().padStart(2,'0');
    const m = d.getMinutes().toString().padStart(2,'0');
    const isToday = d.toDateString() === now.toDateString();
    if (isToday) return h + ':' + m;
    const M = (d.getMonth()+1).toString().padStart(2,'0');
    const D = d.getDate().toString().padStart(2,'0');
    return M + '-' + D + ' ' + h + ':' + m;
}

// ═══════════════════════════════════════
// 全局点击：关闭表情面板
// ═══════════════════════════════════════

$('chat-msgs').addEventListener('click', () => {
    const panel = $('sticker-panel');
    if (panel.style.display !== 'none') {
        panel.style.display = 'none';
        $('chat-msgs').classList.remove('has-panel');
        $('sticker-toggle-btn').textContent = '😊';
    }
});
