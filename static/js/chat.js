const chatConfig = window.TG_CHAT || {};
const chatId = chatConfig.chatId;
const topicId = chatConfig.topicId;
const isChannel = Boolean(chatConfig.isChannel);

async function requestOk(url, options) {
  const response = await fetch(url, options);
  const isJson = response.headers
    .get("content-type")
    ?.includes("application/json");
  if (!response.ok || !isJson) {
    const body = isJson ? await response.json().catch(() => ({})) : {};
    const error = new Error(body.error || `HTTP ${response.status}`);
    error.status = response.status;
    error.code = body.code;
    throw error;
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
let usersLoadGeneration = 0;

function showToast(message) {
  const x = document.getElementById("toast");
  x.textContent = message;
  x.className = "show";
  setTimeout(function () {
    x.className = x.className.replace("show", "");
  }, 3000);
}

function markToggleUnconfirmed(button, indicator, error) {
  indicator.classList.remove("on", "off");
  indicator.classList.add("unknown");
  button.setAttribute("aria-pressed", "mixed");
  button.querySelector(".label").textContent += " (unknown)";
  const detail =
    error?.status === 409 && error.message ? `${error.message} ` : "";
  button.title = `${detail}Could not confirm the change. Reload and check before retrying.`;
  showToast(button.title);
}

function bindRuleToggle(button, ruleType, label) {
  if (!button) return;
  button.addEventListener("click", async () => {
    const indicator = button.querySelector(".status-indicator");
    const wasEnabled = indicator.classList.contains("on");
    const newState = !wasEnabled;

    indicator.classList.toggle("on", newState);
    indicator.classList.toggle("off", !newState);
    button.setAttribute("aria-pressed", String(newState));
    button.disabled = true;
    let unconfirmed = false;

    try {
      await requestOk(`/api/rules/${ruleType}/toggle`, {
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
      showToast(`${label} ${newState ? "Enabled" : "Disabled"}`);
    } catch (e) {
      console.error(e);
      if (["invalid_request", "inherited_rule"].includes(e?.code)) {
        indicator.classList.toggle("on", wasEnabled);
        indicator.classList.toggle("off", !wasEnabled);
        button.setAttribute("aria-pressed", String(wasEnabled));
        showToast(e.message);
      } else {
        unconfirmed = true;
        markToggleUnconfirmed(button, indicator, e);
      }
    } finally {
      button.disabled = unconfirmed;
    }
  });
}

const autoreadBtn = document.getElementById("btn-toggle-autoread");
const aiAutoreadBtn = document.getElementById("btn-toggle-ai-autoread");
bindRuleToggle(autoreadBtn, "autoread", "Autoread");
bindRuleToggle(aiAutoreadBtn, "ai_autoread", "AI Autoread");

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
      showToast(
        "Could not confirm whether the read was queued. Refresh and check before retrying.",
      );
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
      showToast(
        "Could not confirm whether the topic rules were applied. Refresh and check before retrying.",
      );
    } finally {
      copyTopicsBtn.style.opacity = "1";
    }
  });
}

const arModal = document.getElementById("autoreact-modal");
const arOpenButton = document.getElementById("btn-open-autoreact");
let arModalGeneration = 0;
let autoreactStateUnconfirmed = false;

function markAutoreactUnconfirmed(error) {
  autoreactStateUnconfirmed = true;
  const detail =
    error?.status === 409 && error.message ? `${error.message} ` : "";
  const message = `${detail}Could not confirm the current autoreaction settings. Reload and check before retrying.`;
  const indicator = document.getElementById("autoreact-indicator");
  indicator.className = "status-indicator unknown";
  const label = arOpenButton.querySelector(".label");
  if (!label.textContent.endsWith(" (unknown)"))
    label.textContent += " (unknown)";
  arOpenButton.title = message;
  arOpenButton.disabled = true;
  const saveButton = document.getElementById("save-ar-btn");
  saveButton.disabled = true;
  saveButton.textContent = "Reload to check";
  document.getElementById("autoreact-status").textContent = message;
  arModal.setAttribute("aria-busy", "false");
  showToast(message);
}

