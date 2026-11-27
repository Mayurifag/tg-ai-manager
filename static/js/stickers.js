import { json } from "./stickers-api.js";
import {
  $,
  button,
  closeMenu,
  confirmAction,
  error,
  key,
  node,
  openDialog,
  reorderHandle,
  showMenu,
} from "./stickers-controls.js";
import { createStickerGrid } from "./stickers-grid.js";
import { createStickerJobs } from "./stickers-jobs.js";
import { createStickerMedia } from "./stickers-media.js";

const dialog = $("dialog");
const media = createStickerMedia(
  $("preview"),
  $("preview-art"),
  $("animations"),
);
const grid = createStickerGrid(stickerActions, media);
const jobs = createStickerJobs({
  refresh: refreshAfterWrite,
  onRemoved: removeScanMatches,
  showResults: showScanResults,
});
const labels = { favorites: "Favorites", recent: "Recent" };
const sizeFormat = new Intl.NumberFormat(undefined, {
  maximumFractionDigits: 2,
});
const listView = () => view in labels;
const pack = (id = source) => packs.find((item) => item.id === id);
let packs = [],
  items = null,
  view = "installed",
  source = null,
  results = null;
let checkedPacks = new Set();
let itemLoad = null;
let packsCollapsed = false;
let packRows = [];
const noMatchingPacks = node("p", "No matching packs.");

function filterPacks() {
  const libraryVisible = !$("library").hidden;
  const query = $("search").value.trim().toLocaleLowerCase();
  const hideReorder = view !== "installed" || Boolean(query);
  const keep = new Set();
  for (const { item, name, row, cover, reorder } of packRows) {
    const hidden = !item[view] || !name.includes(query);
    if (row.hidden !== hidden) row.hidden = hidden;
    if (reorder && reorder.hidden !== hideReorder) reorder.hidden = hideReorder;
    if (!hidden && libraryVisible) {
      keep.add(item.id);
      if (item.count && !cover.dataset.art && !cover.firstElementChild)
        media.watchThumbnail(cover, item.id);
    } else if (cover.dataset.art) media.unwatchThumbnail(cover);
  }
  noMatchingPacks.hidden = keep.size > 0;
  media.retainCovers(keep);
}

function packActions() {
  const chosen = packs.filter((item) => checkedPacks.has(item.id));
  const bar = $("pack-actions");
  bar.hidden = chosen.length === 0;
  bar.replaceChildren(
    node("small", `${chosen.length} packs`),
    button("⋯", (event) => {
      const menu = [
        ["Find duplicates", () => jobs.scan(chosen.map(({ id }) => id))],
      ];
      if (view === "installed")
        menu.unshift([`Uninstall (${chosen.length})`, () => uninstall(chosen)]);
      showMenu(menu, event.currentTarget);
    }),
  );
  bar.lastElementChild.setAttribute("aria-label", "Selected pack actions");
}

function updatePackVisibility() {
  const toggle = $("packs-toggle");
  $("library").hidden = listView() || packsCollapsed;
  toggle.hidden = listView();
  toggle.setAttribute("aria-expanded", String(!packsCollapsed));
  toggle.textContent = packsCollapsed ? "Show packs" : "Hide packs";
}

function packRow(item, order, index, cover) {
  const row = node("div", null, "st-pack");
  row.dataset.pack = item.id;
  const check = node("input");
  check.type = "checkbox";
  check.checked = checkedPacks.has(item.id);
  check.setAttribute("aria-label", `Select pack ${item.name}`);
  check.addEventListener("change", () => {
    if (check.checked) checkedPacks.add(item.id);
    else checkedPacks.delete(item.id);
    packActions();
  });
  const open = button(null, () => void openSource(item.id), "st-pack-open");
  cover ??= node("span", null, "st-pack-preview");
  cover.setAttribute("aria-hidden", "true");
  const text = node("span");
  text.append(
    node("span", item.name, "st-pack-name"),
    node(
      "small",
      `${item.count} stickers · ${item.owned ? "Owned" : "Third-party"}`,
    ),
  );
  open.append(cover, text);
  row.append(check, open);
  const reorder =
    item.installed && handle("installed", order, index, item.name);
  if (reorder) row.append(reorder);
  row.addEventListener("contextmenu", (event) => {
    if (event.target === check) return;
    event.preventDefault();
    showMenu(packMenu(item), open, event);
  });
  return { item, name: item.name.toLocaleLowerCase(), row, cover, reorder };
}

