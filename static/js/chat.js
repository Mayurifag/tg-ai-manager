const chatConfig = window.TG_CHAT || {};
const chatId = chatConfig.chatId;
const topicId = chatConfig.topicId;
const isChannel = Boolean(chatConfig.isChannel);
const chatControls = document.getElementById("chat-controls");
if (chatControls && window.matchMedia?.("(max-width: 800px)").matches)
  chatControls.open = false;

async function requestOk(url, options) {
  const response = await fetch(url, options);
  const isJson = response.headers
    .get("content-type")
    ?.includes("application/json");
  if (!response.ok || !isJson) {
    const body = isJson ? await response.json().catch(() => ({})) : {};
    throw new Error(body.error || `HTTP ${response.status}`);
  }
  return response;
}

let arConfig = {
  enabled: false,
  emoji: "💩",
  target_users: [],
};
let availableUsers = [];
let usersLoaded = false;

function showToast(message) {
  const x = document.getElementById("toast");
  x.textContent = message;
  x.className = "show";
  setTimeout(function () {
    x.className = x.className.replace("show", "");
  }, 3000);
}

window.retryMedia = function (button) {
  const frame = button.closest(".media-preview-frame");
  const image = frame?.querySelector("img.lightbox-trigger");
  const video = frame?.querySelector("video");
  if (!image && !video) return;
  if (document.activeElement === button)
    (image?.closest("button") || video)?.focus({ preventScroll: true });
  frame.classList.remove("media-failed");
  if (image) {
    const url = new URL(image.getAttribute("src"), window.location.href);
    url.searchParams.set("retry", Date.now().toString());
    image.src = url.href;
  } else {
    video.load();
  }
};

const autoreadBtn = document.getElementById("btn-toggle-autoread");
if (autoreadBtn) {
  autoreadBtn.addEventListener("click", async () => {
    const indicator = autoreadBtn.querySelector(".status-indicator");
    const wasEnabled = indicator.classList.contains("on");
    const newState = !wasEnabled;

    indicator.classList.toggle("on", newState);
    indicator.classList.toggle("off", !newState);
    autoreadBtn.setAttribute("aria-pressed", String(newState));
    autoreadBtn.disabled = true;

    try {
      await requestOk("/api/rules/autoread/toggle", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Requested-With": "XMLHttpRequest",
        },
        body: JSON.stringify({
          chat_id: chatId,
          topic_id: topicId,
          enabled: newState,
        }),
      });
      showToast(newState ? "Autoread Enabled" : "Autoread Disabled");
    } catch (e) {
      console.error(e);
      indicator.classList.toggle("on", wasEnabled);
      indicator.classList.toggle("off", !wasEnabled);
      autoreadBtn.setAttribute("aria-pressed", String(wasEnabled));
      showToast(e.message || "Failed to toggle Autoread");
    } finally {
      autoreadBtn.disabled = false;
    }
  });
}

const aiAutoreadBtn = document.getElementById("btn-toggle-ai-autoread");
if (aiAutoreadBtn) {
  aiAutoreadBtn.addEventListener("click", async () => {
    const indicator = aiAutoreadBtn.querySelector(".status-indicator");
    const wasEnabled = indicator.classList.contains("on");
    const newState = !wasEnabled;

    indicator.classList.toggle("on", newState);
    indicator.classList.toggle("off", !newState);
    aiAutoreadBtn.setAttribute("aria-pressed", String(newState));
    aiAutoreadBtn.disabled = true;

    try {
      await requestOk("/api/rules/ai_autoread/toggle", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Requested-With": "XMLHttpRequest",
        },
        body: JSON.stringify({
          chat_id: chatId,
          topic_id: topicId,
          enabled: newState,
        }),
      });
      showToast(newState ? "AI Autoread Enabled" : "AI Autoread Disabled");
    } catch (e) {
      console.error(e);
      indicator.classList.toggle("on", wasEnabled);
      indicator.classList.toggle("off", !wasEnabled);
      aiAutoreadBtn.setAttribute("aria-pressed", String(wasEnabled));
      showToast(e.message || "Failed to toggle AI Autoread");
    } finally {
      aiAutoreadBtn.disabled = false;
    }
  });
}

const markReadBtn = document.getElementById("btn-read-chat");
if (markReadBtn) {
  markReadBtn.addEventListener("click", async () => {
    markReadBtn.style.opacity = "0.5";
    markReadBtn.disabled = true;

    try {
      await requestOk(`/api/chat/${chatId}/read`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Requested-With": "XMLHttpRequest",
        },
        body: JSON.stringify({ topic_id: topicId }),
      });

      showToast("Read queued");
      markReadBtn.style.opacity = "1";
      markReadBtn.disabled = false;
    } catch (e) {
      console.error(e);
      showToast("Failed to mark read");
      setTimeout(() => {
        markReadBtn.style.opacity = "1";
        markReadBtn.disabled = false;
      }, 500);
    }
  });
}

const copyTopicsBtn = document.getElementById("btn-apply-topics");
if (copyTopicsBtn) {
  copyTopicsBtn.addEventListener("click", async () => {
    if (!confirm("Enable autoread for ALL topics in this forum?")) return;

    copyTopicsBtn.style.opacity = "0.5";
    try {
      await requestOk("/api/rules/autoread/apply_all", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Requested-With": "XMLHttpRequest",
        },
        body: JSON.stringify({ forum_id: chatId, enabled: true }),
      });
      showToast("Applied to all topics");
    } catch (e) {
      showToast("Failed to apply");
    } finally {
      copyTopicsBtn.style.opacity = "1";
    }
  });
}

const arModal = document.getElementById("autoreact-modal");
const arOpenButton = document.getElementById("btn-open-autoreact");
let arModalGeneration = 0;

arOpenButton.addEventListener("click", () => {
  arModalGeneration++;
  arModal.showModal();
  arModal.classList.add("show");
  arModal.querySelector(".modal-close").focus();
  loadArSettings();
});

window.closeArModal = function () {
  if (arModal.open) arModal.close();
};

arModal.addEventListener("close", () => {
  arModalGeneration++;
  arModal.classList.remove("show");
  closeUserDropdown();
  arOpenButton.focus();
});

arModal.addEventListener("click", (e) => {
  if (e.target === arModal) closeArModal();
});
arModal.addEventListener("cancel", (e) => {
  const dropdown = document.getElementById("user-dropdown");
  if (!dropdown.hidden) {
    e.preventDefault();
    closeUserDropdown();
    document.getElementById("user-dropdown-toggle").focus();
  }
});

