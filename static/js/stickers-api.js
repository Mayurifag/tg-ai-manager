class RequestError extends Error {
  constructor(status, message) {
    super(message || `Request failed (${status}).`);
    this.status = status;
  }
}

async function request(path, body, signal) {
  const headers = { "X-Account-Token": window.TG_CONFIG.account };
  const options = { headers, signal };
  if (body !== undefined) {
    Object.assign(headers, {
      "Content-Type": "application/json",
      ...window.TG.mutationHeaders,
    });
    Object.assign(options, { method: "POST", body: JSON.stringify(body) });
  }
  const response = await fetch(`/api/stickers/${path}`, options);
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new RequestError(response.status, error?.error);
  }
  return response;
}

export async function json(path, body, signal) {
  return (await request(path, body, signal)).json();
}

export async function artwork(ref, mode, signal) {
  return (await request(`art/${ref}/${mode}`, undefined, signal)).blob();
}

export async function poll(id, onUpdate) {
  let last;
  let backoff = 500;
  const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  for (;;) {
    let state;
    try {
      state = await json(`operations/${id}`);
    } catch (e) {
      if (!(e instanceof TypeError || e.status === 408 || e.status >= 500))
        throw e;
      if (backoff > 8000)
        throw new Error(
          "Could not check this operation. It may still finish. Refresh to check Telegram’s current state before retrying.",
          { cause: e },
        );
      await pause(backoff);
      backoff *= 2;
      continue;
    }
    backoff = 500;
    const snapshot = JSON.stringify(state);
    if (snapshot !== last) onUpdate(state);
    last = snapshot;
    if (state.state !== "running") return state;
    await pause(350);
  }
}