arOpenButton.addEventListener("click", () => {
  if (autoreactStateUnconfirmed) return;
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
  if (!arOpenButton.disabled) arOpenButton.focus();
  else
    [autoreadBtn, aiAutoreadBtn, markReadBtn]
      .find((btn) => btn && !btn.disabled)
      ?.focus();
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
  document.getElementById("autoreact-status").textContent = "";
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
      target_users: data.config.target_users ?? [],
    };

    if (
      generation !== arModalGeneration ||
      !arModal.open ||
      autoreactStateUnconfirmed
    )
      return;
    if (
      !Array.isArray(config.target_users) ||
      config.target_users.some((id) => !Number.isSafeInteger(id) || id === 0)
    ) {
      const message =
        "Autoreaction targets are invalid or cannot be represented exactly in this browser.";
      document.getElementById("autoreact-status").textContent = message;
      arModal.setAttribute("aria-busy", "false");
      showToast(message);
      return;
    }
    if (!isChannel && config.target_users.length > 0) await loadUsers();
    if (
      generation !== arModalGeneration ||
      !arModal.open ||
      autoreactStateUnconfirmed
    )
      return;

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
    if (autoreactStateUnconfirmed) return;
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
  const generation = ++usersLoadGeneration;
  const dd = document.getElementById("user-dropdown");
  try {
    const res = await requestOk(`/api/chat/${chatId}/authors`);
    const data = await res.json();
    if (generation !== usersLoadGeneration) return;
    if (
      !Array.isArray(data?.authors) ||
      data.authors.some(
        (author) =>
          !author ||
          typeof author !== "object" ||
          !Number.isSafeInteger(author.id) ||
          author.id === 0 ||
          typeof author.name !== "string" ||
          [author.username, author.avatar_url].some(
            (value) => value != null && typeof value !== "string",
          ),
      )
    )
      throw new Error("Invalid authors response");
    availableUsers = data.authors;
    renderUserDropdown();
    renderUserDisplay();
    usersLoaded = true;
  } catch (e) {
    if (generation !== usersLoadGeneration) return;
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
  if (autoreactStateUnconfirmed) return;
  const btn = document.getElementById("save-ar-btn");
  const restoreFocus = document.activeElement === btn;
  const generation = arModalGeneration;
  const savedConfig = JSON.stringify(arConfig);
  const savedEnabled = arConfig.enabled;
  btn.textContent = "Saving...";
  btn.disabled = true;
  try {
    const targets = isChannel ? [] : [...arConfig.target_users];

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

    if (
      generation !== arModalGeneration ||
      !arModal.open ||
      autoreactStateUnconfirmed
    ) {
      markAutoreactUnconfirmed();
      return;
    }
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
      if (
        generation !== arModalGeneration ||
        !arModal.open ||
        autoreactStateUnconfirmed
      )
        return;
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
    if (["invalid_request", "inherited_rule"].includes(e?.code)) {
      if (
        generation === arModalGeneration &&
        arModal.open &&
        !autoreactStateUnconfirmed
      ) {
        btn.textContent = "Save Configuration";
        btn.disabled = false;
        document.getElementById("autoreact-status").textContent = e.message;
        showToast(e.message);
        if (restoreFocus && document.activeElement === document.body)
          btn.focus();
      }
    } else {
      markAutoreactUnconfirmed(e);
      if (
        restoreFocus &&
        arModal.open &&
        document.activeElement === document.body
      )
        arModal.querySelector(".modal-close").focus();
    }
  }
});

const container = document.getElementById("messages-container");
const currentTopicId = topicId;
let isLoading = false;
let allLoaded = false;
let prefetchedHistory = null;
let eventVersion = 0;

function nearLatest() {
  return (
    document.documentElement.scrollHeight -
      window.innerHeight -
      window.scrollY <
    300
  );
}

function messageRow(root, id) {
  const row = root.querySelector(`[data-id="${id}"]`);
  return (
    row ||
    root.querySelector(`[data-message-id="${id}"]`)?.closest(".message-row") ||
    null
  );
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

function showAlbumChanged() {
  showToast("Album changed. Reload to see its latest contents.");
}

function historyUrl(offsetId) {
  let url = `/api/chat/${chatId}/history?offset_id=${offsetId}`;
  if (currentTopicId != null) url += `&topic_id=${currentTopicId}`;
  return url;
}

async function fetchHistory(offsetId) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 15000);
  try {
    const response = await requestOk(historyUrl(offsetId), {
      signal: controller.signal,
    });
    return await response.json();
  } finally {
    clearTimeout(timeout);
  }
}

