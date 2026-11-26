import { artwork } from "./stickers-api.js";

// Leave room for the live event stream and full preview on HTTP/1.1.
const THUMBNAIL_REQUEST_LIMIT = 4;

export function createStickerMedia(preview, art) {
  const lane = (concurrency, limit) => ({
    concurrency,
    limit,
    pending: new Set(),
    cache: new Map(),
    requests: new Map(),
  });
  // A cold pack cover needs a separate Telegram lookup. Keep grid capacity.
  const grid = lane(3, 300);
  const packs = lane(2, 80);
  const laneFor = (ref) => (ref.includes("/") ? grid : packs);
  const placeholders = new WeakMap();
  let fullRequest = null;

  const observer = new IntersectionObserver(
    (entries) => {
      for (const { target, isIntersecting } of entries) {
        const ref = target.dataset.art;
        if (!ref) continue;
        const { pending } = laneFor(ref);
        if (isIntersecting && target.isConnected) pending.add(target);
        else pending.delete(target);
      }
      pump();
    },
    { rootMargin: "160px" },
  );

  function imageAt(url) {
    const image = document.createElement("img");
    image.alt = "";
    image.draggable = false;
    image.decoding = "async";
    image.src = url;
    return image;
  }

  function wantsThumbnail(target, ref) {
    return (
      target.isConnected && target.dataset.art === ref && !target.dataset.full
    );
  }

  function thumbnailUnavailable(target, ref) {
    if (!wantsThumbnail(target, ref)) return;
    if (target.querySelector(".st-media-fallback"))
      placeholder(target, {}, "Still unavailable");
    else delete target.dataset.art; // Let the next pack refresh retry this cover.
  }

  async function showThumbnail(target, ref, blob) {
    if (!wantsThumbnail(target, ref)) return;
    const url = URL.createObjectURL(blob);
    const image = imageAt(url);
    try {
      await image.decode();
      if (wantsThumbnail(target, ref)) target.replaceChildren(image);
    } catch {
      thumbnailUnavailable(target, ref);
    } finally {
      URL.revokeObjectURL(url);
    }
  }

  function useCached(target, cache) {
    const ref = target.dataset.art;
    const blob = cache.get(ref);
    if (!blob) return false;
    cache.delete(ref);
    cache.set(ref, blob);
    void showThumbnail(target, ref, blob);
    return true;
  }

  function pump() {
    pumpLane(grid);
    pumpLane(packs);
  }

  function pumpLane(queue) {
    for (const target of queue.pending) {
      const ref = target.dataset.art;
      if (
        target.isConnected &&
        !target.closest("[hidden]") &&
        !useCached(target, queue.cache)
      ) {
        showPlaceholderImage(target);
        const active = queue.requests.get(ref);
        if (active) active.targets.add(target);
        else {
          if (
            queue.requests.size >= queue.concurrency ||
            grid.requests.size + packs.requests.size >= THUMBNAIL_REQUEST_LIMIT
          )
            continue;
          const request = {
            controller: new AbortController(),
            targets: new Set([target]),
          };
          queue.requests.set(ref, request);
          void loadThumbnail(queue, ref, request);
        }
      }
      observer.unobserve(target);
      queue.pending.delete(target);
    }
  }

  async function loadThumbnail(queue, ref, { controller, targets }) {
    const { signal } = controller;
    try {
      const blob = await artwork(ref, "display", signal);
      if (signal.aborted) return;
      queue.cache.set(ref, blob);
      if (queue.cache.size > queue.limit)
        queue.cache.delete(queue.cache.keys().next().value);
      for (const target of targets) void showThumbnail(target, ref, blob);
    } catch {
      if (!signal.aborted)
        for (const target of targets) thumbnailUnavailable(target, ref);
    } finally {
      if (!signal.aborted) {
        queue.requests.delete(ref);
        pump();
      }
    }
  }

  function cancelRequests(queue, keep) {
    for (const ref of queue.requests.keys())
      if (!keep.has(ref)) cancelRequest(queue, ref);
    pump();
  }

  function cancelRequest(queue, ref) {
    queue.requests.get(ref).controller.abort();
    queue.requests.delete(ref);
  }

  function invalidateCovers(keep) {
    for (const ref of packs.cache.keys())
      if (!keep.has(ref)) packs.cache.delete(ref);
    for (const target of packs.pending)
      if (!keep.has(target.dataset.art)) packs.pending.delete(target);
    cancelRequests(packs, keep);
  }

  function watchThumbnail(target, ref) {
    target.dataset.art = ref;
    observer.observe(target);
  }

  function unwatchThumbnail(target) {
    observer.unobserve(target);
    const ref = target.dataset.art;
    if (!ref) return;
    const queue = laneFor(ref);
    queue.pending.delete(target);
    const request = queue.requests.get(ref);
    if (request) {
      request.targets.delete(target);
      // An offscreen preview may be the only consumer of the still.
      if (target === art && !request.targets.size) cancelRequest(queue, ref);
    }
    delete target.dataset.art;
  }

  function placeholder(target, { placeholder: data }, text) {
    const label = document.createElement("small");
    label.className = "st-media-fallback";
    label.textContent = text;
    target.replaceChildren(label);
    if (data) placeholders.set(label, data);
  }

  function showPlaceholderImage(target) {
    const label = target.firstElementChild;
    const data = placeholders.get(label);
    if (!data) return;
    placeholders.delete(label);
    const image = imageAt(`data:image/jpeg;base64,${data}`);
    image.className = "st-blur";
    image.onload = () => {
      if (target.isConnected && target.firstElementChild === label)
        target.append(image);
    };
  }

  function releaseFull() {
    fullRequest?.abort();
    fullRequest = null;
    unwatchThumbnail(art);
    delete art.dataset.full;
  }

  function showPreview(item, ref) {
    releaseFull();
    art.dataset.art = ref;
    const showStill = () => {
      placeholder(
        art,
        item,
        item.thumb ? "Loading still…" : "No Telegram still available",
      );
      if (item.thumb && !useCached(art, grid.cache))
        grid.pending = new Set([art, ...grid.pending]);
      pump();
    };
    showStill();
    if (item.format !== "webp" && item.format !== "webm") return;

    const controller = (fullRequest = new AbortController());
    const { signal } = controller;
    const isCurrent = () => fullRequest === controller && preview.open;
    const showFull = (element) => {
      if (!isCurrent()) return;
      art.dataset.full = "true";
      art.replaceChildren(element);
    };
    const fullUnavailable = () => {
      if (!isCurrent()) return;
      fullRequest = null;
      controller.abort();
      const hadFull = art.hasAttribute("data-full");
      delete art.dataset.full;
      if (!item.thumb) placeholder(art, item, "Full sticker unavailable");
      else if (hadFull) showStill();
    };
    void artwork(ref, "full", signal)
      .then(async (blob) => {
        if (!isCurrent()) return;
        const url = URL.createObjectURL(blob);
        signal.addEventListener("abort", () => URL.revokeObjectURL(url), {
          once: true,
        });
        if (item.format === "webm") {
          const video = document.createElement("video");
          video.muted = video.loop = video.playsInline = video.controls = true;
          video.autoplay = !matchMedia("(prefers-reduced-motion: reduce)")
            .matches;
          video.preload = "auto";
          video.addEventListener("loadedmetadata", () => showFull(video), {
            once: true,
            signal,
          });
          video.addEventListener("error", fullUnavailable, {
            once: true,
            signal,
          });
          video.src = url;
        } else {
          const image = imageAt(url);
          await image.decode();
          showFull(image);
        }
      })
      .catch(fullUnavailable);
  }

  preview.addEventListener("close", () => {
    if (preview.open) return;
    releaseFull();
    art.replaceChildren();
    pump();
  });

  return {
    invalidateCovers,
    placeholder,
    retainCovers: (refs) => cancelRequests(packs, refs),
    retainGrid: (refs) => cancelRequests(grid, refs),
    watchThumbnail,
    unwatchThumbnail,
    showPreview,
  };
}