function closeUserDropdown() {
  const dropdown = document.getElementById("user-dropdown");
  dropdown.classList.remove("show");
  dropdown.hidden = true;
  document
    .getElementById("user-dropdown-toggle")
    .setAttribute("aria-expanded", "false");
}

async function loadArSettings() {
  const generation = arModalGeneration;
  const saveButton = document.getElementById("save-ar-btn");
  saveButton.disabled = true;
  saveButton.textContent = "Save Configuration";
  arModal.setAttribute("aria-busy", "true");
  arModal
    .querySelectorAll(
      "input, .emoji-opt, .user-selector-toggle, .user-item, .user-chip button",
    )
    .forEach((control) => (control.disabled = true));
  try {
    let url = `/api/rules/autoreact/get?chat_id=${chatId}`;
    if (topicId) url += `&topic_id=${topicId}`;
    const res = await requestOk(url);
    const data = await res.json();

    const config = {
      enabled: Boolean(data.enabled),
      emoji: data.config.emoji || "💩",
      target_users: data.config.target_users || [],
    };

    if (!isChannel && config.target_users.length > 0) await loadUsers();
    if (generation !== arModalGeneration || !arModal.open) return;

    arConfig = config;
    renderArUi();
    arModal
      .querySelectorAll(
        "input, .emoji-opt, .user-selector-toggle, .user-item, .user-chip button",
      )
      .forEach((control) => (control.disabled = false));
    arModal.setAttribute("aria-busy", "false");
    saveButton.disabled = false;
  } catch (e) {
    if (generation !== arModalGeneration || !arModal.open) return;
    arModal.setAttribute("aria-busy", "false");
    console.error("Failed to load AR settings", e);
    showToast("Failed to load Autoreact settings");
  }
}

function renderArUi() {
  document.getElementById("autoreact-checkbox").checked = arConfig.enabled;

  document.querySelectorAll(".emoji-opt").forEach((el) => {
    const selected = el.getAttribute("data-emoji") === arConfig.emoji;
    el.setAttribute("aria-pressed", String(selected));
    if (selected) {
      el.classList.add("selected");
      document.getElementById("custom-emoji-input").value = "";
    } else {
      el.classList.remove("selected");
    }
  });

  const predefined = ["💩", "🤡", "❤️"];
  if (!predefined.includes(arConfig.emoji))
    document.getElementById("custom-emoji-input").value = arConfig.emoji;

  if (!isChannel) renderUserDisplay();
}

async function loadUsers() {
  if (usersLoaded || isChannel) return;
  const dd = document.getElementById("user-dropdown");
  try {
    const res = await requestOk(`/api/chat/${chatId}/authors`);
    const data = await res.json();
    availableUsers = data.authors;
    usersLoaded = true;
    renderUserDropdown();
    renderUserDisplay();
  } catch (e) {
    dd.innerHTML =
      '<div style="padding:10px; color:red;">Failed to load users</div>';
  }
}

function renderUserDisplay() {
  if (isChannel) return;

  const display = document.getElementById("user-display");
  display.innerHTML = "";

  const warningBox = document.getElementById("ar-warning-box");
  const helpText = document.getElementById("ar-help-text");

  if (arConfig.target_users.length === 0) {
    display.innerHTML =
      '<span style="color: #666; font-style: italic;">All incoming messages (Dangerous)</span>';
    warningBox.style.display = "block";
    helpText.style.display = "none";
  } else {
    warningBox.style.display = "none";
    helpText.style.display = "block";

    arConfig.target_users.forEach((uid) => {
      const user = availableUsers.find((u) => u.id === uid);
      const name = user ? user.name : `ID: ${uid}`;

      let avatarHtml = "";
      if (user && user.avatar_url) {
        avatarHtml = `<img src="${window.TG.escapeHtml(user.avatar_url)}" alt="" style="width:16px;height:16px;border-radius:50%;margin-right:5px;object-fit:cover;">`;
      } else {
        const initial = (user ? user.name : "?").charAt(0).toUpperCase();
        avatarHtml = `<div style="width:16px;height:16px;background:#555;border-radius:50%;margin-right:5px;display:flex;align-items:center;justify-content:center;font-size:10px;color:#fff;">${window.TG.escapeHtml(initial)}</div>`;
      }

      const chip = document.createElement("div");
      chip.className = "user-chip";
      chip.innerHTML = `${avatarHtml} ${window.TG.escapeHtml(name)}`;
      const remove = document.createElement("button");
      remove.type = "button";
      remove.textContent = "×";
      remove.setAttribute("aria-label", `Remove ${name}`);
      remove.addEventListener("click", () => removeUser(uid));
      chip.appendChild(remove);
      display.appendChild(chip);
    });
  }
}

function renderUserDropdown() {
  const dd = document.getElementById("user-dropdown");
  dd.innerHTML = "";
  availableUsers.forEach((u) => {
    const isSelected = arConfig.target_users.includes(u.id);
    const item = document.createElement("button");
    item.type = "button";
    item.className = "user-item " + (isSelected ? "active" : "");
    item.dataset.userId = String(u.id);
    item.setAttribute("aria-pressed", String(isSelected));

    let avatar = "";
    if (u.avatar_url) {
      avatar = `<img src="${window.TG.escapeHtml(u.avatar_url)}" alt="" style="width:24px;height:24px;border-radius:50%;margin-right:10px;object-fit:cover;">`;
    } else {
      avatar = `<div style="width:24px;height:24px;background:#555;border-radius:50%;margin-right:10px;display:flex;align-items:center;justify-content:center;color:#fff;">${window.TG.escapeHtml(u.name.charAt(0).toUpperCase())}</div>`;
    }

    item.innerHTML = `${avatar} <span>${window.TG.escapeHtml(u.name)} <span style="display:block;font-size:0.75rem;color:#888;">${window.TG.escapeHtml(u.username || "")}</span></span>`;
    item.onclick = (e) => {
      e.stopPropagation();
      toggleUser(u.id);
    };
    dd.appendChild(item);
  });
}

window.toggleUserDropdown = function () {
  if (isChannel) return;
  const dd = document.getElementById("user-dropdown");
  if (!dd.hidden) {
    closeUserDropdown();
    return;
  }
  loadUsers();
  dd.hidden = false;
  dd.classList.add("show");
  document
    .getElementById("user-dropdown-toggle")
    .setAttribute("aria-expanded", "true");
};

