import { $, button, key, node, showMenu } from "./stickers-controls.js";
import { createStickerMedia } from "./stickers-media.js";

export function createStickerGrid(actions) {
  const grid = $("grid");
  const preview = $("preview");
  const media = createStickerMedia(preview, $("preview-art"));
  let tiles = [];
  let selected = new Set();
  let focused = 0;
  let previewIndex = 0;
  let gesture = null;

  function render({ items, results, message, emptyMessage, moveHandle }) {
    if (preview.open) preview.close();
    media.resetGrid();
    tiles = [];

    function tile(item, showPack) {
      const index = tiles.length;
      const card = node("div", null, "st-tile");
      const open = button(
        null,
        (event) => {
          if (suppressClick) return;
          if (event.shiftKey || event.ctrlKey || event.metaKey) toggle(item);
          else openPreview(index);
        },
        "st-tile-open",
      );
      open.tabIndex = index === focused ? 0 : -1;
      open.id = `st-tile-${index}`;
      open.dataset.art = key(item);
      open.dataset.index = index;
      open.setAttribute(
        "aria-label",
        `Sticker ${index + 1}${item.emoji ? ` ${item.emoji}` : ""}${showPack ? ` from ${item.pack}` : ""}`,
      );
      open.setAttribute("aria-pressed", String(selected.has(key(item))));
      open.append(
        node("small", item.thumb ? item.emoji || "Still" : "No Telegram still"),
      );
      open.addEventListener("focus", () => focusTile(index, false));
      open.addEventListener("contextmenu", (event) => {
        event.preventDefault();
        stickerMenu(item, open, event);
      });
      card.dataset.sticker = item.id;
      card.append(open);
      if (moveHandle) card.append(moveHandle(index));
      if (showPack) card.append(node("small", item.pack));
      tiles.push({ item, card, open });
      return card;
    }
    if (results) {
      const groups = (entries) =>
        entries.map((entry) => {
          const row = node("div", null, "st-match");
          row.append(...entry.map((item) => tile(item, true)));
          return row;
        });
      grid.replaceChildren(
        node("h3", "Exact matches", "st-result-heading"),
        ...(results.exact.length
          ? groups(results.exact)
          : [node("p", "No verified exact matches.", "st-result-note")]),
        node("h3", "Lookalikes", "st-result-heading"),
        node(
          "p",
          "Suggestions only. Small text edits can look alike; crops and recolors can be missed.",
          "st-result-note",
        ),
        ...(results.lookalikes.length
          ? groups(results.lookalikes.map(({ items: pair }) => pair))
          : [node("p", "No close thumbnail matches.", "st-result-note")]),
      );
    } else {
      grid.replaceChildren(...items.map((item) => tile(item, false)));
      if (!items.length && (message || emptyMessage))
        grid.append(node("p", message || emptyMessage));
    }
    for (const { item, open } of tiles) media.watchThumbnail(open, item.thumb);
    if (!message) {
      const visible = new Set(tiles.map(({ item }) => key(item)));
      for (const id of selected) if (!visible.has(id)) selected.delete(id);
    }
    focusTile(Math.min(focused, tiles.length - 1), false);
    updateSelection();
  }

  function focusTile(index, moveFocus) {
    if (!tiles[index]) return;
    if (tiles[focused]) tiles[focused].open.tabIndex = -1;
    focused = index;
    tiles[index].open.tabIndex = 0;
    if (moveFocus) tiles[index].open.focus();
  }

  function selection() {
    return [
      ...new Map(
        tiles
          .filter(({ item }) => selected.has(key(item)))
          .map(({ item }) => [key(item), item]),
      ).values(),
    ];
  }

  function updateSelection(showBar = true) {
    for (const { open, item } of tiles)
      open.setAttribute("aria-pressed", String(selected.has(key(item))));
    if (!showBar) return;
    const batch = selection();
    const bar = $("selection");
    bar.hidden = !batch.length;
    if (batch.length)
      bar.replaceChildren(
        node("span", `${batch.length} selected`),
        ...actions(batch).map(([label, run, classes]) =>
          button(label, run, classes),
        ),
        button("Clear", () => {
          selected.clear();
          updateSelection();
          focusTile(focused, true);
        }),
      );
    if (preview.open) updatePreviewSelection();
  }

  function toggle(item) {
    const id = key(item);
    if (selected.has(id)) selected.delete(id);
    else selected.add(id);
    updateSelection();
  }

  function stickerMenu(item, owner, point) {
    if (!selected.has(key(item))) {
      selected = new Set([key(item)]);
      updateSelection();
    }
    showMenu(actions(selection(), true), owner, point);
  }

  grid.addEventListener("keydown", (event) => {
    const index = Number(event.target.dataset.index);
    if (!tiles[index]) return;
    const item = tiles[index].item;
    if (event.key === " ") {
      event.preventDefault();
      toggle(item);
    }
    if (
      event.key === "ContextMenu" ||
      (event.shiftKey && event.key === "F10")
    ) {
      event.preventDefault();
      stickerMenu(item, event.target);
    }
    if (event.key === "Escape" && selected.size) {
      selected.clear();
      updateSelection();
    }
    const columns = getComputedStyle(
      tiles[index].card.parentElement,
    ).gridTemplateColumns.split(" ").length;
    const next = {
      ArrowLeft: index - 1,
      ArrowRight: index + 1,
      ArrowUp: index - columns,
      ArrowDown: index + columns,
      Home: 0,
      End: tiles.length - 1,
    }[event.key];
    if (next !== undefined) {
      event.preventDefault();
      focusTile(Math.max(0, Math.min(tiles.length - 1, next)), true);
    }
  });

  let suppressClick = false;
  grid.addEventListener("pointerdown", (event) => {
    if (
      event.pointerType !== "mouse" ||
      event.button !== 0 ||
      event.target.closest(".st-handle") ||
      !tiles.length
    )
      return;
    gesture = {
      pointer: event.pointerId,
      x: event.clientX,
      y: event.clientY,
      seed: event.shiftKey ? new Set(selected) : new Set(),
    };
  });
  window.addEventListener("pointermove", (event) => {
    if (!gesture || event.pointerId !== gesture.pointer) return;
    const { x, y, seed } = gesture;
    if (!gesture.active && Math.hypot(event.clientX - x, event.clientY - y) < 6)
      return;
    if (!gesture.active) grid.setPointerCapture(event.pointerId);
    gesture.active = true;
    const left = Math.min(x, event.clientX),
      right = Math.max(x, event.clientX);
    const top = Math.min(y, event.clientY),
      bottom = Math.max(y, event.clientY);
    Object.assign($("marquee").style, {
      left: `${left}px`,
      top: `${top}px`,
      width: `${right - left}px`,
      height: `${bottom - top}px`,
    });
    $("marquee").hidden = false;
    const next = new Set(seed);
    for (const { item, open } of tiles) {
      const r = open.getBoundingClientRect();
      if (left < r.right && right > r.left && top < r.bottom && bottom > r.top)
        next.add(key(item));
    }
    if (
      next.size !== selected.size ||
      [...next].some((id) => !selected.has(id))
    ) {
      selected = next;
      updateSelection(false);
    }
  });
  function stopRectangle(event) {
    if (!gesture || event.pointerId !== gesture.pointer) return;
    if (gesture.active) {
      suppressClick = true;
      setTimeout(() => {
        suppressClick = false;
      }, 0);
      updateSelection();
    }
    gesture = null;
    $("marquee").hidden = true;
  }
  window.addEventListener("pointerup", stopRectangle);
  window.addEventListener("pointercancel", stopRectangle);

  function openPreview(index) {
    if (!preview.open) preview.showModal();
    paintPreview(index);
  }

  function paintPreview(index) {
    if (!tiles[index]) return;
    previewIndex = index;
    const { item, open } = tiles[index];
    $("preview-label").textContent =
      `${index + 1} / ${tiles.length}${item.emoji ? ` · ${item.emoji}` : ""}${item.pack ? ` · ${item.pack}` : ""}`;
    $("prev").disabled = index === 0;
    $("next").disabled = index === tiles.length - 1;
    updatePreviewSelection();
    media.showPreview(item, open);
  }

  function updatePreviewSelection() {
    const has = selected.has(key(tiles[previewIndex].item));
    $("preview-select").textContent = has ? "Deselect" : "Select";
    $("preview-select").setAttribute("aria-pressed", String(has));
  }

  $("preview-close").onclick = () => preview.close();
  $("preview-select").onclick = () => toggle(tiles[previewIndex].item);
  $("prev").onclick = () => paintPreview(previewIndex - 1);
  $("next").onclick = () => paintPreview(previewIndex + 1);
  preview.addEventListener("close", () => {
    if (!preview.open) focusTile(previewIndex, true);
  });
  preview.addEventListener("keydown", (event) => {
    if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
      event.preventDefault();
      paintPreview(previewIndex + (event.key === "ArrowLeft" ? -1 : 1));
    }
  });

  function resetSelection() {
    selected.clear();
    focused = 0;
  }

  return { render, resetSelection };
}
