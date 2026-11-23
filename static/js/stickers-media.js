import { artwork } from "./stickers-api.js";

export function createStickerMedia(preview, art) {
  const thumbnails = new Map();
  let pending = [],
    active = 0;
  let videoRequest = null;

  const observer = new IntersectionObserver(
    (entries) => {
      for (const entry of entries) {
        if (!entry.isIntersecting) continue;
        observer.unobserve(entry.target);
        pending.push(entry.target);
      }
      pump();
    },
    { rootMargin: "160px" },
  );

  function showThumbnail(target) {
    const ref = target.dataset.art;
    const url = thumbnails.get(ref);
    if (!url) return false;
    const image = document.createElement("img");
    image.alt = "";
    image.draggable = false;
    image.decoding = "async";
    image.onload = () => {
      if (
        target.isConnected &&
        target.dataset.art === ref &&
        !target.querySelector("video")
      )
        target.replaceChildren(image);
    };
    image.src = url;
    return true;
  }

  function pump() {
    while (active < 4 && pending.length) {
      const target = pending.shift();
      if (!target.isConnected || showThumbnail(target)) continue;
      const ref = target.dataset.art;
      active++;
      void artwork(ref, "thumb")
        .then((blob) => {
          if (!thumbnails.has(ref))
            thumbnails.set(ref, URL.createObjectURL(blob));
          if (target.isConnected) showThumbnail(target);
          if (preview.open && art.dataset.art === ref && !videoRequest)
            showThumbnail(art);
          if (thumbnails.size > 300) {
            const first = thumbnails.keys().next().value;
            URL.revokeObjectURL(thumbnails.get(first));
            thumbnails.delete(first);
          }
        })
        .catch(() => {
          if (target.isConnected)
            target.firstElementChild.textContent = "Still unavailable";
        })
        .finally(() => {
          active--;
          pump();
        });
    }
  }

  function resetGrid() {
    observer.disconnect();
    pending = [];
  }

  function watchThumbnail(target, hasThumbnail) {
    if (!showThumbnail(target) && hasThumbnail) observer.observe(target);
  }

  function releaseVideo() {
    videoRequest?.abort();
    videoRequest = null;
    const video = art.querySelector("video");
    if (video) URL.revokeObjectURL(video.src);
  }

  function showPreview(item, tile) {
    releaseVideo();
    const ref = tile.dataset.art;
    art.dataset.art = ref;
    const placeholder = document.createElement("p");
    placeholder.textContent = item.thumb
      ? "Telegram still not loaded"
      : "No Telegram still available";
    art.replaceChildren(placeholder);
    if (!showThumbnail(art) && item.thumb) {
      pending.unshift(tile);
      pump();
    }
    if (item.format !== "webm") return;

    const controller = (videoRequest = new AbortController());
    const isCurrent = () => videoRequest === controller && preview.open;
    void artwork(ref, "full", controller.signal)
      .then((blob) => {
        if (!isCurrent()) return;
        const video = document.createElement("video");
        video.muted = true;
        video.loop = true;
        video.playsInline = true;
        video.controls = true;
        video.autoplay = true;
        video.src = URL.createObjectURL(blob);
        art.replaceChildren(video);
      })
      .catch((e) => {
        if (e.name !== "AbortError" && isCurrent() && !thumbnails.has(ref)) {
          placeholder.textContent = "Video unavailable";
          art.replaceChildren(placeholder);
        }
      });
  }

  preview.addEventListener("close", () => {
    if (preview.open) return;
    releaseVideo();
    art.replaceChildren();
  });

  return { resetGrid, watchThumbnail, showPreview };
}
