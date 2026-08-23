/* ex-memory 趣味效果：表情雨 + 反应 + 烟花 + 撒花 + 摇一摇 */

// ═══════════════════════════════════════
// 表情雨效果
// ═══════════════════════════════════════

const EMOJI_RAIN_TRIGGERS = {
    '❤️': ['❤️', '💕', '💖', '💗', '💓'],
    '🎉': ['🎉', '🎊', '🎈', '🎆', '🎇'],
    '⭐': ['⭐', '🌟', '✨', '💫', '🌠'],
    '🔥': ['🔥', '💥', '💫', '⚡', '✨'],
    '🌹': ['🌹', '🌸', '🌺', '🌻', '🌷'],
};

function triggerEmojiRain(emoji) {
    const container = document.createElement('div');
    container.className = 'emoji-rain-container';
    document.body.appendChild(container);

    const emojis = EMOJI_RAIN_TRIGGERS[emoji] || [emoji];
    const count = 30;

    for (let i = 0; i < count; i++) {
        setTimeout(() => {
            const el = document.createElement('div');
            el.className = 'emoji-rain-item';
            el.textContent = emojis[Math.floor(Math.random() * emojis.length)];
            el.style.left = Math.random() * 100 + 'vw';
            el.style.animationDuration = (2 + Math.random() * 3) + 's';
            el.style.animationDelay = Math.random() * 0.5 + 's';
            el.style.fontSize = (20 + Math.random() * 30) + 'px';
            container.appendChild(el);
        }, i * 50);
    }

    setTimeout(() => container.remove(), 5000);
}

// 检测消息中的触发词
function checkEmojiRain(text) {
    for (const trigger of Object.keys(EMOJI_RAIN_TRIGGERS)) {
        if (text.includes(trigger)) {
            triggerEmojiRain(trigger);
            break;
        }
    }
}

// ═══════════════════════════════════════
// 表情反应（长按消息添加表情）
// ═══════════════════════════════════════

const QUICK_REACTIONS = ['❤️', '👍', '😂', '😮', '😢', '🔥'];

function showReactionPicker(msgRow, x, y) {
    let picker = document.getElementById('reaction-picker');
    if (!picker) {
        picker = document.createElement('div');
        picker.id = 'reaction-picker';
        picker.className = 'reaction-picker';
        document.body.appendChild(picker);
    }

    picker.innerHTML = QUICK_REACTIONS.map(emoji =>
        `<button class="reaction-btn" data-emoji="${emoji}">${emoji}</button>`
    ).join('');

    picker.querySelectorAll('.reaction-btn').forEach(btn => {
        btn.onclick = () => {
            addReaction(msgRow, btn.dataset.emoji);
            picker.style.display = 'none';
        };
    });

    picker.style.left = Math.min(x, window.innerWidth - 200) + 'px';
    picker.style.top = Math.max(y - 50, 10) + 'px';
    picker.style.display = 'flex';
}

function addReaction(msgRow, emoji) {
    let reactionContainer = msgRow.querySelector('.msg-reactions');
    if (!reactionContainer) {
        reactionContainer = document.createElement('div');
        reactionContainer.className = 'msg-reactions';
        msgRow.appendChild(reactionContainer);
    }

    const reaction = document.createElement('span');
    reaction.className = 'msg-reaction';
    reaction.textContent = emoji;
    reaction.onclick = () => reaction.remove();
    reactionContainer.appendChild(reaction);

    // 动画效果
    reaction.style.animation = 'reactionPop 0.3s cubic-bezier(0.175, 0.885, 0.32, 1.275)';
}

// 关闭反应选择器
document.addEventListener('click', (e) => {
    const picker = document.getElementById('reaction-picker');
    if (picker && !e.target.closest('.reaction-picker')) {
        picker.style.display = 'none';
    }
});

// ═══════════════════════════════════════
// 消息发送烟花效果
// ═══════════════════════════════════════

