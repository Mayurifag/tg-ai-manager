const chatList = document.getElementById("chat-list-container");
const chatListEnd = document.getElementById("chat-list-end");
const listStatus = document.getElementById("chat-list-status");
const cardRefreshes = new Map();
const changedUnloadedCards = new Set();
const pendingReads = new Map();
const CARD_REFRESH_INTERVAL = 500;
const MAX_CARD_REFRESHES = 4;
const MAX_DIRTY_CARDS = 512;
const LIST_MAX_RETRY_DELAY = 60_000;
const LIST_RETRY_INTERVAL = 5_000;
const LIST_REQUEST_TIMEOUT = 15_000;
const LIST_REFRESH_BATCH_SIZE = 200;
let cardRefreshTimer = null;
let activeCardRefreshes = 0;
let listGeneration = 0;
let refreshingList = false;
let loadingChats = false;
let listRefreshTimer = null;
let listRefreshPending = false;
let listRetryDelay = LIST_RETRY_INTERVAL;
let listRetryAt = 0;
let listPageActive = true;
let listRequestController = null;

function cardGroup(card) {
  const unread = card.dataset.isUnread === "true";
  const pinned = card.dataset.isPinned === "true";
  return (unread ? 0 : 2) + (pinned ? 0 : 1);
}

function insertCard(card) {
  const group = cardGroup(card);
  const date = Date.parse(card.dataset.messageDate) || 0;
  const next = Array.from(chatList.children).find((other) => {
    if (other === card) return false;
    const otherGroup = cardGroup(other);
    return (
      otherGroup > group ||
      (otherGroup === group &&
        card.dataset.isPinned !== "true" &&
        date > (Date.parse(other.dataset.messageDate) || 0))
    );
  });
  if (
    card.parentElement !== chatList ||
    card.nextElementSibling !== (next ?? null)
  ) {
    const active = document.activeElement;
    chatList.insertBefore(card, next ?? null);
    if (card.contains(active)) active.focus({ preventScroll: true });
  }
}

function findCard(chatId) {
  return chatList.querySelector(`.chat-card-wrapper[data-chat-id="${chatId}"]`);
}

function prepareCard(card) {
  if (pendingReads.has(card.dataset.chatId)) {
    const button = card.querySelector(".mark-read-btn");
    if (button) button.disabled = true;
  }
  window.parseAppleEmojis(card);
  return card;
}

async function loadMoreChats() {
  if (
    loadingChats ||
    listRefreshPending ||
    chatListEnd.hidden ||
    !listPageActive ||
    document.hidden ||
    chatListEnd.getBoundingClientRect().top > window.innerHeight + 300
  )
    return;
  loadingChats = true;
  listStatus.textContent = "Loading chats…";

  try {
    const params = new URLSearchParams({
      snapshot: chatListEnd.dataset.snapshot,
      offset: chatListEnd.dataset.nextOffset,
    });
    const page = await fetchListPage(`/api/chats?${params}`, true);
    if (page === null) {
      // Rebuild the loaded pages before appending from a different snapshot.
      listRefreshPending = true;
      return;
    }
    const fragment = document.createElement("div");
    fragment.innerHTML = page.html;
    for (const card of Array.from(fragment.children)) {
      if (!findCard(card.dataset.chatId)) insertCard(prepareCard(card));
      if (changedUnloadedCards.delete(card.dataset.chatId))
        reloadCard(card.dataset.chatId);
    }

    updateChatListCursor(page);
  } catch (error) {
    console.error("Error loading chats:", error);
    listStatus.textContent =
      "Could not load more chats. Retrying automatically.";
    listRefreshPending = true;
    delayListRetry(LIST_RETRY_INTERVAL);
  } finally {
    loadingChats = false;
    scheduleCardRefresh();
    if (listRefreshPending) void reconcileChatList();
    else void loadMoreChats();
  }
}

window.addEventListener("scroll", loadMoreChats, { passive: true });
window.addEventListener("resize", loadMoreChats);
void loadMoreChats();

function updateChatListCursor(page) {
  chatListEnd.dataset.snapshot = page.snapshot;
  chatListEnd.dataset.nextOffset = page.next_offset ?? "";
  chatListEnd.hidden = page.next_offset === null;
  listStatus.textContent = chatList.children.length ? "" : "No chats yet.";
}

function clearListRefreshTimer() {
  if (listRefreshTimer !== null) clearTimeout(listRefreshTimer);
  listRefreshTimer = null;
}

function scheduleListRetry() {
  clearListRefreshTimer();
  if (!listPageActive || document.hidden) return;
  listRefreshTimer = setTimeout(
    () => {
      listRefreshTimer = null;
      void reconcileChatList();
    },
    Math.max(0, listRetryAt - performance.now()),
  );
}

function requestListRefresh() {
  listRefreshPending = true;
  return reconcileChatList();
}

function delayListRetry(delay) {
  listRetryAt = Math.max(listRetryAt, performance.now() + delay);
}

