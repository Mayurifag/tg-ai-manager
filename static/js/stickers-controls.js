export const $ = (id) => document.getElementById(`st-${id}`);
export const key = (item) => `${item.source}/${item.id}`;

const dialog = $("dialog");
let menuOwner = null;

export function node(tag, text, classes) {
  const element = document.createElement(tag);
  if (text) element.textContent = text;
  if (classes) element.className = classes;
  return element;
}

export function button(text, fn, classes) {
  const element = node("button", text, classes);
  element.type = "button";
  if (fn) element.addEventListener("click", fn);
  return element;
}

export function error(message) {
  $("error").textContent = message || "";
  $("error").hidden = !message;
}

export function openDialog(title, body, actions) {
  closeMenu(false);
  $("dialog-title").textContent = title;
  $("dialog-body").replaceChildren(...body);
  $("dialog-actions").replaceChildren(
    ...actions,
    button("Cancel", () => dialog.close()),
  );
  if (!dialog.open) dialog.showModal();
  const first =
    $("dialog-body").querySelector("input, button") ||
    $("dialog-actions").lastElementChild;
  first.focus();
}

dialog
  .querySelector("form")
  .addEventListener("submit", (event) => event.preventDefault());

export function confirmAction(title, detail, action) {
  openDialog(
    title,
    [node("p", detail)],
    [
      button(
        title,
        () => {
          dialog.close();
          action();
        },
        "st-primary",
      ),
    ],
  );
}

export function showMenu(actions, owner, point) {
  const menu = $("menu");
  menuOwner = owner;
  menu.replaceChildren(
    ...actions.map(([label, action]) =>
      button(label, () => {
        closeMenu(true);
        action();
      }),
    ),
  );
  menu.hidden = false;
  const anchor = owner.getBoundingClientRect();
  menu.style.left = `${Math.max(4, Math.min(point?.clientX ?? anchor.left, innerWidth - menu.offsetWidth - 4))}px`;
  menu.style.top = `${Math.max(4, Math.min(point?.clientY ?? anchor.bottom, innerHeight - menu.offsetHeight - 4))}px`;
  menu.firstElementChild?.focus({ preventScroll: true });
}

export function closeMenu(returnFocus) {
  $("menu").hidden = true;
  if (returnFocus) menuOwner?.focus({ preventScroll: true });
  menuOwner = null;
}

document.addEventListener("pointerdown", (event) => {
  if (!$("menu").contains(event.target)) closeMenu(false);
});
$("menu").addEventListener("keydown", (event) => {
  const actions = [...$("menu").children];
  const step = { ArrowDown: 1, ArrowUp: -1 }[event.key];
  if (event.key === "Escape") closeMenu(true);
  else if (step) {
    event.preventDefault();
    actions[
      (actions.indexOf(event.target) + step + actions.length) % actions.length
    ].focus();
  }
});

export function reorderHandle(name, { kind, before, index, move, isBusy }) {
  const control = button("⠿", null, "st-handle");
  control.title = "Drag to reorder, or use arrow keys";
  control.setAttribute("aria-label", `Move ${name}`);
  control.addEventListener("keydown", (event) => {
    const direction = {
      ArrowUp: -1,
      ArrowDown: 1,
      ArrowLeft: -1,
      ArrowRight: 1,
    }[event.key];
    if (!direction) return;
    event.preventDefault();
    move(index + direction, true);
  });
  control.addEventListener("pointerdown", (event) => {
    if (isBusy() || event.button !== 0) return;
    let target,
      to = index;
    control.setPointerCapture(event.pointerId);
    function clear() {
      target?.classList.remove("st-drop-before", "st-drop-after");
      target = null;
    }
    function track(pointer) {
      clear();
      const hit = document
        .elementFromPoint(pointer.clientX, pointer.clientY)
        ?.closest(`[data-${kind}]`);
      to = hit ? before.indexOf(hit.dataset[kind]) : index;
      if (to === index || to < 0) return;
      target = hit;
      target.classList.add(to > index ? "st-drop-after" : "st-drop-before");
    }
    const stopEvents = ["pointerup", "pointercancel", "lostpointercapture"];
    function stop(pointer) {
      control.removeEventListener("pointermove", track);
      for (const type of stopEvents) control.removeEventListener(type, stop);
      clear();
      if (pointer.type === "pointerup") move(to);
    }
    control.addEventListener("pointermove", track);
    for (const type of stopEvents) control.addEventListener(type, stop);
  });
  return control;
}
