const dialogSessions = new WeakMap();

async function patchSetting(payload) {
  const dialog = document.querySelector("dialog.modal-overlay[open]");
  const session = dialog && dialogSessions.get(dialog);
  const status =
    dialog?.querySelector(".modal-status") ||
    document.getElementById("settings-status");
  const saveButtons = dialog
    ? Array.from(dialog.querySelectorAll(".btn-save"))
    : [];
  const focusedSave = saveButtons.find(
    (button) => button === document.activeElement,
  );
  const setStatus = (message) => {
    if (!dialog || (dialog.open && dialogSessions.get(dialog) === session))
      status.textContent = message;
  };
  if (saveButtons.some((button) => button.disabled)) return false;
  saveButtons.forEach((button) => (button.disabled = true));
  setStatus("Saving…");
  try {
    const res = await fetch("/api/settings", {
      method: "PATCH",
      headers: {
        "Content-Type": "application/json",
        "X-Requested-With": "XMLHttpRequest",
      },
      body: JSON.stringify(payload),
    });
    if (
      !res.ok ||
      !res.headers.get("content-type")?.includes("application/json")
    ) {
      setStatus("Could not save setting. Try again.");
      return false;
    }
    document.getElementById("settings-status").textContent = "Setting saved.";
    setStatus("Setting saved.");
    return true;
  } catch (e) {
    console.error(e);
    setStatus("Network error. Could not save setting.");
    return false;
  } finally {
    saveButtons.forEach((button) => (button.disabled = false));
    if (
      focusedSave &&
      dialog.open &&
      dialogSessions.get(dialog) === session &&
      document.activeElement === document.body
    )
      focusedSave.focus();
  }
}

window.toggleSetting = async function (key) {
  const idMap = {
    autoread_service_messages: "t-service",
    autoread_polls: "t-polls",
    autoread_self: "t-self",
    debug_mode: "t-debug",
  };
  const checkbox = document.getElementById(idMap[key] || key);
  const restoreFocus = document.activeElement === checkbox;
  checkbox.disabled = true;
  try {
    const payload = {};
    const next = checkbox.checked;
    payload[key] = next;
    if (!(await patchSetting(payload)) && checkbox.checked === next)
      checkbox.checked = !next;
  } finally {
    checkbox.disabled = false;
    if (restoreFocus && document.activeElement === document.body)
      checkbox.focus();
  }
};

window.openModal = function (id) {
  const dialog = document.getElementById(id);
  dialogSessions.set(dialog, {});
  dialog.querySelector(".modal-status").textContent = "";
  dialog.showModal();
  dialog.classList.add("show");
  dialog.querySelector("input, textarea")?.focus();
};

window.closeModal = function (id) {
  const dialog = document.getElementById(id);
  if (dialog.open) dialog.close();
};

function finishDialogSave(dialog, session, input, value, clear = false) {
  if (!dialog.open || dialogSessions.get(dialog) !== session) return;
  if (input.value !== value) {
    dialog.querySelector(".modal-status").textContent =
      "Previous value saved. Save your newer changes.";
    return;
  }
  if (clear) input.value = "";
  closeModal(dialog.id);
}

window.saveField = async function (key, inputId) {
  const input = document.getElementById(inputId);
  const val = input.value;
  const dialog = document.getElementById(
    inputId === "input-bots" ? "modal-bots" : "modal-regex",
  );
  const session = dialogSessions.get(dialog);
  const payload = {};
  payload[key] = val;
  if (!(await patchSetting(payload))) return;
  if (key === "autoread_bots") renderBotTags(val);
  if (key === "autoread_regex") renderRegexDisplay(val);
  finishDialogSave(dialog, session, input, val);
};

function renderBotTags(bots) {
  const display = document.querySelector(
    '.gs-display-card[data-tooltip*="usernames"] .gs-display-content',
  );
  if (!display) return;
  if (!bots || !bots.trim()) {
    display.innerHTML = '<span class="gs-none">None configured</span>';
    display.closest(".gs-display-card").classList.remove("has-content");
    return;
  }
  const tags = bots
    .split(",")
    .filter((b) => b.trim())
    .map(
      (b) => `<span class="bot-tag">${window.TG.escapeHtml(b.trim())}</span>`,
    )
    .join("");
  display.innerHTML = `<div class="bot-tags">${tags}</div>`;
  display.closest(".gs-display-card").classList.add("has-content");
}

function renderRegexDisplay(regex) {
  const display = document.querySelector(
    '.gs-display-card[data-tooltip*="RegEx"] .gs-display-content',
  );
  if (!display) return;
  if (!regex || !regex.trim()) {
    display.innerHTML = '<span class="gs-none">None configured</span>';
    display.closest(".gs-display-card").classList.remove("has-content");
    return;
  }
  display.innerHTML = `<code class="regex-block">${window.TG.escapeHtml(regex)}</code>`;
  display.closest(".gs-display-card").classList.add("has-content");
}