function renderPacks() {
  const sameOrder =
    noMatchingPacks.isConnected &&
    packs.length === packRows.length &&
    packs.every(
      ({ id, installed }, index) =>
        id === packRows[index].item.id &&
        installed === packRows[index].item.installed,
    );
  if (!sameOrder)
    for (const { cover } of packRows) media.unwatchThumbnail(cover);
  const order = packs.filter(({ installed }) => installed).map(({ id }) => id);
  let installedIndex = 0;
  packRows = packs.map((item, index) => {
    const position = item.installed ? installedIndex++ : -1;
    if (!sameOrder) return packRow(item, order, position);
    const previous = packRows[index];
    if (sameFields(item, previous.item)) return previous;
    const sameCover = item.cover_version === previous.item.cover_version;
    if (!sameCover) media.unwatchThumbnail(previous.cover);
    const next = packRow(
      item,
      order,
      position,
      sameCover ? previous.cover : undefined,
    );
    previous.row.replaceWith(next.row);
    return next;
  });
  if (!sameOrder)
    $("packs").replaceChildren(
      ...packRows.map(({ row }) => row),
      noMatchingPacks,
    );
  updatePackView();
}

function sameFields(next, previous) {
  const keys = Object.keys(next);
  return (
    keys.length === Object.keys(previous).length &&
    keys.every((key) => next[key] === previous[key])
  );
}

function updatePackView() {
  updatePackVisibility();
  filterPacks();
  packActions();
  markOpenPack();
}

function markOpenPack() {
  const list = $("packs");
  const current = list.querySelector(".st-pack-open[aria-current]");
  const next =
    source && !results
      ? list.querySelector(`[data-pack="${CSS.escape(source)}"] .st-pack-open`)
      : null;
  if (current === next) return;
  current?.removeAttribute("aria-current");
  next?.setAttribute("aria-current", "true");
}

async function refreshPacks() {
  const updated = await json("packs");
  const versions = new Map(
    packs.map(({ id, cover_version }) => [id, cover_version]),
  );
  const keep = new Set();
  for (const { id, cover_version } of updated)
    if (versions.get(id) === cover_version) keep.add(id);
  packs = updated;
  media.invalidateCovers(keep);
}

function packMenu(item) {
  const actions = [
    [
      "Clone pack…",
      async () => {
        try {
          const all =
            source === item.id && !results && items?.length
              ? items
              : await json(`items/${item.id}`);
          if (!all.length) throw new Error("There are no stickers to copy.");
          copyPicker(all);
        } catch (e) {
          error(e.message);
        }
      },
    ],
    ["Find duplicates", () => jobs.scan([item.id])],
  ];
  if (item.owned && view === "owned")
    actions.push(["Rename pack…", () => rename(item)]);
  if (item.installed)
    actions.push(["Uninstall pack…", () => uninstall([item])]);
  else if (item.owned)
    actions.push([
      "Install pack",
      () => jobs.write({ kind: "install", source: item.id }),
    ]);
  return actions;
}

function rename(item) {
  const field = node("input");
  field.maxLength = 64;
  field.value = item.name;
  const label = node("label", "Display title");
  label.append(field);
  openDialog(
    "Rename pack",
    [label],
    [
      button(
        "Save title",
        () => {
          if (!field.value.trim()) return;
          dialog.close();
          jobs.write({ kind: "rename", source: item.id, title: field.value });
        },
        "st-primary",
      ),
    ],
  );
  field.select();
}

