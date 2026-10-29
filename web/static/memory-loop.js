/* 会话恢复、回答参考和显式纠正；不在浏览器持久化聊天正文。 */
(function () {
    const conversationIdPattern = /^[A-Za-z0-9_-]{1,128}$/;
    const sourceKinds = {
        imported_chat: '导入聊天', narrative: '口述资料',
        generated_summary: 'AI 生成摘要', conversation_history: '应用内对话',
        unknown: '未分类资料',
    };
    const sourceLabels = {web: '网页', mcp: '聊天软件', astrbot: '聊天软件', cli: '命令行', wechat: '微信', qq: 'QQ', telegram: 'Telegram'};
    const speakerLabels = {target: '目标人物', self: '用户', user: '用户', mixed: '多位说话人'};

    function selectionKey(userId, slug) {
        return `ex-memory-conversation:v1:${userId}:${encodeURIComponent(slug)}`;
    }

    function create(options) {
        const { document, request, identity, storage, renderMessages, setBusy, notify } = options;
        const node = id => document.getElementById(id);
        const select = node('conversation-select');
        const status = node('conversation-status');
        const dialog = node('correction-dialog');
        const activeRequests = new Set();
        let generation = 0;
        let userId = null;
        let slug = '';
        let conversationId = 'web';
        let conversations = [];
        let loading = false;
        let correction = null;
        let correctionOperation = 0;

        function capture() {
            return { generation, epoch: identity().epoch, slug, conversationId };
        }

        function isCurrent(view) {
            const active = identity();
            return view && view.generation === generation && view.epoch === active.epoch
                && view.slug === slug && view.slug === active.slug
                && view.conversationId === conversationId;
        }

        function closeCorrection() {
            correctionOperation++;
            correction = null;
            node('correction-content').value = '';
            node('correction-answer').textContent = '';
            node('correction-source').textContent = '';
            node('correction-result').textContent = '';
            node('correction-save').disabled = false;
            if (dialog.open) dialog.close();
        }

        function cancelRequests() {
            activeRequests.forEach(controller => controller.abort());
            activeRequests.clear();
        }

        function busy(value) {
            loading = value;
            select.disabled = value;
            node('conversation-new').disabled = value;
            node('conversation-refresh').disabled = value;
            setBusy(value);
        }

        function reset() {
            generation++;
            cancelRequests();
            closeCorrection();
            slug = '';
            userId = null;
            conversationId = 'web';
            conversations = [];
            select.replaceChildren();
            status.textContent = '';
            node('conversation-bar').hidden = true;
            loading = false;
        }

        function saveSelection() {
            if (userId === null || !slug) return;
            try { storage.setItem(selectionKey(userId, slug), conversationId); } catch (_) { /* 私密模式仍可聊天。 */ }
        }

        function renderOptions() {
            select.replaceChildren();
            conversations.forEach(item => {
                const option = document.createElement('option');
                option.value = item.conversation_id;
                const origin = sourceLabels[item.source] || '其他入口';
                const explicitLabel = item.label && item.label !== item.conversation_id;
                const time = typeof item.updated_at === 'string' ? item.updated_at.replace('T', ' ').slice(5, 16) : '';
                option.textContent = explicitLabel
                    ? `${item.label}${item.source && item.source !== 'web' ? ` · ${origin}` : ''}`
                    : `${item.source === 'web' || item.conversation_id.startsWith('web') ? '网页会话' : '聊天软件会话'}${time ? ` · ${time}` : ''}`;
                select.appendChild(option);
            });
            select.value = conversationId;
        }

        async function readMessages(view) {
            const result = await request('GET', `/exes/${encodeURIComponent(slug)}/conversations/${encodeURIComponent(conversationId)}/messages`);
            if (!isCurrent(view)) return;
            if (String(result.user_id) !== String(userId) || result.conversation_id !== conversationId) {
                throw new Error('会话身份不匹配，请重新读取');
            }
            const messages = (Array.isArray(result.messages) ? result.messages : []).filter(message =>
                message && ['user', 'assistant'].includes(message.role) && typeof message.content === 'string');
            renderMessages(messages, view);
            saveSelection();
            const archiveLabels = {
                pending: '有尚未归档的对话', running: '长期记忆整理中',
                failed: '长期记忆整理未完成，下次对话可重试', complete: '长期记忆已更新',
            };
            const archiveLabel = archiveLabels[result.archive?.state];
            status.textContent = `已恢复 ${messages.length} 条消息${archiveLabel ? ` · ${archiveLabel}` : ''}`;
        }

        async function open(mirrorSlug) {
            generation++;
            cancelRequests();
            closeCorrection();
            slug = mirrorSlug;
            userId = null;
            conversationId = 'web';
            node('conversation-bar').hidden = false;
            status.textContent = '正在读取会话…';
            busy(true);
            const initial = capture();
            try {
                const result = await request('GET', `/exes/${encodeURIComponent(slug)}/conversations`);
                if (!isCurrent(initial)) return;
                if (!/^[1-9][0-9]*$/.test(String(result.user_id))) throw new Error('无法确认当前账号');
                userId = String(result.user_id);
                const seen = new Set();
                conversations = (Array.isArray(result.conversations) ? result.conversations : []).filter(item => {
                    if (!item || !conversationIdPattern.test(item.conversation_id) || seen.has(item.conversation_id)) return false;
                    seen.add(item.conversation_id);
                    return true;
                });
                const defaultId = conversationIdPattern.test(result.default_conversation_id) ? result.default_conversation_id : 'web';
                if (!seen.has(defaultId)) conversations.unshift({ conversation_id: defaultId, label: '网页会话', source: 'web' });
                let saved = '';
                try { saved = storage.getItem(selectionKey(userId, slug)) || ''; } catch (_) { /* 无持久存储时使用默认会话。 */ }
                conversationId = conversations.some(item => item.conversation_id === saved) ? saved : defaultId;
                renderOptions();
                const view = capture();
                await readMessages(view);
                if (isCurrent(view)) busy(false);
            } catch (error) {
                // 切账号/切镜像之后，旧请求的错误不能覆盖新页面。
                if (initial.generation !== generation || initial.epoch !== identity().epoch || initial.slug !== identity().slug) return;
                status.textContent = error.message || '会话读取失败，请重试';
                select.disabled = false;
                node('conversation-refresh').disabled = false;
                node('conversation-new').disabled = false;
                loading = true;
                setBusy(true);
            }
        }

        async function choose(nextId) {
            if (!conversations.some(item => item.conversation_id === nextId)) return;
            generation++;
            cancelRequests();
            closeCorrection();
            conversationId = nextId;
            select.value = nextId;
            const view = capture();
            renderMessages([], view);
            status.textContent = '正在恢复会话…';
            busy(true);
            try {
                await readMessages(view);
                if (isCurrent(view)) busy(false);
            } catch (error) {
                if (!isCurrent(view)) return;
                status.textContent = error.message || '会话读取失败';
                select.disabled = false;
                node('conversation-refresh').disabled = false;
                loading = true;
                setBusy(true);
            }
        }

        function newConversation() {
            if (!slug || userId === null) return;
            generation++;
            cancelRequests();
            closeCorrection();
            const uuid = options.randomId ? options.randomId() : crypto.randomUUID();
            conversationId = `web_${uuid.replace(/-/g, '')}`;
            conversations.push({ conversation_id: conversationId, label: '新网页会话', source: 'web' });
            renderOptions();
            renderMessages([], capture());
            status.textContent = '新会话将在发送第一条消息后保存';
            busy(false);
        }

        function openCorrection(metadata, answer, source) {
            if (!metadata.message_id || metadata.conversation_id !== conversationId || loading) return;
            closeCorrection();
            correction = { view: capture(), messageId: metadata.message_id, sourceIds: source ? [source.id] : [] };
            node('correction-answer').textContent = answer.slice(0, 1200);
            node('correction-source').textContent = source ? source.text : '';
            node('correction-source').hidden = !source;
            dialog.showModal();
            node('correction-content').focus();
        }

        async function submitCorrection() {
            const selected = correction;
            const content = node('correction-content').value.trim();
            if (!selected || !isCurrent(selected.view)) { closeCorrection(); return; }
            if (!content || content.length > 500) {
                node('correction-result').textContent = '请填写 1–500 字的纠正内容';
                return;
            }
            if (node('correction-save').disabled) return;
            const operation = ++correctionOperation;
            node('correction-save').disabled = true;
            node('correction-result').textContent = '正在保存…';
            try {
                const result = await request('POST', `/exes/${encodeURIComponent(selected.view.slug)}/corrections`, {
                    content, message_id: selected.messageId,
                    conversation_id: selected.view.conversationId, source_ids: selected.sourceIds,
                }, 0);
                if (operation !== correctionOperation || !isCurrent(selected.view)) return;
                if (result.ok !== true) throw new Error('纠正未保存，请重试');
                closeCorrection();
                notify('已保存纠正，后续回答将使用这条记录', 'success');
            } catch (error) {
                if (operation !== correctionOperation || !isCurrent(selected.view)) return;
                node('correction-result').textContent = error.message || '保存失败，请重试';
                node('correction-save').disabled = false;
            }
        }

        function decorate(row, metadata, view) {
            if (!isCurrent(view) || metadata.conversation_id !== view.conversationId) return;
            row.querySelector('.answer-tools')?.remove();
            const answer = row.querySelector('.msg')?.textContent || '';
            const tools = document.createElement('div');
            tools.className = 'answer-tools';
            const correct = document.createElement('button');
            correct.type = 'button';
            correct.className = 'answer-correct';
            correct.textContent = '纠正';
            correct.disabled = !metadata.message_id;
            correct.title = metadata.message_id ? '记录事实或表达习惯的纠正' : '这条回复未保存，暂不能纠正';
            correct.addEventListener('click', () => {
                if (isCurrent(view)) openCorrection(metadata, answer);
            });
            tools.appendChild(correct);
            const sources = Array.isArray(metadata.sources) ? metadata.sources.slice(0, 12) : [];
            const details = document.createElement('details');
            details.className = 'answer-sources';
            const summary = document.createElement('summary');
            summary.textContent = `本次检索参考${sources.length ? `（${sources.length}）` : ''}`;
            details.appendChild(summary);
            const explanation = document.createElement('p');
            explanation.textContent = sources.length ? '这些是本次生成时提供给模型的参考，不表示每句话都已被资料证实。' : '这条回答没有可展示的检索参考。';
            details.appendChild(explanation);
            sources.forEach(source => {
                if (!source || typeof source.text !== 'string') return;
                const item = document.createElement('section');
                const label = document.createElement('p');
                label.className = 'source-label';
                label.textContent = [sourceKinds[source.kind] || sourceKinds.unknown, sourceLabels[source.source] || source.source, speakerLabels[source.speaker] || source.speaker, source.start_ts, source.end_ts && source.end_ts !== source.start_ts ? source.end_ts : ''].filter(Boolean).join(' · ');
                const quote = document.createElement('blockquote');
                quote.textContent = source.text;
                item.appendChild(label);
                item.appendChild(quote);
                if (metadata.message_id && typeof source.id === 'string') {
                    const use = document.createElement('button');
                    use.type = 'button';
                    use.textContent = '据此纠正回答';
                    use.addEventListener('click', () => {
                        if (isCurrent(view)) openCorrection(metadata, answer, source);
                    });
                    item.appendChild(use);
                }
                details.appendChild(item);
            });
            tools.appendChild(details);
            row.appendChild(tools);
            row.classList.add('has-answer-tools');
        }

        select.addEventListener('change', () => choose(select.value));
        node('conversation-refresh').addEventListener('click', () => open(slug));
        node('conversation-new').addEventListener('click', newConversation);
        node('correction-cancel').addEventListener('click', closeCorrection);
        dialog.addEventListener('cancel', closeCorrection);
        node('correction-form').addEventListener('submit', event => { event.preventDefault(); submitCorrection(); });

        return {
            open, choose, reset, capture, isCurrent, decorate, newConversation, submitCorrection,
            get conversationId() { return conversationId; },
            get ready() { return !!slug && userId !== null && !loading; },
            track(controller) { activeRequests.add(controller); },
            untrack(controller) { activeRequests.delete(controller); },
        };
    }

    window.ExMemoryMemoryLoop = { create, selectionKey };
})();