function toggleUser(uid) {
  const dd = document.getElementById("user-dropdown");
  const restoreFocus = dd.contains(document.activeElement);
  if (arConfig.target_users.includes(uid)) {
    arConfig.target_users = arConfig.target_users.filter((id) => id !== uid);
  } else {
    arConfig.target_users.push(uid);
  }
  renderUserDisplay();
  renderUserDropdown();
  if (restoreFocus) {
    Array.from(dd.querySelectorAll(".user-item"))
      .find((item) => item.dataset.userId === String(uid))
      ?.focus();
  }
}

window.removeUser = function (uid) {
  const restoreFocus = document
    .getElementById("user-display")
    .contains(document.activeElement);
  arConfig.target_users = arConfig.target_users.filter((id) => id !== uid);
  renderUserDisplay();
  renderUserDropdown();
  if (restoreFocus) document.getElementById("user-dropdown-toggle").focus();
};

document.querySelectorAll(".emoji-opt").forEach((el) => {
  el.addEventListener("click", () => {
    arConfig.emoji = el.getAttribute("data-emoji");
    renderArUi();
  });
});

document.getElementById("custom-emoji-input").addEventListener("input", (e) => {
  if (e.target.value) {
    arConfig.emoji = e.target.value;
    document.querySelectorAll(".emoji-opt").forEach((opt) => {
      opt.classList.remove("selected");
      opt.setAttribute("aria-pressed", "false");
    });
  }
});

document
  .getElementById("autoreact-checkbox")
  .addEventListener("change", (e) => {
    arConfig.enabled = e.target.checked;
  });

document.getElementById("save-ar-btn").addEventListener("click", async () => {
  const btn = document.getElementById("save-ar-btn");
  const restoreFocus = document.activeElement === btn;
  const generation = arModalGeneration;
  const savedConfig = JSON.stringify(arConfig);
  const savedEnabled = arConfig.enabled;
  btn.textContent = "Saving...";
  btn.disabled = true;
  try {
    const targets = isChannel ? [] : arConfig.target_users;

    await requestOk("/api/rules/autoreact/config", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Requested-With": "XMLHttpRequest",
      },
      body: JSON.stringify({
        chat_id: chatId,
        topic_id: topicId,
        enabled: arConfig.enabled,
        config: {
          emoji: arConfig.emoji,
          target_users: targets,
        },
      }),
    });

    if (generation !== arModalGeneration || !arModal.open) return;
    const ind = document.getElementById("autoreact-indicator");
    if (savedEnabled) {
      ind.className =
        targets.length === 0
          ? "status-indicator status-all"
          : "status-indicator status-some";
    } else {
      ind.className = "status-indicator status-off";
    }

    btn.textContent = "Saved!";
    setTimeout(() => {
      if (generation !== arModalGeneration || !arModal.open) return;
      btn.textContent = "Save Configuration";
      btn.disabled = false;
      if (savedConfig !== JSON.stringify(arConfig)) {
        showToast("Previous configuration saved. Save your newer changes.");
        return;
      }
      closeArModal();
    }, 800);
    showToast("Autoreact Configured");
  } catch (e) {
    if (generation !== arModalGeneration || !arModal.open) return;
    btn.textContent = "Error";
    btn.disabled = false;
    if (
      restoreFocus &&
      arModal.open &&
      document.activeElement === document.body
    )
      btn.focus();
    showToast(e.message || "Failed to save autoreact settings");
  }
});

const container = document.getElementById("messages-container");
const currentTopicId = topicId;
const jumpToLatest = document.getElementById("jump-to-latest");
const loadNewerButton = document.getElementById("load-newer-messages");
const returnToReading = document.getElementById("return-to-reading");
const readingPositionStatus = document.getElementById(
  "reading-position-status",
);
const positionKey = chatConfig.accountId
  ? `tg-reading:${chatConfig.accountId}:${chatId}:${currentTopicId ?? "chat"}`
  : null;
let newMessageCount = 0;
const seenNewMessageIds = new Set();
let firstUnreadId = null;
let unreadMarker = null;
let userMoved = false;
let isLoading = false;
let allLoaded = false;
let prefetchedHistory = null;
let detachedHistory = false;
let loadingNewer = false;
let historyGeneration = 0;
let interactionVersion = 0;
let eventVersion = 0;
let readingRequestVersion = 0;
let previousReadingPosition = null;

["wheel", "touchstart", "pointerdown", "keydown"].forEach((name) => {
  window.addEventListener(
    name,
    () => {
      userMoved = true;
      interactionVersion++;
    },
    { passive: true },
  );
});

function nearLatest() {
  return (
    document.documentElement.scrollHeight -
      window.innerHeight -
      window.scrollY <
    300
  );
}

function clearNewMessages() {
  newMessageCount = 0;
  seenNewMessageIds.clear();
  if (jumpToLatest) jumpToLatest.hidden = true;
}

function noteNewMessage(id) {
  const key = String(id);
  if (seenNewMessageIds.has(key)) return;
  seenNewMessageIds.add(key);
  if (seenNewMessageIds.size > 1000)
    seenNewMessageIds.delete(seenNewMessageIds.values().next().value);
  newMessageCount++;
  showLatestControl();
}

function readSavedPosition() {
  if (!positionKey) return null;
  try {
    const saved = JSON.parse(sessionStorage.getItem(positionKey));
    if (
      !saved ||
      !/^msg-[1-9][0-9]{0,9}$/.test(saved.id) ||
      Number(saved.id.slice(4)) >= 2 ** 31 ||
      !Number.isFinite(saved.top)
    )
      return null;
    return saved;
  } catch (_) {
    return null;
  }
}

function messageRow(root, id) {
  const row = root.querySelector(`[data-id="${id}"]`);
  return (
    row ||
    root.querySelector(`[data-message-id="${id}"]`)?.closest(".message-row") ||
    null
  );
}

function messageIdFromHash(hash) {
  const match = /^#msg-([1-9][0-9]{0,9})$/.exec(hash || "");
  if (!match || Number(match[1]) >= 2 ** 31) return null;
  return Number(match[1]);
}

function focusMessage(id) {
  const target = document.getElementById(`msg-${id}`);
  if (!target || !container.contains(target)) return false;
  target.classList.add("message-target");
  target.scrollIntoView({ block: "center" });
  target.tabIndex = -1;
  target.focus({ preventScroll: true });
  setTimeout(() => target.classList.remove("message-target"), 3000);
  return true;
}

