const chatConfig = window.TG_CHAT || {};
const chatId = chatConfig.chatId;
const topicId = chatConfig.topicId;
const isPremium = Boolean(chatConfig.isPremium);
const isChannel = Boolean(chatConfig.isChannel);

async function requestOk(url, options) {
  const response = await fetch(url, options);
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
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
        headers: { "Content-Type": "application/json" },
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
      showToast("Failed to toggle Autoread");
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
        headers: { "Content-Type": "application/json" },
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
      showToast("Failed to toggle AI Autoread");
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
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ topic_id: topicId }),
      });

      showToast("Read queued");
      window.location.href = topicId ? `/forum/${chatId}` : "/";
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
        headers: { "Content-Type": "application/json" },
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

arOpenButton.addEventListener("click", () => {
  arModal.hidden = false;
  arModal.classList.add("show");
  arModal.querySelector(".modal-close").focus();
  loadArSettings();
});

window.closeArModal = function () {
  arModal.classList.remove("show");
  arModal.hidden = true;
  document.getElementById("user-dropdown").classList.remove("show");
  arOpenButton.focus();
};

arModal.addEventListener("click", (e) => {
  if (e.target === arModal) closeArModal();
});
arModal.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeArModal();
});

const userSelectorWrapper = document.querySelector(".user-selector-wrapper");
if (userSelectorWrapper) {
  userSelectorWrapper.addEventListener("mouseleave", () => {
    const dd = document.getElementById("user-dropdown");
    if (dd && dd.classList.contains("show")) dd.classList.remove("show");
  });
}

async function loadArSettings() {
  const saveButton = document.getElementById("save-ar-btn");
  saveButton.disabled = true;
  try {
    let url = `/api/rules/autoreact/get?chat_id=${chatId}`;
    if (topicId) url += `&topic_id=${topicId}`;
    const res = await requestOk(url);
    const data = await res.json();

    arConfig = {
      enabled: Boolean(data.enabled),
      emoji: data.config.emoji || "💩",
      target_users: data.config.target_users || [],
    };

    if (!isChannel && arConfig.target_users.length > 0) await loadUsers();

    renderArUi();
    saveButton.disabled = false;
  } catch (e) {
    console.error("Failed to load AR settings", e);
    showToast("Failed to load Autoreact settings");
  }
}

function renderArUi() {
  document.getElementById("autoreact-checkbox").checked = arConfig.enabled;

  document.querySelectorAll(".emoji-opt").forEach((el) => {
    if (el.getAttribute("data-emoji") === arConfig.emoji) {
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
        avatarHtml = `<img src="${window.TG.escapeHtml(user.avatar_url)}" style="width:16px;height:16px;border-radius:50%;margin-right:5px;object-fit:cover;">`;
      } else {
        const initial = (user ? user.name : "?").charAt(0).toUpperCase();
        avatarHtml = `<div style="width:16px;height:16px;background:#555;border-radius:50%;margin-right:5px;display:flex;align-items:center;justify-content:center;font-size:10px;color:#fff;">${window.TG.escapeHtml(initial)}</div>`;
      }

      const chip = document.createElement("div");
      chip.className = "user-chip";
      chip.innerHTML = `${avatarHtml} ${window.TG.escapeHtml(name)} <span style="margin-left:5px; cursor:pointer;" onclick="event.stopPropagation(); removeUser(${uid})">&times;</span>`;
      display.appendChild(chip);
    });
  }
}

function renderUserDropdown() {
  const dd = document.getElementById("user-dropdown");
  dd.innerHTML = "";
  availableUsers.forEach((u) => {
    const isSelected = arConfig.target_users.includes(u.id);
    const item = document.createElement("div");
    item.className = "user-item " + (isSelected ? "active" : "");

    let avatar = "";
    if (u.avatar_url) {
      avatar = `<img src="${window.TG.escapeHtml(u.avatar_url)}" style="width:24px;height:24px;border-radius:50%;margin-right:10px;object-fit:cover;">`;
    } else {
      avatar = `<div style="width:24px;height:24px;background:#555;border-radius:50%;margin-right:10px;display:flex;align-items:center;justify-content:center;color:#fff;">${window.TG.escapeHtml(u.name.charAt(0).toUpperCase())}</div>`;
    }

    item.innerHTML = `${avatar} <div>${window.TG.escapeHtml(u.name)} <div style="font-size:0.75rem;color:#888;">${window.TG.escapeHtml(u.username || "")}</div></div>`;
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
  if (!dd.classList.contains("show")) loadUsers();
  dd.classList.toggle("show");
};

function toggleUser(uid) {
  if (arConfig.target_users.includes(uid)) {
    arConfig.target_users = arConfig.target_users.filter((id) => id !== uid);
  } else {
    arConfig.target_users.push(uid);
  }
  renderUserDisplay();
  renderUserDropdown();
}

window.removeUser = function (uid) {
  arConfig.target_users = arConfig.target_users.filter((id) => id !== uid);
  renderUserDisplay();
  renderUserDropdown();
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
    document
      .querySelectorAll(".emoji-opt")
      .forEach((opt) => opt.classList.remove("selected"));
  }
});

