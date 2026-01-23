// --- Global Tooltip Logic ---
const tooltip = document.getElementById('global-tooltip');
let activeTooltipTarget = null;

function showTooltip(target, content) {
    activeTooltipTarget = target;
    tooltip.innerHTML = content;
    tooltip.style.display = 'block';
    tooltip.style.opacity = '1';
}

function hideTooltip(target) {
    if (activeTooltipTarget === target) {
        tooltip.style.display = 'none';
        tooltip.style.opacity = '0';
        activeTooltipTarget = null;
    }
}

document.body.addEventListener('mouseover', (e) => {
    const wrapper = e.target.closest('.event-text-wrapper');
    if (wrapper && window.isDebugMode) {
        const rawData = wrapper.getAttribute('data-full-html');
        showTooltip(wrapper, '<pre style="margin:0; font-size: 0.75rem;">' + rawData + '</pre>');
        return;
    }
    const el = e.target.closest('[data-tooltip]');
    if (el) {
        const text = el.getAttribute('data-tooltip');
        if (text) showTooltip(el, `<div style="max-width:200px; line-height:1.4;">${text}</div>`);
    }
});

document.body.addEventListener('mouseout', (e) => {
    const wrapper = e.target.closest('.event-text-wrapper');
    if (wrapper) hideTooltip(wrapper);
    const el = e.target.closest('[data-tooltip]');
    if (el) hideTooltip(el);
});

document.body.addEventListener('mousemove', (e) => {
    if (tooltip.style.display === 'block') {
        const width = tooltip.offsetWidth;
        const height = tooltip.offsetHeight;
        let left = e.clientX - width - 15;
        let top = e.clientY + 15;
        if (left < 10) left = e.clientX + 15;
        if (top + height > window.innerHeight) top = e.clientY - height - 10;
        tooltip.style.left = left + 'px';
        tooltip.style.top = top + 'px';
    }
});

// --- Lightbox Logic ---
let currentLightboxIndex = -1;
let lightboxItems = [];

function refreshLightboxItems() {
    const triggers = document.querySelectorAll('.lightbox-trigger');
    lightboxItems = Array.from(triggers).map(el => ({
        el: el,
        src: el.src,
        fullSrc: el.getAttribute('data-full-src')
    }));
}

function openLightbox(element) {
    refreshLightboxItems();
    currentLightboxIndex = lightboxItems.findIndex(item => item.el === element);
    if (currentLightboxIndex === -1) return;
    updateLightboxView();
    document.getElementById('lightbox').classList.add('active');
}

function updateLightboxView() {
    const item = lightboxItems[currentLightboxIndex];
    const img = document.getElementById('lightbox-img');
    const loader = document.getElementById('lightbox-loading');
    if (!item) return;
    img.style.display = 'none';
    loader.style.display = 'block';
    img.onload = () => { loader.style.display = 'none'; img.style.display = 'block'; };
    img.onerror = () => { img.src = item.src; loader.style.display = 'none'; img.style.display = 'block'; };
    img.src = item.fullSrc;
}

function navigateLightbox(dir) {
    const newIndex = currentLightboxIndex + dir;
    if (newIndex >= 0 && newIndex < lightboxItems.length) {
        currentLightboxIndex = newIndex;
        updateLightboxView();
    }
}

function closeLightbox() {
    document.getElementById('lightbox').classList.remove('active');
    setTimeout(() => document.getElementById('lightbox-img').src = "", 300);
}

document.addEventListener('keydown', (e) => {
    if (!document.getElementById('lightbox').classList.contains('active')) return;
    if (e.key === 'Escape') closeLightbox();
    if (e.key === 'ArrowLeft') navigateLightbox(-1);
    if (e.key === 'ArrowRight') navigateLightbox(1);
});

// --- Lottie & Initializers ---
function renderLottieAnimations(container) {
    const els = (container || document).querySelectorAll('.lottie-anim');
    els.forEach(el => {
        if (el.dataset.loaded) return;
        const path = el.getAttribute('data-path');
        if (path) {
            lottie.loadAnimation({
                container: el,
                renderer: 'svg',
                loop: true,
                autoplay: true,
                path: path
            });
            el.dataset.loaded = "true";
        }
    });
}

document.addEventListener('DOMContentLoaded', () => {
    window.parseAppleEmojis(document.body);
    window.renderLocalTimes(document.body);
    renderLottieAnimations(document.body);

    const feedContainer = document.getElementById('event-feed-list');
    const evtSource = new EventSource("/api/events/stream");

    evtSource.onmessage = function (e) {
        const data = JSON.parse(e.data);
        const customEvent = new CustomEvent('tg:event', { detail: data });
        document.dispatchEvent(customEvent);

        if (data.type === 'read' || data.type === 'edited') return;

        if (feedContainer) {
            const emptyMsg = feedContainer.querySelector('.empty-feed');
            if (emptyMsg) emptyMsg.remove();

            const item = document.createElement('div');
            item.className = `event-item type-${data.type}`;

            let iconHtml = '';
            if (data.type === 'deleted') iconHtml = '<span class="event-type-icon">🗑️</span>';
            else if (data.type === 'action') iconHtml = '<span class="event-type-icon">⚡</span>';
            else if (data.type === 'reaction_update') iconHtml = '<span class="event-type-icon">👍</span>';

            let chatLabel = data.chat_name;
            if (data.topic_name) chatLabel = `${data.topic_name} - ${chatLabel}`;

            const debugJson = window.isDebugMode ? JSON.stringify(data, null, 2)
                .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;") : "";

            item.innerHTML = `
                <div class="event-header">
                    ${iconHtml}
                    <a href="${data.link || '#'}" class="event-chat-link" title="${chatLabel}">${chatLabel}</a>
                </div>
                <div class="event-text-wrapper" data-full-html="${debugJson}">
                    <div class="event-text">${data.text}</div>
                </div>
            `;

            window.parseAppleEmojis(item);
            feedContainer.insertBefore(item, feedContainer.firstChild);

            const items = feedContainer.querySelectorAll('.event-item');
            if (items.length > 10) items[items.length - 1].remove();
        }
    };
});
