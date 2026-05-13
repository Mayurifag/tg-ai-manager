const chatConfig = window.TG_CHAT || {};
const chatId = chatConfig.chatId;
const topicId = chatConfig.topicId;
const isPremium = Boolean(chatConfig.isPremium);
const isChannel = Boolean(chatConfig.isChannel);

let arConfig = {
    enabled: false,
    emoji: '💩',
    target_users: []
};
let availableUsers = [];
let usersLoaded = false;

function showToast(message) {
    const x = document.getElementById('toast');
    x.textContent = message;
    x.className = 'show';
    setTimeout(function () { x.className = x.className.replace('show', ''); }, 3000);
}

const autoreadBtn = document.getElementById('btn-toggle-autoread');
if (autoreadBtn) {
    autoreadBtn.addEventListener('click', async () => {
        const indicator = autoreadBtn.querySelector('.status-indicator');
        const wasEnabled = indicator.classList.contains('on');
        const newState = !wasEnabled;

        indicator.classList.toggle('on', newState);
        indicator.classList.toggle('off', !newState);

        try {
            await fetch('/api/rules/autoread/toggle', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ chat_id: chatId, topic_id: topicId, enabled: newState })
            });
            showToast(newState ? 'Autoread Enabled' : 'Autoread Disabled');
        } catch (e) {
            console.error(e);
            indicator.classList.toggle('on', wasEnabled);
            indicator.classList.toggle('off', !wasEnabled);
            showToast('Failed to toggle Autoread');
        }
    });
}

const aiAutoreadBtn = document.getElementById('btn-toggle-ai-autoread');
if (aiAutoreadBtn) {
    aiAutoreadBtn.addEventListener('click', async () => {
        const indicator = aiAutoreadBtn.querySelector('.status-indicator');
        const wasEnabled = indicator.classList.contains('on');
        const newState = !wasEnabled;

        indicator.classList.toggle('on', newState);
        indicator.classList.toggle('off', !newState);

        try {
            await fetch('/api/rules/ai_autoread/toggle', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ chat_id: chatId, topic_id: topicId, enabled: newState })
            });
            showToast(newState ? 'AI Autoread Enabled' : 'AI Autoread Disabled');
        } catch (e) {
            console.error(e);
            indicator.classList.toggle('on', wasEnabled);
            indicator.classList.toggle('off', !wasEnabled);
            showToast('Failed to toggle AI Autoread');
        }
    });
}

const markReadBtn = document.getElementById('btn-read-chat');
if (markReadBtn) {
    markReadBtn.addEventListener('click', async () => {
        markReadBtn.style.opacity = '0.5';
        markReadBtn.style.pointerEvents = 'none';

        try {
            await fetch(`/api/chat/${chatId}/read`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ topic_id: topicId })
            });

            showToast('Marked as Read');
            window.location.href = topicId ? `/forum/${chatId}` : '/';
        } catch (e) {
            console.error(e);
            showToast('Failed to mark read');
            setTimeout(() => {
                markReadBtn.style.opacity = '1';
                markReadBtn.style.pointerEvents = 'auto';
            }, 500);
        }
    });
}

const copyTopicsBtn = document.getElementById('btn-apply-topics');
if (copyTopicsBtn) {
    copyTopicsBtn.addEventListener('click', async () => {
        if (!confirm('Enable autoread for ALL topics in this forum?')) return;

        copyTopicsBtn.style.opacity = '0.5';
        try {
            await fetch('/api/rules/autoread/apply_all', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ forum_id: chatId, enabled: true })
            });
            showToast('Applied to all topics');
        } catch (e) {
            showToast('Failed to apply');
        } finally {
            copyTopicsBtn.style.opacity = '1';
        }
    });
}

const arModal = document.getElementById('autoreact-modal');

document.getElementById('btn-open-autoreact').addEventListener('click', () => {
    arModal.classList.add('show');
    loadArSettings();
});

window.closeArModal = function () {
    arModal.classList.remove('show');
    document.getElementById('user-dropdown').classList.remove('show');
};

arModal.addEventListener('click', (e) => {
    if (e.target === arModal) closeArModal();
});

const userSelectorWrapper = document.querySelector('.user-selector-wrapper');
if (userSelectorWrapper) {
    userSelectorWrapper.addEventListener('mouseleave', () => {
        const dd = document.getElementById('user-dropdown');
        if (dd && dd.classList.contains('show')) dd.classList.remove('show');
    });
}