document
  .getElementById("autoreact-checkbox")
  .addEventListener("change", (e) => {
    arConfig.enabled = e.target.checked;
  });

document.getElementById("save-ar-btn").addEventListener("click", async () => {
  const btn = document.getElementById("save-ar-btn");
  btn.textContent = "Saving...";
  btn.disabled = true;
  try {
    const targets = isChannel ? [] : arConfig.target_users;

    await requestOk("/api/rules/autoreact/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
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

    const ind = document.getElementById("autoreact-indicator");
    if (arConfig.enabled) {
      ind.className =
        targets.length === 0
          ? "status-indicator status-all"
          : "status-indicator status-some";
    } else {
      ind.className = "status-indicator status-off";
    }

    btn.textContent = "Saved!";
    setTimeout(() => {
      btn.textContent = "Save Configuration";
      btn.disabled = false;
      closeArModal();
    }, 800);
    showToast("Autoreact Configured");
  } catch (e) {
    btn.textContent = "Error";
    btn.disabled = false;
  }
});

const container = document.getElementById("messages-container");
const currentTopicId = topicId;
let isLoading = false;
let allLoaded = false;
let prefetchedHistory = null;

function historyUrl(offsetId) {
  let url = `/api/chat/${chatId}/history?offset_id=${offsetId}`;
  if (currentTopicId) url += `&topic_id=${currentTopicId}`;
  return url;
}

function prefetchOlderMessages() {
  const messages = container.querySelectorAll("[data-oldest-id]");
  if (allLoaded || !messages.length) return;
  const offsetId = messages[messages.length - 1].dataset.oldestId;
  if (prefetchedHistory?.offsetId === offsetId) return;
  const promise = requestOk(historyUrl(offsetId))
    .then((response) => response.json())
    .catch(() => null);
  prefetchedHistory = { offsetId, promise };
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
  const indicator = document.getElementById("loading-indicator");
  indicator.textContent = "Loading older messages…";
  indicator.hidden = false;

  const messages = container.querySelectorAll("[data-oldest-id]");
  if (!messages.length) {
    allLoaded = true;
    isLoading = false;
    indicator.hidden = true;
    return;
  }

  const oldestMsg = messages[messages.length - 1];
  const offsetId = oldestMsg.dataset.oldestId;

  try {
    let data =
      prefetchedHistory?.offsetId === offsetId
        ? await prefetchedHistory.promise
        : null;
    prefetchedHistory = null;
    if (!data) {
      const response = await requestOk(historyUrl(offsetId));
      data = await response.json();
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
  const reactionContainer = button
    .closest(".message-bubble")
    .querySelector(".reactions-container");
  const previousHtml = reactionContainer.innerHTML;
  let pill = Array.from(
    reactionContainer.querySelectorAll(".reaction-pill"),
  ).find((el) => el.dataset.emoji === emoji);
  if (!pill) {
    pill = document.createElement("button");
    pill.type = "button";
    pill.className = "reaction-pill";
    pill.dataset.msgId = msgId;
    pill.dataset.emoji = emoji;
    pill.setAttribute("aria-label", `React with ${emoji}`);
    const icon = document.createElement("span");
    icon.textContent = emoji;
    const count = document.createElement("span");
    count.className = "reaction-count";
    count.textContent = "0";
    pill.append(icon, count);
    reactionContainer.appendChild(pill);
    window.parseAppleEmojis(pill);
  }
  const isChosen = pill.classList.contains("chosen");
  const countSpan = pill.querySelector(".reaction-count");
  const count = parseInt(countSpan.textContent, 10) || 0;

  if (!isPremium && !isChosen) {
    const siblings = reactionContainer.querySelectorAll(
      ".reaction-pill.chosen",
    );
    siblings.forEach((sib) => {
      if (sib !== pill) {
        sib.classList.remove("chosen");
        sib.setAttribute("aria-pressed", "false");
        const sibCountSpan = sib.querySelector(".reaction-count");
        const sibCount = parseInt(sibCountSpan.textContent, 10) || 0;
        if (sibCount > 0) sibCountSpan.textContent = sibCount - 1;
      }
    });
  }
  pill.classList.toggle("chosen", !isChosen);
  pill.setAttribute("aria-pressed", String(!isChosen));
  countSpan.textContent = Math.max(0, count + (isChosen ? -1 : 1));

  try {
    await requestOk(`/api/chat/${chatId}/message/${msgId}/reaction`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reaction: emoji.toString() }),
    });
  } catch (e) {
    reactionContainer.innerHTML = previousHtml;
    console.error("Failed to toggle reaction", e);
    showToast("Failed to queue reaction");
  } finally {
    pendingReactions.delete(msgId);
  }
});