window.saveAiModel = async function () {
  const input = document.getElementById("input-ai-model");
  const val = input.value;
  const dialog = document.getElementById("modal-ai-model");
  const session = dialogSessions.get(dialog);
  if (!(await patchSetting({ ai_model: val }))) return;
  renderAiModelDisplay(val);
  finishDialogSave(dialog, session, input, val);
};

window.saveAiKey = async function () {
  const input = document.getElementById("input-ai-key");
  const val = input.value;
  const dialog = document.getElementById("modal-ai-key");
  const session = dialogSessions.get(dialog);
  if (!val) {
    closeModal("modal-ai-key");
    return;
  }
  if (!(await patchSetting({ ai_api_key: val }))) return;
  const card = document.getElementById("card-ai-key");
  const display = card.querySelector(".gs-display-content");
  display.innerHTML = "<span>••••••••</span>";
  card.classList.add("has-content");
  finishDialogSave(dialog, session, input, val, true);
};

window.saveAiPrompt = async function () {
  const input = document.getElementById("input-ai-prompt");
  const val = input.value;
  const dialog = document.getElementById("modal-ai-prompt");
  const session = dialogSessions.get(dialog);
  if (!(await patchSetting({ ai_prompt: val }))) return;
  renderAiPromptDisplay(val);
  finishDialogSave(dialog, session, input, val);
};

function renderAiModelDisplay(val) {
  const card = document.getElementById("card-ai-model");
  if (!card) return;
  const display = card.querySelector(".gs-display-content");
  if (!val || !val.trim()) {
    display.innerHTML = '<span class="gs-none">Not configured</span>';
    card.classList.remove("has-content");
  } else {
    display.innerHTML = `<code class="regex-block">${window.TG.escapeHtml(val)}</code>`;
    card.classList.add("has-content");
  }
}

function renderAiPromptDisplay(val) {
  const card = document.getElementById("card-ai-prompt");
  if (!card) return;
  const display = card.querySelector(".gs-display-content");
  if (!val || !val.trim()) {
    display.innerHTML = '<span class="gs-none">Not configured</span>';
    card.classList.remove("has-content");
  } else {
    const preview = val.length > 100 ? val.slice(0, 100) + "…" : val;
    display.innerHTML = `<span>${window.TG.escapeHtml(preview)}</span>`;
    card.classList.add("has-content");
  }
}

document.querySelectorAll(".modal-overlay").forEach((overlay) => {
  overlay.addEventListener("close", () => overlay.classList.remove("show"));
  overlay.addEventListener("click", (e) => {
    if (e.target === overlay) closeModal(overlay.id);
  });
});

const rulesContainer = document.getElementById("rules-container");

function buildRuleItemHTML(rule) {
  let detail = "";
  if (rule.rule_type === "autoreact") {
    const emoji = rule.config.emoji || "💩";
    const targets = rule.config.target_users || [];
    const targetsHTML = targets.length
      ? targets
          .map(
            (uid) =>
              `<div class="user-chip-lazy" data-user-id="${uid}"><div class="u-avatar">?</div><span class="u-name">${uid}</span></div>`,
          )
          .join("")
      : '<span class="ar-default">All incoming eligible messages in this scope</span>';
    detail = `<div class="ar-display"><div class="ar-emoji">${window.TG.escapeHtml(emoji)}</div><div class="ar-targets">${targetsHTML}</div></div>`;
  } else if (rule.config && Object.keys(rule.config).length) {
    detail = `<span class="rule-config">${window.TG.escapeHtml(JSON.stringify(rule.config))}</span>`;
  }
  const deleteBtn = rule.id
    ? `<button class="rule-delete-btn" onclick="deleteRule(${rule.id})" title="Delete rule">✕</button>`
    : "";
  return `
        <div class="rule-item" data-rule-id="${rule.id}">
            <span class="rule-type-badge badge-${window.TG.escapeHtml(rule.rule_type)}">${window.TG.escapeHtml(rule.rule_type)}</span>
            <div class="rule-detail">${detail}</div>
            ${deleteBtn}
        </div>`;
}