function checkListResponse(response) {
  if (response.ok) return;
  // Telegram flood waits are returned as seconds in Retry-After.
  const seconds = Number(response.headers.get("Retry-After"));
  if (Number.isFinite(seconds) && seconds > 0) delayListRetry(seconds * 1000);
  throw new Error(`HTTP ${response.status}`);
}

async function fetchListPage(url, allowExpired = false) {
  const controller = (listRequestController = new AbortController());
  const timeout = setTimeout(() => controller.abort(), LIST_REQUEST_TIMEOUT);
  try {
    const response = await fetch(url, { signal: controller.signal });
    if (allowExpired && response.status === 410) return null;
    checkListResponse(response);
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
  if (listRetryAt > performance.now()) {
    scheduleListRetry();
    return;
  }
  listRetryAt = 0;
  clearListRefreshTimer();
  listRefreshPending = false;
  loadingChats = true;
  refreshingList = true;
  listGeneration++;
  changedUnloadedCards.clear();
  const loadedCount = chatList.children.length;
  try {
    const limit = (remaining) => Math.min(LIST_REFRESH_BATCH_SIZE, remaining);
    const first = new URLSearchParams({ refresh: "1", prefetch: "0" });
    if (loadedCount > 20) first.set("limit", limit(loadedCount));
    let page = await fetchListPage(`/api/chats?${first}`);
    const updated = [];
    while (true) {
      const fragment = document.createElement("div");
      fragment.innerHTML = page.html;
      updated.push(...fragment.children);
      if (updated.length >= loadedCount || page.next_offset === null) break;
      const params = new URLSearchParams({
        snapshot: page.snapshot,
        offset: page.next_offset,
        limit: limit(loadedCount - updated.length),
        prefetch: "0",
      });
      page = await fetchListPage(`/api/chats?${params}`);
    }
    if (!listPageActive || document.hidden) {
      listRefreshPending = true;
      return;
    }
    const existing = Array.from(chatList.children);
    const currentCards = new Map(
      existing.map((card) => [card.dataset.chatId, card]),
    );
    const next = updated.map((card) => {
      prepareCard(card);
      const current = currentCards.get(card.dataset.chatId);
      return current?.outerHTML === card.outerHTML ? current : card;
    });
    listRetryDelay = LIST_RETRY_INTERVAL;
    if (
      next.length === existing.length &&
      next.every((card, index) => card === existing[index])
    ) {
      updateChatListCursor(page);
      return;
    }
    // Capture the position at commit time, not before the network requests:
    // the user may have scrolled while the refresh was in flight.
    const anchor = existing.find(
      (card) => card.getBoundingClientRect().bottom > 0,
    );
    const anchorTop = anchor?.getBoundingClientRect().top;
    const active = document.activeElement;
    const focusedCard = active?.closest(".chat-card-wrapper");
    const focusSelector = active?.classList.contains("mark-read-btn")
      ? ".mark-read-btn"
      : ".chat-card-link";
    chatList.replaceChildren(...next);
    updateChatListCursor(page);
    if (anchor && anchorTop !== undefined) {
      const restoredAnchor = findCard(anchor.dataset.chatId);
      if (restoredAnchor)
        window.scrollBy(
          0,
          restoredAnchor.getBoundingClientRect().top - anchorTop,
        );
    }
    if (focusedCard) {
      const replacement = findCard(focusedCard.dataset.chatId);
      const target =
        replacement?.querySelector(focusSelector) ||
        replacement?.querySelector(".chat-card-link") ||
        chatList.children[0]?.querySelector(".chat-card-link");
      target?.focus({ preventScroll: true });
    }
  } catch (error) {
    console.error("Could not refresh chats:", error);
    listStatus.textContent = "Could not update chats. Retrying automatically.";
    listRefreshPending = true;
    delayListRetry(listRetryDelay);
    listRetryDelay = Math.min(listRetryDelay * 2, LIST_MAX_RETRY_DELAY);
  } finally {
    loadingChats = false;
    refreshingList = false;
    scheduleCardRefresh();
    if (listRefreshPending) {
      void reconcileChatList();
    } else {
      void loadMoreChats();
    }
  }
}

window.addEventListener("pagehide", () => {
  listPageActive = false;
  listRequestController?.abort();
  clearListRefreshTimer();
});
window.addEventListener("pageshow", (event) => {
  listPageActive = true;
  if (!event.persisted) void loadMoreChats();
});
document.addEventListener("visibilitychange", () => {
  if (document.hidden) clearListRefreshTimer();
  else if (listRefreshPending) void reconcileChatList();
  else void loadMoreChats();
});

chatList.addEventListener("click", async (event) => {
  const button = event.target.closest(".mark-read-btn");
  if (!button) return;
  event.preventDefault();
  event.stopPropagation();

  const chatId = button.dataset.chatId;
  if (!chatId || pendingReads.has(chatId)) return;
  const read = { finished: false };
  pendingReads.set(chatId, read);
  button.disabled = true;

  try {
    const response = await fetch(`/api/chat/${chatId}/read`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...window.TG.mutationHeaders,
      },
      body: JSON.stringify({ topic_id: button.dataset.topicId || null }),
    });
    if (
      !response.ok ||
      !response.headers.get("content-type")?.includes("application/json")
    )
      throw new Error(`HTTP ${response.status}`);
    if (!read.finished) {
      const current = findCard(chatId)?.querySelector(".mark-read-btn");
      if (current) current.textContent = "queued";
      listStatus.textContent = "Read queued; waiting for Telegram.";
    }
  } catch (error) {
    if (read.finished) return;
    console.error("Error marking chat as read:", error);
    listStatus.textContent =
      "Could not confirm whether the read was queued. Refresh and check before retrying.";
    const current = findCard(chatId)?.querySelector(".mark-read-btn");
    if (current) current.disabled = false;
  } finally {
    pendingReads.delete(chatId);
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
      void requestListRefresh();
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
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), LIST_REQUEST_TIMEOUT);
  try {
    const response = await fetch(`/api/chat/${chatId}/card`, {
      signal: controller.signal,
    });
    checkListResponse(response);
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
    prepareCard(newCard);

    const current = findCard(chatId);
    const focused =
      current &&
      document.activeElement &&
      current.contains(document.activeElement);
    const focusReadButton =
      focused && document.activeElement.classList.contains("mark-read-btn");
    if (
      newCard.dataset.isPinned === "true" &&
      (!current || cardGroup(current) !== cardGroup(newCard))
    ) {
      void requestListRefresh();
      return;
    }
    if (current) {
      current.replaceWith(newCard);
    }
    if (
      !current ||
      (newCard.dataset.isPinned !== "true" &&
        (cardGroup(current) !== cardGroup(newCard) ||
          current.dataset.messageDate !== newCard.dataset.messageDate))
    )
      insertCard(newCard);
    listStatus.textContent = "";
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
    delayListRetry(LIST_RETRY_INTERVAL);
    void requestListRefresh();
  } finally {
    clearTimeout(timeout);
    state.inFlight = false;
    activeCardRefreshes--;
    if (!state.dirty) cardRefreshes.delete(chatId);
    scheduleCardRefresh();
  }
}

