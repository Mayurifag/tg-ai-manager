let arConfig = {
    enabled: false,
    emoji: '💩',
    target_users: []
};
let availableUsers = [];
let usersLoaded = false;

// --- Toast Logic ---
function showToast(message) {
    const x = document.getElementById("toast");
    x.textContent = message;
    x.className = "show";
    setTimeout(function () { x.className = x.className.replace("show", ""); }, 3000);
}

// --- Autoread Logic ---
function initAutoread(chatId, topicId) {
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
                showToast(newState ? "Autoread Enabled" : "Autoread Disabled");
            } catch (e) {
                console.error(e);
                indicator.classList.toggle('on', wasEnabled);
                indicator.classList.toggle('off', !wasEnabled);
                showToast("Failed to toggle Autoread");
            }
        });
    }
}

// --- Mark Read Logic ---
function initMarkRead(chatId, topicId) {
    const markReadBtn = document.getElementById('btn-read-chat');
    if (markReadBtn) {
        markReadBtn.addEventListener('click', async () => {
            markReadBtn.style.opacity = '0.5';
            markReadBtn.style.pointerEvents = 'none';

            try {
                const url = `/api/chat/${chatId}/read`;
                await fetch(url, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ topic_id: topicId })
                });

                showToast("Marked as Read");
                if (topicId) {
                    window.location.href = `/forum/${chatId}`;
                } else {
                    window.location.href = `/`;
                }
            } catch (e) {
                console.error(e);
                showToast("Failed to mark read");
                setTimeout(() => {
                    markReadBtn.style.opacity = '1';
                    markReadBtn.style.pointerEvents = 'auto';
                }, 500);
            }
        });
    }
}

// --- Autoreact Modal Logic ---
function initAutoreact(chatId, topicId, isChannel) {
    const arModal = document.getElementById('autoreact-modal');
    if (!arModal) return;

    document.getElementById('btn-open-autoreact').addEventListener('click', () => {
        arModal.classList.add('show');
        loadArSettings(chatId, topicId, isChannel);
    });

    window.closeArModal = function() {
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
            if (dd && dd.classList.contains('show')) {
                dd.classList.remove('show');
            }
        });
    }

    document.getElementById('autoreact-checkbox').addEventListener('change', (e) => {
        arConfig.enabled = e.target.checked;
    });

    document.querySelectorAll('.emoji-opt').forEach(el => {
        el.addEventListener('click', () => {
            arConfig.emoji = el.getAttribute('data-emoji');
            renderArUi(isChannel);
        });
    });

    document.getElementById('custom-emoji-input').addEventListener('input', (e) => {
        if (e.target.value) {
            arConfig.emoji = e.target.value;
            document.querySelectorAll('.emoji-opt').forEach(opt => opt.classList.remove('selected'));
        }
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
                    config: { emoji: arConfig.emoji, target_users: targets }
                })
            });

            const ind = document.getElementById('autoreact-indicator');
            if (arConfig.enabled) {
                if (targets.length === 0) ind.className = 'status-indicator status-all';
                else ind.className = 'status-indicator status-some';
            } else {
                ind.className = 'status-indicator status-off';
            }

            btn.textContent = 'Saved!';
            setTimeout(() => {
                btn.textContent = 'Save Configuration';
                btn.disabled = false;
                closeArModal();
            }, 800);
            showToast("Autoreact Configured");
        } catch (e) {
            btn.textContent = 'Error';
            btn.disabled = false;
        }
    });
}

async function loadArSettings(chatId, topicId, isChannel) {
    try {
        let url = `/api/rules/autoreact/get?chat_id=${chatId}`;
        if (topicId) url += `&topic_id=${topicId}`;
        const res = await fetch(url);
        const data = await res.json();

        arConfig.enabled = data.enabled;
        if (data.config.emoji) arConfig.emoji = data.config.emoji;
        if (data.config.target_users) arConfig.target_users = data.config.target_users;

        if (!isChannel && arConfig.target_users.length > 0) {
            await loadUsers(chatId);
        }
        renderArUi(isChannel);
    } catch (e) { console.error("Failed to load AR settings", e); }
}

function renderArUi(isChannel) {
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
    if (!predefined.includes(arConfig.emoji)) {
        document.getElementById('custom-emoji-input').value = arConfig.emoji;
    }

    if (!isChannel) renderUserDisplay();
}

