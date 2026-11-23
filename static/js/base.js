window.TG = window.TG || {};
window.TG.mutationHeaders = {
  "X-Requested-With": "XMLHttpRequest",
  "X-Account-Token": window.TG_CONFIG.account,
};

window.TG.escapeHtml = function (value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
};

window.parseAppleEmojis = function (container) {
  window.twemoji?.parse(container, {
    callback: function (icon) {
      const points = icon.split("-");
      let filename = points.map((point) => point.padStart(4, "0")).join("-");
      // Apple filenames retain variation selectors that Twemoji normalizes.
      if (
        points.length === 1 &&
        !/\p{Emoji_Presentation}/u.test(
          String.fromCodePoint(parseInt(icon, 16)),
        )
      )
        filename += "-fe0f";
      if (points.length === 2 && points[1] === "20e3")
        filename = `${points[0].padStart(4, "0")}-fe0f-20e3`;
      const source =
        "https://cdn.jsdelivr.net/npm/emoji-datasource-apple@16.0.0/img/apple/64/" +
        filename +
        ".png";
      return `https://wsrv.nl/?url=${encodeURIComponent(source)}&output=webp&ll`;
    },
    attributes: () => ({
      loading: "lazy",
      decoding: "async",
      referrerpolicy: "no-referrer",
    }),
  });
};

// A missing image should not hide the original character.
document.addEventListener(
  "error",
  (event) => {
    const image = event.target;
    if (image instanceof HTMLImageElement && image.classList.contains("emoji"))
      image.replaceWith(image.alt);
    else if (
      image instanceof HTMLImageElement &&
      image.closest(".sidebar-avatar")
    )
      image.remove();
  },
  true,
);

document.getElementById("twemoji-script").addEventListener("load", () => {
  if (document.readyState !== "loading") window.parseAppleEmojis(document.body);
});

window.renderLocalTimes = function (context) {
  const els = (context || document).querySelectorAll(
    ".local-time[data-timestamp], .local-date-time[data-timestamp]",
  );
  els.forEach((el) => {
    const iso = el.getAttribute("data-timestamp");
    if (iso) {
      const date = new Date(iso);
      const options = {
        hour: "2-digit",
        minute: "2-digit",
        hour12: false,
      };
      el.textContent = el.classList.contains("local-date-time")
        ? date.toLocaleString([], {
            ...options,
            year: "numeric",
            month: "short",
            day: "numeric",
            second: "2-digit",
          })
        : date.toLocaleTimeString([], options);
      el.title = date.toLocaleString([], { timeZoneName: "short" });
    }
  });
};

const tooltip = document.getElementById("global-tooltip");
let activeTooltipTarget = null;
let currentLightboxIndex = -1;
let lightboxItems = [];
let lightboxReturnFocus = null;

function showTooltip(target, content) {
  activeTooltipTarget = target;
  tooltip.innerHTML = content;
  tooltip.style.display = "block";
  tooltip.style.opacity = "1";
}

function hideTooltip(target) {
  if (activeTooltipTarget === target) {
    tooltip.style.display = "none";
    tooltip.style.opacity = "0";
    activeTooltipTarget = null;
  }
}

function refreshLightboxItems() {
  const triggers = document.querySelectorAll(".lightbox-trigger");
  lightboxItems = Array.from(triggers).map((el) => ({
    el: el,
    src: el.src,
    fullSrc: el.getAttribute("data-full-src"),
  }));
}

window.openLightbox = function (element) {
  refreshLightboxItems();
  currentLightboxIndex = lightboxItems.findIndex((item) => item.el === element);
  if (currentLightboxIndex === -1) return;
  lightboxReturnFocus = document.activeElement;
  updateLightboxView();
  const lightbox = document.getElementById("lightbox");
  lightbox.showModal();
  lightbox.classList.add("active");
  document.querySelector(".lightbox-close").focus();
};

