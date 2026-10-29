const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const appSource = fs.readFileSync(path.join(__dirname, '../web/static/app.js'), 'utf8');

function appFunction(start, end) {
    const from = appSource.indexOf(start);
    const to = appSource.indexOf(end, from + start.length);
    assert.ok(from >= 0 && to > from);
    return appSource.slice(from, to);
}

test('进入镜像时从服务器恢复选中会话，不只清空消息区', async () => {
    const opened = [];
    const nodes = new Map();
    const context = vm.createContext({
        currentSlug: '', currentName: '', lastMsgTime: 0, isDesktop: false,
        memoryLoop: { open(slug) { opened.push(slug); } },
        $: id => {
            if (!nodes.has(id)) nodes.set(id, { style: {}, focus() {} });
            return nodes.get(id);
        },
        switchTab() {}, setTimeout() {},
    });
    vm.runInContext(appFunction('function enterChat(', 'let allStickers'), context);
    await context.enterChat('synthetic', '合成镜像');
    assert.deepEqual(opened, ['synthetic']);
});

test('聊天菜单的新建会话使用会话控制器，不删除既有历史', () => {
    let options;
    let created = 0;
    const context = vm.createContext({
        memoryLoop: { newConversation() { created++; } },
        showActionSheet(value) { options = value; },
        api() { throw new Error('新建会话不应删除服务端记录'); },
    });
    vm.runInContext(appFunction('function showChatActions(', 'async function loadContactList('), context);
    context.showChatActions();
    assert.equal(options[0].label, '新建会话');
    options[0].onClick();
    assert.equal(created, 1);
});

const loopSource = fs.readFileSync(path.join(__dirname, '../web/static/memory-loop.js'), 'utf8');

function element(tag = 'div') {
    return {
        tag, children: [], listeners: {}, dataset: {}, value: '', hidden: false,
        disabled: false, open: false, className: '', _text: '', parentNode: null,
        get textContent() { return this._text + this.children.map(child => child.textContent).join(''); },
        set textContent(value) { this._text = String(value); this.children = []; },
        get innerHTML() { return this.textContent; },
        set innerHTML(_) { throw new Error('参考原文不能写入 innerHTML'); },
        appendChild(child) { child.parentNode = this; this.children.push(child); return child; },
        replaceChildren(...children) { this.children = []; this._text = ''; children.forEach(child => this.appendChild(child)); },
        addEventListener(name, listener) { this.listeners[name] = listener; },
        click() { if (!this.disabled) this.listeners.click?.({ preventDefault() {} }); },
        focus() { this.focused = true; },
        showModal() { this.open = true; },
        close() { this.open = false; },
        remove() {
            if (this.parentNode) this.parentNode.children = this.parentNode.children.filter(child => child !== this);
            this.parentNode = null;
        },
        querySelector(selector) {
            const matches = node => selector.startsWith('.')
                ? node.className.split(' ').includes(selector.slice(1))
                : node.tag === selector;
            for (const child of this.children) {
                if (matches(child)) return child;
                const match = child.querySelector(selector);
                if (match) return match;
            }
            return null;
        },
        classList: { add() {} },
    };
}

function deferred() {
    let resolve, reject;
    const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
    return { promise, resolve, reject };
}

function harness(handler, stored = new Map()) {
    const nodes = new Map();
    const state = { epoch: 0, slug: 'mirror' };
    const effects = { requests: [], messages: [], busy: [], notices: [] };
    const document = {
        createElement: element,
        getElementById(id) { if (!nodes.has(id)) nodes.set(id, element()); return nodes.get(id); },
    };
    const context = vm.createContext({ window: {}, crypto, document });
    vm.runInContext(loopSource, context);
    const api = context.window.ExMemoryMemoryLoop;
    const controller = api.create({
        document,
        request(method, url, body) {
            effects.requests.push({ method, url, body });
            return handler(method, url, body, effects.requests.length);
        },
        identity: () => state,
        storage: { getItem: key => stored.get(key), setItem: (key, value) => stored.set(key, value) },
        renderMessages(messages) { effects.messages.push(JSON.parse(JSON.stringify(messages))); },
        setBusy(value) { effects.busy.push(value); },
        notify(message) { effects.notices.push(message); },
        randomId: () => '00000000-0000-4000-8000-000000000001',
    });
    return { controller, nodes, state, effects, stored, key: api.selectionKey };
}

