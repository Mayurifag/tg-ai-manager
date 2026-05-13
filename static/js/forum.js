const forumConfig = window.TG_FORUM || {};

const readForumBtn = document.getElementById('btn-read-forum');
if (readForumBtn) {
    readForumBtn.addEventListener('click', async () => {
        readForumBtn.style.opacity = '0.5';
        readForumBtn.style.pointerEvents = 'none';

        try {
            await fetch(`/api/chat/${forumConfig.chatId}/read`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({})
            });
            document.querySelectorAll('.unread-badge, .unread-count, [class*="unread"]').forEach(el => {
                el.textContent = '';
                el.style.display = 'none';
            });
            readForumBtn.textContent = '✅ All Read';
        } catch (e) {
            console.error(e);
        } finally {
            setTimeout(() => {
                readForumBtn.style.opacity = '1';
                readForumBtn.style.pointerEvents = 'auto';
            }, 500);
        }
    });
}

document.getElementById('topic-list-container').addEventListener('click', async (e) => {
    if (e.target.classList.contains('mark-read-btn')) {
        e.preventDefault();
        e.stopPropagation();

        const btn = e.target;
        const chatId = btn.getAttribute('data-chat-id');
        const topicId = btn.getAttribute('data-topic-id');

        if (!chatId || !topicId) return;

        btn.style.opacity = '0.5';
        btn.style.pointerEvents = 'none';

        try {
            const response = await fetch(`/api/chat/${chatId}/read`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ topic_id: parseInt(topicId) })
            });

            if (response.ok) {
                const card = btn.closest('.chat-card');
                if (card) {
                    const unread = card.querySelector('.unread');
                    if (unread) unread.remove();
                    btn.remove();
                }
            }
        } catch (err) {
            console.error('Error marking topic as read:', err);
            btn.style.opacity = '1';
            btn.style.pointerEvents = 'auto';
        }
    }
});