function updateLightboxView() {
  const item = lightboxItems[currentLightboxIndex];
  const img = document.getElementById("lightbox-img");
  const loader = document.getElementById("lightbox-loading");
  if (!item) return;
  img.style.display = "none";
  loader.style.display = "block";
  img.onload = () => {
    loader.style.display = "none";
    img.style.display = "block";
  };
  img.onerror = () => {
    img.onerror = null;
    img.src = item.src;
    loader.style.display = "none";
    img.style.display = "block";
  };
  img.src = item.fullSrc;
}

window.navigateLightbox = function (dir) {
  const newIndex = currentLightboxIndex + dir;
  if (newIndex >= 0 && newIndex < lightboxItems.length) {
    currentLightboxIndex = newIndex;
    updateLightboxView();
  }
};

window.closeLightbox = function () {
  const lightbox = document.getElementById("lightbox");
  if (lightbox.open) lightbox.close();
};

document.getElementById("lightbox").addEventListener("close", () => {
  const lightbox = document.getElementById("lightbox");
  lightbox.classList.remove("active");
  const img = document.getElementById("lightbox-img");
  img.onload = null;
  img.onerror = null;
  img.removeAttribute("src");
  if (lightboxReturnFocus?.isConnected) lightboxReturnFocus.focus();
  lightboxReturnFocus = null;
});

document.addEventListener("keydown", (e) => {
  if (!document.getElementById("lightbox").open) return;
  if (e.key === "ArrowLeft") navigateLightbox(-1);
  if (e.key === "ArrowRight") navigateLightbox(1);
});