function list(userId = 1) {
    return { user_id: userId, default_conversation_id: 'web', conversations: [
        { conversation_id: 'web', label: '网页会话', source: 'web' },
        { conversation_id: 'im_private', label: '私聊', source: 'astrbot' },
        { conversation_id: 'im_group', label: '群聊', source: 'astrbot' },
    ] };
}

function responseFor(url, userId = 1) {
    if (url.endsWith('/conversations')) return list(userId);
    const id = decodeURIComponent(url.split('/').at(-2));
    return { user_id: userId, conversation_id: id, messages: [
        { id: 'turn-user', role: 'user', content: `来自 ${id}`, created_at: '2026-10-29T10:00:00' },
    ], archive: { state: 'idle' } };
}

test('按服务端账号和镜像恢复选中会话，仅存会话ID', async () => {
    const stored = new Map([['ex-memory-conversation:v1:1:mirror', 'im_private']]);
    const h = harness(async (_method, url) => responseFor(url), stored);
    await h.controller.open('mirror');
    assert.equal(h.controller.conversationId, 'im_private');
    assert.match(h.effects.requests[1].url, /im_private\/messages$/);
    assert.equal(h.effects.messages[0][0].content, '来自 im_private');
    assert.deepEqual([...stored.values()], ['im_private']);
    assert.equal(h.controller.ready, true);
});

test('另一账号的同名镜像不会恢复前账号的IM会话', async () => {
    const h = harness(async (_method, url) => responseFor(url, 2), new Map([
        ['ex-memory-conversation:v1:1:mirror', 'im_group'],
    ]));
    await h.controller.open('mirror');
    assert.equal(h.controller.conversationId, 'web');
    assert.equal(h.stored.get(h.key(2, 'mirror')), 'web');
});

test('本地存储中的未知会话不能绕过服务器列表', async () => {
    const h = harness(async (_method, url) => responseFor(url), new Map([
        ['ex-memory-conversation:v1:1:mirror', '../another-user'],
    ]));
    await h.controller.open('mirror');
    assert.equal(h.controller.conversationId, 'web');
    assert.ok(h.effects.requests.every(request => !request.url.includes('another-user')));
});

test('缺少展示名的会话用入口和时间标识，不显示内部ID', async () => {
    const h = harness(async (_method, url) => {
        if (!url.endsWith('/conversations')) return responseFor(url);
        return {user_id: 1, default_conversation_id: 'web', conversations: [
            {conversation_id: 'im_abcdef123', label: 'im_abcdef123', source: 'mcp', updated_at: '2026-10-29T17:00:00'},
            {conversation_id: 'web_123abc', label: 'web_123abc', source: 'web', updated_at: '2026-10-28T09:00:00'},
        ]};
    });
    await h.controller.open('mirror');
    const text = h.nodes.get('conversation-select').textContent;
    assert.match(text, /聊天软件会话 · 10-29 17:00/);
    assert.match(text, /网页会话 · 10-28 09:00/);
    assert.ok(!text.includes('im_abcdef123'));
    assert.ok(!text.includes('web_123abc'));
});

test('切换账号后旧列表迟到不能恢复旧消息或写入新选择', async () => {
    const old = deferred();
    const h = harness(async (_method, url, _body, count) => count === 1 ? old.promise : responseFor(url, 2));
    const first = h.controller.open('mirror');
    h.state.epoch++;
    h.controller.reset();
    await h.controller.open('mirror');
    old.resolve(list(1));
    await first;
    assert.equal(h.effects.messages.length, 1);
    assert.ok(!h.stored.has(h.key(1, 'mirror')));
    assert.equal(h.nodes.get('correction-dialog').open, false);
});

