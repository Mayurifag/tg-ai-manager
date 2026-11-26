import { json, poll } from "./stickers-api.js";
import { $, button, confirmAction, error, node } from "./stickers-controls.js";

function paintStatus(target, state, actions = []) {
  target.hidden = false;
  const parts = [node("span", state.message)];
  if (state.archived?.length)
    parts.push(
      node(
        "p",
        `Telegram archived these packs to make room: ${state.archived.join(", ")}.`,
      ),
    );
  if (state.copied !== undefined)
    parts.push(
      node(
        "p",
        `${state.copied} copied · ${state.duplicates} exact duplicates covered${state.remaining ? ` · ${state.remaining} not attempted` : ""}`,
      ),
    );
  if (state.failures?.length) {
    const details = node("details");
    details.append(
      node("summary", `${state.failures.length} sticker failures`),
    );
    for (const item of state.failures)
      details.append(node("p", `Sticker ${item.id}: ${item.reason}`));
    parts.push(details);
  }
  if (state.slug) {
    const link = node("a", `https://t.me/addstickers/${state.slug}`);
    link.href = `https://t.me/addstickers/${encodeURIComponent(state.slug)}`;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    const paragraph = node("p");
    paragraph.append(link);
    parts.push(paragraph);
  }
  target.replaceChildren(...parts, ...actions);
}

export function createStickerJobs({ refresh, onRemoved, showResults }) {
  let writing = false;
  let scanning = false;

  async function write(operation, { path, after } = {}) {
    if (writing) {
      error("Wait for the current sticker change to finish.");
      return;
    }
    writing = true;
    error();
    const status = $("job");
    const actions = [];
    paintStatus(status, { message: "Starting…" });
    try {
      const { id } = await json(path ?? "operations", path ? {} : operation);
      const state = await poll(id, (value) => paintStatus(status, value));
      if (state.removed?.length) onRemoved(state.removed);
      if (state.retry)
        actions.push(
          button("Retry failed items", () =>
            write(operation, { path: `operations/${id}/retry` }),
          ),
        );
      if (state.can_install)
        actions.push(
          button("Install destination", () =>
            write({}, { path: `operations/${id}/install` }),
          ),
        );
      if (state.uninstall_original)
        actions.push(
          button("Uninstall original…", () =>
            confirmAction(
              "Uninstall original",
              `Uninstall ${state.uninstall_original.name} (${state.uninstall_original.count} stickers)? The pack itself will not be deleted.`,
              () => write({}, { path: `operations/${id}/uninstall` }),
            ),
          ),
        );
      for (const action of actions) action.disabled = true;
      paintStatus(status, state, actions);
    } catch (e) {
      paintStatus(status, { message: e.message });
    } finally {
      try {
        await refresh(operation);
        after?.();
      } catch (e) {
        error(`Refresh needed: ${e.message}`);
      }
      writing = false;
      for (const action of actions) action.disabled = false;
    }
  }

  async function scan(sources) {
    if (scanning) return;
    scanning = true;
    error();
    const target = $("scan");
    paintStatus(target, { message: "Starting duplicate scan…" });
    try {
      const { id } = await json("operations", { kind: "scan", sources });
      const cancel = button("Cancel scan", async () => {
        try {
          await json(`operations/${id}/cancel`, {});
        } catch (e) {
          error(e.message);
        }
      });
      const state = await poll(id, (value) =>
        paintStatus(target, value, value.state === "running" ? [cancel] : []),
      );
      if (state.state === "done")
        paintStatus(target, state, [
          button("Show results", () => showResults(state)),
        ]);
    } catch (e) {
      paintStatus(target, { message: e.message });
    } finally {
      scanning = false;
    }
  }

  return {
    write,
    scan,
    get writing() {
      return writing;
    },
  };
}