window.debugProcessMessage = async function (el, msgId) {
  el.style.opacity = "0.5";
  try {
    const res = await fetch("/api/debug/process", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ chat_id: chatId, msg_id: msgId }),
    });
    const data = await res.json();
    let text = "Debug:\n";
    if (data.autoread)
      text += `Read: ${data.autoread.status} (${data.autoread.reason})\n`;
    if (data.autoreact)
      text += `React: ${data.autoreact.status} (${data.autoreact.detail})`;
    showToast(text);
  } catch (e) {
    console.error(e);
    showToast("Debug Request Failed");
  } finally {
    el.style.opacity = "1";
  }
};

function buildReactionsHtml(msgId, reactions) {
  let html = "";
  reactions.forEach((r) => {
    const chosenClass = r.is_chosen ? "chosen" : "";
    const emojiVal = r.custom_emoji_id || r.emoji;
    let innerContent = "";
    if (r.custom_emoji_id) {
      innerContent = `<img src="/media/custom_emoji/${r.custom_emoji_id}" style="width: 20px; height: 20px; margin-right: 4px;">`;
    } else {
      innerContent = `<span>${window.TG.escapeHtml(r.emoji)}\uFE0F</span>`;
    }
    html += `
        <button type="button" class="reaction-pill ${chosenClass}" data-msg-id="${msgId}" data-emoji="${window.TG.escapeHtml(emojiVal)}"
            aria-pressed="${Boolean(r.is_chosen)}" aria-label="React with ${window.TG.escapeHtml(r.emoji)}">
            ${innerContent}
            <span class="reaction-count">${r.count}</span>
        </button>`;
  });
  return html;
}

document.addEventListener("tg:event", (e) => {
  const data = e.detail;
  if (data.chat_id !== chatId) return;
  if (currentTopicId && data.topic_id !== currentTopicId) {
    if (data.topic_id) return;
  }

  if (data.type === "message" && data.rendered_html) {
    if (
      data.message_model &&
      document.getElementById(`msg-${data.message_model.id}`)
    )
      return;
    const nearBottom =
      document.documentElement.scrollHeight -
        window.innerHeight -
        window.scrollY <
      300;
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
    const msgRow = document.getElementById(`msg-${msgId}`);
    if (msgRow) {
      let textEl = msgRow.querySelector(".text");
      if (!textEl && data.message_model.text) {
        const bubble = msgRow.querySelector(".message-bubble");
        if (bubble) {
          textEl = document.createElement("div");
          textEl.className = "text";
          bubble.insertBefore(
            textEl,
            bubble.querySelector(".webpage-preview, .reactions-container"),
          );
        }
      }
      if (textEl) {
        textEl.innerHTML = data.message_model.text;
        window.parseAppleEmojis(textEl);
      }
    }
  } else if (data.type === "deleted" && data.message_model) {
    const msgId = data.message_model.id;
    const msgRow = document.getElementById(`msg-${msgId}`);
    if (msgRow) {
      msgRow.style.opacity = "0";
      setTimeout(() => msgRow.remove(), 300);
    }
  } else if (data.type === "reaction_update" && data.message_model) {
    const msgId = data.message_model.id;
    const reactionsContainer = document.getElementById(`reactions-${msgId}`);
    if (reactionsContainer) {
      reactionsContainer.innerHTML = buildReactionsHtml(
        msgId,
        data.message_model.reactions,
      );
      window.parseAppleEmojis(reactionsContainer);
    }
  }
});