test('切换会话后旧历史迟到不能混入新会话', async () => {
    const old = deferred();
    const h = harness(async (_method, url) => url.includes('/im_private/') ? old.promise : responseFor(url));
    await h.controller.open('mirror');
    const first = h.controller.choose('im_private');
    await h.controller.choose('im_group');
    old.resolve(responseFor('/exes/mirror/conversations/im_private/messages'));
    await first;
    assert.equal(h.controller.conversationId, 'im_group');
    assert.equal(h.effects.messages.at(-1)[0].content, '来自 im_group');
    assert.equal(h.stored.get(h.key(1, 'mirror')), 'im_group');
});

test('会话响应归属不匹配时禁止继续发送', async () => {
    const h = harness(async (_method, url) => url.endsWith('/conversations') ? list(1) : responseFor(url, 2));
    await h.controller.open('mirror');
    assert.equal(h.controller.ready, false);
    assert.equal(h.effects.messages.length, 0);
    assert.match(h.nodes.get('conversation-status').textContent, /身份不匹配/);
});

for (const [state, text] of [
    ['pending', '有尚未归档的对话'], ['running', '长期记忆整理中'],
    ['failed', '长期记忆整理未完成，下次对话可重试'], ['complete', '长期记忆已更新'],
]) {
    test(`归档状态 ${state} 使用准确进度文案`, async () => {
        const h = harness(async (_method, url) => {
            const result = responseFor(url);
            if (result.archive) result.archive.state = state;
            return result;
        });
        await h.controller.open('mirror');
        assert.ok(h.nodes.get('conversation-status').textContent.endsWith(text));
    });
}

test('新建网页会话不会复制群聊历史或立即写服务器', async () => {
    const h = harness(async (_method, url) => responseFor(url));
    await h.controller.open('mirror');
    const before = h.effects.requests.length;
    h.controller.newConversation();
    assert.match(h.controller.conversationId, /^web_[0-9a-f]{32}$/);
    assert.deepEqual(h.effects.messages.at(-1), []);
    assert.equal(h.effects.requests.length, before);
});

function assistantRow() {
    const row = element();
    row.className = 'msg-row assistant';
    const text = element();
    text.className = 'msg';
    text.textContent = '合成回答';
    row.appendChild(text);
    return row;
}

test('来源原文按纯文本显示，并明确生成摘要不是原始聊天', async () => {
    const h = harness(async (_method, url) => responseFor(url));
    await h.controller.open('mirror');
    const row = assistantRow();
    h.controller.decorate(row, { message_id: 'turn-assistant', conversation_id: 'web', sources: [
        { id: 's1', text: '<img src=x onerror=alert(1)>', source: '<script>source</script>', kind: 'generated_summary' },
    ] }, h.controller.capture());
    assert.match(row.querySelector('.answer-sources').textContent, /AI 生成摘要/);
    assert.match(row.querySelector('blockquote').textContent, /<img src=x/);
    assert.equal(row.querySelector('img'), null);
    assert.match(row.textContent, /不表示每句话都已被资料证实/);
});

test('未保存的回答不能提交纠正', async () => {
    const h = harness(async (_method, url) => responseFor(url));
    await h.controller.open('mirror');
    const row = assistantRow();
    h.controller.decorate(row, { conversation_id: 'web', sources: [] }, h.controller.capture());
    const button = row.querySelector('.answer-correct');
    assert.equal(button.disabled, true);
    button.click();
    assert.equal(h.nodes.get('correction-dialog').open, false);
});

