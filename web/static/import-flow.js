/* 首次导入的任务状态独立于界面：查询中断后继续同一任务，失败后复用同一草稿。 */
window.ExMemoryImportFlow = class {
    constructor({ request, upload, createSlug, onProgress = () => {}, wait = ms => new Promise(r => setTimeout(r, ms)) }) {
        this.request = request;
        this.upload = upload;
        this.createSlug = createSlug;
        this.onProgress = onProgress;
        this.wait = wait;
        this.epoch = 0;
        this.reset();
    }

    reset() {
        this.epoch += 1;
        this.draft = null;
        this.taskId = null;
        this.completed = false;
        this.busy = false;
        this.needsReconcile = false;
    }

    restore(mirror) {
        if (this.busy) throw new Error('当前导入尚未结束，请先等待任务结果');
        this.reset();
        this.draft = { slug: mirror.slug, name: mirror.name, confirmed: true };
        this.needsReconcile = true;
    }

    assertSession(epoch) {
        if (epoch !== this.epoch) throw new Error('会话已变化，请重新进入创建页面');
    }

    validate({ name, targetName, file, consented, config }) {
        if (!name?.trim() || name.trim().length > 64) throw new Error('请填写 1–64 字的镜像名称');
        if (!targetName?.trim()) throw new Error('请填写记录中的目标说话人名称');
        if (!file) throw new Error('请选择聊天记录；没有文件时可先下载虚构示例');
        if (file.size === 0) throw new Error('文件为空，请选择有聊天内容的文件');
        if (!config) throw new Error('导入说明尚未加载，请重试');
        const extension = '.' + file.name.split('.').pop().toLowerCase();
        if (!config.allowed_extensions?.includes(extension)) throw new Error('暂不支持此格式，请按说明转换后上传');
        if (file.size > config.max_upload_bytes) throw new Error('文件过大，请拆分后再上传');
        if (!config.llm_configured || !config.embedding_configured) throw new Error('服务尚未准备好，请稍后重试或联系站主');
        if (!config.third_party_data_policy_version || !consented) throw new Error('请先阅读并确认下面的数据处理说明');
    }

    async submit(input) {
        if (this.busy) throw new Error('导入正在进行，请稍候');
        if (this.completed) throw new Error('镜像已准备好，可以开始对话');
        const epoch = this.epoch;
        this.busy = true;
        try {
            if (this.needsReconcile) {
                await this.reconcile(epoch);
                if (this.completed) return { state: 'completed', recovered: true };
            }
            // 已受理的任务只查询进度，不能因网络错误重新上传、重复调用模型。
            if (!this.taskId) {
                this.validate(input);
                await this.request('POST', '/consents', {
                    policy_type: 'third_party_data',
                    policy_version: input.config.third_party_data_policy_version,
                });
                this.assertSession(epoch);
                if (!this.draft) {
                    this.draft = { slug: this.createSlug(), name: input.name.trim(), confirmed: false };
                    await this.createDraft(input, epoch);
                } else if (!this.draft.confirmed) {
                    // 创建响应丢失时，先核对本人名下的随机草稿，避免重复创建或覆盖。
                    const mirrors = await this.request('GET', '/exes');
                    this.assertSession(epoch);
                    const existing = mirrors.find(m => m.slug === this.draft.slug);
                    if (existing) {
                        if (existing.state === 'completed') throw new Error('此镜像已生成，请从镜像列表打开');
                        this.draft.confirmed = true;
                    } else {
                        await this.createDraft(input, epoch);
                    }
                }
                this.onProgress('正在上传聊天记录…');
                // 响应丢失不代表后台未受理；下次必须先核对任务和镜像状态。
                this.needsReconcile = true;
                const accepted = await this.upload(this.draft.slug, input.file, input.targetName.trim());
                this.assertSession(epoch);
                if (!accepted.task_id) throw new Error('服务未返回导入任务，请重试');
                this.taskId = accepted.task_id;
                this.needsReconcile = false;
            }
            return await this.waitForCompletion(epoch);
        } finally {
            if (epoch === this.epoch) this.busy = false;
        }
    }

    async reconcile(epoch = this.epoch) {
        const mirrors = await this.request('GET', '/exes');
        this.assertSession(epoch);
        const mirror = mirrors.find(item => item.slug === this.draft?.slug);
        if (!mirror) throw new Error('未找到此镜像，请返回镜像列表核对');
        if (mirror.state === 'completed') {
            this.completed = true;
            this.taskId = null;
            this.needsReconcile = false;
            return;
        }
        const { tasks } = await this.request('GET', '/tasks?limit=100');
        this.assertSession(epoch);
        if (!Array.isArray(tasks)) throw new Error('无法核对任务状态，请稍后重试');
        const active = tasks.find(task => task.slug === this.draft.slug && task.type === 'import_chat' && ['queued', 'running'].includes(task.status));
        this.taskId = active?.task_id || null;
        if (!active) {
            // 任务可能在两次读取之间完成；再次确认镜像，避免要求用户重传已完成的资料。
            const latest = await this.request('GET', '/exes');
            this.assertSession(epoch);
            const current = latest.find(item => item.slug === this.draft.slug);
            if (!current) throw new Error('未找到此镜像，请返回镜像列表核对');
            this.completed = current.state === 'completed';
        }
        this.needsReconcile = false;
    }

    async createDraft(input, epoch) {
        this.onProgress('正在准备镜像…');
        await this.request('POST', '/exes', {
            slug: this.draft.slug, name: this.draft.name,
            answers: [input.basic || '', input.personality || ''], defer_generation: true,
        });
        this.assertSession(epoch);
        this.draft.confirmed = true;
    }

    async waitForCompletion(epoch) {
        const deadline = Date.now() + 600000;
        while (Date.now() < deadline) {
            const task = await this.request('GET', `/tasks/${this.taskId}`);
            this.assertSession(epoch);
            this.onProgress(`${task.detail || '正在处理记录并生成镜像'} ${task.progress || 0}%`);
            if (task.status === 'failed') {
                this.taskId = null;
                throw new Error(task.error || '导入失败，请核对文件后重试');
            }
            if (task.status === 'succeeded') {
                this.taskId = null;
                if (!(task.result?.messages > 0) || task.result.state !== 'completed') {
                    throw new Error('记录尚未形成可对话的镜像，请重新导入');
                }
                this.completed = true;
                return task.result;
            }
            await this.wait(800);
            this.assertSession(epoch);
        }
        throw new Error('任务仍在处理，可点击“继续查看进度”，无需重新上传');
    }
};