document.addEventListener("DOMContentLoaded", () => {
  window.parseAppleEmojis(document.body);
  window.renderLocalTimes(document.body);

  const isDebug = Boolean(window.TG_CONFIG && window.TG_CONFIG.debug);

  if (isDebug) {
    document.body.addEventListener("mouseover", (e) => {
      const wrapper = e.target.closest(".event-text-wrapper");
      if (wrapper) {
        const rawData = wrapper.getAttribute("data-full-html");
        showTooltip(
          wrapper,
          '<pre style="margin:0; font-size: 0.75rem;">' +
            window.TG.escapeHtml(rawData) +
            "</pre>",
        );
      }
    });
    document.body.addEventListener("mouseout", (e) => {
      const wrapper = e.target.closest(".event-text-wrapper");
      if (wrapper) hideTooltip(wrapper);
    });
  }

  document.body.addEventListener("mouseover", (e) => {
    const el = e.target.closest("[data-tooltip]");
    if (el) {
      const text = el.getAttribute("data-tooltip");
      if (text)
        showTooltip(
          el,
          `<div style="max-width:200px; line-height:1.4;">${window.TG.escapeHtml(text)}</div>`,
        );
    }
  });
  document.body.addEventListener("mouseout", (e) => {
    const el = e.target.closest("[data-tooltip]");
    if (el) hideTooltip(el);
  });

  document.body.addEventListener("mousemove", (e) => {
    if (tooltip.style.display === "block") {
      const width = tooltip.offsetWidth;
      const height = tooltip.offsetHeight;
      let left = e.clientX - width - 15;
      let top = e.clientY + 15;
      if (left < 10) left = e.clientX + 15;
      if (top + height > window.innerHeight) top = e.clientY - height - 10;
      tooltip.style.left = left + "px";
      tooltip.style.top = top + "px";
    }
  });

  const feedContainer = document.getElementById("event-feed-list");
  const EVENT_RECONNECT_DELAY = 5_000;
  let eventSource;
  let reconnectTimer;
  let reconnectDelay = EVENT_RECONNECT_DELAY;

  function connectEvents(resyncOnOpen = false) {
    const params = new URLSearchParams({ account: window.TG_CONFIG.account });
    if (window.TG_CHAT) {
      params.set("chat_id", window.TG_CHAT.chatId);
      if (window.TG_CHAT.topicId != null)
        params.set("topic_id", window.TG_CHAT.topicId);
    }
    if (isDebug) params.set("debug", "1");
    const snapshot =
      window.TG_CHAT?.snapshot ??
      document.getElementById("chat-list-end")?.dataset.snapshot;
    if (!resyncOnOpen && snapshot) params.set("snapshot", snapshot);
    const source = (eventSource = new EventSource(
      `/api/events/stream?${params}`,
    ));
    function reconnect() {
      if (eventSource !== source) return;
      source.close();
      eventSource = null;
      reconnectTimer = setTimeout(() => connectEvents(true), reconnectDelay);
      reconnectDelay = Math.min(reconnectDelay * 2, 60_000);
    }
    source.onerror = reconnect;
    source.onopen = () => {
      if (eventSource !== source) return;
      reconnectDelay = EVENT_RECONNECT_DELAY;
      if (resyncOnOpen) {
        document.dispatchEvent(
          new CustomEvent("tg:event", { detail: { type: "resync_required" } }),
        );
      }
    };

    source.onmessage = function (e) {
      if (eventSource !== source) return;
      let data;
      try {
        data = JSON.parse(e.data);
        if (!data || typeof data.type !== "string")
          throw new Error("Invalid event");
      } catch (_) {
        reconnect();
        return;
      }
      if (data.type === "resync_required") {
        reconnect();
        return;
      }
      if (data.type === "account_changed") {
        source.close();
        location.reload();
        return;
      }
      if (data.type === "snapshot_stale") data.type = "resync_required";
      const customEvent = new CustomEvent("tg:event", { detail: data });
      document.dispatchEvent(customEvent);

      if (
        [
          "read",
          "edited",
          "profile_updated",
          "chat_list_changed",
          "resync_required",
          "transcript_snapshot",
        ].includes(data.type)
      )
        return;

      if (feedContainer) {
        const emptyMsg = feedContainer.querySelector(".empty-feed");
        if (emptyMsg) emptyMsg.remove();

        const item = document.createElement("div");
        item.className = `event-item type-${data.type}`;

        let iconHtml = "";
        if (data.type === "deleted")
          iconHtml = '<span class="event-type-icon">🗑️</span>';
        else if (data.type === "action")
          iconHtml = '<span class="event-type-icon">⚡</span>';
        else if (data.type === "reaction_update")
          iconHtml = '<span class="event-type-icon">👍</span>';

        let chatLabel = data.chat_name;
        if (data.topic_name) chatLabel = `${data.topic_name} - ${chatLabel}`;
        const linkHref = data.link || "#";
        const debugJson = isDebug
          ? window.TG.escapeHtml(JSON.stringify(data, null, 2))
          : "";

        item.innerHTML = `
                <div class="event-header">
                    ${iconHtml}
                    <a href="${window.TG.escapeHtml(linkHref)}" class="event-chat-link" title="${window.TG.escapeHtml(chatLabel)}">${window.TG.escapeHtml(chatLabel)}</a>
                </div>
                <div class="event-text-wrapper" data-full-html="${debugJson}">
                    <div class="event-text">${window.TG.escapeHtml(data.text_plain ?? data.text)}</div>
                </div>
            `;

        window.parseAppleEmojis(item);
        feedContainer.insertBefore(item, feedContainer.firstChild);

        const items = feedContainer.querySelectorAll(".event-item");
        if (items.length > 10) items[items.length - 1].remove();
      }
    };
  }
  connectEvents();
  window.addEventListener("pagehide", () => {
    clearTimeout(reconnectTimer);
    eventSource?.close();
    eventSource = null;
  });
  window.addEventListener("pageshow", (event) => {
    if (event.persisted) connectEvents(true);
  });

  document.addEventListener("click", (event) => {
    const link = event.target.closest("a.back-link");
    if (
      !link ||
      event.defaultPrevented ||
      event.button !== 0 ||
      event.metaKey ||
      event.ctrlKey ||
      event.shiftKey ||
      event.altKey
    )
      return;
    try {
      const previous = new URL(document.referrer);
      const target = new URL(link.href);
      if (
        history.length > 1 &&
        previous.origin === target.origin &&
        previous.pathname === target.pathname
      ) {
        event.preventDefault();
        history.back();
      }
    } catch (_) {
      // Direct visits use the normal link instead.
    }
  });
});