async function loadUsers(chatId) {
    if (usersLoaded) return;
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
            const initial = name.charAt(0).toUpperCase();
            
            let avatarHtml = `<div style="width:16px;height:16px;background:#555;border-radius:50%;margin-right:5px;display:flex;align-items:center;justify-content:center;font-size:10px;color:#fff;">${initial}</div>`;
            if (user && user.avatar_url) {
                avatarHtml = `<img src="${user.avatar_url}" style="width:16px;height:16px;border-radius:50%;margin-right:5px;object-fit:cover;">`;
            }

            const chip = document.createElement('div');
            chip.className = 'user-chip';
            chip.innerHTML = `${avatarHtml} ${name} <span style="margin-left:5px; cursor:pointer;" onclick="event.stopPropagation(); removeUser(${uid})">&times;</span>`;
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
        
        const initial = u.name.charAt(0).toUpperCase();
        let avatar = `<div style="width:24px;height:24px;background:#555;border-radius:50%;margin-right:10px;display:flex;align-items:center;justify-content:center;color:#fff;">${initial}</div>`;
        if (u.avatar_url) {
            avatar = `<img src="${u.avatar_url}" style="width:24px;height:24px;border-radius:50%;margin-right:10px;object-fit:cover;">`;
        }

        item.innerHTML = `${avatar} <div>${u.name} <div style="font-size:0.75rem;color:#888;">${u.username || ''}</div></div>`;
        item.onclick = (e) => {
            event.stopPropagation();
            toggleUser(u.id);
        };
        dd.appendChild(item);
    });
}

window.toggleUserDropdown = function() {
    const dd = document.getElementById('user-dropdown');
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

window.removeUser = function(uid) {
    arConfig.target_users = arConfig.target_users.filter(id => id !== uid);
    renderUserDisplay();
    renderUserDropdown();
};

// --- Infinite Scroll Logic ---
function initInfiniteScroll(chatId, topicId) {
    const container = document.getElementById('messages-container');
    let isLoading = false;
    let allLoaded = false;

    window.scrollTo(0, document.body.scrollHeight);
    window.renderLocalTimes(container);

    window.addEventListener('scroll', () => {
        if (isLoading || allLoaded) return;
        if (window.scrollY < 100) loadOlderMessages(chatId, topicId);
    });

    async function loadOlderMessages(chatId, topicId) {
        isLoading = true;
        const indicator = document.getElementById('loading-indicator');
        indicator.style.display = 'block';

        const messages = container.querySelectorAll('.message-row');
        if (messages.length === 0) { isLoading = false; return; }

        const oldestMsg = messages[messages.length - 1];
        const offsetId = oldestMsg.getAttribute('data-id');
        const previousHeight = document.body.scrollHeight;

        try {
            let url = `/api/chat/${chatId}/history?offset_id=${offsetId}`;
            if (topicId) url += `&topic_id=${topicId}`;

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
}

// --- Reactions Logic ---
async function toggleReaction(el, msgId, emoji, chatId, isPremium) {
    const container = el.parentNode;
    const isChosen = el.classList.contains('chosen');
    const countSpan = el.querySelector('.reaction-count');
    let count = parseInt(countSpan ? countSpan.textContent : '0');

    if (!isPremium) {
        const siblings = container.querySelectorAll('.reaction-pill.chosen');
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
        const url = `/api/chat/${chatId}/message/${msgId}/reaction`;
        await fetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ reaction: emoji.toString() })
        });
    } catch (e) {
        console.error('Failed to toggle reaction', e);
    }
}

function buildReactionsHtml(msgId, reactions, chatId, isPremium) {
    let html = '';
    reactions.forEach(r => {
        const chosenClass = r.is_chosen ? 'chosen' : '';
        const emojiVal = r.custom_emoji_id || r.emoji;
        let innerContent = '';
        if (r.custom_emoji_id) {
            innerContent = `<img src="/media/custom_emoji/${r.custom_emoji_id}" style="width: 20px; height: 20px; margin-right: 4px;">`;
        } else {
            innerContent = `<span>${r.emoji}\uFE0F</span>`;
        }
        
        // Note: onclick handler needs to handle args. 
        // We'll rely on global exposure or careful binding in templates.
        // For dynamic updates, we assume toggleReaction is globally available.
        html += `
        <div class="reaction-pill ${chosenClass}" data-emoji="${emojiVal}"
            onclick="toggleReaction(this, ${msgId}, '${emojiVal}', ${chatId}, ${isPremium})">
            ${innerContent}
            <span class="reaction-count">${r.count}</span>
        </div>`;
    });
    return html;
}

// --- Live Events ---
function initChatEvents(chatId, topicId, isPremium) {
    const container = document.getElementById('messages-container');
    document.addEventListener('tg:event', (e) => {
        const data = e.detail;
        if (data.chat_id !== chatId) return;
        if (topicId && data.topic_id !== topicId) {
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
                reactionsContainer.innerHTML = buildReactionsHtml(msgId, data.message_model.reactions, chatId, isPremium);
                window.parseAppleEmojis(reactionsContainer);
            }
        }
    });
}
