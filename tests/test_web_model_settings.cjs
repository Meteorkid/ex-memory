const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const context = { window: {}, URL };
vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../web/static/model-settings.js'), 'utf8'), context);
const settings = context.window.ExMemoryModelSettings;
function fixture() {
    const saved = { revision: 2, llm: { provider: 'deepseek', model: 'deepseek-chat', has_key: true, key_last4: '1234', base_url: 'https://api.deepseek.com' }, embedding: { provider: 'siliconflow', model: 'BAAI/bge-m3', has_key: true, key_last4: '5678', base_url: 'https://api.siliconflow.cn/v1', locked: true } };
    const values = Object.fromEntries(['llm', 'embedding'].map(kind => [kind, { ...saved[kind], api_key: '', delete_key: false }]));
    return { saved, values };
}
test('空 Key 不发送也不会用尾号覆盖已保存密钥', () => {
    const { saved, values } = fixture();
    const result = settings.payload(saved, values);
    assert.equal(result.expected_revision, 2);
    assert.equal('api_key' in result.llm, false);
    assert.equal('key_last4' in result.llm, false);
});
test('Key 替换与明确删除互斥，删除不携带占位Key', () => {
    const { saved, values } = fixture();
    values.llm.api_key = 'synthetic-new-secret'; values.llm.delete_key = true;
    assert.throws(() => settings.payload(saved, values), /不能同时/);
    values.llm.api_key = '';
    assert.equal(settings.payload(saved, values).llm.delete_key, true);
});
test('更换服务地址不能默默沿用旧Key', () => {
    const { saved, values } = fixture();
    values.llm.provider = 'custom'; values.llm.base_url = 'https://api.example.com/v1/';
    assert.throws(() => settings.payload(saved, values), /新的 Key/);
    values.llm.api_key = 'synthetic-new-secret';
    const result = settings.payload(saved, values);
    assert.equal(result.llm.base_url, 'https://api.example.com/v1');
    assert.equal(result.llm.api_key, 'synthetic-new-secret');
});
test('锁定Embedding仍可换Key，但不能换模型', () => {
    const { saved, values } = fixture();
    values.embedding.api_key = 'synthetic-rotated-key';
    assert.equal(settings.payload(saved, values).embedding.api_key, 'synthetic-rotated-key');
    values.embedding.model = 'another-model';
    assert.throws(() => settings.payload(saved, values), /已有记忆索引/);
});
test('自定义地址格式错误在提交前阻止且错误不回显Key', () => {
    const { saved, values } = fixture();
    values.llm.provider = 'custom'; values.llm.api_key = 'synthetic-secret';
    for (const address of ['http://api.example.com', 'https://user:secret@api.example.com', 'https://api.example.com?key=secret']) {
        values.llm.base_url = address;
        assert.throws(() => settings.payload(saved, values), error => !error.message.includes('synthetic-secret'));
    }
});