function uninstall(chosen) {
  confirmAction(
    chosen.length === 1 ? "Uninstall pack" : "Uninstall packs",
    `${chosen.map(({ name, count }) => `${name} (${count} stickers)`).join(", ")}. This only removes ${chosen.length === 1 ? "the pack" : "these packs"} from your picker.`,
    () =>
      jobs.write({ kind: "uninstall", sources: chosen.map(({ id }) => id) }),
  );
}

function stopItemLoad() {
  itemLoad?.abort();
  itemLoad = null;
}

async function openSource(next, { keep = false } = {}) {
  stopItemLoad();
  const previous = keep && next === source && !results ? items : null;
  results = null;
  source = next;
  if (!keep) grid.resetSelection();
  items = previous;
  markOpenPack();
  if (!previous) renderGrid(next ? "Loading stickers…" : null);
  if (!next) return;
  const controller = (itemLoad = new AbortController());
  const { signal } = controller;
  try {
    const data = await json(`items/${next}`, undefined, signal);
    if (signal.aborted) return;
    items = data;
    if (
      previous &&
      data.length === previous.length &&
      data.every((item, index) => sameFields(item, previous[index]))
    ) {
      closeMenu(false);
      renderGridHeading();
      grid.updateView(stickerMoveHandle());
    } else renderGrid();
  } catch (e) {
    if (!signal.aborted) {
      items = null;
      renderGrid(`Could not load stickers: ${e.message}`);
    }
  } finally {
    if (itemLoad === controller) itemLoad = null;
  }
}

$("views").addEventListener("click", (event) => {
  const next = event.target.dataset.view;
  if (!next || view === next) return;
  $("views").querySelector("[aria-current]")?.removeAttribute("aria-current");
  event.target.setAttribute("aria-current", "page");
  view = next;
  for (const check of $("packs").querySelectorAll("input:checked"))
    check.checked = false;
  checkedPacks.clear();
  $("search").value = "";
  const retained = !listView() && source && pack()?.[view] ? source : null;
  updatePackView();
  if (!retained || results || (!items && !itemLoad)) {
    void openSource(listView() ? view : retained);
  } else if (items) {
    closeMenu(false);
    grid.updateView(stickerMoveHandle());
  }
});
$("packs-toggle").addEventListener("click", () => {
  packsCollapsed = !packsCollapsed;
  updatePackVisibility();
  filterPacks();
});
$("search").addEventListener("input", filterPacks);
$("pack-menu").onclick = (event) => {
  if (pack()) showMenu(packMenu(pack()), event.currentTarget);
};
$("results-close").onclick = () => void openSource(source);

function stickerMoveHandle() {
  if (!items?.length || results || view !== "owned" || !pack()?.owned) return;
  const order = items.map(({ id }) => id);
  const collection = source;
  return (index) => handle(collection, order, index, `sticker ${index + 1}`);
}

function renderGridHeading(message) {
  const info = pack();
  const size = $("pack-size");
  size.hidden = !info || Boolean(results) || !items;
  if (!size.hidden) {
    const bytes = items.reduce((total, item) => total + item.size_bytes, 0);
    const total =
      bytes >= 1_000_000
        ? `${sizeFormat.format(bytes / 1_000_000)} MB`
        : bytes >= 1_000
          ? `${sizeFormat.format(bytes / 1_000)} kB`
          : `${sizeFormat.format(bytes)} B`;
    size.textContent = `${items.length} ${items.length === 1 ? "sticker" : "stickers"} · ${total} total`;
  }
  $("heading").textContent = results
    ? "Duplicate scan"
    : labels[source] || info?.name || "Choose a pack";
  $("detail").textContent =
    message ??
    (results
      ? results.message
      : source
        ? "Drag to select · Shift-drag to add · Space selects the focused sticker"
        : "Open a pack to see its stickers.");
  $("pack-menu").hidden = !info || Boolean(results);
  $("results-close").hidden = !results;
}

function renderGrid(message) {
  closeMenu(false);
  renderGridHeading(message);
  grid.render({
    items: items ?? [],
    results,
    message,
    emptyMessage: source ? "No stickers in this view." : "",
    moveHandle: stickerMoveHandle(),
  });
}