test('纠正需明确保存，携带当前回答和选中来源ID', async () => {
    const h = harness(async (method, url) => method === 'POST' ? { ok: true, revision: 3 } : responseFor(url));
    await h.controller.open('mirror');
    const row = assistantRow();
    h.controller.decorate(row, { message_id: 'turn-assistant', conversation_id: 'web', sources: [
        { id: 's1', text: '合成参考', kind: 'imported_chat' },
    ] }, h.controller.capture());
    row.querySelector('.answer-sources').querySelector('button').click();
    assert.equal(h.effects.requests.filter(request => request.method === 'POST').length, 0);
    h.nodes.get('correction-content').value = '  不要使用这个称呼  ';
    await h.controller.submitCorrection();
    const saved = h.effects.requests.at(-1);
    assert.equal(saved.url, '/exes/mirror/corrections');
    assert.deepEqual(JSON.parse(JSON.stringify(saved.body)), {
        content: '不要使用这个称呼', message_id: 'turn-assistant', conversation_id: 'web', source_ids: ['s1'],
    });
    assert.equal(h.nodes.get('correction-dialog').open, false);
    assert.equal(h.effects.notices.length, 1);
});

test('保存失败保留输入并允许重试，不假报成功', async () => {
    const h = harness(async (method, url) => {
        if (method === 'POST') throw new Error('合成保存失败');
        return responseFor(url);
    });
    await h.controller.open('mirror');
    const row = assistantRow();
    h.controller.decorate(row, { message_id: 'turn-assistant', conversation_id: 'web' }, h.controller.capture());
    row.querySelector('.answer-correct').click();
    h.nodes.get('correction-content').value = '我的纠正';
    await h.controller.submitCorrection();
    assert.equal(h.nodes.get('correction-content').value, '我的纠正');
    assert.match(h.nodes.get('correction-result').textContent, /保存失败/);
    assert.equal(h.nodes.get('correction-save').disabled, false);
    assert.equal(h.effects.notices.length, 0);
});

test('保存中切换账号会清空纠正草稿，迟到成功不影响新账号', async () => {
    const saved = deferred();
    const h = harness(async (method, url) => method === 'POST' ? saved.promise : responseFor(url));
    await h.controller.open('mirror');
    const row = assistantRow();
    h.controller.decorate(row, { message_id: 'turn-assistant', conversation_id: 'web' }, h.controller.capture());
    row.querySelector('.answer-correct').click();
    h.nodes.get('correction-content').value = '旧账号私人纠正';
    const pending = h.controller.submitCorrection();
    h.state.epoch++;
    h.controller.reset();
    saved.resolve({ ok: true, revision: 2 });
    await pending;
    assert.equal(h.nodes.get('correction-content').value, '');
    assert.equal(h.effects.notices.length, 0);
});

test('切会话会终止旧流请求，旧view不能附加来源或纠正入口', async () => {
    const h = harness(async (_method, url) => responseFor(url));
    await h.controller.open('mirror');
    const oldView = h.controller.capture();
    const controller = new AbortController();
    h.controller.track(controller);
    await h.controller.choose('im_private');
    assert.equal(controller.signal.aborted, true);
    const row = assistantRow();
    h.controller.decorate(row, { message_id: 'old-id', conversation_id: 'web' }, oldView);
    assert.equal(row.querySelector('.answer-tools'), null);
});

function streamHarness() {
    const messages = element();
    const state = { current: true };
    const decorated = [];
    let loggedOut = 0;
    const view = { slug: 'mirror', conversationId: 'im_private' };
    const context = vm.createContext({
        TextDecoder, currentName: '合成镜像',
        setTimeout: callback => queueMicrotask(callback),
        memoryLoop: {
            capture: () => view, isCurrent: () => state.current,
            decorate(row, metadata) { decorated.push({ row, metadata }); },
        },
        chatBubble(role, text) {
            const row = element(); row.className = `msg-row ${role}`;
            const content = element(); content.className = 'msg'; content.textContent = text;
            row.appendChild(content);
            return row;
        },
        noticeMsg(item) { const row = element(); row.textContent = item.message; return row; },
        sysMsg(text) { const row = element(); row.textContent = text; return row; },
        storeLastMessage() {}, showDesktopNotification() {}, logout() { loggedOut++; },
    });
    vm.runInContext([
        appFunction('async function parseChatStream(', 'function initTheme('),
        appFunction('async function renderSegments(', '// 红包系统'),
    ].join('\n'), context);
    return { context, messages, state, decorated, view, loggedOut: () => loggedOut };
}

