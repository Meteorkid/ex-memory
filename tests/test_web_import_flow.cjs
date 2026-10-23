const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const context = { window: {}, setTimeout, Date };
vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../web/static/import-flow.js'), 'utf8'), context);
const ImportFlow = context.window.ExMemoryImportFlow;
const config = {
    allowed_extensions: ['.json', '.txt'], max_upload_bytes: 1024,
    third_party_data_policy_version: 'v2', llm_configured: true, embedding_configured: true,
};
const input = () => ({
    name: '我的镜像', targetName: '小明', basic: '同学', personality: '',
    file: { name: 'chat.json', size: 100 }, consented: true, config,
});

function harness(overrides = {}) {
    const calls = [];
    let taskState = { status: 'succeeded', result: { messages: 12, chunks: 4, state: 'completed' } };
    const flow = new ImportFlow({
        createSlug: () => 'mirror_123', wait: async () => {},
        request: async (method, url, body) => {
            calls.push({ method, url, body });
            if (url === '/exes' && method === 'GET') return [];
            if (url.startsWith('/tasks/')) return taskState;
            return {};
        },
        upload: async (slug, file, target) => {
            calls.push({ upload: slug, file, target });
            return { task_id: 'task1' };
        },
        ...overrides,
    });
    return { flow, calls, setTask: value => { taskState = value; } };
}

test('没有明确确认、有效文件或模型配置时，不创建空镜像', async () => {
    for (const change of [
        { consented: false }, { file: null }, { file: { name: 'empty.json', size: 0 } },
        { file: { name: 'archive.html', size: 2 } }, { file: { name: 'big.json', size: 1025 } },
        { targetName: '' }, { config: { ...config, embedding_configured: false } },
    ]) {
        const { flow, calls } = harness();
        await assert.rejects(flow.submit({ ...input(), ...change }));
        assert.equal(calls.length, 0);
    }
});

test('按确认、草稿、导入、完成任务的顺序执行；展示名与说话人独立', async () => {
    const { flow, calls } = harness();
    const result = await flow.submit(input());
    assert.equal(calls[0].url, '/consents');
    assert.equal(calls[0].body.policy_version, 'v2');
    assert.equal(calls[1].body.defer_generation, true);
    assert.equal(calls[1].body.name, '我的镜像');
    assert.equal(calls[2].target, '小明');
    assert.equal(result.messages, 12);
    assert.equal(flow.draft.slug, 'mirror_123');
    assert.equal(flow.completed, true);
});

test('失败保留草稿，重传使用同一镜像且不重复创建', async () => {
    const { flow, calls, setTask } = harness();
    setTask({ status: 'failed', error: '未找到目标人物' });
    await assert.rejects(flow.submit(input()), /未找到目标人物/);
    assert.equal(flow.completed, false);
    assert.equal(flow.draft.slug, 'mirror_123');
    setTask({ status: 'succeeded', result: { messages: 12, chunks: 4, state: 'completed' } });
    await flow.submit(input());
    assert.equal(calls.filter(c => c.method === 'POST' && c.url === '/exes').length, 1);
    assert.equal(calls.filter(c => c.upload).length, 2);
});

test('受理后查询断网只重查原任务，不重复上传', async () => {
    let fail = true;
    const { flow, calls } = harness({ request: async (_method, url) => {
        if (url.startsWith('/tasks/')) {
            if (fail) throw new Error('网络中断');
            return { status: 'succeeded', result: { messages: 1, state: 'completed' } };
        }
        return {};
    } });
    await assert.rejects(flow.submit(input()), /网络中断/);
    assert.equal(flow.taskId, 'task1');
    fail = false;
    await flow.submit({});
    assert.equal(calls.filter(c => c.upload).length, 1);
});

