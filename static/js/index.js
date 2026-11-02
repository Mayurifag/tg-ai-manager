const chatList = document.getElementById("chat-list-container");
const loadMoreButton = document.getElementById("load-more-chats");
const listStatus = document.getElementById("chat-list-status");
const cardRefreshes = new Map();
const CARD_REFRESH_INTERVAL = 500;
const MAX_CARD_REFRESHES = 4;
const MAX_DIRTY_CARDS = 512;
const LIST_REFRESH_INTERVAL = 60_000;
const LIST_RETRY_INTERVAL = 5_000;
const LIST_REQUEST_TIMEOUT = 15_000;
let cardRefreshTimer = null;
let activeCardRefreshes = 0;
let listGeneration = 0;
let refreshingList = false;
let loadingChats = false;
let chatObserver = null;
let listRefreshTimer = null;
let listRefreshPending = false;
let listRefreshForced = false;
let listRetryDelay = LIST_RETRY_INTERVAL;
let listPageActive = true;
let listEventVersion = 0;
let listSnapshotEventVersion = 0;
let listRequestController = null;

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
    const params = new URLSearchParams();
    if (loadMoreButton.dataset.snapshot) {
      params.set("snapshot", loadMoreButton.dataset.snapshot);
      params.set("offset", loadMoreButton.dataset.nextOffset);
    }
    let page = await fetchListPage(`/api/chats?${params}`, true);
    // A restart or an expired snapshot should not strand a long-lived page.
    if (page === null) page = await fetchListPage("/api/chats");
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
    scheduleCardRefresh();
    loadMoreButton.disabled = false;
    chatList.setAttribute("aria-busy", "false");
    if (listRefreshPending) void reconcileChatList();
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

function scheduleListRefresh(delay = LIST_REFRESH_INTERVAL) {
  if (listRefreshTimer !== null) clearTimeout(listRefreshTimer);
  listRefreshTimer = null;
  if (!listPageActive || document.hidden) return;
  listRefreshTimer = setTimeout(() => {
    listRefreshTimer = null;
    requestListRefresh();
  }, delay);
}

function requestListRefresh(force = false) {
  if (listRefreshTimer !== null) clearTimeout(listRefreshTimer);
  listRefreshTimer = null;
  listRefreshPending = true;
  listRefreshForced ||= force;
  return reconcileChatList();
}

async function fetchListPage(url, allowExpired = false) {
  const controller = (listRequestController = new AbortController());
  const timeout = setTimeout(() => controller.abort(), LIST_REQUEST_TIMEOUT);
  try {
    const response = await fetch(url, { signal: controller.signal });
    if (allowExpired && response.status === 410) return null;
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return await response.json();
  } finally {
    clearTimeout(timeout);
    if (listRequestController === controller) listRequestController = null;
  }
}

// Keep the existing list visible while reconciling its loaded pages.
async function reconcileChatList() {
  if (!listRefreshPending || loadingChats || !listPageActive || document.hidden)
    return;
  listRefreshPending = false;
  // A warmed snapshot from before a live event must not overwrite the
  // newer card with stale preview or unread data.
  const eventVersion = listEventVersion;
  const force = listRefreshForced || eventVersion !== listSnapshotEventVersion;
  listRefreshForced = false;
  loadingChats = true;
  refreshingList = true;
  listGeneration++;
  loadMoreButton.disabled = true;
  if (chatObserver) chatObserver.unobserve(loadMoreButton);
  const loadedCount = chatList.children.length;
  let failed = false;
  try {
    // Normal background checks reuse the server's warmed cache. A reconnect
    // forces one fresh snapshot because events may have been missed.
    let page = await fetchListPage(
      force ? "/api/chats?refresh=1" : "/api/chats",
    );
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
      page = await fetchListPage(`/api/chats?${params}`);
    }
    if (!listPageActive || document.hidden) {
      listRefreshPending = true;
      listRefreshForced ||= force;
      return;
    }
    // Capture the position at commit time, not before the network requests:
    // the user may have scrolled while the refresh was in flight.
    const anchor = Array.from(chatList.children).find(
      (card) => card.getBoundingClientRect().bottom > 0,
    );
    const anchorTop = anchor?.getBoundingClientRect().top;
    const active = document.activeElement;
    const focusedCard =
      active &&
      Array.from(chatList.children).find((card) => card.contains(active));
    const focusSelector = active?.classList.contains("mark-read-btn")
      ? ".mark-read-btn"
      : ".chat-card-link";
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
    if (focusedCard) {
      findCard(focusedCard.dataset.chatId)
        ?.querySelector(focusSelector)
        ?.focus({ preventScroll: true });
    }
    listSnapshotEventVersion = eventVersion;
    listRetryDelay = LIST_RETRY_INTERVAL;
  } catch (error) {
    console.error("Could not refresh chats:", error);
    listStatus.textContent = "Could not update chats. Retrying automatically.";
    listRefreshForced ||= force;
    failed = true;
  } finally {
    loadingChats = false;
    refreshingList = false;
    scheduleCardRefresh();
    loadMoreButton.disabled = false;
    if (chatObserver && !loadMoreButton.hidden)
      chatObserver.observe(loadMoreButton);
    if (listRefreshPending && listPageActive && !document.hidden) {
      void reconcileChatList();
    } else {
      scheduleListRefresh(failed ? listRetryDelay : LIST_REFRESH_INTERVAL);
      if (failed)
        listRetryDelay = Math.min(listRetryDelay * 2, LIST_REFRESH_INTERVAL);
    }
  }
}

window.addEventListener("pagehide", () => {
  listPageActive = false;
  listRequestController?.abort();
  if (listRefreshTimer !== null) clearTimeout(listRefreshTimer);
  listRefreshTimer = null;
});
window.addEventListener("pageshow", (event) => {
  listPageActive = true;
  if (event.persisted) return requestListRefresh(true);
  scheduleListRefresh();
});
document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    if (listRefreshTimer !== null) clearTimeout(listRefreshTimer);
    listRefreshTimer = null;
  } else {
    void requestListRefresh();
  }
});
scheduleListRefresh();

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
      listStatus.textContent = "Catching up on chat updates…";
      void requestListRefresh(true);
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
      "Some chat updates are unavailable. Retrying automatically.";
    listRefreshForced = true;
    scheduleListRefresh(LIST_RETRY_INTERVAL);
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
    void requestListRefresh(true);
    return;
  }
  if (!data.chat_id) return;
  if (["message", "action", "edited", "deleted", "read"].includes(data.type))
    listEventVersion++;
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
