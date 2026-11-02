const chatList = document.getElementById("chat-list-container");
const loadMoreButton = document.getElementById("load-more-chats");
const listStatus = document.getElementById("chat-list-status");
const cardRefreshes = new Map();
const CARD_REFRESH_INTERVAL = 500;
const MAX_CARD_REFRESHES = 4;
const MAX_DIRTY_CARDS = 512;
let cardRefreshTimer = null;
let activeCardRefreshes = 0;
let listGeneration = 0;
let refreshingList = false;
let loadingChats = false;
let chatObserver = null;
let initialLoad = false;
let forceRefresh = false;

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
  if (initialLoad) {
    refreshingList = true;
    listGeneration++;
  }
  loadMoreButton.disabled = true;
  listStatus.textContent = "Loading chats…";
  if (chatObserver) chatObserver.unobserve(loadMoreButton);

  try {
    const params = new URLSearchParams();
    if (loadMoreButton.dataset.snapshot) {
      params.set("snapshot", loadMoreButton.dataset.snapshot);
      params.set("offset", loadMoreButton.dataset.nextOffset);
    }
    if (forceRefresh) params.set("refresh", "1");
    let response = await fetch(`/api/chats?${params}`);
    // A restart or an expired snapshot should not strand a long-lived page.
    if (response.status === 410) response = await fetch("/api/chats");
    if (!response.ok) throw new Error(`HTTP ${response.status}`);

    const page = await response.json();
    forceRefresh = false;
    if (initialLoad) {
      chatList.replaceChildren();
      initialLoad = false;
    }
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
    listStatus.textContent = chatList.children.length ? "" : "No chats yet.";
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
    refreshingList = false;
    scheduleCardRefresh();
    loadMoreButton.disabled = false;
    chatList.setAttribute("aria-busy", "false");
  }
}

loadMoreButton.addEventListener("click", loadMoreChats);
document.getElementById("refresh-chats").addEventListener("click", () => {
  if (loadingChats) return;
  initialLoad = true;
  forceRefresh = true;
  loadMoreButton.dataset.snapshot = "";
  loadMoreButton.hidden = false;
  loadMoreChats();
});
if ("IntersectionObserver" in window && !loadMoreButton.hidden) {
  chatObserver = new IntersectionObserver(
    (entries) => {
      if (entries.some((entry) => entry.isIntersecting)) loadMoreChats();
    },
    { rootMargin: "300px" },
  );
  chatObserver.observe(loadMoreButton);
}

// Restore the existing list immediately from the browser's back/forward cache,
// then reconcile in the background without throwing away scroll position.
window.addEventListener("pageshow", async (event) => {
  if (!event.persisted || loadingChats) return;
  loadingChats = true;
  refreshingList = true;
  listGeneration++;
  loadMoreButton.disabled = true;
  if (chatObserver) chatObserver.unobserve(loadMoreButton);
  const anchor = Array.from(chatList.children).find(
    (card) => card.getBoundingClientRect().bottom > 0,
  );
  const anchorTop = anchor?.getBoundingClientRect().top;
  const loadedCount = chatList.children.length;
  try {
    let response = await fetch("/api/chats?refresh=1");
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    let page = await response.json();
    const updated = [];
    while (true) {
      const fragment = document.createElement("div");
      fragment.innerHTML = page.html;
      updated.push(...fragment.children);
      if (updated.length >= loadedCount || page.next_offset === null) break;
      const params = new URLSearchParams({
        snapshot: page.snapshot,
        offset: page.next_offset,
      });
      response = await fetch(`/api/chats?${params}`);
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      page = await response.json();
    }
    const seen = new Set();
    for (const card of updated) {
      seen.add(card.dataset.chatId);
      window.parseAppleEmojis(card);
      const current = findCard(card.dataset.chatId);
      if (current && current.outerHTML === card.outerHTML) {
        chatList.append(current);
      } else {
        if (current) current.remove();
        chatList.append(card);
      }
    }
    if (page.next_offset === null) {
      for (const card of Array.from(chatList.children)) {
        if (!seen.has(card.dataset.chatId)) card.remove();
      }
    } else {
      for (const card of Array.from(chatList.children)) {
        if (!seen.has(card.dataset.chatId)) chatList.append(card);
      }
    }
    loadMoreButton.dataset.snapshot = page.snapshot;
    loadMoreButton.dataset.nextOffset = page.next_offset ?? "";
    loadMoreButton.hidden = page.next_offset === null;
    listStatus.textContent = chatList.children.length ? "" : "No chats yet.";
    if (anchor && anchorTop !== undefined) {
      const restoredAnchor = findCard(anchor.dataset.chatId);
      if (restoredAnchor)
        window.scrollBy(
          0,
          restoredAnchor.getBoundingClientRect().top - anchorTop,
        );
    }
  } catch (error) {
    console.error("Could not refresh chats after returning:", error);
  } finally {
    loadingChats = false;
    refreshingList = false;
    scheduleCardRefresh();
    loadMoreButton.disabled = false;
    if (chatObserver && !loadMoreButton.hidden)
      chatObserver.observe(loadMoreButton);
  }
});

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
      headers: {
        "Content-Type": "application/json",
        "X-Requested-With": "XMLHttpRequest",
      },
      body: JSON.stringify({ topic_id: button.dataset.topicId || null }),
    });
    if (
      !response.ok ||
      !response.headers.get("content-type")?.includes("application/json")
    )
      throw new Error(`HTTP ${response.status}`);
    button.textContent = "queued";
    listStatus.textContent = "Read queued; waiting for Telegram.";
  } catch (error) {
    console.error("Error marking chat as read:", error);
    listStatus.textContent = "Could not mark chat as read. Try again.";
    button.disabled = false;
  }
});