async function loadArSettings() {
    try {
        let url = `/api/rules/autoreact/get?chat_id=${chatId}`;
        if (topicId) url += `&topic_id=${topicId}`;
        const res = await fetch(url);
        const data = await res.json();

        arConfig.enabled = data.enabled;
        if (data.config.emoji) arConfig.emoji = data.config.emoji;
        if (data.config.target_users) arConfig.target_users = data.config.target_users;

        if (!isChannel && arConfig.target_users.length > 0) await loadUsers();

        renderArUi();
    } catch (e) {
        console.error('Failed to load AR settings', e);
    }
}

function renderArUi() {
    document.getElementById('autoreact-checkbox').checked = arConfig.enabled;

    document.querySelectorAll('.emoji-opt').forEach(el => {
        if (el.getAttribute('data-emoji') === arConfig.emoji) {
            el.classList.add('selected');
            document.getElementById('custom-emoji-input').value = '';
        } else {
            el.classList.remove('selected');
        }
    });

    const predefined = ['💩', '🤡', '❤️'];
    if (!predefined.includes(arConfig.emoji)) document.getElementById('custom-emoji-input').value = arConfig.emoji;

    if (!isChannel) renderUserDisplay();
}

async function loadUsers() {
    if (usersLoaded || isChannel) return;
    const dd = document.getElementById('user-dropdown');
    try {
        const res = await fetch(`/api/chat/${chatId}/authors`);
        const data = await res.json();
        availableUsers = data.authors;
        usersLoaded = true;
        renderUserDropdown();
        renderUserDisplay();
    } catch (e) {
        dd.innerHTML = '<div style="padding:10px; color:red;">Failed to load users</div>';
    }
}

function renderUserDisplay() {
    if (isChannel) return;

    const display = document.getElementById('user-display');
    display.innerHTML = '';

    const warningBox = document.getElementById('ar-warning-box');
    const helpText = document.getElementById('ar-help-text');

    if (arConfig.target_users.length === 0) {
        display.innerHTML = '<span style="color: #666; font-style: italic;">All incoming messages (Dangerous)</span>';
        warningBox.style.display = 'block';
        helpText.style.display = 'none';
    } else {
        warningBox.style.display = 'none';
        helpText.style.display = 'block';

        arConfig.target_users.forEach(uid => {
            const user = availableUsers.find(u => u.id === uid);
            const name = user ? user.name : `ID: ${uid}`;

            let avatarHtml = '';
            if (user && user.avatar_url) {
                avatarHtml = `<img src="${window.TG.escapeHtml(user.avatar_url)}" style="width:16px;height:16px;border-radius:50%;margin-right:5px;object-fit:cover;">`;
            } else {
                const initial = (user ? user.name : '?').charAt(0).toUpperCase();
                avatarHtml = `<div style="width:16px;height:16px;background:#555;border-radius:50%;margin-right:5px;display:flex;align-items:center;justify-content:center;font-size:10px;color:#fff;">${window.TG.escapeHtml(initial)}</div>`;
            }

            const chip = document.createElement('div');
            chip.className = 'user-chip';
            chip.innerHTML = `${avatarHtml} ${window.TG.escapeHtml(name)} <span style="margin-left:5px; cursor:pointer;" onclick="event.stopPropagation(); removeUser(${uid})">&times;</span>`;
            display.appendChild(chip);
        });
    }
}

function renderUserDropdown() {
    const dd = document.getElementById('user-dropdown');
    dd.innerHTML = '';
    availableUsers.forEach(u => {
        const isSelected = arConfig.target_users.includes(u.id);
        const item = document.createElement('div');
        item.className = 'user-item ' + (isSelected ? 'active' : '');

        let avatar = '';
        if (u.avatar_url) {
            avatar = `<img src="${window.TG.escapeHtml(u.avatar_url)}" style="width:24px;height:24px;border-radius:50%;margin-right:10px;object-fit:cover;">`;
        } else {
            avatar = `<div style="width:24px;height:24px;background:#555;border-radius:50%;margin-right:10px;display:flex;align-items:center;justify-content:center;color:#fff;">${window.TG.escapeHtml(u.name.charAt(0).toUpperCase())}</div>`;
        }

        item.innerHTML = `${avatar} <div>${window.TG.escapeHtml(u.name)} <div style="font-size:0.75rem;color:#888;">${window.TG.escapeHtml(u.username || '')}</div></div>`;
        item.onclick = (e) => {
            e.stopPropagation();
            toggleUser(u.id);
        };
        dd.appendChild(item);
    });
}

