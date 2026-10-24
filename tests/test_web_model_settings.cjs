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

for (const provider of ['custom', 'custom_anthropic', 'custom_gemini']) {
    test(`${provider} 保留自定义地址并遵守密钥替换规则`, () => {
        const { saved, values } = fixture();
        values.llm.provider = provider;
        values.llm.base_url = 'https://gateway.example.com/api/';
        assert.equal(settings.isCustom(provider), true);
        assert.throws(() => settings.payload(saved, values), /新的 Key/);
        values.llm.api_key = 'synthetic-own-key';
        const result = settings.payload(saved, values);
        assert.equal(result.llm.base_url, 'https://gateway.example.com/api');
        assert.equal(result.llm.provider, provider);
        values.llm.base_url = 'http://gateway.example.com';
        assert.throws(() => settings.payload(saved, values), /HTTPS/);
    });
}
test('Gemini 模型前缀规范化，不能拼接额外API路径', () => {
    const { saved, values } = fixture();
    values.llm.provider = 'custom_gemini'; values.llm.api_key = 'synthetic-own-key';
    values.llm.base_url = 'https://models.example.com/v1beta';
    values.llm.model = 'models/gemini-example';
    assert.equal(settings.payload(saved, values).llm.model, 'gemini-example');
    values.llm.model = 'models/../other';
    assert.throws(() => settings.payload(saved, values), /模型 ID/);
});
test('Embedding 不允许选择 Anthropic，已建索引不能改用 Gemini', () => {
    const { saved, values } = fixture();
    values.embedding.provider = 'custom_anthropic';
    assert.throws(() => settings.payload(saved, values), /不提供 Embedding/);
    values.embedding.provider = 'custom_gemini';
    values.embedding.base_url = 'https://models.example.com/v1beta';
    values.embedding.model = 'embedding-test'; values.embedding.api_key = 'synthetic-own-key';
    assert.throws(() => settings.payload(saved, values), /已有记忆索引/);
});
test('表单地址说明与所选原生协议匹配', () => {
    assert.match(settings.addressHint('custom_anthropic'), /\/messages/);
    assert.match(settings.addressHint('custom_gemini'), /v1beta/);
    assert.match(settings.addressHint('custom'), /OpenAI/);
    assert.equal(settings.isCustom('anthropic'), false);
});
