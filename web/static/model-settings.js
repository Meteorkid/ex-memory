/* 只组装提交数据，不在浏览器持久化模型 Key。 */
window.ExMemoryModelSettings = {
    isCustom(provider) {
        return ['custom', 'custom_anthropic', 'custom_gemini'].includes(provider);
    },
    addressHint(provider) {
        if (provider === 'custom_anthropic') return 'Anthropic Messages 基础地址，例如 https://api.example.com/v1；应用会追加 /messages。';
        if (provider === 'custom_gemini') return 'Gemini 原生 API 基础地址，例如 https://api.example.com/v1beta；应用会追加模型与方法路径。';
        return 'OpenAI 兼容基础地址，例如 https://api.example.com/v1；应用会追加对话或 Embedding 路径。';
    },
    payload(saved, values) {
        if (!saved || !Number.isInteger(saved.revision)) throw new Error('请先读取当前账号的模型设置');
        const result = { expected_revision: saved.revision };
        for (const kind of ['llm', 'embedding']) {
            const value = values[kind];
            const current = saved[kind];
            const key = value.api_key.trim();
            let model = value.model.trim();
            const baseUrl = value.base_url.trim().replace(/\/+$/, '');
            if (!model) throw new Error(`${kind === 'llm' ? '对话' : 'Embedding'}模型名不能为空`);
            if (key && value.delete_key) throw new Error('不能同时填写新 Key 和选择删除 Key');
            if (kind === 'embedding' && ['anthropic', 'custom_anthropic'].includes(value.provider)) throw new Error('Anthropic Messages 不提供 Embedding，请选择 OpenAI 兼容或 Gemini 服务');
            if (['gemini', 'custom_gemini'].includes(value.provider)) {
                if (!/^(?:models\/)?[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(model)) throw new Error('请填写 Gemini 模型 ID，可带 models/ 前缀');
                model = model.replace(/^models\//, '');
            }
            const custom = this.isCustom(value.provider);
            if (custom) {
                let parsed;
                try { parsed = new URL(baseUrl); } catch { throw new Error('请填写有效的公网 HTTPS API 地址'); }
                if (parsed.protocol !== 'https:' || parsed.username || parsed.password || parsed.search || parsed.hash) throw new Error('自定义地址需使用 HTTPS，且不能包含账号、查询参数或片段');
            }
            const addressChanged = value.provider !== current.provider || (custom && baseUrl !== current.base_url);
            if (current.has_key && addressChanged && !key && !value.delete_key) throw new Error('更换服务商或地址时，请填写新的 Key，或明确删除旧 Key');
            if (kind === 'embedding' && current.locked && (addressChanged || model !== current.model)) throw new Error('已有记忆索引，暂不能更换 Embedding 服务或模型；仍可替换同一服务的 Key');
            const settings = { provider: value.provider, model, delete_key: value.delete_key };
            if (custom) settings.base_url = baseUrl;
            if (key) settings.api_key = key;
            result[kind] = settings;
        }
        return result;
    },
    keyLabel(settings) {
        return settings.has_key ? `已配置 · 尾号 ${settings.key_last4 || '隐藏'}` : '尚未配置 Key';
    },
};
