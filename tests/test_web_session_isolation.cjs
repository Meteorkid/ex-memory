const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../web/static/app.js'), 'utf8');

// 执行应用里的实际函数；DOM 替身只提供列表渲染需要的接口。
function extractFunction(start, next) {
    const from = source.indexOf(start);
    const to = source.indexOf(next, from + start.length);
    assert.ok(from >= 0 && to > from, `无法定位 ${start}`);
    return source.slice(from, to);
}

const functions = [
    extractFunction('function isNetworkFailure(', 'async function api('),
    extractFunction('async function api(', 'function resetSessionView('),
    extractFunction('function resetSessionView(', 'function logout('),
    extractFunction('async function loadContactList(', 'function initSearch('),
].join('\n');

function deferred() {
    let resolve;
    let reject;
    const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
    return { promise, resolve, reject };
}

function response(body, status = 200) {
    return { status, ok: status >= 200 && status < 300, json: async () => body };
}

function element(fragment = false) {
    return {
        fragment, children: [], dataset: {}, disabled: false,
        _html: '',
        get innerHTML() { return this._html + this.children.map(child => child.innerHTML).join(''); },
        set innerHTML(value) { this._html = value; this.children = []; },
        querySelectorAll() { return this.children; },
        appendChild(child) { this.children.push(...(child.fragment ? child.children : [child])); },
        addEventListener() {},
        classList: { toggle() {} },
    };
}

function harness(fetchOverride, schedule = callback => queueMicrotask(callback)) {
    const requests = [];
    const nodes = new Map(['contact-list', 'chat-msgs', 'msg-input', 'msg-send', 'health-tip-container']
        .map(id => [id, element()]));
    const effects = { resets: 0, logouts: 0, offline: 0 };
    const sandbox = {
        AbortSignal: { timeout: () => undefined },
        setTimeout: schedule,
        document: {
            createDocumentFragment: () => element(true),
            createElement: () => element(),
            querySelectorAll: () => [],
        },
        $: id => nodes.get(id),
        PerfMonitor: { start() {}, end() {} },
        avatarColor: () => '#123456',
        escHtml: value => String(value),
        getLastMessage: () => '',
        loadHealthTip() {},
        markNetworkOnline() {},
        markNetworkOffline() { effects.offline++; },
        resetImportForm() { effects.resets++; },
        resetModelSettings() {},
        invalidateImportConfig() {},
        logout() { effects.logouts++; },
        fetch: (url, options) => {
            const pending = deferred();
            const request = { url, options, ...pending };
            requests.push(request);
            return fetchOverride ? fetchOverride(request, requests.length) : pending.promise;
        },
    };
    const context = vm.createContext(sandbox);
    vm.runInContext(`
        const API = '/api';
        const pendingRequests = new Map();
        const messagePreviews = new Map([['old-private-mirror', 'old preview']]);
        let authEpoch = 0;
        let token = 'old-synthetic-token';
        let currentSlug = 'old-private-mirror';
        let currentName = 'old mirror';
        const isDesktop = false;
        ${functions}
    `, context);
    return {
        requests, nodes, effects,
        run: code => vm.runInContext(code, context),
        switchAccount: () => vm.runInContext("resetSessionView(); token = 'new-synthetic-token';", context),
    };
}

const oldMirror = { slug: 'old-private-mirror', name: '旧账号的虚构镜像', state: 'completed' };
const newMirror = { slug: 'new-private-mirror', name: '新账号的虚构镜像', state: 'completed' };

test('同一会话的相同GET仍去重', async () => {
    const h = harness();
    const first = h.run("api('GET', '/exes')");
    const second = h.run("api('GET', '/exes')");
    assert.equal(h.requests.length, 1);
    h.requests[0].resolve(response([oldMirror]));
    assert.deepEqual(await first, [oldMirror]);
    assert.deepEqual(await second, [oldMirror]);
});

test('旧账号列表迟到不能覆盖新账号，GET也不能跨会话复用', async () => {
    const h = harness();
    const oldList = h.run('loadContactList()');
    h.nodes.get('chat-msgs').innerHTML = 'old private chat';
    h.switchAccount();

    assert.equal(h.run('messagePreviews.size'), 0);
    assert.equal(h.run('currentSlug'), '');
    assert.equal(h.nodes.get('chat-msgs').innerHTML, '');
    assert.equal(h.nodes.get('msg-input').disabled, true);
    assert.equal(h.nodes.get('msg-send').disabled, true);
    assert.equal(h.effects.resets, 1);

    const newList = h.run('loadContactList()');
    assert.equal(h.requests.length, 2);
    assert.equal(h.requests[0].options.headers.Authorization, 'Bearer old-synthetic-token');
    assert.equal(h.requests[1].options.headers.Authorization, 'Bearer new-synthetic-token');
    h.requests[1].resolve(response([newMirror]));
    await newList;
    h.requests[0].resolve(response([oldMirror]));
    await oldList;

    const rendered = h.nodes.get('contact-list').innerHTML;
    assert.ok(rendered.includes(newMirror.name));
    assert.ok(!rendered.includes(oldMirror.name));
    assert.ok(!rendered.includes('加载失败'));
});