document.addEventListener("tg:event", (event) => {
  const data = event.detail;
  if (data.type === "resync_required" || data.type === "chat_list_changed") {
    void requestListRefresh();
    return;
  }
  if (!data.chat_id) return;
  if (
    (data.type === "read" || data.type === "read_failed") &&
    data.read_origin === "manual" &&
    data.topic_id == null
  ) {
    const read = pendingReads.get(data.chat_id);
    if (read) {
      read.finished = true;
      if (data.type === "read") listStatus.textContent = "Read confirmed.";
    }
  }
  if (data.type === "read_failed") {
    const button = findCard(data.chat_id)?.querySelector(".mark-read-btn");
    if (button) {
      button.disabled = false;
      button.textContent = "mark read";
    }
    listStatus.textContent =
      data.text_plain || "Read was not confirmed. Refresh before retrying.";
    return;
  }

  if (
    ![
      "message",
      "action",
      "edited",
      "deleted",
      "read",
      "profile_updated",
    ].includes(data.type)
  )
    return;

  const wrapper = findCard(data.chat_id);
  if (refreshingList || data.type !== "message") {
    if (wrapper || refreshingList || data.type === "action") {
      reloadCard(data.chat_id);
    } else if (!chatListEnd.hidden) {
      // A later page still contains the old snapshot. Repair a changed card
      // if it is eventually shown, instead of fetching every unseen chat.
      if (changedUnloadedCards.size < MAX_DIRTY_CARDS)
        changedUnloadedCards.add(data.chat_id);
      else if (!changedUnloadedCards.has(data.chat_id))
        void requestListRefresh();
    }
    return;
  }

  const card = wrapper?.querySelector(".chat-card");
  if (!card || card.dataset.chatType === "forum") {
    reloadCard(data.chat_id);
    return;
  }

  // A message patched in place is newer than any pending card response.
  if (cardRefreshes.has(String(data.chat_id))) reloadCard(data.chat_id);

  const message = data.message_model;
  const currentId = Number(wrapper.dataset.messageId);
  if (
    !Number.isSafeInteger(message?.id) ||
    !Number.isFinite(Date.parse(message.date)) ||
    message.id < currentId ||
    !wrapper.dataset.messageId
  ) {
    // The authoritative card may already count this message, or this may
    // be a delayed arrival behind the message shown in the preview.
    reloadCard(data.chat_id);
    return;
  }
  if (message.id === currentId) return;
  wrapper.dataset.messageId = message.id;
  wrapper.dataset.messageDate = message.date;

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
        button.disabled = pendingReads.has(button.dataset.chatId);
        button.title = "Mark as Read";
        button.textContent = "mark read";
        card.appendChild(button);
      }
    }
    wrapper.dataset.isUnread = "true";
  }

  if (wrapper.dataset.isPinned !== "true") insertCard(wrapper);
  else if (previousGroup !== cardGroup(wrapper)) void requestListRefresh();
});