function buildGroupHTML(chatId, data) {
  const chatRulesHTML = data.chat_rules.map(buildRuleItemHTML).join("");
  const topicsHTML = Object.entries(data.topics)
    .map(
      ([topicId, rules]) => `
        <div class="topic-group">
            <div class="topic-header"><span style="margin-right:5px;">#</span> Topic ID: ${window.TG.escapeHtml(topicId)}</div>
            ${rules.map(buildRuleItemHTML).join("")}
        </div>`,
    )
    .join("");

  return `
        <div class="rule-group-card" data-chat-id="${window.TG.escapeHtml(chatId)}">
            <a href="/chat/${window.TG.escapeHtml(chatId)}" class="rg-header" title="Open Chat">
                <div class="rg-avatar">?</div>
                <div class="rg-info">
                    <div class="rg-name">Chat ID: ${window.TG.escapeHtml(chatId)}</div>
                    <div class="rg-type rg-loading">Loading info...</div>
                </div>
                <div style="font-size:0.8rem;color:#555;">ID: ${window.TG.escapeHtml(chatId)}</div>
            </a>
            ${chatRulesHTML ? `<div class="rule-list">${chatRulesHTML}</div>` : ""}
            ${topicsHTML ? `<div class="rule-list" style="padding-top:0;">${topicsHTML}</div>` : ""}
        </div>`;
}

async function loadRules() {
  try {
    const res = await fetch("/api/rules");
    if (!res.ok) return;
    const grouped = await res.json();
    const keys = Object.keys(grouped);
    if (!keys.length) {
      rulesContainer.innerHTML =
        '<div style="text-align:center;color:#777;padding:30px;">No specific rules configured.</div>';
      return;
    }
    rulesContainer.innerHTML = keys
      .map((cid) => buildGroupHTML(cid, grouped[cid]))
      .join("");
    enrichRuleCards();
  } catch (e) {
    console.error("Failed to load rules", e);
  }
}

async function enrichRuleCards() {
  document.querySelectorAll(".rule-group-card").forEach(async (card) => {
    const chatId = card.getAttribute("data-chat-id");
    if (!chatId) return;
    try {
      const res = await fetch(`/api/chat/${chatId}/info`);
      if (!res.ok) return;
      const data = await res.json();
      card.querySelector(".rg-name").textContent = data.name;
      const typeEl = card.querySelector(".rg-type");
      typeEl.textContent = data.type;
      typeEl.classList.remove("rg-loading");
      const avatarEl = card.querySelector(".rg-avatar");
      avatarEl.innerHTML = data.avatar_url
        ? `<img src="${window.TG.escapeHtml(data.avatar_url)}" style="width:100%;height:100%;border-radius:50%;object-fit:cover;">`
        : window.TG.escapeHtml(data.name.charAt(0).toUpperCase());
    } catch (e) {}
  });

  document.querySelectorAll(".user-chip-lazy").forEach(async (chip) => {
    const uid = chip.getAttribute("data-user-id");
    if (!uid) return;
    try {
      const res = await fetch(`/api/chat/${uid}/info`);
      if (!res.ok) return;
      const data = await res.json();
      chip.querySelector(".u-name").textContent = data.name;
      chip.querySelector(".u-avatar").innerHTML = data.avatar_url
        ? `<img src="${window.TG.escapeHtml(data.avatar_url)}" style="width:100%;height:100%;border-radius:50%;object-fit:cover;">`
        : window.TG.escapeHtml(data.name.charAt(0).toUpperCase());
      chip.title = `@${data.username || "unknown"}`;
    } catch (e) {}
  });
}

window.deleteRule = async function (ruleId) {
  if (!confirm("Delete this rule?")) return;
  try {
    const res = await fetch(`/api/rules/${ruleId}`, {
      method: "DELETE",
      headers: { "X-Requested-With": "XMLHttpRequest" },
    });
    if (res.ok) {
      const item = document.querySelector(`[data-rule-id="${ruleId}"]`);
      if (item) {
        item.style.opacity = "0.4";
        item.style.pointerEvents = "none";
      }
      await loadRules();
    } else {
      alert("Failed to delete rule.");
    }
  } catch (e) {
    alert("Network Error");
  }
};

const resetBtn = document.getElementById("reset-btn");
if (resetBtn) {
  resetBtn.addEventListener("click", async () => {
    if (
      !confirm(
        "Forget this account and its rules on this manager? This does not revoke the Telegram session. Existing action and debug logs are retained until their normal expiry.",
      )
    )
      return;
    resetBtn.textContent = "Resetting...";
    resetBtn.disabled = true;
    try {
      const res = await fetch("/api/settings/reset", {
        method: "POST",
        headers: { "X-Requested-With": "XMLHttpRequest" },
      });
      if (res.ok) {
        window.location.href = "/login";
      } else {
        alert("Failed to reset account.");
        resetBtn.textContent = "Forget local account";
        resetBtn.disabled = false;
      }
    } catch (err) {
      alert("Network Error");
      resetBtn.textContent = "Forget local account";
      resetBtn.disabled = false;
    }
  });
}

document.addEventListener("DOMContentLoaded", () => {
  loadRules();
});