function historyEdge(newer = false) {
  let edge = null;
  for (const row of container.querySelectorAll("[data-oldest-id]")) {
    const id = Number(newer ? row.dataset.id : row.dataset.oldestId);
    if (!edge || (newer ? id > edge.id : id < edge.id)) edge = { row, id };
  }
  return edge;
}

function visibleMessageAnchor(excluded = null) {
  let visible = null;
  for (const row of container.querySelectorAll("[data-oldest-id]")) {
    if (row === excluded) continue;
    const rect = row.getBoundingClientRect();
    if (
      rect.bottom > 0 &&
      rect.top < window.innerHeight &&
      (!visible || rect.top < visible.top)
    )
      visible = { row, top: rect.top };
  }
  return visible;
}

function keepVisibleAnchor(visible) {
  if (visible)
    window.scrollBy(0, visible.row.getBoundingClientRect().top - visible.top);
}

function removeMessageRow(row) {
  const visible = visibleMessageAnchor(row);
  const restoreFocus = row.contains(document.activeElement);
  row.remove();
  keepVisibleAnchor(visible);
  if (restoreFocus) {
    const target = visible?.row || container;
    target.tabIndex = -1;
    target.focus({ preventScroll: true });
  }
}

function restoreReadingPosition(saved = readSavedPosition()) {
  if (!saved) return false;
  try {
    const row =
      document.getElementById(saved.id) ||
      messageRow(container, saved.id.slice(4));
    if (!row || !container.contains(row)) return false;
    const top = Math.max(
      -window.innerHeight,
      Math.min(window.innerHeight, saved.top),
    );
    window.scrollBy(0, row.getBoundingClientRect().top - top);
    return true;
  } catch (_) {
    return false;
  }
}

function showPositionStatus(message) {
  if (!readingPositionStatus) return;
  readingPositionStatus.textContent = message;
  readingPositionStatus.hidden = false;
}

function showAlbumChanged() {
  showPositionStatus("An album changed. Reload to see its latest contents.");
  showToast("Album changed. Reload to see its latest contents.");
}

function showLatestControl() {
  if (!jumpToLatest) return;
  jumpToLatest.textContent = newMessageCount
    ? `${newMessageCount} new ${newMessageCount === 1 ? "message" : "messages"} · Jump to latest`
    : "Jump to latest";
  jumpToLatest.hidden = false;
}

function historyUrlFor(key, id) {
  let url = `/api/chat/${chatId}/history?${key}=${id}`;
  if (currentTopicId != null) url += `&topic_id=${currentTopicId}`;
  return url;
}

async function loadReadingWindow(
  id,
  savedTop = null,
  retry = false,
  jumpToMessage = false,
  requestVersion = ++readingRequestVersion,
) {
  const generation = historyGeneration;
  const movement = interactionVersion;
  const events = eventVersion;
  showPositionStatus("Loading reading position…");
  try {
    const response = await requestOk(historyUrlFor("around_id", id));
    const data = await response.json();
    if (
      generation !== historyGeneration ||
      requestVersion !== readingRequestVersion
    )
      return false;
    // Layout and scroll anchoring can change scrollY without user input.
    if (movement !== interactionVersion) {
      showPositionStatus("Reading position is outside the loaded messages.");
      return false;
    }
    if (events !== eventVersion) {
      if (!retry)
        return await loadReadingWindow(
          id,
          savedTop,
          true,
          jumpToMessage,
          requestVersion,
        );
      showPositionStatus(
        "Messages changed. Reload to restore the reading position.",
      );
      return false;
    }
    const fragment = document.createElement("div");
    fragment.innerHTML = data.html;
    const row = messageRow(fragment, id);
    if (!row) throw new Error("Message unavailable");

    historyGeneration++;
    cancelAlbumRefreshes();
    isLoading = false;
    loadingNewer = false;
    allLoaded = false;
    prefetchedHistory = null;
    unreadMarker = null;
    detachedHistory = true;
    container.replaceChildren(...fragment.childNodes);
    document.getElementById("loading-indicator").hidden = true;
    window.parseAppleEmojis(container);
    window.renderLocalTimes(container);
    if (loadNewerButton) {
      loadNewerButton.hidden = false;
      loadNewerButton.disabled = false;
      loadNewerButton.textContent = "Load newer messages";
    }
    showLatestControl();
    if (readingPositionStatus) readingPositionStatus.hidden = true;
    if (jumpToMessage) {
      focusMessage(id);
    } else if (savedTop === null) {
      markFirstUnread()?.scrollIntoView({ block: "start" });
    } else {
      const top = Math.max(
        -window.innerHeight,
        Math.min(window.innerHeight, savedTop),
      );
      window.scrollBy(0, row.getBoundingClientRect().top - top);
    }
    prefetchOlderMessages();
    return true;
  } catch (error) {
    if (
      generation !== historyGeneration ||
      requestVersion !== readingRequestVersion
    )
      return false;
    console.error("Could not load reading position:", error);
    showPositionStatus(
      jumpToMessage
        ? "Message unavailable in this chat or topic."
        : "Reading position is unavailable. Showing current messages.",
    );
    return false;
  }
}

async function navigateToMessage(id, remember = true) {
  const requestVersion = ++readingRequestVersion;
  userMoved = true;
  const visible = remember ? visibleMessageAnchor() : null;
  const previous = visible ? { id: visible.row.id, top: visible.top } : null;
  let found = focusMessage(id);
  if (!found)
    found = await loadReadingWindow(id, null, false, true, requestVersion);
  if (found && previous && returnToReading) {
    previousReadingPosition = previous;
    returnToReading.hidden = false;
  }
}

if (returnToReading) {
  returnToReading.addEventListener("click", async () => {
    const previous = previousReadingPosition;
    if (!previous || returnToReading.disabled) return;
    const requestVersion = ++readingRequestVersion;
    returnToReading.disabled = true;
    try {
      let restored = restoreReadingPosition(previous);
      if (!restored) {
        restored = await loadReadingWindow(
          Number(previous.id.slice(4)),
          previous.top,
          false,
          false,
          requestVersion,
        );
      }
      if (restored && requestVersion === readingRequestVersion) {
        previousReadingPosition = null;
        returnToReading.hidden = true;
        const row = document.getElementById(previous.id);
        if (row) {
          row.tabIndex = -1;
          row.focus({ preventScroll: true });
        }
      }
    } finally {
      returnToReading.disabled = false;
    }
  });
}