const albumRefreshes = new Map();
const MAX_PENDING_ALBUM_REFRESHES = 32;
let albumRefreshTimer = null;
let albumRefreshRunning = false;

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
  if (!state) {
    if (albumRefreshes.size >= MAX_PENDING_ALBUM_REFRESHES) {
      showAlbumChanged();
      return true;
    }
    state = {
      groupId,
      version: 0,
      required: new Set(),
      deleted: new Set(),
      dirty: false,
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
  if (!albumRows(state.groupId).length) {
    albumRefreshes.delete(state.groupId);
    scheduleAlbumRefresh();
    return;
  }
  state.dirty = false;
  albumRefreshRunning = true;
  const version = state.version;
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 10000);
  const isCurrent = () =>
    albumRefreshes.get(state.groupId) === state && state.version === version;
  try {
    // The existing history endpoint uses an exclusive older-message offset.
    const newestId = Math.max(...Array.from(state.required, Number));
    const offset = newestId < 2 ** 31 - 1 ? newestId + 1 : 0;
    const response = await requestOk(historyUrl(offset), {
      signal: controller.signal,
    });
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
    window.parseAppleEmojis(replacement);
    window.renderLocalTimes(replacement);
    for (const row of rows) row.remove();
    const before = Array.from(
      container.querySelectorAll("[data-oldest-id]"),
    ).find((row) => Number(row.dataset.id) < Number(replacement.dataset.id));
    container.insertBefore(replacement, before || null);
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
  const promise = fetchHistory(offsetId).catch(() => null);
  prefetchedHistory = { offsetId, promise, events: eventVersion };
}

requestAnimationFrame(() => {
  window.scrollTo(0, document.documentElement.scrollHeight);
  prefetchOlderMessages();
  if (document.documentElement.scrollHeight <= window.innerHeight + 100)
    loadOlderMessages();
});
window.renderLocalTimes(container);

window.addEventListener("scroll", () => {
  if (isLoading || allLoaded) return;
  if (window.scrollY < 100) loadOlderMessages();
});

async function loadOlderMessages() {
  if (isLoading || allLoaded) return;
  isLoading = true;
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
      data = await fetchHistory(offsetId);
    }

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
      window.scrollBy(0, oldestMsg.getBoundingClientRect().top - previousTop);
      prefetchOlderMessages();
    }
  } catch (error) {
    console.error("Failed to load history:", error);
    indicator.textContent =
      "Could not load older messages. Scroll up to retry.";
    return;
  } finally {
    isLoading = false;
  }
  indicator.hidden = true;
  if (
    !allLoaded &&
    document.documentElement.scrollHeight <= window.innerHeight + 100
  ) {
    requestAnimationFrame(loadOlderMessages);
  }
}

const pendingReactions = new Set();
container.addEventListener("click", async (event) => {
  const button = event.target.closest(".reaction-pill, .hover-reaction");
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
    const detail = e?.status === 409 && e.message ? ` ${e.message}` : "";
    showToast(
      `Could not confirm whether the reaction was queued. Refresh and check before retrying.${detail}`,
    );
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
    showToast(
      data.text_plain || "Read was not confirmed. Refresh before retrying.",
    );
  if (data.type === "reaction_failed")
    showToast("Telegram could not apply the reaction");
  if (["reaction_uncertain", "reaction_blocked"].includes(data.type))
    showToast(data.text);
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
      [
        "deleted",
        "reaction_update",
        "reaction_failed",
        "reaction_uncertain",
        "reaction_blocked",
      ].includes(data.type)
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
      const alreadyPending = albumRefreshes
        .get(groupId)
        ?.required.has(String(msgId));
      if (!alreadyPending) queueAlbumRefresh(groupId, msgId, { added: true });
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
        showToast("A message changed. Reload to see its latest contents.");
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