function streamResponse(events, complete = true) {
    const text = events.map(event => `data: ${JSON.stringify(event)}\n\n`).join('')
        + (complete ? 'data: [DONE]\n\n' : '');
    const bytes = new TextEncoder().encode(text);
    const chunks = [];
    for (let index = 0; index < bytes.length; index += 17) chunks.push(bytes.slice(index, index + 17));
    return { ok: true, status: 200, body: { getReader: () => ({
        read: async () => chunks.length ? { done: false, value: chunks.shift() } : { done: true },
    }) } };
}

test('流式来源绑定最终分段回答和服务端message_id', async () => {
    const h = streamHarness();
    await h.context.parseChatStream(streamResponse([
        { type: 'text', content: '第一段第二段' },
        { type: 'sources', sources: [{ id: 'reference', text: '原话' }] },
        { type: 'segments', segments: [{ text: '第一段', delay: 0.01 }, { text: '第二段', delay: 0.01 }] },
        { type: 'metadata', conversation_id: 'im_private', message_id: 'answer-id', sources: [{ id: 'reference', text: '原话' }] },
    ]), h.messages);
    assert.equal(h.messages.children.length, 2);
    assert.equal(h.decorated.length, 1);
    assert.equal(h.decorated[0].row, h.messages.children[1]);
    assert.equal(h.decorated[0].metadata.message_id, 'answer-id');
    assert.equal(h.decorated[0].metadata.sources[0].id, 'reference');
});

test('不完整流没有可保存纠正的回答元数据', async () => {
    const h = streamHarness();
    await assert.rejects(h.context.parseChatStream(streamResponse([
        { type: 'text', content: '未完成的回答' },
    ], false), h.messages), /未完整接收/);
    assert.equal(h.decorated.length, 0);
});

test('审核阻断不留下带纠正按钮的镜像回答', async () => {
    const h = streamHarness();
    await h.context.parseChatStream(streamResponse([
        { type: 'blocked', message: '合成审核提示' },
    ]), h.messages);
    assert.equal(h.messages.children.length, 1);
    assert.equal(h.messages.children[0].textContent, '合成审核提示');
    assert.equal(h.decorated.length, 0);
});

test('切换会话后迟到流停止读取，不向新会话插入旧来源', async () => {
    const h = streamHarness();
    const pending = deferred();
    let cancelled = 0;
    const response = { ok: true, status: 200, body: { getReader: () => ({
        read: () => pending.promise, cancel() { cancelled++; },
    }) } };
    const reading = h.context.parseChatStream(response, h.messages);
    h.state.current = false;
    h.messages.replaceChildren();
    pending.resolve({ done: false, value: new TextEncoder().encode('data: {"type":"text","content":"旧私密内容"}\n\n') });
    await reading;
    assert.equal(cancelled, 1);
    assert.equal(h.messages.children.length, 0);
    assert.equal(h.decorated.length, 0);
});

for (const pathname of ['/api/exes/mirror/conversations', '/api/exes/mirror/conversations/im_private/messages', '/runtime/api/exes/mirror/corrections']) {
    test(`Service Worker 不缓存或回放私有闭环数据：${pathname}`, async () => {
        const listeners = new Map();
        let cacheAccesses = 0;
        const context = vm.createContext({
            URL, Response,
            self: { addEventListener(name, callback) { listeners.set(name, callback); } },
            fetch: async () => { throw new Error('synthetic offline'); },
            caches: {
                async open() { cacheAccesses++; return { match: async () => new Response('旧账号私密资料') }; },
                match: async () => new Response('旧账号私密资料'),
            },
        });
        vm.runInContext(fs.readFileSync(path.join(__dirname, '../web/static/sw.js'), 'utf8'), context);
        let pending;
        listeners.get('fetch')({
            request: { method: 'GET', url: `https://example.test${pathname}` },
            respondWith(promise) { pending = promise; },
        });
        const response = await pending;
        assert.equal(response.status, 503);
        assert.equal(cacheAccesses, 0);
        assert.ok(!(await response.text()).includes('旧账号'));
    });
}