container.addEventListener("click", (event) => {
  if (
    event.defaultPrevented ||
    event.button !== 0 ||
    event.metaKey ||
    event.ctrlKey ||
    event.shiftKey ||
    event.altKey
  )
    return;
  const link = event.target.closest("a.reply-indicator");
  const id = messageIdFromHash(link?.getAttribute("href"));
  if (id === null) return;
  event.preventDefault();
  void navigateToMessage(id);
});

window.addEventListener("hashchange", () => {
  const id = messageIdFromHash(window.location?.hash);
  if (id !== null) void navigateToMessage(id);
});

function markFirstUnread() {
  if (!firstUnreadId || unreadMarker) return null;
  let row = document.getElementById(`msg-${firstUnreadId}`);
  if (row?.dataset.messageId) row = row.closest(".message-row");
  if (!row || !container.contains(row)) {
    row = container
      .querySelector(`[data-message-id="${firstUnreadId}"]`)
      ?.closest(".message-row");
  }
  if (!row) return null;
  unreadMarker = document.createElement("div");
  unreadMarker.className = "unread-marker";
  unreadMarker.textContent = "Unread messages at opening";
  unreadMarker.setAttribute("role", "separator");
  row.after(unreadMarker);
  if (readingPositionStatus) readingPositionStatus.hidden = true;
  return unreadMarker;
}

async function findFirstUnread() {
  let url = `/api/chat/${chatId}/first-unread`;
  if (currentTopicId != null) url += `?topic_id=${currentTopicId}`;
  try {
    const response = await requestOk(url);
    const data = await response.json();
    if (
      !Number.isSafeInteger(data.message_id) ||
      data.message_id <= 0 ||
      data.message_id >= 2 ** 31
    )
      return;
    firstUnreadId = data.message_id;
    const marker = markFirstUnread();
    if (marker && !userMoved) {
      marker.scrollIntoView({ block: "start" });
    } else if (!marker) {
      if (!userMoved) {
        await loadReadingWindow(firstUnreadId);
      } else {
        showPositionStatus("First unread is outside the loaded messages.");
      }
    }
  } catch (error) {
    console.error("Could not locate first unread:", error);
    if (readingPositionStatus) {
      readingPositionStatus.textContent = "First unread is unavailable.";
      readingPositionStatus.hidden = false;
    }
  }
}

window.addEventListener("pagehide", () => {
  if (!positionKey) return;
  const visible = visibleMessageAnchor();
  if (!visible) return;
  try {
    sessionStorage.setItem(
      positionKey,
      JSON.stringify({ id: visible.row.id, top: visible.top }),
    );
  } catch (_) {
    // Storage may be disabled; navigation still works without restoration.
  }
});

if (jumpToLatest) {
  jumpToLatest.addEventListener("click", async () => {
    const restoreFocus = document.activeElement === jumpToLatest;
    if (detachedHistory) {
      const generation = historyGeneration;
      const movement = interactionVersion;
      const events = eventVersion;
      jumpToLatest.disabled = true;
      try {
        const response = await requestOk(historyUrlFor("offset_id", 0));
        const data = await response.json();
        if (
          generation !== historyGeneration ||
          movement !== interactionVersion ||
          events !== eventVersion
        ) {
          showPositionStatus("Messages changed. Try jumping to latest again.");
          return;
        }
        historyGeneration++;
        cancelAlbumRefreshes();
        isLoading = false;
        loadingNewer = false;
        allLoaded = false;
        prefetchedHistory = null;
        unreadMarker = null;
        firstUnreadId = null;
        detachedHistory = false;
        container.innerHTML =
          data.html || '<p class="empty-messages">No messages to show.</p>';
        document.getElementById("loading-indicator").hidden = true;
        window.parseAppleEmojis(container);
        window.renderLocalTimes(container);
        if (loadNewerButton) loadNewerButton.hidden = true;
        if (readingPositionStatus) readingPositionStatus.hidden = true;
        prefetchOlderMessages();
      } catch (error) {
        console.error("Could not load latest messages:", error);
        showPositionStatus("Could not load latest messages. Try again.");
        return;
      } finally {
        jumpToLatest.disabled = false;
      }
    }
    window.scrollTo(0, document.documentElement.scrollHeight);
    if (
      restoreFocus &&
      (document.activeElement === document.body ||
        document.activeElement === jumpToLatest)
    ) {
      const newest = container.querySelector("[data-id]");
      if (newest) {
        newest.tabIndex = -1;
        newest.focus({ preventScroll: true });
      }
    }
    clearNewMessages();
  });
}

function historyUrl(offsetId) {
  return historyUrlFor("offset_id", offsetId);
}

const albumRefreshes = new Map();
const MAX_PENDING_ALBUM_REFRESHES = 32;
let albumRefreshTimer = null;
let albumRefreshRunning = false;
let albumRefreshController = null;

function cancelAlbumRefreshes() {
  albumRefreshes.clear();
  if (albumRefreshTimer) clearTimeout(albumRefreshTimer);
  albumRefreshTimer = null;
  albumRefreshController?.abort();
}

function albumRows(groupId) {
  return Array.from(container.querySelectorAll("[data-grouped-id]")).filter(
    (row) => row.dataset.groupedId === groupId,
  );
}

function albumMemberIds(row) {
  const parts = Array.from(row.querySelectorAll(".album-item"));
  if (!parts.length)
    return row.dataset.deleted === "true" ? [] : [row.dataset.id];
  return parts
    .filter((part) => part.dataset.deleted !== "true")
    .map((part) => part.dataset.messageId);
}

function scheduleAlbumRefresh() {
  if (albumRefreshTimer || albumRefreshRunning) return;
  if (!Array.from(albumRefreshes.values()).some((state) => state.dirty)) return;
  albumRefreshTimer = setTimeout(() => {
    albumRefreshTimer = null;
    void refreshNextAlbum();
  }, 200);
}

function queueAlbumRefresh(
  groupId,
  anchorId,
  { added = false, deleted = false } = {},
) {
  const rows = albumRows(groupId);
  if (!rows.length) return false;
  let state = albumRefreshes.get(groupId);
  if (state && state.generation !== historyGeneration) {
    albumRefreshes.delete(groupId);
    state = null;
  }
  if (!state) {
    if (albumRefreshes.size >= MAX_PENDING_ALBUM_REFRESHES) {
      showAlbumChanged();
      return true;
    }
    state = {
      groupId,
      generation: historyGeneration,
      version: 0,
      required: new Set(),
      deleted: new Set(),
      dirty: false,
      anchorId: null,
    };
    albumRefreshes.set(groupId, state);
  }
  if (deleted) {
    state.deleted.add(String(anchorId));
    state.required.delete(String(anchorId));
  }
  for (const row of rows) {
    if (row.dataset.deleted === "true") state.deleted.add(row.dataset.id);
    for (const part of row.querySelectorAll(".album-item")) {
      if (part.dataset.deleted === "true")
        state.deleted.add(part.dataset.messageId);
    }
    for (const id of albumMemberIds(row)) {
      if (!state.deleted.has(id)) state.required.add(id);
    }
  }
  if (added && !state.deleted.has(String(anchorId)))
    state.required.add(String(anchorId));
  if (!state.required.size) {
    albumRefreshes.delete(groupId);
    return false;
  }
  state.anchorId =
    !deleted && state.required.has(String(anchorId))
      ? String(anchorId)
      : state.required.values().next().value;
  state.version++;
  state.dirty = true;
  scheduleAlbumRefresh();
  return true;
}

