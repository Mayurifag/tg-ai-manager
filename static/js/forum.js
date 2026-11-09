const forumConfig = window.TG_FORUM || {};
const topicList = document.getElementById("topic-list-container");
const topicStatus = document.getElementById("topic-list-status");
const topicRefreshes = new Map();
const MAX_TOPIC_REFRESHES = 3;
const TOPIC_REQUEST_TIMEOUT = 15_000;
let topicRefreshTimer = null;
let activeTopicRefreshes = 0;
let pageReloadTimer = null;

function findTopic(topicId) {
  return topicList.querySelector(
    `.chat-card-wrapper[data-chat-id="${topicId}"]`,
  );
}

function scheduleTopicRefresh() {
  if (topicRefreshTimer !== null || activeTopicRefreshes >= MAX_TOPIC_REFRESHES)
    return;
  if (
    !Array.from(topicRefreshes.values()).some(
      (state) => state.dirty && !state.inFlight,
    )
  )
    return;
  topicRefreshTimer = setTimeout(flushTopicRefreshes, 500);
}

function refreshTopic(topicId) {
  const id = String(topicId);
  // Only refresh cards on this page; do not append topics from another page.
  if (!findTopic(id)) return;
  let state = topicRefreshes.get(id);
  if (!state) {
    state = { revision: 0, dirty: false, inFlight: false };
    topicRefreshes.set(id, state);
  }
  state.revision++;
  state.dirty = true;
  scheduleTopicRefresh();
}

function flushTopicRefreshes() {
  topicRefreshTimer = null;
  for (const [topicId, state] of topicRefreshes) {
    if (activeTopicRefreshes >= MAX_TOPIC_REFRESHES) break;
    if (!state.dirty || state.inFlight) continue;
    state.dirty = false;
    state.inFlight = true;
    activeTopicRefreshes++;
    void fetchTopic(topicId, state);
  }
}

async function fetchTopic(topicId, state) {
  const revision = state.revision;
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), TOPIC_REQUEST_TIMEOUT);
  try {
    const response = await fetch(
      `/api/forum/${forumConfig.chatId}/topic/${topicId}/card`,
      { signal: controller.signal },
    );
    const html = response.status === 404 ? null : await response.text();
    if (!response.ok && response.status !== 404)
      throw new Error(`HTTP ${response.status}`);
    if (revision !== state.revision) {
      state.dirty = true;
      return;
    }
    const current = findTopic(topicId);
    if (!current) return;
    if (html === null) {
      current.remove();
      return;
    }
    const fragment = document.createElement("div");
    fragment.innerHTML = html;
    const card = fragment.firstElementChild;
    if (!card || card.dataset.chatId !== topicId)
      throw new Error("Invalid topic card");
    const focused = current.contains(document.activeElement);
    const focusReadButton =
      focused && document.activeElement.classList.contains("mark-read-btn");
    current.replaceWith(card);
    window.parseAppleEmojis(card);
    if (focused) {
      const target =
        (focusReadButton && card.querySelector(".mark-read-btn")) ||
        card.querySelector(".chat-card-link");
      target?.focus({ preventScroll: true });
    }
  } catch (error) {
    console.error("Could not update topic:", error);
    topicStatus.textContent = "Some topics may be stale. Refresh to retry.";
  } finally {
    clearTimeout(timeout);
    state.inFlight = false;
    activeTopicRefreshes--;
    if (!state.dirty) topicRefreshes.delete(topicId);
    scheduleTopicRefresh();
  }
}

function refreshVisibleTopics() {
  topicList
    .querySelectorAll(".chat-card-wrapper")
    .forEach((card) => refreshTopic(card.dataset.chatId));
}

const readForumBtn = document.getElementById("btn-read-forum");
if (readForumBtn) {
  readForumBtn.addEventListener("click", async () => {
    readForumBtn.disabled = true;

    try {
      const response = await fetch(`/api/chat/${forumConfig.chatId}/read`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Requested-With": "XMLHttpRequest",
        },
        body: JSON.stringify({}),
      });
      if (
        !response.ok ||
        !response.headers.get("content-type")?.includes("application/json")
      )
        throw new Error("Read request failed");
      readForumBtn.textContent = "Read queued";
      topicStatus.textContent = "Read queued; waiting for Telegram.";
    } catch (e) {
      console.error(e);
      topicStatus.textContent =
        "Could not confirm whether the read was queued. Refresh and check before retrying.";
    } finally {
      setTimeout(() => {
        readForumBtn.disabled = false;
      }, 500);
    }
  });
}

topicList.addEventListener("click", async (e) => {
  if (e.target.classList.contains("mark-read-btn")) {
    e.preventDefault();
    e.stopPropagation();

    const btn = e.target;
    const chatId = btn.getAttribute("data-chat-id");
    const topicId = btn.getAttribute("data-topic-id");

    if (!chatId || !topicId) return;

    btn.disabled = true;

    try {
      const response = await fetch(`/api/chat/${chatId}/read`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Requested-With": "XMLHttpRequest",
        },
        body: JSON.stringify({ topic_id: parseInt(topicId) }),
      });

      if (
        !response.ok ||
        !response.headers.get("content-type")?.includes("application/json")
      )
        throw new Error("Read request failed");
      btn.textContent = "queued";
      topicStatus.textContent = "Read queued; waiting for Telegram.";
    } catch (err) {
      console.error("Error marking topic as read:", err);
      topicStatus.textContent =
        "Could not confirm whether the read was queued. Refresh and check before retrying.";
      btn.disabled = false;
    }
  }
});

document.addEventListener("tg:event", (event) => {
  const data = event.detail;
  if (data.type === "resync_required") {
    window.location.reload();
    return;
  }
  if (data.chat_id !== forumConfig.chatId) return;
  if (data.type === "read_failed") {
    topicStatus.textContent =
      data.text_plain || "Read was not confirmed. Refresh before retrying.";
    if (data.topic_id) refreshTopic(data.topic_id);
    else refreshVisibleTopics();
    return;
  }
  if (!["message", "action", "edited", "deleted", "read"].includes(data.type))
    return;
  if (!data.topic_id || data.scope_known === false) {
    refreshVisibleTopics();
  } else if (findTopic(data.topic_id)) {
    refreshTopic(data.topic_id);
  } else if (data.type === "action" && pageReloadTimer === null) {
    // A new topic may enter the current page and change its cursor.
    pageReloadTimer = setTimeout(() => window.location.reload(), 500);
  }
});
