const dialogSessions = new WeakMap();
let settingsStateUnconfirmed = false;
const unconfirmedSettingMessage =
  "Could not confirm whether the setting was saved. Reload and check before changing it again.";

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
    if (!dialog && settingsStateUnconfirmed) return;
    if (!dialog || (dialog.open && dialogSessions.get(dialog) === session))
      status.textContent = message;
  };
  const reportUnconfirmed = () => {
    settingsStateUnconfirmed = true;
    document.getElementById("settings-status").textContent =
      unconfirmedSettingMessage;
    if (dialog) setStatus(unconfirmedSettingMessage);
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
    const isJson = res.headers
      .get("content-type")
      ?.includes("application/json");
    if (!res.ok || !isJson) {
      if (res.status === 400 && isJson) {
        const body = await res.json().catch(() => ({}));
        if (body.code === "invalid_request") {
          setStatus(body.error || "Invalid setting.");
          return false;
        }
      }
      reportUnconfirmed();
      return null;
    }
    if (!settingsStateUnconfirmed)
      document.getElementById("settings-status").textContent = "Setting saved.";
    if (dialog) setStatus("Setting saved.");
    return true;
  } catch (e) {
    console.error(e);
    reportUnconfirmed();
    return null;
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
  let unconfirmed = false;
  checkbox.disabled = true;
  try {
    const payload = {};
    const next = checkbox.checked;
    payload[key] = next;
    const result = await patchSetting(payload);
    unconfirmed = result === null;
    if (unconfirmed) checkbox.indeterminate = true;
    else if (!result && checkbox.checked === next) checkbox.checked = !next;
  } finally {
    checkbox.disabled = unconfirmed;
    if (
      !unconfirmed &&
      restoreFocus &&
      document.activeElement === document.body
    )
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
const rulesStatus = document.getElementById("rules-status");
const RULES_LOAD_TIMEOUT = 15_000;
const RULES_RETRY_INTERVAL = 5_000;
const RULES_RETRY_MAX_INTERVAL = 60_000;
let rulesLoadGeneration = 0;
let activeRulesLoad = null;
let rulesRetryTimer = null;
let rulesRetryDelay = RULES_RETRY_INTERVAL;
// Confirmed deletions stay unavailable until an authoritative list loads.
const ruleDeleteStates = new Map();

function ruleDeleteButton(ruleId) {
  const item = Array.from(
    rulesContainer.querySelectorAll("[data-rule-id]"),
  ).find((element) => element.dataset.ruleId === ruleId);
  return item?.querySelector(".rule-delete-btn");
}

function setRuleDeleteState(ruleId, state) {
  if (state) ruleDeleteStates.set(ruleId, state);
  else ruleDeleteStates.delete(ruleId);
  const button = ruleDeleteButton(ruleId);
  if (!button) return;
  // Keep the button focusable; deleteRule enforces the disabled state.
  if (state) button.setAttribute("aria-disabled", "true");
  else button.removeAttribute("aria-disabled");
  if (state === "pending") button.setAttribute("aria-busy", "true");
  else button.removeAttribute("aria-busy");
}

function buildRuleItemHTML(rule, chatId) {
  let detail = "";
  if (rule.rule_type === "autoreact") {
    const config = rule.config || {};
    const emoji = config.emoji || "💩";
    const targets = Array.isArray(config.target_users)
      ? config.target_users
      : [];
    const targetsHTML = targets.length
      ? targets
          .map(
            (uid) =>
              `<div class="user-chip-lazy" data-user-id="${window.TG.escapeHtml(uid)}"><div class="u-avatar">?</div><span class="u-name">${window.TG.escapeHtml(uid)}</span></div>`,
          )
          .join("")
      : '<span class="ar-default">All incoming eligible messages in this scope</span>';
    detail = `<div class="ar-display"><div class="ar-emoji">${window.TG.escapeHtml(emoji)}</div><div class="ar-targets">${targetsHTML}</div></div>`;
  } else if (rule.config && Object.keys(rule.config).length) {
    detail = `<span class="rule-config">${window.TG.escapeHtml(JSON.stringify(rule.config))}</span>`;
  }
  const scope =
    rule.topic_id == null
      ? `chat ${chatId}`
      : `chat ${chatId}, topic ${rule.topic_id}`;
  const deleteLabel = window.TG.escapeHtml(
    `Delete ${rule.rule_type} rule for ${scope}`,
  );
  const deleteState = ruleDeleteStates.get(String(rule.id));
  const deleteStateAttributes = deleteState
    ? ` aria-disabled="true"${deleteState === "pending" ? ' aria-busy="true"' : ""}`
    : "";
  const deleteBtn = rule.id
    ? `<button class="rule-delete-btn" onclick="deleteRule(${rule.id})" title="Delete rule" aria-label="${deleteLabel}"${deleteStateAttributes}>✕</button>`
    : "";
  return `
        <div class="rule-item" data-rule-id="${rule.id}">
            <span class="rule-type-badge badge-${window.TG.escapeHtml(rule.rule_type)}">${window.TG.escapeHtml(rule.rule_type)}</span>
            <div class="rule-detail">${detail}</div>
            ${deleteBtn}
        </div>`;
}

function buildGroupHTML(chatId, data) {
  const chatRulesHTML = data.chat_rules
    .map((rule) => buildRuleItemHTML(rule, chatId))
    .join("");
  const topicsHTML = Object.entries(data.topics)
    .map(
      ([topicId, rules]) => `
        <div class="topic-group">
            <div class="topic-header"><span style="margin-right:5px;">#</span> Topic ID: ${window.TG.escapeHtml(topicId)}</div>
            ${rules.map((rule) => buildRuleItemHTML(rule, chatId)).join("")}
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
  clearTimeout(rulesRetryTimer);
  rulesRetryTimer = null;
  const generation = ++rulesLoadGeneration;
  activeRulesLoad?.abort();
  const controller = new AbortController();
  activeRulesLoad = controller;
  const timeout = setTimeout(() => controller.abort(), RULES_LOAD_TIMEOUT);
  try {
    const res = await fetch("/api/rules", { signal: controller.signal });
    if (!res.ok) {
      const error = new Error(`HTTP ${res.status}`);
      if (res.status === 503) {
        const body = await res.json().catch(() => ({}));
        if (Number.isSafeInteger(body.rule_id) && body.rule_id > 0)
          error.ruleId = body.rule_id;
      }
      throw error;
    }
    const grouped = await res.json();
    if (generation !== rulesLoadGeneration) return;
    if (!grouped || typeof grouped !== "object" || Array.isArray(grouped))
      throw new Error("Invalid rules response");
    const active = document.activeElement;
    const restoreRulesFocus =
      active !== rulesContainer && rulesContainer.contains(active);
    const keys = Object.keys(grouped);
    if (!keys.length) {
      rulesContainer.innerHTML =
        '<div style="text-align:center;color:var(--text-muted);padding:30px;">No specific rules configured.</div>';
    } else {
      rulesContainer.innerHTML = keys
        .map((cid) => buildGroupHTML(cid, grouped[cid]))
        .join("");
      enrichRuleCards();
    }
    for (const [id, state] of ruleDeleteStates) {
      if (state === "confirmed") setRuleDeleteState(id, null);
    }
    rulesStatus.textContent = "";
    rulesRetryDelay = RULES_RETRY_INTERVAL;
    if (restoreRulesFocus) rulesContainer.focus({ preventScroll: true });
  } catch (e) {
    if (generation !== rulesLoadGeneration) return;
    console.error("Failed to load rules", e);
    rulesStatus.textContent = e.ruleId
      ? `Stored rule ${e.ruleId} has invalid configuration. Displayed rules may be out of date.`
      : "Could not load rules. Displayed rules may be out of date. Retrying automatically.";
    rulesRetryTimer = setTimeout(() => void loadRules(), rulesRetryDelay);
    rulesRetryDelay = Math.min(rulesRetryDelay * 2, RULES_RETRY_MAX_INTERVAL);
  } finally {
    clearTimeout(timeout);
    if (generation === rulesLoadGeneration) activeRulesLoad = null;
  }
}

let enrichmentGeneration = 0;
let activeEnrichment = null;
const ENRICHMENT_REQUEST_TIMEOUT = 15_000;

async function enrichRuleCards() {
  const generation = ++enrichmentGeneration;
  if (activeEnrichment) {
    activeEnrichment.controller.abort();
    await activeEnrichment.done;
  }
  if (generation !== enrichmentGeneration) return;
  const controller = new AbortController();
  const targets = new Map();
  const add = (id, element, kind) => {
    // Older stored rule configs may contain values that are not Telegram IDs.
    if (!/^-?[1-9]\d{0,18}$/.test(id || "")) return;
    if (!targets.has(id)) targets.set(id, []);
    targets.get(id).push({ element, kind });
  };
  document.querySelectorAll(".rule-group-card").forEach((card) => {
    add(card.getAttribute("data-chat-id"), card, "chat");
  });
  document.querySelectorAll(".user-chip-lazy").forEach((chip) => {
    add(chip.getAttribute("data-user-id"), chip, "user");
  });

  const jobs = Array.from(targets);
  let next = 0;
  async function worker() {
    while (!controller.signal.aborted && next < jobs.length) {
      const [id, elements] = jobs[next++];
      const requestController = new AbortController();
      const abort = () => requestController.abort();
      controller.signal.addEventListener("abort", abort, { once: true });
      const timeout = setTimeout(abort, ENRICHMENT_REQUEST_TIMEOUT);
      try {
        const res = await fetch(`/api/chat/${id}/info`, {
          signal: requestController.signal,
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        if (typeof data.name !== "string") throw new Error("Invalid chat info");
        if (controller.signal.aborted) return;
        const avatar = data.avatar_url
          ? `<img src="${window.TG.escapeHtml(data.avatar_url)}" style="width:100%;height:100%;border-radius:50%;object-fit:cover;">`
          : window.TG.escapeHtml(data.name.charAt(0).toUpperCase());
        for (const { element, kind } of elements) {
          element.querySelector(
            kind === "chat" ? ".rg-name" : ".u-name",
          ).textContent = data.name;
          element.querySelector(
            kind === "chat" ? ".rg-avatar" : ".u-avatar",
          ).innerHTML = avatar;
          if (kind === "chat") {
            const type = element.querySelector(".rg-type");
            type.textContent = data.type;
            type.classList.remove("rg-loading");
          } else {
            element.title = `@${data.username || "unknown"}`;
          }
        }
      } catch (_) {
        if (controller.signal.aborted) return;
        for (const { element, kind } of elements) {
          if (kind === "chat") {
            const type = element.querySelector(".rg-type");
            type.textContent = "Info unavailable";
            type.classList.remove("rg-loading");
          }
        }
      } finally {
        clearTimeout(timeout);
        controller.signal.removeEventListener("abort", abort);
      }
    }
  }
  // shortcut: four concurrent lookups; revisit after measuring account RPC load.
  const done = Promise.all(
    Array.from({ length: Math.min(4, jobs.length) }, worker),
  );
  activeEnrichment = { controller, done };
  try {
    await done;
  } finally {
    if (activeEnrichment?.done === done) activeEnrichment = null;
  }
}

window.deleteRule = async function (ruleId) {
  const id = String(ruleId);
  if (ruleDeleteStates.has(id)) return;
  if (!confirm("Delete this rule?")) return;
  setRuleDeleteState(id, "pending");
  try {
    const res = await fetch(`/api/rules/${ruleId}`, {
      method: "DELETE",
      headers: { "X-Requested-With": "XMLHttpRequest" },
    });
    if (res.ok) {
      setRuleDeleteState(id, "confirmed");
      const item = ruleDeleteButton(id)?.closest(".rule-item");
      if (item) {
        item.style.opacity = "0.4";
        item.style.pointerEvents = "none";
      }
      await loadRules();
    } else {
      alert(
        "Could not confirm whether the rule was deleted. Reload the page and check before retrying.",
      );
    }
  } catch (e) {
    alert(
      "Could not confirm whether the rule was deleted. Reload the page and check before retrying.",
    );
  } finally {
    if (ruleDeleteStates.get(id) === "pending") setRuleDeleteState(id, null);
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
        alert(
          "Could not confirm whether the account was forgotten. Refresh and check before retrying.",
        );
        resetBtn.textContent = "Forget local account";
        resetBtn.disabled = false;
      }
    } catch (err) {
      alert(
        "Could not confirm whether the account was forgotten. Refresh and check before retrying.",
      );
      resetBtn.textContent = "Forget local account";
      resetBtn.disabled = false;
    }
  });
}

document.addEventListener("DOMContentLoaded", () => {
  loadRules();
});