function queueSplitAlbums() {
  const seen = new Set();
  for (const row of container.querySelectorAll("[data-grouped-id]")) {
    const groupId = row.dataset.groupedId;
    if (seen.has(groupId)) queueAlbumRefresh(groupId, row.dataset.id);
    else seen.add(groupId);
  }
}

async function refreshNextAlbum() {
  const state = Array.from(albumRefreshes.values()).find((item) => item.dirty);
  if (!state) return;
  if (
    state.generation !== historyGeneration ||
    !albumRows(state.groupId).length
  ) {
    albumRefreshes.delete(state.groupId);
    scheduleAlbumRefresh();
    return;
  }
  state.dirty = false;
  albumRefreshRunning = true;
  const version = state.version;
  const controller = (albumRefreshController = new AbortController());
  const timeout = setTimeout(() => controller.abort(), 10000);
  const isCurrent = () =>
    albumRefreshes.get(state.groupId) === state &&
    state.generation === historyGeneration &&
    state.version === version;
  try {
    const response = await requestOk(
      historyUrlFor("around_id", state.anchorId),
      {
        signal: controller.signal,
      },
    );
    const data = await response.json();
    if (!isCurrent()) return;
    const rows = albumRows(state.groupId);
    if (!rows.length) return;
    const fragment = document.createElement("div");
    fragment.innerHTML = data.html;
    const candidates = Array.from(
      fragment.querySelectorAll("[data-grouped-id]"),
    ).filter((row) => row.dataset.groupedId === state.groupId);
    if (candidates.length !== 1) throw new Error("Album unavailable");
    const replacement = candidates[0];
    const ids = new Set(albumMemberIds(replacement));
    const oldest = historyEdge()?.id;
    const newest = historyEdge(true)?.id;
    if (
      Array.from(state.required).some((id) => !ids.has(id)) ||
      Array.from(state.deleted).some((id) => ids.has(id)) ||
      Array.from(ids).some((id) => {
        if (state.required.has(id)) return false;
        const existing = messageRow(container, id);
        return (
          (existing && !rows.includes(existing)) ||
          Number(id) < oldest ||
          Number(id) > newest
        );
      })
    )
      throw new Error("Incomplete or out-of-window album");

    const visible = visibleMessageAnchor();
    const restoreFocus = rows.some((row) =>
      row.contains(document.activeElement),
    );
    const moveUnreadMarker =
      unreadMarker &&
      rows.some(
        (row) =>
          messageRow(row, firstUnreadId) ||
          row.dataset.id === String(firstUnreadId),
      );
    window.parseAppleEmojis(replacement);
    window.renderLocalTimes(replacement);
    for (const row of rows) row.remove();
    const before = Array.from(
      container.querySelectorAll("[data-oldest-id]"),
    ).find((row) => Number(row.dataset.id) < Number(replacement.dataset.id));
    container.insertBefore(replacement, before || null);
    if (moveUnreadMarker) {
      unreadMarker.remove();
      unreadMarker = null;
      markFirstUnread();
    }
    if (visible) {
      if (rows.includes(visible.row)) visible.row = replacement;
      keepVisibleAnchor(visible);
    }
    if (restoreFocus) {
      replacement.tabIndex = -1;
      replacement.focus({ preventScroll: true });
    }
  } catch (error) {
    if (isCurrent()) {
      console.error("Could not refresh album:", error);
      showAlbumChanged();
    }
  } finally {
    clearTimeout(timeout);
    if (albumRefreshController === controller) albumRefreshController = null;
    albumRefreshRunning = false;
    if (albumRefreshes.get(state.groupId) === state && !state.dirty)
      albumRefreshes.delete(state.groupId);
    scheduleAlbumRefresh();
  }
}

function prefetchOlderMessages() {
  const edge = historyEdge();
  if (allLoaded || !edge) return;
  const offsetId = String(edge.id);
  if (prefetchedHistory?.offsetId === offsetId) return;
  const promise = requestOk(historyUrl(offsetId))
    .then((response) => response.json())
    .catch(() => null);
  prefetchedHistory = { offsetId, promise, events: eventVersion };
}

requestAnimationFrame(() => {
  const targetId = messageIdFromHash(window.location?.hash);
  if (targetId !== null) {
    void navigateToMessage(targetId, false);
    prefetchOlderMessages();
    return;
  }
  const saved = readSavedPosition();
  const restored = restoreReadingPosition(saved);
  if (!restored) {
    window.scrollTo(0, document.documentElement.scrollHeight);
    if (saved) void loadReadingWindow(Number(saved.id.slice(4)), saved.top);
    else if (chatConfig.accountId) void findFirstUnread();
  }
  prefetchOlderMessages();
  if (document.documentElement.scrollHeight <= window.innerHeight + 100)
    loadOlderMessages();
});
window.renderLocalTimes(container);

window.addEventListener("scroll", () => {
  if (!detachedHistory && nearLatest()) clearNewMessages();
  if (isLoading || allLoaded) return;
  if (window.scrollY < 100) loadOlderMessages();
});