function editActions(batch) {
  const actions = [];
  if (batch.every((item) => item.source === "favorites"))
    actions.push(["Unfavorite", "unfavorite"]);
  if (batch.every((item) => item.source === "recent"))
    actions.push(["Remove from Recent", "forget"]);
  if (
    (view === "owned" || results) &&
    batch.every((item) => pack(item.source)?.owned)
  )
    actions.push(["Remove from my pack", "remove"]);
  return actions;
}

function stickerActions(batch, menu = false) {
  const copy = menu
    ? `Copy (${batch.length}) stickers to pack`
    : `Copy (${batch.length})`;
  const actions = [[copy, () => copyPicker(batch), "st-primary"]];
  const edits = editActions(batch).map(([label, action]) => [
    label,
    () => edit(action, label, batch),
  ]);
  const favorite = batch.every((item) => item.source !== "favorites")
    ? [["Add to favorites", () => edit("favorite", "Add to favorites", batch)]]
    : [];
  return menu
    ? actions.concat(favorite, edits)
    : actions.concat(edits, favorite);
}

function edit(action, label, batch) {
  const change = () =>
    jobs.write({
      kind: "edit",
      action,
      entries: batch.map((item) => [item.source, item.id]),
    });
  if (action === "favorite") {
    change();
    return;
  }
  const sources = new Map();
  for (const item of batch)
    sources.set(item.source, (sources.get(item.source) || 0) + 1);
  const detail = [...sources]
    .map(([id, count]) => `${pack(id)?.name || labels[id]} (${count} stickers)`)
    .join(", ");
  confirmAction(
    label,
    `${detail}. ${action === "remove" ? "This edits your Telegram pack. To keep all stickers, choose Uninstall pack instead." : "This only changes the list, not the source packs."}`,
    change,
  );
}

function handle(collection, before, index, name) {
  return reorderHandle(name, {
    kind: collection === "installed" ? "pack" : "sticker",
    before,
    index,
    move: (to, keyboard) => moveItem(collection, before, index, to, keyboard),
    isBusy: () => jobs.writing,
  });
}

function moveItem(collection, before, index, to, keyboard = false) {
  if (jobs.writing || to < 0 || to >= before.length || index === to) return;
  if (
    collection === "installed"
      ? view !== "installed" || $("search").value.trim()
      : view !== "owned" || source !== collection || results || !items
  )
    return;
  const order = before.slice(),
    moved = order.splice(index, 1)[0];
  order.splice(to, 0, moved);
  if (collection === "installed") {
    packs = [
      ...order.map((id) => pack(id)),
      ...packs.filter((item) => !item.installed),
    ];
    renderPacks();
  } else {
    const map = new Map(items.map((item) => [item.id, item]));
    items = order.map((id) => map.get(id));
    renderGrid();
  }
  const refocus = () => {
    const attribute = collection === "installed" ? "data-pack" : "data-sticker";
    document
      .querySelector(`[${attribute}="${moved}"] > .st-handle`)
      ?.focus({ preventScroll: true });
  };
  if (keyboard) refocus();
  jobs.write(
    { kind: "reorder", source: collection, before, order, moved },
    { after: keyboard ? refocus : undefined },
  );
}

