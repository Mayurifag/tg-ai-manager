const chatList = document.getElementById("chat-list-container");
const loadMoreButton = document.getElementById("load-more-chats");
const listStatus = document.getElementById("chat-list-status");
const updateTimeouts = new Map();
let loadingChats = false;
let chatObserver = null;

function cardGroup(card) {
  const unread = card.dataset.isUnread === "true";
  const pinned = card.dataset.isPinned === "true";
  return (unread ? 0 : 2) + (pinned ? 0 : 1);
}

function insertCard(card, atFront = false) {
  const group = cardGroup(card);
  card.remove();
  const next = Array.from(chatList.children).find((other) => {
    const otherGroup = cardGroup(other);
    return otherGroup > group || (atFront && otherGroup === group);
  });
  chatList.insertBefore(card, next || null);
}

function findCard(chatId) {
  return chatList.querySelector(`.chat-card-wrapper[data-chat-id="${chatId}"]`);
}

async function loadMoreChats() {
  if (loadingChats || loadMoreButton.hidden) return;
  loadingChats = true;
  loadMoreButton.disabled = true;
  listStatus.textContent = "Loading chats…";
  if (chatObserver) chatObserver.unobserve(loadMoreButton);

  try {
    const params = new URLSearchParams({
      snapshot: loadMoreButton.dataset.snapshot,
      offset: loadMoreButton.dataset.nextOffset,
    });
    let response = await fetch(`/api/chats?${params}`);
    // A restart or an expired snapshot should not strand a long-lived page.
    if (response.status === 410) response = await fetch("/api/chats");
    if (!response.ok) throw new Error(`HTTP ${response.status}`);

    const page = await response.json();
    const fragment = document.createElement("div");
    fragment.innerHTML = page.html;
    for (const card of Array.from(fragment.children)) {
      if (!findCard(card.dataset.chatId)) {
        insertCard(card);
        window.parseAppleEmojis(card);
      }
    }

    loadMoreButton.dataset.snapshot = page.snapshot;
    loadMoreButton.dataset.nextOffset = page.next_offset ?? "";
    loadMoreButton.hidden = page.next_offset === null;
    loadMoreButton.textContent = "Load more chats";
    listStatus.textContent = "";
    // Re-observe even if the button never left the viewport, such as when
    // duplicates were skipped or the page is shorter than the screen.
    if (chatObserver && !loadMoreButton.hidden)
      chatObserver.observe(loadMoreButton);
  } catch (error) {
    console.error("Error loading chats:", error);
    loadMoreButton.textContent = "Retry loading chats";
    listStatus.textContent = "Could not load more chats. Try again.";
  } finally {
    loadingChats = false;
    loadMoreButton.disabled = false;
  }
}

loadMoreButton.addEventListener("click", loadMoreChats);
if ("IntersectionObserver" in window && !loadMoreButton.hidden) {
  chatObserver = new IntersectionObserver(
    (entries) => {
      if (entries.some((entry) => entry.isIntersecting)) loadMoreChats();
    },
    { rootMargin: "300px" },
  );
  chatObserver.observe(loadMoreButton);
}

chatList.addEventListener("click", async (event) => {
  const button = event.target.closest(".mark-read-btn");
  if (!button) return;
  event.preventDefault();
  event.stopPropagation();

  const chatId = button.dataset.chatId;
  if (!chatId) return;
  button.disabled = true;

  try {
    const response = await fetch(`/api/chat/${chatId}/read`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ topic_id: button.dataset.topicId || null }),
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);

    const wrapper = button.closest(".chat-card-wrapper");
    if (wrapper) {
      wrapper.querySelector(".unread")?.remove();
      button.remove();
      wrapper.dataset.isUnread = "false";
      insertCard(wrapper, true);
    }
  } catch (error) {
    console.error("Error marking chat as read:", error);
    listStatus.textContent = "Could not mark chat as read. Try again.";
    button.disabled = false;
  }
});

function reloadCard(chatId) {
  if (!chatId) return;
  clearTimeout(updateTimeouts.get(chatId));
  updateTimeouts.set(
    chatId,
    setTimeout(async () => {
      updateTimeouts.delete(chatId);
      try {
        const response = await fetch(
          `/api/chat/${chatId}/card?t=${Date.now()}`,
        );
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const fragment = document.createElement("div");
        fragment.innerHTML = await response.text();
        const newCard = fragment.firstElementChild;
        if (!newCard) return;

        const current = findCard(chatId);
        if (current && cardGroup(current) === cardGroup(newCard)) {
          current.replaceWith(newCard);
        } else {
          if (current) current.remove();
          insertCard(newCard, true);
        }
        window.parseAppleEmojis(newCard);
      } catch (error) {
        console.error("Error updating chat card:", error);
      }
    }, 500),
  );
}

document.addEventListener("tg:event", (event) => {
  const data = event.detail;
  if (!data.chat_id) return;

  if (
    data.type === "read" ||
    data.type === "deleted" ||
    data.type === "edited"
  ) {
    reloadCard(data.chat_id);
    return;
  }
  if (data.type !== "message") return;

  const wrapper = findCard(data.chat_id);
  const card = wrapper?.querySelector(".chat-card");
  if (!card || card.dataset.chatType === "forum") {
    reloadCard(data.chat_id);
    return;
  }

  const previousGroup = cardGroup(wrapper);
  const preview = card.querySelector(".preview");
  if (preview) {
    preview.textContent = data.text;
    window.parseAppleEmojis(preview);
  }

  if (!data.is_read && !data.message_model?.is_outgoing) {
    let badge = card.querySelector(".unread");
    if (badge) {
      badge.textContent = (parseInt(badge.textContent, 10) || 0) + 1;
    } else {
      let meta = card.querySelector(".meta");
      if (!meta) {
        meta = document.createElement("div");
        meta.className = "meta";
        card.appendChild(meta);
      }
      badge = document.createElement("div");
      badge.className = "unread";
      badge.textContent = "1";
      meta.appendChild(badge);
      const header = card.querySelector(".chat-header");
      if (header && !header.querySelector(".mark-read-btn")) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "mark-read-btn";
        button.dataset.chatId = wrapper.dataset.chatId;
        button.title = "Mark as Read";
        button.textContent = "mark read";
        header.appendChild(button);
      }
    }
    wrapper.dataset.isUnread = "true";
  }

  if (
    wrapper.dataset.isPinned !== "true" ||
    previousGroup !== cardGroup(wrapper)
  ) {
    insertCard(wrapper, true);
  }
});