test('消息数为零或者只入库未生成人物，不能显示成功', async () => {
    for (const result of [{ messages: 0, state: 'completed' }, { messages: 4, state: 'awaiting_import' }]) {
        const { flow, setTask } = harness();
        setTask({ status: 'succeeded', result });
        await assert.rejects(flow.submit(input()));
        assert.equal(flow.completed, false);
    }
});

test('退出账号后，迟到的确认响应不能继续创建或上传', async () => {
    let resolveConsent;
    const calls = [];
    const { flow } = harness({ request: async (_method, url) => {
        calls.push(url);
        return new Promise(resolve => { resolveConsent = resolve; });
    } });
    const pending = flow.submit(input());
    flow.reset();
    resolveConsent({});
    await assert.rejects(pending, /会话已变化/);
    assert.deepEqual(calls, ['/consents']);
    assert.equal(flow.draft, null);
});

test('创建响应丢失后查询本人草稿，恢复而不重复POST', async () => {
    const calls = [];
    const { flow } = harness({ request: async (method, url) => {
        calls.push([method, url]);
        if (method === 'POST' && url === '/exes') throw new Error('请求超时');
        if (method === 'GET' && url === '/exes') return [{ slug: 'mirror_123', name: '我的镜像', state: 'awaiting_import' }];
        if (url.startsWith('/tasks/')) return { status: 'succeeded', result: { messages: 3, state: 'completed' } };
        return {};
    } });
    await assert.rejects(flow.submit(input()), /请求超时/);
    await flow.submit(input());
    assert.equal(calls.filter(([method, url]) => method === 'POST' && url === '/exes').length, 1);
});

test('上传响应丢失后先恢复已受理任务，不再次上传', async () => {
    let uploads = 0;
    const calls = [];
    const { flow } = harness({
        request: async (method, url) => {
            calls.push([method, url]);
            if (url === '/exes' && method === 'GET') return [{ slug: 'mirror_123', state: 'awaiting_import' }];
            if (url === '/tasks?limit=100') return { tasks: [{ slug: 'mirror_123', type: 'import_chat', status: 'running', task_id: 'task1' }] };
            if (url === '/tasks/task1') return { status: 'succeeded', result: { messages: 3, state: 'completed' } };
            return {};
        },
        upload: async () => { uploads++; throw new Error('上传响应中断'); },
    });
    await assert.rejects(flow.submit(input()), /上传响应中断/);
    assert.equal(flow.needsReconcile, true);
    await flow.submit({});
    assert.equal(uploads, 1);
    assert.equal(flow.completed, true);
    assert.ok(calls.some(([, url]) => url === '/tasks?limit=100'));
});

test('恢复草稿时任务读取失败，继续阻止写入；已完成则可直接打开', async () => {
    let state = 'awaiting_import';
    const calls = [];
    const { flow } = harness({ request: async (method, url) => {
        calls.push([method, url]);
        if (url === '/exes') return [{ slug: 'mirror_123', state }];
        throw new Error('暂时无法读取任务');
    } });
    flow.restore({ slug: 'mirror_123', name: '镜像' });
    await assert.rejects(flow.submit(input()), /暂时无法读取任务/);
    assert.equal(flow.needsReconcile, true);
    assert.ok(calls.every(([method]) => method === 'GET'));
    state = 'completed';
    const result = await flow.submit({});
    assert.equal(result.recovered, true);
    assert.equal(flow.completed, true);
});

test('任务在恢复查询之间完成时重新核对镜像，不要求重传', async () => {
    let reads = 0;
    const { flow } = harness({ request: async (method, url) => {
        assert.equal(method, 'GET');
        if (url === '/exes') return [{ slug: 'mirror_123', state: ++reads === 1 ? 'awaiting_import' : 'completed' }];
        return { tasks: [{ slug: 'mirror_123', type: 'import_chat', status: 'succeeded' }] };
    } });
    flow.restore({ slug: 'mirror_123', name: '镜像' });
    const result = await flow.submit({});
    assert.equal(result.recovered, true);
    assert.equal(reads, 2);
});