function copyPicker(batch) {
  const entries = batch.map((item) => [item.source, item.id]);
  const search = node("input");
  search.type = "search";
  search.placeholder = "Search owned packs";
  search.setAttribute("aria-label", "Search owned packs");
  const list = node("div", null, "st-destinations");
  const chosen = node("p");
  const create = button("Create new pack…", () => newPack(batch));
  let destination = null;
  const copy = button("Copy", () => submit(false));
  const install = button("Copy & install", () => submit(true), "st-primary");
  function submit(installPack) {
    dialog.close();
    jobs.write({ kind: "copy", entries, destination, install: installPack });
  }
  function fill() {
    const query = search.value.trim().toLocaleLowerCase();
    const owned = packs.filter(
      (item) => item.owned && item.name.toLocaleLowerCase().includes(query),
    );
    list.replaceChildren(
      ...owned.map((item) => {
        const entry = button(
          `${item.name} · ${item.count} stickers${item.installed ? " · Installed" : ""}`,
          () => {
            destination = item.id;
            for (const child of list.children)
              child.setAttribute("aria-pressed", String(child === entry));
            showChoice();
          },
        );
        entry.setAttribute("aria-pressed", String(destination === item.id));
        return entry;
      }),
    );
    if (!owned.length) list.append(node("p", "No matching owned packs."));
  }
  function showChoice() {
    copy.disabled = install.disabled = !destination;
    chosen.textContent = destination
      ? `Destination: ${pack(destination).name}`
      : "Choose an owned pack or create one.";
  }
  search.oninput = fill;
  fill();
  showChoice();
  openDialog(
    `Copy (${batch.length}) stickers to pack`,
    [search, list, create, chosen],
    [copy, install],
  );
}

function newPack(batch) {
  const name = node("input"),
    slug = node("input"),
    link = node("p");
  name.required = slug.required = true;
  name.maxLength = slug.maxLength = 64;
  slug.pattern = "[A-Za-z][A-Za-z0-9_]*";
  const suffix = Array.from(crypto.getRandomValues(new Uint8Array(3)), (byte) =>
    byte.toString(16).padStart(2, "0"),
  ).join("");
  let slugEdited = false;
  const titleLabel = node("label", "Display title"),
    slugLabel = node("label", "Telegram link name");
  titleLabel.append(name);
  slugLabel.append(slug);
  function setLink() {
    link.textContent = `https://t.me/addstickers/${slug.value}`;
  }
  name.oninput = () => {
    if (!slugEdited) {
      const base = name.value
        .normalize("NFKD")
        .replace(/[^a-z0-9]+/gi, "_")
        .replace(/^[^a-z]+|_$/gi, "");
      slug.value = `${base.slice(0, 55).replace(/_$/, "") || "pack"}_${suffix}`;
    }
    setLink();
  };
  slug.oninput = () => {
    slugEdited = true;
    setLink();
  };
  function submit(install) {
    slug.setCustomValidity(
      slug.value.includes("__") ? "Use only single underscores." : "",
    );
    if (!dialog.querySelector("form").reportValidity()) return;
    dialog.close();
    jobs.write({
      kind: "copy",
      entries: batch.map((item) => [item.source, item.id]),
      title: name.value,
      slug: slug.value,
      install,
    });
  }
  openDialog(
    "Create new pack",
    [
      titleLabel,
      slugLabel,
      link,
      button("← Back to owned packs", () => copyPicker(batch)),
    ],
    [
      button("Copy", () => submit(false)),
      button("Copy & install", () => submit(true), "st-primary"),
    ],
  );
}

async function refreshAfterWrite({ kind, action }) {
  if (kind === "edit" && action !== "remove") {
    const changed = action === "forget" ? "recent" : "favorites";
    if (source === changed && !results)
      await openSource(changed, { keep: true });
    return;
  }
  await refreshPacks();
  checkedPacks = new Set([...checkedPacks].filter((id) => pack(id)?.[view]));
  renderPacks();
  if (!results)
    await openSource(listView() ? view : pack()?.[view] ? source : null, {
      keep: true,
    });
}

function removeScanMatches(ids) {
  if (!results) return;
  const removed = new Set(ids);
  results = {
    ...results,
    exact: results.exact
      .map((group) => group.filter((item) => !removed.has(key(item))))
      .filter((group) => group.length > 1),
    lookalikes: results.lookalikes.filter((pair) =>
      pair.items.every((item) => !removed.has(key(item))),
    ),
  };
  renderGrid();
}

function showScanResults(state) {
  stopItemLoad();
  results = state;
  grid.resetSelection();
  markOpenPack();
  renderGrid();
}

$("packs").textContent = "Loading packs…";
void refreshPacks()
  .then(renderPacks)
  .catch((e) => {
    $("packs").textContent = "";
    error(e.message);
  });