function showFireworks(x, y) {
    const container = document.createElement('div');
    container.className = 'fireworks-container';
    container.style.left = x + 'px';
    container.style.top = y + 'px';
    document.body.appendChild(container);

    const colors = ['#7c6cff', '#c4a0e8', '#ff6b9d', '#ffd93d', '#6bcb77'];
    const particles = 20;

    for (let i = 0; i < particles; i++) {
        const particle = document.createElement('div');
        particle.className = 'firework-particle';
        particle.style.background = colors[Math.floor(Math.random() * colors.length)];
        particle.style.setProperty('--angle', (360 / particles * i) + 'deg');
        particle.style.setProperty('--distance', (50 + Math.random() * 100) + 'px');
        container.appendChild(particle);
    }

    setTimeout(() => container.remove(), 1000);
}

// ═══════════════════════════════════════
// 双击点赞特效
// ═══════════════════════════════════════

let lastClickTime = 0;
let lastClickTarget = null;

document.addEventListener('click', (e) => {
    const msgRow = e.target.closest('.msg-row');
    if (!msgRow) return;

    const now = Date.now();
    if (lastClickTarget === msgRow && now - lastClickTime < 300) {
        // 双击
        showReactionPicker(msgRow, e.clientX, e.clientY);
        showConfetti(e.clientX, e.clientY);
        lastClickTime = 0;
        lastClickTarget = null;
    } else {
        lastClickTime = now;
        lastClickTarget = msgRow;
    }
});

// ═══════════════════════════════════════
// 撒花效果
// ═══════════════════════════════════════

function showConfetti(x, y) {
    const container = document.createElement('div');
    container.className = 'confetti-container';
    document.body.appendChild(container);

    const colors = ['#7c6cff', '#c4a0e8', '#ff6b9d', '#ffd93d', '#6bcb77', '#4ecdc4'];
    const count = 30;

    for (let i = 0; i < count; i++) {
        const confetti = document.createElement('div');
        confetti.className = 'confetti';
        confetti.style.left = x + 'px';
        confetti.style.top = y + 'px';
        confetti.style.background = colors[Math.floor(Math.random() * colors.length)];
        confetti.style.width = (5 + Math.random() * 10) + 'px';
        confetti.style.height = (5 + Math.random() * 10) + 'px';
        confetti.style.borderRadius = Math.random() > 0.5 ? '50%' : '2px';
        confetti.style.setProperty('--angle', (Math.random() * 360) + 'deg');
        confetti.style.animationDuration = (1 + Math.random() * 2) + 's';
        confetti.style.animationDelay = Math.random() * 0.3 + 's';
        container.appendChild(confetti);
    }

    setTimeout(() => container.remove(), 3000);
}

// ═══════════════════════════════════════
// 摇一摇彩蛋
// ═══════════════════════════════════════

let shakeThreshold = 15;
let lastShakeTime = 0;

if (window.DeviceMotionEvent) {
    window.addEventListener('devicemotion', (e) => {
        const acc = e.accelerationIncludingGravity;
        if (!acc) return;

        const now = Date.now();
        if (now - lastShakeTime < 1000) return;

        const force = Math.abs(acc.x) + Math.abs(acc.y) + Math.abs(acc.z);
        if (force > shakeThreshold * 3) {
            lastShakeTime = now;
            triggerShakeEffect();
        }
    });
}

function triggerShakeEffect() {
    // 随机效果
    const effects = [
        () => showConfetti(window.innerWidth / 2, window.innerHeight / 2),
        () => triggerEmojiRain('🎉'),
        () => triggerEmojiRain('❤️'),
        () => showFireworks(window.innerWidth / 2, window.innerHeight / 2),
    ];

    const effect = effects[Math.floor(Math.random() * effects.length)];
    effect();

    showToast('🎉 摇一摇彩蛋！', 'success');
}

// 桌面端：键盘快捷键触发
document.addEventListener('keydown', (e) => {
    // Ctrl+Shift+S 触发撒花
    if (e.ctrlKey && e.shiftKey && e.key === 'S') {
        e.preventDefault();
        showConfetti(window.innerWidth / 2, window.innerHeight / 2);
    }
    // Ctrl+Shift+E 触发表情雨
    if (e.ctrlKey && e.shiftKey && e.key === 'E') {
        e.preventDefault();
        triggerEmojiRain('🎉');
    }
});
