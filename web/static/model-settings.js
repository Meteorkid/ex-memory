/* 只组装提交数据，不在浏览器持久化模型 Key。 */
window.ExMemoryModelSettings = {
    payload(saved, values) {
        if (!saved || !Number.isInteger(saved.revision)) throw new Error('请先读取当前账号的模型设置');
        const result = { expected_revision: saved.revision };
        for (const kind of ['llm', 'embedding']) {
            const value = values[kind];
            const current = saved[kind];
            const key = value.api_key.trim();
            const model = value.model.trim();
            const baseUrl = value.base_url.trim().replace(/\/+$/, '');
            if (!model) throw new Error(`${kind === 'llm' ? '对话' : 'Embedding'}模型名不能为空`);
            if (key && value.delete_key) throw new Error('不能同时填写新 Key 和选择删除 Key');
            if (value.provider === 'custom') {
                let parsed;
                try { parsed = new URL(baseUrl); } catch { throw new Error('请填写有效的公网 HTTPS API 地址'); }
                if (parsed.protocol !== 'https:' || parsed.username || parsed.password || parsed.search || parsed.hash) throw new Error('自定义地址需使用 HTTPS，且不能包含账号、查询参数或片段');
            }
            const addressChanged = value.provider !== current.provider || (value.provider === 'custom' && baseUrl !== current.base_url);
            if (current.has_key && addressChanged && !key && !value.delete_key) throw new Error('更换服务商或地址时，请填写新的 Key，或明确删除旧 Key');
            if (kind === 'embedding' && current.locked && (addressChanged || model !== current.model)) throw new Error('已有记忆索引，暂不能更换 Embedding 服务或模型；仍可替换同一服务的 Key');
            const settings = { provider: value.provider, model, delete_key: value.delete_key };
            if (value.provider === 'custom') settings.base_url = baseUrl;
            if (key) settings.api_key = key;
            result[kind] = settings;
        }
        return result;
    },
    keyLabel(settings) {
        return settings.has_key ? `已配置 · 尾号 ${settings.key_last4 || '隐藏'}` : '尚未配置 Key';
    },
};
