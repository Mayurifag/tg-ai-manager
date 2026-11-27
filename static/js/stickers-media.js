import { artwork, gridArtwork } from "./stickers-api.js";

const GRID_BATCH_SIZE = 48;

export function createStickerMedia(preview, art) {
  const gridCache = new Map();
  const coverCache = new Map();
  const covers = new Set();
  const coverRequests = new Map();
  const placeholders = new WeakMap();
  let stopGrid;
  let stillRequest = null;
  let fullRequest = null;
  let playing = null;

  const observer = new IntersectionObserver(
    (entries) => {
      for (const { target, isIntersecting } of entries) {
        if (!target.dataset.art) continue;
        if (isIntersecting && target.isConnected) covers.add(target);
        else covers.delete(target);
      }
      pumpCovers();
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

  function cachedImage(cache, ref) {
    const blob = cache.get(ref);
    if (blob) {
      cache.delete(ref);
      cache.set(ref, blob);
    }
    return blob;
  }

  function cacheImage(cache, limit, ref, blob) {
    cache.set(ref, blob);
    if (cache.size > limit) cache.delete(cache.keys().next().value);
  }

  function pumpCovers() {
    for (const target of covers) {
      const ref = target.dataset.art;
      if (target.isConnected && !target.closest("[hidden]")) {
        const blob = cachedImage(coverCache, ref);
        const active = coverRequests.get(ref);
        if (blob) void showThumbnail(target, ref, blob);
        else if (active) active.targets.add(target);
        else {
          if (coverRequests.size >= 2) continue;
          const request = {
            controller: new AbortController(),
            targets: new Set([target]),
          };
          coverRequests.set(ref, request);
          void loadCover(ref, request);
        }
      }
      observer.unobserve(target);
      covers.delete(target);
    }
  }

  async function loadCover(ref, { controller, targets }) {
    const { signal } = controller;
    try {
      const blob = await artwork(ref, "display", signal);
      if (signal.aborted) return;
      cacheImage(coverCache, 80, ref, blob);
      for (const target of targets) void showThumbnail(target, ref, blob);
    } catch {
      if (!signal.aborted)
        for (const target of targets) thumbnailUnavailable(target, ref);
    } finally {
      if (!signal.aborted) {
        coverRequests.delete(ref);
        pumpCovers();
      }
    }
  }

  function retainCovers(keep) {
    for (const [ref, request] of coverRequests) {
      if (keep.has(ref)) continue;
      request.controller.abort();
      coverRequests.delete(ref);
    }
    pumpCovers();
  }

  function invalidateCovers(keep) {
    for (const ref of coverCache.keys())
      if (!keep.has(ref)) coverCache.delete(ref);
    for (const target of covers)
      if (!keep.has(target.dataset.art)) covers.delete(target);
    retainCovers(keep);
  }

  function watchThumbnail(target, ref) {
    target.dataset.art = ref;
    observer.observe(target);
  }

  function unwatchThumbnail(target) {
    observer.unobserve(target);
    covers.delete(target);
    coverRequests.get(target.dataset.art)?.targets.delete(target);
    delete target.dataset.art;
  }

  function watchGrid(tiles) {
    stopGrid?.();
    stopGrid = null;
    if (!tiles.length) return;

    const waiting = new Map();
    const nearby = new Set();
    const controller = new AbortController();
    const { signal } = controller;
    let loading = false;
    const observer = new IntersectionObserver(
      (entries) => {
        for (const { target, isIntersecting } of entries) {
          if (isIntersecting) nearby.add(target);
          else nearby.delete(target);
        }
        void loadNearby();
      },
      { rootMargin: "160px" },
    );
    stopGrid = () => {
      controller.abort();
      observer.disconnect();
    };
    for (const [ref, target] of tiles) {
      target.dataset.art = ref;
      if (!waiting.has(ref)) waiting.set(ref, []);
      waiting.get(ref).push(target);
      observer.observe(target);
    }

    async function loadNearby() {
      if (loading || signal.aborted) return;
      const batch = new Map();
      for (const target of nearby) {
        nearby.delete(target);
        const ref = target.dataset.art;
        const targets = waiting.get(ref);
        if (!targets) continue;
        waiting.delete(ref);
        const cached = cachedImage(gridCache, ref);
        for (const tile of targets) {
          nearby.delete(tile);
          observer.unobserve(tile);
          if (cached) void showThumbnail(tile, ref, cached);
          else showPlaceholderImage(tile);
        }
        if (!cached) batch.set(ref, targets);
        if (batch.size === GRID_BATCH_SIZE) break;
      }
      if (!batch.size) return;

      loading = true;
      function paint(ref, blob) {
        const targets = batch.get(ref);
        if (signal.aborted || !targets) return;
        batch.delete(ref);
        if (blob) cacheImage(gridCache, 300, ref, blob);
        for (const target of targets) {
          if (blob) void showThumbnail(target, ref, blob);
          else thumbnailUnavailable(target, ref);
        }
      }
      try {
        await gridArtwork([...batch.keys()], signal, paint);
      } catch {
        // Keep any thumbnails that arrived before the stream failed.
      } finally {
        loading = false;
        if (!signal.aborted) {
          for (const ref of batch.keys()) paint(ref, null);
          void loadNearby();
        }
      }
    }
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

  function stopVideo() {
    if (!playing) return;
    const { controller, control, video } = playing;
    playing = null;
    controller.abort();
    video.pause();
    video.removeAttribute("src");
    video.load();
    video.remove();
    control.textContent = "Play";
    control.setAttribute("aria-label", "Play animated sticker");
  }

  async function toggleVideo(ref, card, control) {
    const alreadyPlaying = playing?.control === control;
    stopVideo();
    if (alreadyPlaying) return;

    const controller = new AbortController();
    const { signal } = controller;
    const video = document.createElement("video");
    video.className = "st-tile-video";
    video.setAttribute("aria-hidden", "true");
    video.muted = video.loop = video.playsInline = true;
    video.hidden = true;
    playing = { controller, control, video };
    control.textContent = "Stop";
    control.setAttribute("aria-label", "Stop animated sticker");
    const unavailable = () => {
      if (signal.aborted) return;
      stopVideo();
      control.textContent = "Retry";
      control.setAttribute("aria-label", "Retry animated sticker");
    };
    try {
      const blob = await artwork(ref, "full", signal);
      if (signal.aborted) return;
      const url = URL.createObjectURL(blob);
      signal.addEventListener("abort", () => URL.revokeObjectURL(url), {
        once: true,
      });
      video.addEventListener("error", unavailable, { once: true, signal });
      video.src = url;
      card.append(video);
      await video.play();
      if (!signal.aborted) video.hidden = false;
    } catch {
      unavailable();
    }
  }

  function releaseFull() {
    fullRequest?.abort();
    fullRequest = null;
    stillRequest?.abort();
    stillRequest = null;
    delete art.dataset.art;
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
      if (!item.thumb) return;
      const cached = cachedImage(gridCache, ref);
      if (cached) {
        void showThumbnail(art, ref, cached);
        return;
      }
      showPlaceholderImage(art);
      stillRequest?.abort();
      const controller = (stillRequest = new AbortController());
      const { signal } = controller;
      void artwork(ref, "display", signal)
        .then((blob) => {
          if (signal.aborted) return;
          cacheImage(gridCache, 300, ref, blob);
          return showThumbnail(art, ref, blob);
        })
        .catch(() => {
          if (!signal.aborted) thumbnailUnavailable(art, ref);
        });
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
  });

  return {
    invalidateCovers,
    placeholder,
    retainCovers,
    watchGrid,
    watchThumbnail,
    unwatchThumbnail,
    showPreview,
    stopVideo,
    toggleVideo,
  };
}