function scheduleCardRefresh() {
  if (
    cardRefreshTimer !== null ||
    refreshingList ||
    activeCardRefreshes >= MAX_CARD_REFRESHES
  )
    return;
  if (
    !Array.from(cardRefreshes.values()).some(
      (state) => state.dirty && !state.inFlight,
    )
  )
    return;
  cardRefreshTimer = setTimeout(flushCardRefreshes, CARD_REFRESH_INTERVAL);
}

function reloadCard(chatId) {
  if (!chatId) return;
  const id = String(chatId);
  let state = cardRefreshes.get(id);
  if (!state) {
    if (cardRefreshes.size >= MAX_DIRTY_CARDS) {
      listStatus.textContent = "Too many chat updates. Refresh the list.";
      return;
    }
    state = { revision: 0, dirty: false, inFlight: false };
    cardRefreshes.set(id, state);
  }
  state.revision++;
  state.dirty = true;
  scheduleCardRefresh();
}

function flushCardRefreshes() {
  cardRefreshTimer = null;
  if (refreshingList) return;
  for (const [chatId, state] of cardRefreshes) {
    if (activeCardRefreshes >= MAX_CARD_REFRESHES) break;
    if (!state.dirty || state.inFlight) continue;
    state.dirty = false;
    state.inFlight = true;
    activeCardRefreshes++;
    void fetchCard(chatId, state);
  }
}

async function fetchCard(chatId, state) {
  const revision = state.revision;
  const generation = listGeneration;
  try {
    const response = await fetch(`/api/chat/${chatId}/card?t=${Date.now()}`);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const html = await response.text();
    // Never overwrite newer events or a fresh list with an older request.
    if (
      state.revision !== revision ||
      listGeneration !== generation ||
      refreshingList
    ) {
      state.dirty = true;
      return;
    }
    const fragment = document.createElement("div");
    fragment.innerHTML = html;
    const newCard = fragment.firstElementChild;
    if (!newCard) return;

    const current = findCard(chatId);
    const focused =
      current &&
      document.activeElement &&
      current.contains(document.activeElement);
    const focusReadButton =
      focused && document.activeElement.classList.contains("mark-read-btn");
    if (current && cardGroup(current) === cardGroup(newCard)) {
      current.replaceWith(newCard);
    } else {
      if (current) current.remove();
      insertCard(newCard, true);
    }
    window.parseAppleEmojis(newCard);
    if (focused) {
      const target =
        (focusReadButton && newCard.querySelector(".mark-read-btn")) ||
        newCard.querySelector(".chat-card-link");
      target?.focus({ preventScroll: true });
    }
  } catch (error) {
    console.error("Error updating chat card:", error);
    listStatus.textContent =
      "Some chat updates are unavailable. Refresh to retry.";
  } finally {
    state.inFlight = false;
    activeCardRefreshes--;
    if (!state.dirty) cardRefreshes.delete(chatId);
    scheduleCardRefresh();
  }
}

document.addEventListener("tg:event", (event) => {
  const data = event.detail;
  if (data.type === "resync_required") {
    window.location.reload();
    return;
  }
  if (!data.chat_id) return;
  if (
    refreshingList &&
    ["message", "action", "edited", "deleted", "read"].includes(data.type)
  ) {
    reloadCard(data.chat_id);
    return;
  }

  if (data.type === "read_failed") {
    const button = findCard(data.chat_id)?.querySelector(".mark-read-btn");
    if (button) {
      button.disabled = false;
      button.textContent = "mark read";
    }
    listStatus.textContent =
      "Telegram could not mark the chat as read. Try again.";
    return;
  }

  if (
    data.type === "read" ||
    data.type === "deleted" ||
    data.type === "edited" ||
    data.type === "action"
  ) {
    if (findCard(data.chat_id)) reloadCard(data.chat_id);
    return;
  }
  if (data.type !== "message") return;

  const wrapper = findCard(data.chat_id);
  const card = wrapper?.querySelector(".chat-card");
  if (!card || card.dataset.chatType === "forum") {
    reloadCard(data.chat_id);
    return;
  }

  // A message patched in place is newer than any pending card response.
  if (cardRefreshes.has(String(data.chat_id))) reloadCard(data.chat_id);

  const previousGroup = cardGroup(wrapper);
  const preview = card.querySelector(".preview");
  if (preview) {
    preview.textContent = data.text_plain ?? data.text;
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
      if (!card.querySelector(".mark-read-btn")) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "mark-read-btn";
        button.dataset.chatId = wrapper.dataset.chatId;
        button.title = "Mark as Read";
        button.textContent = "mark read";
        card.appendChild(button);
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