async function loadOlderMessages() {
  if (isLoading || allLoaded) return;
  isLoading = true;
  const generation = historyGeneration;
  const events = eventVersion;
  const indicator = document.getElementById("loading-indicator");
  indicator.textContent = "Loading older messages…";
  indicator.hidden = false;

  const edge = historyEdge();
  if (!edge) {
    allLoaded = true;
    isLoading = false;
    indicator.hidden = true;
    return;
  }

  const oldestMsg = edge.row;
  const offsetId = String(edge.id);

  try {
    let data =
      prefetchedHistory?.offsetId === offsetId &&
      prefetchedHistory.events === events
        ? await prefetchedHistory.promise
        : null;
    prefetchedHistory = null;
    if (!data) {
      const response = await requestOk(historyUrl(offsetId));
      data = await response.json();
    }

    if (generation !== historyGeneration) return;
    if (events !== eventVersion) {
      indicator.textContent = "Messages changed. Scroll up to retry.";
      return;
    }

    if (data.count === 0) {
      allLoaded = true;
    } else {
      indicator.hidden = true;
      const previousTop = oldestMsg.getBoundingClientRect().top;
      const fragment = document.createElement("div");
      fragment.innerHTML = data.html;
      window.parseAppleEmojis(fragment);
      window.renderLocalTimes(fragment);
      while (fragment.firstChild) container.appendChild(fragment.firstChild);
      queueSplitAlbums();
      markFirstUnread();
      window.scrollBy(0, oldestMsg.getBoundingClientRect().top - previousTop);
      prefetchOlderMessages();
    }
  } catch (error) {
    if (generation !== historyGeneration) return;
    console.error("Failed to load history:", error);
    indicator.textContent =
      "Could not load older messages. Scroll up to retry.";
    return;
  } finally {
    if (generation === historyGeneration) isLoading = false;
  }
  indicator.hidden = true;
  if (
    !allLoaded &&
    document.documentElement.scrollHeight <= window.innerHeight + 100
  ) {
    requestAnimationFrame(loadOlderMessages);
  }
}

if (loadNewerButton) {
  loadNewerButton.addEventListener("click", async () => {
    if (!detachedHistory || loadingNewer) return;
    const edge = historyEdge(true);
    if (!edge) return;
    const newest = edge.row;
    const generation = historyGeneration;
    const events = eventVersion;
    const newestId = edge.id;
    loadingNewer = true;
    loadNewerButton.disabled = true;
    loadNewerButton.textContent = "Loading newer messages…";
    try {
      const response = await requestOk(historyUrlFor("after_id", newestId));
      const data = await response.json();
      if (generation !== historyGeneration) return;
      if (events !== eventVersion) {
        loadNewerButton.textContent =
          "Messages changed. Retry loading newer messages";
        loadNewerButton.disabled = false;
        return;
      }
      if (!data.count) {
        loadNewerButton.textContent = "No newer messages";
        return;
      }
      const previousTop = newest.getBoundingClientRect().top;
      const fragment = document.createElement("div");
      fragment.innerHTML = data.html;
      window.parseAppleEmojis(fragment);
      window.renderLocalTimes(fragment);
      while (fragment.lastChild) container.prepend(fragment.lastChild);
      queueSplitAlbums();
      window.scrollBy(0, newest.getBoundingClientRect().top - previousTop);
      loadNewerButton.textContent = "Load newer messages";
      loadNewerButton.disabled = false;
    } catch (error) {
      if (generation !== historyGeneration) return;
      console.error("Failed to load newer messages:", error);
      loadNewerButton.textContent = "Could not load newer messages. Retry";
      loadNewerButton.disabled = false;
    } finally {
      if (generation === historyGeneration) loadingNewer = false;
    }
  });
}

const pendingReactions = new Set();
container.addEventListener("click", async (event) => {
  const button = event.target.closest(
    ".reaction-pill, .hover-reaction, .album-part-reaction",
  );
  if (!button) return;
  const msgId = Number(button.dataset.msgId);
  const emoji = button.dataset.emoji;
  if (pendingReactions.has(msgId)) return;
  pendingReactions.add(msgId);
  try {
    await requestOk(`/api/chat/${chatId}/message/${msgId}/reaction`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Requested-With": "XMLHttpRequest",
      },
      body: JSON.stringify({ reaction: emoji.toString() }),
    });
    showToast("Reaction queued");
  } catch (e) {
    console.error("Failed to toggle reaction", e);
    showToast("Failed to queue reaction");
  } finally {
    pendingReactions.delete(msgId);
  }
});

window.debugProcessMessage = async function (el, msgId) {
  el.style.opacity = "0.5";
  try {
    const res = await requestOk("/api/debug/process", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Requested-With": "XMLHttpRequest",
      },
      body: JSON.stringify({
        chat_id: chatId,
        msg_id: msgId,
        topic_id: topicId,
      }),
    });
    const data = await res.json();
    let text = "Debug:\n";
    if (data.autoread)
      text += `Read: ${data.autoread.status} (${data.autoread.reason}${data.autoread.inherited ? ", inherited" : ""})\n`;
    if (data.autoreact)
      text += `React: ${data.autoreact.status} (${data.autoreact.detail}${data.autoreact.inherited ? ", inherited" : ""})\n`;
    if (data.note) text += data.note;
    showToast(text);
  } catch (e) {
    console.error(e);
    showToast("Debug Request Failed");
  } finally {
    el.style.opacity = "1";
  }
};

function buildReactionsHtml(msgId, reactions, albumIndex = null) {
  let html = "";
  reactions.forEach((r) => {
    const chosenClass = r.is_chosen ? "chosen" : "";
    const emojiVal = r.custom_emoji_id || r.emoji;
    let innerContent = "";
    if (r.custom_emoji_id) {
      innerContent = `<img src="/media/custom_emoji/${window.TG.escapeHtml(r.custom_emoji_id)}" alt="" loading="lazy">`;
    } else {
      innerContent = `<span>${window.TG.escapeHtml(r.emoji)}\uFE0F</span>`;
    }
    html += `
        <button type="button" class="reaction-pill ${chosenClass}" data-msg-id="${msgId}" data-emoji="${window.TG.escapeHtml(emojiVal)}"
            aria-pressed="${Boolean(r.is_chosen)}" aria-label="React ${albumIndex ? `to album item ${window.TG.escapeHtml(albumIndex)} ` : ""}with ${window.TG.escapeHtml(r.emoji)}">
            ${innerContent}
            <span class="reaction-count">${window.TG.escapeHtml(r.count)}</span>
        </button>`;
  });
  return html;
}