test('旧账号401响应不能把新账号登出', async () => {
    const h = harness();
    const pending = h.run("api('GET', '/exes')");
    const rejected = assert.rejects(pending, /会话已变化/);
    h.switchAccount();
    h.requests[0].resolve(response({ detail: 'expired' }, 401));
    await rejected;
    assert.equal(h.effects.logouts, 0);
});

test('读取响应体期间切换账号也不能返回旧数据', async () => {
    const h = harness();
    const json = deferred();
    const bodyStarted = deferred();
    const pending = h.run("api('GET', '/exes')");
    const rejected = assert.rejects(pending, /会话已变化/);
    h.requests[0].resolve({ status: 200, ok: true, json: () => {
        bodyStarted.resolve();
        return json.promise;
    } });
    await bodyStarted.promise;
    h.switchAccount();
    json.resolve([oldMirror]);
    await rejected;
});

test('旧会话的迟到网络错误不得重发旧凭据或改变新会话网络状态', async () => {
    const h = harness((request, count) => count === 1
        ? request.promise
        : Promise.resolve(response([oldMirror])));
    const pending = h.run("api('GET', '/exes')");
    const rejected = assert.rejects(pending, /会话已变化/);
    h.switchAccount();
    h.requests[0].reject(new TypeError('synthetic old-account network failure'));
    await rejected;
    assert.equal(h.requests.length, 1);
    assert.equal(h.effects.offline, 0);
});

test('网络重试退避期间切换账号，定时器到期后也不能重发旧凭据', async () => {
    const timerReady = deferred();
    let continueRetry;
    const h = harness((request, count) => count === 1
        ? request.promise
        : Promise.resolve(response([oldMirror])), callback => {
        continueRetry = callback;
        timerReady.resolve();
    });
    const pending = h.run("api('GET', '/exes')");
    const rejected = assert.rejects(pending, /会话已变化/);
    h.requests[0].reject(new TypeError('synthetic failure before sign-out'));
    await timerReady.promise;
    h.switchAccount();
    continueRetry();
    await rejected;
    assert.equal(h.requests.length, 1);
});

function chatStreamHarness() {
    function node() {
        return {
            children: [], style: {}, textContent: '', parentNode: null,
            appendChild(child) { child.parentNode = this; this.children.push(child); },
            querySelector() { return this.children[0]; },
            remove() {
                if (this.parentNode) this.parentNode.children.splice(this.parentNode.children.indexOf(this), 1);
                this.parentNode = null;
            },
        };
    }
    const messages = node();
    const message = '今天聊了挺久了，先歇一会儿吧。\n回忆随时都在，但此刻的生活也需要你。';
    function streamResponse() {
        const encoded = new TextEncoder().encode(`data: ${JSON.stringify({ type: 'usage_limit', message })}\n\ndata: [DONE]\n\n`);
        const chunks = [encoded.slice(0, 19), encoded.slice(19)];
        return { status: 200, ok: true, body: { getReader: () => ({
            read: async () => chunks.length ? { done: false, value: chunks.shift() } : { done: true },
        }) } };
    }
    const context = vm.createContext({
        TextDecoder, setTimeout, API: '/api', token: '', currentSlug: 'synthetic', currentName: '虚构镜像',
        document: { createElement: node },
        $: id => id === 'chat-msgs' ? messages : { style: { display: 'none' } },
        chatBubble: (role, text) => {
            const row = node(); row.className = `msg-row ${role}`;
            const content = node(); content.textContent = text; row.appendChild(content);
            return row;
        },
        typingIndicator: node, buildHistory: () => [], fetch: async () => streamResponse(),
        storeLastMessage() {}, showDesktopNotification() {},
    });
    vm.runInContext([
        extractFunction('async function parseChatStream(', 'function initTheme('),
        extractFunction('function noticeMsg(', 'function sysMsg('),
        extractFunction('async function sendVoiceMessage(', 'const CHAT_TIME_GAP'),
    ].join('\n'), context);
    return { context, messages, message, streamResponse };
}

for (const mode of ['文字', '语音']) {
    test(`${mode}流式对话显示服务端使用时长提示，不留下空的镜像回复`, async () => {
        const h = chatStreamHarness();
        if (mode === '文字') await h.context.parseChatStream(h.streamResponse(), h.messages);
        else await h.context.sendVoiceMessage(2, '合成语音转写');
        assert.equal(h.messages.children.length, 1);
        const notice = h.messages.children[0];
        assert.equal(notice.className, 'msg-row sys');
        assert.equal(notice.children[0].textContent, h.message);
        assert.equal(notice.children[0].style.whiteSpace, 'pre-wrap');
    });
}