window.toggleUserDropdown = function () {
    if (isChannel) return;
    const dd = document.getElementById('user-dropdown');
    if (!dd.classList.contains('show')) loadUsers();
    dd.classList.toggle('show');
};

function toggleUser(uid) {
    if (arConfig.target_users.includes(uid)) {
        arConfig.target_users = arConfig.target_users.filter(id => id !== uid);
    } else {
        arConfig.target_users.push(uid);
    }
    renderUserDisplay();
    renderUserDropdown();
}

window.removeUser = function (uid) {
    arConfig.target_users = arConfig.target_users.filter(id => id !== uid);
    renderUserDisplay();
    renderUserDropdown();
};

document.querySelectorAll('.emoji-opt').forEach(el => {
    el.addEventListener('click', () => {
        arConfig.emoji = el.getAttribute('data-emoji');
        renderArUi();
    });
});

document.getElementById('custom-emoji-input').addEventListener('input', (e) => {
    if (e.target.value) {
        arConfig.emoji = e.target.value;
        document.querySelectorAll('.emoji-opt').forEach(opt => opt.classList.remove('selected'));
    }
});

document.getElementById('autoreact-checkbox').addEventListener('change', (e) => {
    arConfig.enabled = e.target.checked;
});

document.getElementById('save-ar-btn').addEventListener('click', async () => {
    const btn = document.getElementById('save-ar-btn');
    btn.textContent = 'Saving...';
    btn.disabled = true;
    try {
        const targets = isChannel ? [] : arConfig.target_users;

        await fetch('/api/rules/autoreact/config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                chat_id: chatId,
                topic_id: topicId,
                enabled: arConfig.enabled,
                config: {
                    emoji: arConfig.emoji,
                    target_users: targets
                }
            })
        });

        const ind = document.getElementById('autoreact-indicator');
        if (arConfig.enabled) {
            ind.className = targets.length === 0 ? 'status-indicator status-all' : 'status-indicator status-some';
        } else {
            ind.className = 'status-indicator status-off';
        }

        btn.textContent = 'Saved!';
        setTimeout(() => {
            btn.textContent = 'Save Configuration';
            btn.disabled = false;
            closeArModal();
        }, 800);
        showToast('Autoreact Configured');
    } catch (e) {
        btn.textContent = 'Error';
        btn.disabled = false;
    }
});

const container = document.getElementById('messages-container');
const currentTopicId = topicId;
let isLoading = false;
let allLoaded = false;

window.scrollTo(0, document.body.scrollHeight);
window.renderLocalTimes(container);

window.addEventListener('scroll', () => {
    if (isLoading || allLoaded) return;
    if (window.scrollY < 100) loadOlderMessages();
});

async function loadOlderMessages() {
    isLoading = true;
    const indicator = document.getElementById('loading-indicator');
    indicator.style.display = 'block';

    const messages = container.querySelectorAll('.message-row');
    if (messages.length === 0) {
        isLoading = false;
        return;
    }

    const oldestMsg = messages[messages.length - 1];
    const offsetId = oldestMsg.getAttribute('data-id');
    const previousHeight = document.body.scrollHeight;

    try {
        let url = `/api/chat/${chatId}/history?offset_id=${offsetId}`;
        if (currentTopicId) url += `&topic_id=${currentTopicId}`;

        const response = await fetch(url);
        const data = await response.json();

        if (data.count === 0) {
            allLoaded = true;
        } else {
            container.insertAdjacentHTML('beforeend', data.html);
            window.parseAppleEmojis(container);
            window.renderLocalTimes(container);
            const newHeight = document.body.scrollHeight;
            window.scrollTo(0, newHeight - previousHeight);
        }
    } catch (error) {
        console.error('Failed to load history:', error);
    } finally {
        isLoading = false;
        indicator.style.display = 'none';
    }
}