document.addEventListener("tg:event", (e) => {
  const data = e.detail;
  if (data.type === "resync_required") {
    window.location.reload();
    return;
  }
  if (data.chat_id !== chatId) return;
  if (data.type === "read_failed")
    showToast("Telegram could not mark this chat as read");
  if (data.type === "reaction_failed")
    showToast("Telegram could not apply the reaction");
  if (
    !data.scope_known &&
    ["message", "edited", "action"].includes(data.type)
  ) {
    window.location.reload();
    return;
  }
  if (
    currentTopicId &&
    data.topic_id !== currentTopicId &&
    !(
      data.topic_id == null &&
      ["deleted", "reaction_update", "reaction_failed"].includes(data.type)
    )
  )
    return;

  if (
    ["message", "action", "edited", "deleted", "reaction_update"].includes(
      data.type,
    )
  )
    eventVersion++;

  if (
    (data.type === "message" || data.type === "action") &&
    data.rendered_html
  ) {
    if (data.message_model && messageRow(container, data.message_model.id))
      return;
    const msgId = data.message_model?.id;
    const groupedId = data.message_model?.grouped_id;
    const groupId = groupedId == null ? null : String(groupedId);
    const group = groupId && albumRows(groupId);
    if (group?.length) {
      // Do not extend a detached history window into an unloaded newer range.
      if (detachedHistory && Number(msgId) > historyEdge(true)?.id) {
        noteNewMessage(msgId);
      } else {
        const alreadyPending = albumRefreshes
          .get(groupId)
          ?.required.has(String(msgId));
        if (!alreadyPending) {
          queueAlbumRefresh(groupId, msgId, { added: true });
          if (!nearLatest()) noteNewMessage(msgId);
        }
      }
      if (detachedHistory && loadNewerButton) {
        loadNewerButton.disabled = false;
        loadNewerButton.textContent = "Load newer messages";
      }
      return;
    }
    if (detachedHistory) {
      noteNewMessage(msgId);
      if (loadNewerButton) {
        loadNewerButton.disabled = false;
        loadNewerButton.textContent = "Load newer messages";
      }
      return;
    }
    const nearBottom = nearLatest();
    const tempDiv = document.createElement("div");
    tempDiv.innerHTML = data.rendered_html;
    window.parseAppleEmojis(tempDiv);
    window.renderLocalTimes(tempDiv);
    container.querySelector(".empty-messages")?.remove();
    while (tempDiv.lastChild) container.prepend(tempDiv.lastChild);
    if (nearBottom) {
      window.scrollTo(0, document.documentElement.scrollHeight);
      clearNewMessages();
    } else if (jumpToLatest) {
      noteNewMessage(msgId);
    }
  } else if (data.type === "edited" && data.message_model) {
    const msgId = data.message_model.id;
    const msgRow = messageRow(container, msgId);
    if (msgRow) {
      if (msgRow.dataset.groupedId || data.message_model.grouped_id) {
        if (
          !msgRow.dataset.groupedId ||
          (data.message_model.grouped_id != null &&
            msgRow.dataset.groupedId !== String(data.message_model.grouped_id))
        )
          showAlbumChanged();
        else queueAlbumRefresh(msgRow.dataset.groupedId, msgId);
        return;
      }
      const fragment = document.createElement("div");
      fragment.innerHTML = data.rendered_html || "";
      const replacement = messageRow(fragment, msgId);
      if (!replacement) {
        showPositionStatus(
          "A message changed. Reload to see its latest contents.",
        );
        return;
      }
      const visible = visibleMessageAnchor();
      const restoreFocus = msgRow.contains(document.activeElement);
      const oldReply = msgRow.querySelector(".reply-indicator");
      const newReply = replacement.querySelector(".reply-indicator");
      // Live edit events do not necessarily include the replied-to message.
      // Keep the existing preview only when the target itself is unchanged.
      if (
        data.message_model.reply_to_text == null &&
        oldReply &&
        newReply &&
        oldReply.getAttribute("href") === newReply.getAttribute("href")
      )
        newReply.replaceWith(oldReply);
      window.parseAppleEmojis(replacement);
      window.renderLocalTimes(replacement);
      msgRow.replaceWith(replacement);
      if (visible) {
        if (visible.row === msgRow) visible.row = replacement;
        keepVisibleAnchor(visible);
      }
      if (restoreFocus) {
        replacement.tabIndex = -1;
        replacement.focus({ preventScroll: true });
      }
    }
  } else if (data.type === "deleted" && data.message_model) {
    const msgId = data.message_model.id;
    const msgRow = messageRow(container, msgId);
    if (msgRow) {
      const part = msgRow.querySelector(`[data-message-id="${msgId}"]`);
      if (msgRow.dataset.groupedId) {
        const visible = visibleMessageAnchor();
        const hadFocus = msgRow.contains(document.activeElement);
        const deleted = part || msgRow;
        deleted.textContent = "Deleted message";
        deleted.dataset.deleted = "true";
        deleted.classList.add("album-item-deleted");
        const caption = msgRow.querySelector(".text");
        if (caption) caption.hidden = true;
        if (msgRow.dataset.id === String(msgId)) {
          msgRow
            .querySelectorAll(`[data-msg-id="${msgId}"]`)
            .forEach((button) => {
              button.disabled = true;
            });
          const debugButton = msgRow.querySelector(".debug-process");
          if (debugButton) debugButton.disabled = true;
        }
        if (
          !queueAlbumRefresh(msgRow.dataset.groupedId, msgId, { deleted: true })
        ) {
          for (const row of albumRows(msgRow.dataset.groupedId))
            removeMessageRow(row);
        } else {
          keepVisibleAnchor(visible);
          if (
            hadFocus &&
            (!msgRow.contains(document.activeElement) ||
              document.activeElement.disabled)
          ) {
            msgRow.tabIndex = -1;
            msgRow.focus({ preventScroll: true });
          }
        }
        return;
      }
      msgRow.style.opacity = "0";
      setTimeout(() => removeMessageRow(msgRow), 300);
    }
  } else if (data.type === "reaction_update" && data.message_model) {
    const msgId = data.message_model.id;
    const reactionsContainer = document.getElementById(`reactions-${msgId}`);
    if (reactionsContainer) {
      const focused = reactionsContainer.contains(document.activeElement)
        ? document.activeElement.dataset.emoji
        : null;
      reactionsContainer.innerHTML = buildReactionsHtml(
        msgId,
        data.message_model.reactions,
        reactionsContainer.dataset.albumIndex,
      );
      window.parseAppleEmojis(reactionsContainer);
      if (focused != null) {
        const replacement = Array.from(
          reactionsContainer.querySelectorAll(".reaction-pill"),
        ).find((button) => button.dataset.emoji === focused);
        if (replacement) replacement.focus({ preventScroll: true });
        else messageRow(container, msgId)?.focus({ preventScroll: true });
      }
    }
    const groupId = messageRow(container, msgId)?.dataset.groupedId;
    if (groupId && albumRefreshes.has(groupId))
      queueAlbumRefresh(groupId, msgId);
  }
});