window.toggleReaction = async function (el, msgId, emoji) {
    const reactionContainer = el.parentNode;
    const isChosen = el.classList.contains('chosen');
    const countSpan = el.querySelector('.reaction-count');
    let count = parseInt(countSpan ? countSpan.textContent : '0');

    if (!isPremium) {
        const siblings = reactionContainer.querySelectorAll('.reaction-pill.chosen');
        siblings.forEach(sib => {
            if (sib !== el) {
                sib.classList.remove('chosen');
                const sibCountSpan = sib.querySelector('.reaction-count');
                let sibCount = parseInt(sibCountSpan.textContent);
                if (sibCount > 0) sibCountSpan.textContent = sibCount - 1;
            }
        });
    }

    if (isChosen) {
        el.classList.remove('chosen');
        if (count > 0) countSpan.textContent = count - 1;
    } else {
        el.classList.add('chosen');
        countSpan.textContent = count + 1;
        el.style.transform = 'scale(1.2)';
        setTimeout(() => el.style.transform = '', 150);
    }

    try {
        await fetch(`/api/chat/${chatId}/message/${msgId}/reaction`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ reaction: emoji.toString() })
        });
    } catch (e) {
        console.error('Failed to toggle reaction', e);
    }
};

window.debugProcessMessage = async function (el, msgId) {
    el.style.opacity = '0.5';
    try {
        const res = await fetch('/api/debug/process', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ chat_id: chatId, msg_id: msgId })
        });
        const data = await res.json();
        let text = 'Debug:\n';
        if (data.autoread) text += `Read: ${data.autoread.status} (${data.autoread.reason})\n`;
        if (data.autoreact) text += `React: ${data.autoreact.status} (${data.autoreact.detail})`;
        showToast(text);
    } catch (e) {
        console.error(e);
        showToast('Debug Request Failed');
    } finally {
        el.style.opacity = '1';
    }
};

function buildReactionsHtml(msgId, reactions) {
    let html = '';
    reactions.forEach(r => {
        const chosenClass = r.is_chosen ? 'chosen' : '';
        const emojiVal = r.custom_emoji_id || r.emoji;
        let innerContent = '';
        if (r.custom_emoji_id) {
            innerContent = `<img src="/media/custom_emoji/${r.custom_emoji_id}" style="width: 20px; height: 20px; margin-right: 4px;">`;
        } else {
            innerContent = `<span>${window.TG.escapeHtml(r.emoji)}\uFE0F</span>`;
        }
        html += `
        <div class="reaction-pill ${chosenClass}" data-emoji="${window.TG.escapeHtml(emojiVal)}"
            onclick="toggleReaction(this, ${msgId}, '${emojiVal}')">
            ${innerContent}
            <span class="reaction-count">${r.count}</span>
        </div>`;
    });
    return html;
}

document.addEventListener('tg:event', (e) => {
    const data = e.detail;
    if (data.chat_id !== chatId) return;
    if (currentTopicId && data.topic_id !== currentTopicId) {
        if (data.topic_id) return;
    }

    if (data.type === 'message' && data.rendered_html) {
        const tempDiv = document.createElement('div');
        tempDiv.innerHTML = data.rendered_html;
        window.parseAppleEmojis(tempDiv);
        window.renderLocalTimes(tempDiv);
        container.insertAdjacentHTML('afterbegin', tempDiv.innerHTML);
        if (window.scrollY > document.body.scrollHeight - window.innerHeight - 300) {
            window.scrollTo(0, document.body.scrollHeight);
        }
    } else if (data.type === 'edited' && data.message_model) {
        const msgId = data.message_model.id;
        const msgRow = document.getElementById(`msg-${msgId}`);
        if (msgRow) {
            const textEl = msgRow.querySelector('.text');
            if (textEl) {
                textEl.innerHTML = data.message_model.text;
                window.parseAppleEmojis(textEl);
            }
        }
    } else if (data.type === 'deleted' && data.message_model) {
        const msgId = data.message_model.id;
        const msgRow = document.getElementById(`msg-${msgId}`);
        if (msgRow) {
            msgRow.style.opacity = '0';
            setTimeout(() => msgRow.remove(), 300);
        }
    } else if (data.type === 'reaction_update' && data.message_model) {
        const msgId = data.message_model.id;
        const reactionsContainer = document.getElementById(`reactions-${msgId}`);
        if (reactionsContainer) {
            reactionsContainer.innerHTML = buildReactionsHtml(msgId, data.message_model.reactions);
            window.parseAppleEmojis(reactionsContainer);
        }
    }
});
