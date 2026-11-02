const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

function listeners() {
  const handlers = {};
  return {
    handlers,
    addEventListener(name, callback) {
      (handlers[name] ||= []).push(callback);
    },
    emit(name, event) {
      return (handlers[name] || []).map(callback => callback(event));
    },
    dispatchEvent(event) {
      return (handlers[event.type] || []).map(callback => callback(event));
    },
  };
}

function element() {
  return {
    ...listeners(),
    style: {}, dataset: {}, children: [], hidden: false,
    classList: { add() {}, remove() {}, contains() { return false; } },
    querySelectorAll() { return []; },
    querySelector() { return null; },
    contains(node) { return node === this; },
    setAttribute() {},
  };
}

async function testBackAndEvents() {
  const docEvents = listeners();
  const winEvents = listeners();
  const elements = new Map();
  const streams = [];
  let backCalls = 0;
  const document = {
    ...docEvents,
    referrer: "https://example.test/",
    body: element(),
    getElementById(id) {
      if (id === "event-feed-list") return null;
      if (!elements.has(id)) elements.set(id, element());
      return elements.get(id);
    },
    querySelectorAll() { return []; },
  };
  const context = {
    ...winEvents, document, URL, URLSearchParams, console,
    CustomEvent: class { constructor(type, options) { this.type = type; this.detail = options.detail; } },
    history: { length: 2, back() { backCalls++; } },
    EventSource: class {
      constructor() { this.closed = false; streams.push(this); }
      close() { this.closed = true; }
    },
  };
  context.window = context;
  vm.createContext(context);
  vm.runInContext(fs.readFileSync("static/js/base.js", "utf8"), context);
  document.emit("DOMContentLoaded", {});
  assert.equal(streams.length, 1);
  const received = [];
  document.addEventListener("tg:event", event => received.push(event.detail));
  streams[0].onopen();
  assert.equal(received.length, 0);
  streams[0].onerror();
  streams[0].onopen();
  assert.equal(received.pop().type, "resync_required");
  streams[0].onmessage({data: "not json"});
  assert.equal(received.pop().type, "resync_required");
  let prevented = false;
  const event = {
    target: { closest: () => ({href: "https://example.test/"}) },
    button: 0, preventDefault() { prevented = true; },
  };
  document.emit("click", event);
  assert.equal(backCalls, 1);
  assert.equal(prevented, true);
  document.referrer = "https://example.test/settings";
  prevented = false;
  document.emit("click", event);
  assert.equal(backCalls, 1);
  assert.equal(prevented, false);
  context.emit("pagehide", {});
  assert.equal(streams[0].closed, true);
  context.emit("pageshow", {persisted: true});
  assert.equal(streams.length, 2);
  streams[1].onopen();
  assert.equal(received.pop().type, "resync_required");
}

async function testIndexRetainsRenderedCardsOnReturn() {
  const winEvents = listeners();
  const docEvents = listeners();
  const card = { dataset: {chatId: "42"}, outerHTML: "same", getBoundingClientRect: () => ({top: 10, bottom: 40}) };
  const list = element();
  list.children = [card];
  list.querySelector = () => list.children.find(item => item.dataset.chatId === "42");
  list.append = item => {
    list.children = list.children.filter(child => child !== item);
    list.children.push(item);
  };
  const button = element();
  button.hidden = true;
  button.dataset = {snapshot: "warm", nextOffset: ""};
  const elements = {
    "chat-list-container": list,
    "load-more-chats": button,
    "chat-list-status": element(),
    "refresh-chats": element(),
  };
  let requests = 0;
  let completeRequest;
  const document = {
    ...docEvents,
    getElementById: id => elements[id],
    createElement() {
      return {children: [{...card}]};
    },
  };
  const context = {
    ...winEvents, document, URLSearchParams, console, setTimeout, clearTimeout,
    parseAppleEmojis() {}, scrollBy() {},
    fetch() { requests++; return new Promise(resolve => { completeRequest = resolve; }); },
  };
  context.window = context;
  vm.createContext(context);
  vm.runInContext(fs.readFileSync("static/js/index.js", "utf8"), context);
  assert.equal(requests, 0, "server-rendered cards do not wait for another list request");
  const [refresh] = context.emit("pageshow", {persisted: true});
  assert.equal(requests, 1);
  assert.equal(list.children[0], card, "existing card stays visible during revalidation");
  completeRequest({ok: true, async json() { return {html: "same", snapshot: "new", next_offset: null}; }});
  await refresh;
  assert.equal(list.children[0], card, "unchanged cards are not replaced after revalidation");
}

function indexCardHarness(fetch) {
  const winEvents = listeners();
  const docEvents = listeners();
  const list = element();
  const button = element();
  button.hidden = true;
  const elements = {
    "chat-list-container": list,
    "load-more-chats": button,
    "chat-list-status": element(),
    "refresh-chats": element(),
  };
  let now = 0;
  let nextTimer = 0;
  const timers = new Map();
  const document = {
    ...docEvents,
    activeElement: null,
    getElementById: id => elements[id],
    createElement() {
      let card;
      return {
        set innerHTML(value) { card = makeCard(value); },
        get firstElementChild() { return card; },
      };
    },
  };
  function makeCard(label) {
    const link = {
      classList: {contains() { return false; }},
      focus() { document.activeElement = link; },
    };
    const card = {
      label,
      dataset: {chatId: "42", isUnread: "true", isPinned: "false"},
      contains: node => node === link,
      querySelector: selector => selector === ".chat-card-link" ? link : null,
      remove() { list.children = list.children.filter(child => child !== card); },
      replaceWith(other) {
        const index = list.children.indexOf(card);
        if (index !== -1) list.children[index] = other;
      },
    };
    return card;
  }
  list.children = [makeCard("initial")];
  list.querySelector = selector => selector.includes('data-chat-id="42"') ? list.children[0] : null;
  list.insertBefore = card => list.children.push(card);
  const context = {
    ...winEvents, document, URLSearchParams, console, fetch,
    setTimeout(callback, delay) {
      const id = ++nextTimer;
      timers.set(id, {at: now + delay, callback});
      return id;
    },
    clearTimeout(id) { timers.delete(id); },
    parseAppleEmojis() {}, scrollBy() {},
  };
  context.window = context;
  vm.createContext(context);
  vm.runInContext(fs.readFileSync("static/js/index.js", "utf8"), context);
  async function settle() {
    for (let i = 0; i < 8; i++) await Promise.resolve();
  }
  async function advance(ms) {
    const end = now + ms;
    while (true) {
      const next = [...timers.entries()].sort((a, b) => a[1].at - b[1].at)[0];
      if (!next || next[1].at > end) break;
      now = next[1].at;
      timers.delete(next[0]);
      next[1].callback();
      await settle();
    }
    now = end;
    await settle();
  }
  return {context, list, document, advance, settle};
}

async function testContinuousCardUpdatesAreBoundedAndPreserveFocus() {
  let requests = 0;
  const harness = indexCardHarness(async () => {
    requests++;
    return {ok: true, async text() { return `version-${requests}`; }};
  });
  harness.document.activeElement = harness.list.children[0].querySelector(".chat-card-link");
  for (let i = 0; i < 30; i++) {
    harness.context.reloadCard(42);
    await harness.advance(100);
  }
  assert.ok(requests >= 5 && requests <= 6, `continuous updates caused ${requests} requests`);
  assert.notEqual(harness.list.children[0].label, "initial", "updates are not starved");
  assert.equal(harness.list.children[0].contains(harness.document.activeElement), true, "focus follows the updated card");
}

async function testOldCardResponseCannotOverwriteANewerEvent() {
  let requests = 0;
  let release;
  const harness = indexCardHarness(() => {
    requests++;
    if (requests === 1) return new Promise(resolve => { release = resolve; });
    return Promise.resolve({ok: true, async text() { return "new"; }});
  });
  harness.context.reloadCard(42);
  await harness.advance(500);
  harness.context.reloadCard(42);
  await harness.advance(1500);
  assert.equal(requests, 1, "only one request per card may be in flight");
  release({ok: true, async text() { return "old"; }});
  await harness.settle();
  assert.equal(harness.list.children[0].label, "initial", "obsolete response was discarded");
  await harness.advance(500);
  assert.equal(requests, 2);
  assert.equal(harness.list.children[0].label, "new");
}

async function testForumTopicUpdatesAreBoundedAndDiscardOldResponses() {
  const docEvents = listeners();
  const wrappers = new Map();
  const timers = new Map();
  let now = 0, nextTimer = 0, calls = 0, release;
  const document = {
    ...docEvents,
    activeElement: null,
    getElementById(id) {
      return {"topic-list-container": list, "topic-list-status": status}[id] || null;
    },
    createElement() {
      let card;
      return {
        set innerHTML(label) { card = makeCard("7", label); },
        get firstElementChild() { return card; },
      };
    },
  };
  function makeCard(id, label) {
    return {
      dataset: {chatId: id}, label,
      contains: () => false,
      querySelector: () => null,
      replaceWith(other) { wrappers.set(id, other); },
      remove() { wrappers.delete(id); },
    };
  }
  wrappers.set("7", makeCard("7", "initial"));
  const list = {
    ...element(),
    querySelector(selector) {
      return wrappers.get(selector.match(/data-chat-id="(\d+)"/)?.[1]) || null;
    },
    querySelectorAll() { return [...wrappers.values()]; },
  };
  const status = element();
  const context = {
    document, console, TG_FORUM: {chatId: -100}, parseAppleEmojis() {},
    location: {reload() { throw new Error("unexpected reload"); }},
    setTimeout(callback, delay) {
      const id = ++nextTimer; timers.set(id, {at: now + delay, callback}); return id;
    },
    fetch() {
      calls++;
      if (calls === 1) return new Promise(resolve => { release = resolve; });
      return Promise.resolve({ok: true, async text() { return "new"; }});
    },
  };
  context.window = context;
  vm.createContext(context);
  vm.runInContext(fs.readFileSync("static/js/forum.js", "utf8"), context);
  async function settle() { for (let i = 0; i < 8; i++) await Promise.resolve(); }
  async function advance(ms) {
    const end = now + ms;
    for (;;) {
      const next = [...timers.entries()].sort((a, b) => a[1].at - b[1].at)[0];
      if (!next || next[1].at > end) break;
      now = next[1].at; timers.delete(next[0]); next[1].callback(); await settle();
    }
    now = end; await settle();
  }
  const event = type => document.emit("tg:event", {detail: {type, chat_id: -100, topic_id: 7, scope_known: true}});
  event("message");
  await advance(500);
  assert.equal(calls, 1, "a topic message refreshes its card");
  for (let i = 0; i < 20; i++) { event("edited"); await advance(100); }
  assert.equal(calls, 1, "the same topic has only one in-flight request");
  release({ok: true, async text() { return "old"; }});
  await settle();
  assert.equal(wrappers.get("7").label, "initial", "old response is discarded");
  await advance(500);
  assert.equal(calls, 2);
  assert.equal(wrappers.get("7").label, "new");
  document.emit("tg:event", {detail: {type: "deleted", chat_id: -100, topic_id: null}});
  await advance(500);
  assert.equal(calls, 3, "unknown-topic deletions refresh visible cards");
}

async function testHistoryPrefetch() {
  const winEvents = listeners();
  const docEvents = listeners();
  const elements = new Map();
  const container = element();
  container.querySelectorAll = selector => selector === "[data-oldest-id]"
    ? [{dataset: {oldestId: "13"}}] : [];
  elements.set("messages-container", container);
  const document = {
    ...docEvents,
    documentElement: {scrollHeight: 2000},
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, element());
      return elements.get(id);
    },
    querySelector() { return null; },
    querySelectorAll() { return []; },
  };
  const requests = [];
  const context = {
    ...winEvents, document, console, setTimeout, clearTimeout,
    TG_CHAT: {chatId: 42, topicId: null},
    renderLocalTimes() {}, parseAppleEmojis() {}, scrollTo() {}, scrollBy() {},
    innerHeight: 600, scrollY: 0,
    requestAnimationFrame(callback) { callback(); },
    async fetch(url) { requests.push(url); return {ok: true, headers: {get() { return "application/json"; }}, async json() { return {count: 0}; }}; },
  };
  context.window = context;
  vm.createContext(context);
  vm.runInContext(fs.readFileSync("static/js/chat.js", "utf8"), context);
  assert.deepEqual(requests, ["/api/chat/42/history?offset_id=13"]);
  await vm.runInContext("loadOlderMessages()", context);
  assert.equal(requests.length, 1, "scroll consumes the already requested history");
}

async function testReadingPositionAndNewMessageNotice() {
  const winEvents = listeners();
  const docEvents = listeners();
  const elements = new Map();
  const store = new Map();
  const storage = {
    getItem: key => store.get(key) || null,
    setItem: (key, value) => store.set(key, value),
  };
  const row = {id: "msg-13", dataset: {oldestId: "13"}, getBoundingClientRect: () => ({top: 120, bottom: 200})};
  const container = element();
  container.querySelectorAll = selector => selector === "[data-oldest-id]" ? [row] : [];
  container.contains = node => node === row;
  container.prepend = () => {};
  elements.set("messages-container", container);
  const jump = element();
  jump.hidden = true;
  elements.set("jump-to-latest", jump);
  const document = {
    ...docEvents,
    documentElement: {scrollHeight: 2000},
    getElementById(id) {
      if (id === "msg-13") return row;
      if (id.startsWith("msg-")) return null;
      if (!elements.has(id)) elements.set(id, element());
      return elements.get(id);
    },
    createElement() {
      let child = null;
      let reads = 0;
      return {
        set innerHTML(_) { child = {}; },
        get lastChild() { const current = child; if (++reads % 2 === 0) child = null; return current; },
      };
    },
    querySelector() { return null; },
    querySelectorAll() { return []; },
  };
  const positions = [];
  const context = {
    ...winEvents, document, console, setTimeout, clearTimeout, sessionStorage: storage,
    TG_CHAT: {chatId: 42, topicId: null, accountId: "99"},
    renderLocalTimes() {}, parseAppleEmojis() {},
    scrollTo(_x, y) { positions.push(y); },
    scrollBy(_x, y) { positions.push(y); },
    innerHeight: 600, scrollY: 500,
    requestAnimationFrame(callback) { callback(); },
    async fetch() { return {ok: true, headers: {get() { return "application/json"; }}, async json() { return {count: 0}; }}; },
  };
  context.window = context;
  vm.createContext(context);
  vm.runInContext(fs.readFileSync("static/js/chat.js", "utf8"), context);
  positions.length = 0;
  for (const id of [1, 2]) {
    document.emit("tg:event", {detail: {type: "message", chat_id: 42, topic_id: null, scope_known: true, message_model: {id}, rendered_html: "message"}});
  }
  assert.equal(jump.hidden, false);
  assert.match(jump.textContent, /2 new messages/);
  assert.deepEqual(positions, [], "updates while reading older messages do not scroll");
  jump.emit("click", {});
  assert.equal(jump.hidden, true);
  assert.equal(positions.pop(), 2000, "jump returns to the latest messages");
  context.emit("pagehide", {});
  assert.ok(store.size > 0, "the visible message anchor is stored for this account");

  positions.length = 0;
  vm.runInContext("restoreReadingPosition()", context);
  assert.equal(positions.pop(), 0, "the saved message is restored at its original offset");
}

async function testFirstUnreadDoesNotOverrideUserMovement() {
  async function run(moveBeforeResponse) {
    const winEvents = listeners(), docEvents = listeners();
    const elements = new Map();
    let resolvePosition, scrolls = 0, marker = null;
    const row = {
      id: "msg-13", dataset: {oldestId: "13"},
      scrollIntoView() { scrolls++; },
      after(value) { marker = value; },
      getBoundingClientRect: () => ({top: 100, bottom: 200}),
    };
    const container = element();
    container.contains = node => node === row;
    container.querySelectorAll = () => [row];
    elements.set("messages-container", container);
    const document = {
      ...docEvents, documentElement: {scrollHeight: 2000},
      getElementById(id) {
        if (id === "msg-13") return row;
        if (!elements.has(id)) elements.set(id, element());
        return elements.get(id);
      },
      createElement() { return {...element(), scrollIntoView() { scrolls++; }}; },
      querySelector() { return null; }, querySelectorAll() { return []; },
    };
    const context = {
      ...winEvents, document, console, setTimeout, clearTimeout,
      sessionStorage: {getItem: () => null, setItem() {}},
      TG_CHAT: {chatId: 42, topicId: 7, accountId: "99"},
      renderLocalTimes() {}, parseAppleEmojis() {}, scrollTo() {}, scrollBy() {},
      innerHeight: 600, scrollY: 1400,
      requestAnimationFrame(callback) { callback(); },
      fetch(url) {
        if (url.includes("first-unread")) return new Promise(resolve => { resolvePosition = resolve; });
        return Promise.resolve({ok: true, headers: {get() { return "application/json"; }}, async json() { return {count: 0}; }});
      },
    };
    context.window = context;
    vm.createContext(context);
    vm.runInContext(fs.readFileSync("static/js/chat.js", "utf8"), context);
    if (moveBeforeResponse) context.emit("wheel", {});
    assert.ok(resolvePosition, "first visit asks for an authoritative unread position");
    resolvePosition({ok: true, headers: {get() { return "application/json"; }}, async json() { return {message_id: 13}; }});
    for (let i = 0; i < 8; i++) await Promise.resolve();
    assert.ok(marker, "the first unread message receives a marker");
    assert.equal(scrolls, moveBeforeResponse ? 0 : 1);
  }
  await run(false);
  await run(true);
}

function readingWindowFixture(saved = null, {deferOlder = false, controlledTimers = false, hash = "", mobile = false} = {}) {
  const winEvents = listeners(), docEvents = listeners();
  const elements = new Map(), requests = [], positions = [];
  const timers = new Map();
  let nextTimer = 0;
  let resolveWindow, resolveOlder, markerScrolls = 0;

  function reply(owner, href) {
    return {
      getAttribute(name) { return name === "href" ? href : null; },
      replaceWith(node) { owner.reply = node; },
    };
  }
  function row(id) {
    return {
      ...element(),
      id: `msg-${id}`,
      dataset: {id: String(id), oldestId: String(id)},
      getBoundingClientRect: () => ({top: 120, bottom: 200}),
      focus() { positions.push(["focus", id]); },
      scrollIntoView() { positions.push(["target", id]); },
      contains(node) { return node === this; },
      querySelector(selector) {
        if (selector === ".album-item" && this.isAlbum) return this.parts?.[0] || {};
        if (selector === ".reply-indicator") return this.reply || null;
        if (selector === ".text") return this.caption || null;
        if (selector === ".debug-process") return this.debug || null;
        const match = selector.match(/^\[data-message-id="(\d+)"\]$/);
        if (match) return this.parts?.find(part => part.dataset.messageId === match[1]) || null;
        return null;
      },
      querySelectorAll(selector) {
        if (selector === ".album-item") return this.parts || [];
        const match = selector.match(/^\[data-msg-id="(\d+)"\]$/);
        return match ? (this.buttons || []).filter(button => button.dataset.msgId === match[1]) : [];
      },
      remove() {
        if (this.parent) this.parent.children.splice(this.parent.children.indexOf(this), 1);
        this.parent = null;
      },
      replaceWith(node) {
        const parent = this.parent;
        parent.children[parent.children.indexOf(this)] = node;
        node.parent = parent;
        this.parent = null;
      },
      after(node) { this.parent.children.splice(this.parent.children.indexOf(this) + 1, 0, node); node.parent = this.parent; },
    };
  }
  function fragment() {
    const value = {...element(), children: []};
    Object.defineProperties(value, {
      innerHTML: {set(html) {
        this.children = html ? html.split(",").map(value => {
          const [base, groupId, parts] = value.split("|");
          const [id, replyId] = base.split(":");
          const child = row(Number(id));
          if (replyId) child.reply = reply(child, `#msg-${replyId}`);
          if (groupId) {
            child.isAlbum = true;
            child.dataset.groupedId = groupId;
            child.dataset.oldestId = String(Math.min(...parts.split(".").map(Number)));
            child.parts = parts.split(".").map(id => ({
              ...element(), dataset: {messageId: id}, closest() { return child; },
            }));
          }
          return child;
        }) : [];
        for (const child of this.children) child.parent = this;
      }},
      childNodes: {get() { return this.children; }},
      firstChild: {get() { return this.children[0] || null; }},
      lastChild: {get() { return this.children.at(-1) || null; }},
    });
    value.querySelectorAll = selector => selector === "[data-grouped-id]"
      ? value.children.filter(child => child.dataset?.groupedId) : [];
    value.querySelector = selector => {
      const match = selector.match(/^\[data-id="(\d+)"\]$/);
      if (match) return value.children.find(child => child.dataset?.id === match[1]) || null;
      const part = selector.match(/^\[data-message-id="(\d+)"\]$/);
      return part ? value.children.flatMap(child => child.parts || []).find(item => item.dataset.messageId === part[1]) || null : null;
    };
    value.scrollIntoView = () => { markerScrolls++; };
    return value;
  }
  const container = {...element(), children: [row(200), row(199)]};
  for (const child of container.children) child.parent = container;
  container.contains = node => container.children.includes(node);
  container.querySelectorAll = selector => {
    if (selector === "[data-oldest-id]") return container.children.filter(child => child.dataset?.oldestId);
    if (selector === "[data-grouped-id]") return container.children.filter(child => child.dataset?.groupedId);
    return [];
  };
  container.querySelector = selector => {
    if (selector === "[data-id]") return container.children.find(child => child.dataset?.id) || null;
    const match = selector.match(/^\[data-id="(\d+)"\]$/);
    if (match) return container.children.find(child => child.dataset?.id === match[1]) || null;
    const partMatch = selector.match(/^\[data-message-id="(\d+)"\]$/);
    if (partMatch) {
      const owner = container.children.find(child => child.parts?.some(part => part.dataset.messageId === partMatch[1]));
      return owner ? {closest() { return owner; }} : null;
    }
    return null;
  };
  container.replaceChildren = (...children) => {
    container.children = children;
    for (const child of children) child.parent = container;
  };
  container.prepend = child => {
    if (child.parent) child.parent.children.splice(child.parent.children.indexOf(child), 1);
    child.parent = container;
    container.children.unshift(child);
  };
  container.insertBefore = (child, before) => {
    if (child.parent) child.parent.children.splice(child.parent.children.indexOf(child), 1);
    child.parent = container;
    const index = before ? container.children.indexOf(before) : container.children.length;
    container.children.splice(index, 0, child);
  };
  container.appendChild = child => container.insertBefore(child, null);
  Object.defineProperty(container, "innerHTML", {set(html) {
    const temp = fragment(); temp.innerHTML = html; this.replaceChildren(...temp.childNodes);
  }});
  elements.set("messages-container", container);
  elements.set("chat-controls", {...element(), open: true});
  const document = {
    ...docEvents, documentElement: {scrollHeight: 2000},
    getElementById(id) {
      if (id.startsWith("msg-")) return container.children.find(child => child.id === id) || null;
      if (!elements.has(id)) elements.set(id, element());
      return elements.get(id);
    },
    createElement: fragment,
    querySelector() { return null; }, querySelectorAll() { return []; },
  };
  const response = (data, status = 200) => ({
    ok: status === 200, status, headers: {get() { return "application/json"; }},
    async json() { return data; },
  });
  const context = {
    ...winEvents, document, console, AbortController,
    setTimeout: controlledTimers ? (callback, delay) => { const id = ++nextTimer; timers.set(id, {callback, delay}); return id; } : setTimeout,
    clearTimeout: controlledTimers ? id => timers.delete(id) : clearTimeout,
    sessionStorage: {getItem: () => saved && JSON.stringify(saved), setItem() {}},
    TG_CHAT: {chatId: 42, topicId: 7, accountId: "99"},
    TG: {escapeHtml: value => String(value ?? "")},
    location: {hash},
    matchMedia: () => ({matches: mobile}),
    renderLocalTimes() {}, parseAppleEmojis() {}, innerHeight: 600, scrollY: 1400,
    scrollTo(_x, y) { positions.push(["to", y]); context.scrollY = Math.min(1400, y); },
    scrollBy(_x, y) { positions.push(["by", y]); context.scrollY += y; },
    requestAnimationFrame(callback) { callback(); },
    fetch(url, options) {
      requests.push({url, options});
      if (url.includes("first-unread")) return Promise.resolve(response({message_id: 100}));
      if (url.includes("around_id")) return new Promise(resolve => { resolveWindow = resolve; });
      if (url.includes("after_id")) return Promise.resolve(response({count: 2, html: "106,105"}));
      if (url.includes("offset_id=0")) return Promise.resolve(response({count: 2, html: "200,199"}));
      if (deferOlder && url.includes("offset_id=199")) return new Promise(resolve => { resolveOlder = resolve; });
      return Promise.resolve(response({count: 0, html: ""}));
    },
  };
  context.window = context;
  vm.createContext(context);
  vm.runInContext(fs.readFileSync("static/js/chat.js", "utf8"), context);
  return {
    context, document, container, elements, requests, positions, reply,
    resolveWindow: (data, status = 200) => resolveWindow(response(data, status)),
    resolveOlder: data => resolveOlder(response(data)),
    runTimers: delay => {
      for (const [id, timer] of [...timers]) if (timer.delay <= delay) { timers.delete(id); timer.callback(); }
    },
    markerScrolls: () => markerScrolls,
  };
}

async function settleReadingWindow() {
  for (let i = 0; i < 16; i++) await Promise.resolve();
}

async function testMobileControlsStartCollapsedWithoutChangingDesktop() {
  const mobile = readingWindowFixture({id: "msg-200", top: 120}, {mobile: true});
  const desktop = readingWindowFixture({id: "msg-200", top: 120});
  await settleReadingWindow();
  assert.equal(mobile.elements.get("chat-controls").open, false);
  assert.equal(desktop.elements.get("chat-controls").open, true);
}

async function testRetryMediaKeepsFocusAndRetriesOnlyTheRequestedItem() {
  const fixture = readingWindowFixture({id: "msg-200", top: 120});
  await settleReadingWindow();
  fixture.context.URL = URL;
  fixture.context.location.href = "https://example.test/chat/42";
  let focused = false, cleared = false;
  const open = {focus() { focused = true; }};
  const image = {getAttribute: () => "/media/42/200", closest: () => open};
  const frame = {
    querySelector: selector => selector === "img.lightbox-trigger" ? image : null,
    classList: {remove: name => { cleared = name === "media-failed"; }},
  };
  const retry = {closest: () => frame};
  fixture.document.activeElement = retry;
  fixture.context.retryMedia(retry);
  assert.equal(focused, true);
  assert.equal(cleared, true);
  assert.equal(new URL(image.src).pathname, "/media/42/200");
  assert.ok(new URL(image.src).searchParams.has("retry"));
  assert.ok(fixture.requests.every(request => !request.options?.method || request.options.method === "GET"));

  let loads = 0;
  frame.querySelector = selector => selector === "video" ? {load() { loads++; }, focus() {}} : null;
  fixture.context.retryMedia(retry);
  assert.equal(loads, 1);
}

async function testUnloadedFirstUnreadAndNewerNavigation() {
  const fixture = readingWindowFixture();
  await settleReadingWindow();
  assert.ok(fixture.requests.some(request => request.url === "/api/chat/42/history?around_id=100&topic_id=7"));
  fixture.resolveWindow({count: 3, html: "104,100,99"});
  await settleReadingWindow();
  assert.deepEqual(fixture.container.children.filter(child => child.id).map(child => child.id), ["msg-104", "msg-100", "msg-99"]);
  assert.equal(fixture.markerScrolls(), 1);
  const newer = fixture.elements.get("load-newer-messages");
  assert.equal(newer.hidden, false);
  await Promise.all(newer.emit("click", {}));
  assert.ok(fixture.requests.some(request => request.url === "/api/chat/42/history?after_id=104&topic_id=7"));
  assert.deepEqual(fixture.container.children.filter(child => child.id).map(child => child.id), ["msg-106", "msg-105", "msg-104", "msg-100", "msg-99"]);

  fixture.document.emit("tg:event", {detail: {type: "message", chat_id: 42, topic_id: 7, scope_known: true, message_model: {id: 201}, rendered_html: "201"}});
  assert.equal(fixture.container.children.some(child => child.id === "msg-201"), false, "live messages do not create a hidden history gap");
  const jump = fixture.elements.get("jump-to-latest");
  assert.match(jump.textContent, /1 new message/);
  fixture.document.body = {};
  fixture.document.activeElement = jump;
  let disabled = false;
  Object.defineProperty(jump, "disabled", {
    get() { return disabled; },
    set(value) {
      disabled = value;
      if (value && fixture.document.activeElement === jump) fixture.document.activeElement = fixture.document.body;
    },
  });
  await Promise.all(jump.emit("click", {}));
  assert.deepEqual(fixture.container.children.map(child => child.id), ["msg-200", "msg-199"]);
  assert.equal(newer.hidden, true);
  assert.ok(fixture.positions.some(([kind, id]) => kind === "focus" && id === 200));
  assert.ok(fixture.requests.every(request => !request.options || !request.options.method || request.options.method === "GET"));
}

async function testUnloadedSavedPositionAndUserMovement() {
  const fixture = readingWindowFixture({id: "msg-100", top: 45});
  await settleReadingWindow();
  assert.equal(fixture.requests.some(request => request.url.includes("first-unread")), false);
  fixture.resolveWindow({count: 3, html: "104,100,99"});
  await settleReadingWindow();
  assert.ok(fixture.positions.some(([kind, y]) => kind === "by" && y === 75));

  const moved = readingWindowFixture({id: "msg-100", top: 45});
  await settleReadingWindow();
  moved.context.emit("wheel", {});
  moved.resolveWindow({count: 3, html: "104,100,99"});
  await settleReadingWindow();
  assert.deepEqual(moved.container.children.map(child => child.id), ["msg-200", "msg-199"]);
  assert.equal(moved.positions.some(([kind]) => kind === "by"), false);

  const layoutShift = readingWindowFixture({id: "msg-100", top: 45});
  await settleReadingWindow();
  layoutShift.context.scrollY += 48;
  layoutShift.resolveWindow({count: 3, html: "104,100,99"});
  await settleReadingWindow();
  assert.ok(layoutShift.container.children.some(child => child.id === "msg-100"),
    "a layout-induced scroll change is not mistaken for user movement");
}

async function testReadingWindowRetriesChangesAndDiscardsOldHistoryRequests() {
  const changed = readingWindowFixture();
  await settleReadingWindow();
  changed.document.emit("tg:event", {detail: {type: "message", chat_id: 42, topic_id: 7, scope_known: true, message_model: {id: 201}, rendered_html: "201"}});
  changed.resolveWindow({count: 3, html: "104,100,99"});
  await settleReadingWindow();
  assert.equal(changed.requests.filter(request => request.url.includes("around_id=100")).length, 2);
  changed.resolveWindow({count: 3, html: "104,100,99"});
  await settleReadingWindow();
  assert.ok(changed.container.children.some(child => child.id === "msg-100"));

  const pending = readingWindowFixture({id: "msg-100", top: 45}, {deferOlder: true});
  const oldPage = vm.runInContext("loadOlderMessages()", pending.context);
  await settleReadingWindow();
  pending.resolveWindow({count: 3, html: "104,100,99"});
  await settleReadingWindow();
  pending.resolveOlder({count: 1, html: "198"});
  await oldPage;
  assert.deepEqual(pending.container.children.map(child => child.id), ["msg-104", "msg-100", "msg-99"]);
  assert.equal(pending.elements.get("loading-indicator").hidden, true);
}

async function testUnavailableReadingWindowKeepsExistingHistory() {
  const fixture = readingWindowFixture({id: "msg-100", top: 45});
  fixture.context.console = {...console, error() {}};
  await settleReadingWindow();
  fixture.resolveWindow({error: "Message unavailable"}, 404);
  await settleReadingWindow();
  assert.deepEqual(fixture.container.children.map(child => child.id), ["msg-200", "msg-199"]);
  assert.match(fixture.elements.get("reading-position-status").textContent, /unavailable/);
}

async function testLiveMessageEditReplacesTheCompleteRow() {
  const fixture = readingWindowFixture({id: "msg-200", top: 120});
  await settleReadingWindow();
  const oldRow = fixture.container.children[0];
  fixture.document.emit("tg:event", {detail: {
    type: "edited", chat_id: 42, topic_id: 7, scope_known: true,
    message_model: {id: 200, text: "updated", has_media: true}, rendered_html: "200",
  }});
  assert.notEqual(fixture.container.children[0], oldRow, "the server-rendered row replaces stale media and metadata");
  assert.equal(fixture.container.children[0].id, "msg-200");
  assert.equal(fixture.container.children[1].id, "msg-199");

}

async function testLiveEditPreservesOnlyAnUnchangedUnresolvedReplyPreview() {
  const fixture = readingWindowFixture({id: "msg-200", top: 120});
  await settleReadingWindow();
  const oldRow = fixture.container.children[0];
  const originalReply = fixture.reply(oldRow, "#msg-198");
  oldRow.reply = originalReply;
  const edit = (target, replyText) => fixture.document.emit("tg:event", {detail: {
    type: "edited", chat_id: 42, topic_id: 7, scope_known: true,
    message_model: {id: 200, reply_to_msg_id: target, reply_to_text: replyText},
    rendered_html: `200:${target}`,
  }});
  edit(198, null);
  assert.equal(fixture.container.children[0].reply, originalReply);
  edit(197, null);
  assert.notEqual(fixture.container.children[0].reply, originalReply, "a changed reply target must not keep the old preview");
  const changedReply = fixture.container.children[0].reply;
  edit(197, "fresh preview");
  assert.notEqual(fixture.container.children[0].reply, changedReply, "a fresh preview supersedes the old one");
}

async function testAlbumEventsRefreshTogetherAndDiscardStaleResults() {
  const fixture = readingWindowFixture({id: "msg-200", top: 120}, {controlledTimers: true});
  await settleReadingWindow();
  const album = fixture.container.children[0];
  album.isAlbum = true;
  album.dataset.groupedId = "9223372036854775807";
  album.dataset.oldestId = "197";
  album.parts = [197, 198, 200].map(id => ({...element(), dataset: {messageId: String(id)}}));
  const event = (type, id) => fixture.document.emit("tg:event", {detail: {
    type, chat_id: 42, topic_id: 7, scope_known: true,
    message_model: {id, grouped_id: "9223372036854775807"}, rendered_html: String(id),
  }});

  event("message", 198);
  assert.equal(fixture.container.children.length, 2, "a repeated album member is not inserted twice");
  event("message", 201);
  event("message", 201);
  assert.equal(fixture.requests.filter(request => request.url.includes("around_id")).length, 0, "bursts are coalesced");
  fixture.runTimers(200);
  await settleReadingWindow();
  assert.equal(fixture.requests.filter(request => request.url.includes("around_id=201")).length, 1);

  event("edited", 198);
  fixture.resolveWindow({count: 1, html: "201|9223372036854775807|197.198.200.201"});
  await settleReadingWindow();
  assert.equal(fixture.container.children[0], album, "an older response cannot overwrite a later edit");
  fixture.runTimers(200);
  await settleReadingWindow();
  assert.equal(fixture.requests.filter(request => request.url.includes("around_id=198")).length, 1);
  fixture.resolveWindow({count: 1, html: "201|9223372036854775807|197.198.200.201"});
  await settleReadingWindow();
  assert.deepEqual(fixture.container.children.map(child => child.id), ["msg-201", "msg-199"]);
  assert.deepEqual(fixture.container.children[0].parts.map(part => part.dataset.messageId), ["197", "198", "200", "201"]);
  assert.equal(fixture.requests.filter(request => request.url.includes("around_id")).length, 2);
}

async function testAlbumDeletionRefreshesSurvivorsAndDoesNotResurrectOldParts() {
  const fixture = readingWindowFixture({id: "msg-200", top: 120}, {controlledTimers: true});
  await settleReadingWindow();
  const album = fixture.container.children[0];
  album.isAlbum = true;
  album.dataset.groupedId = "8";
  album.dataset.oldestId = "197";
  album.parts = [197, 198, 200].map(id => ({...element(), dataset: {messageId: String(id)}}));
  album.caption = element();
  album.buttons = [{dataset: {msgId: "200"}, disabled: false}];
  album.debug = {disabled: false};
  album.contains = node => node === album || album.buttons.includes(node);
  const deleted = id => fixture.document.emit("tg:event", {detail: {
    type: "deleted", chat_id: 42, topic_id: null, scope_known: true, message_model: {id},
  }});

  deleted(198);
  assert.equal(album.parts[1].textContent, "Deleted message");
  fixture.runTimers(200);
  await settleReadingWindow();
  fixture.document.activeElement = album.buttons[0];
  deleted(200);
  assert.equal(album.buttons[0].disabled, true);
  assert.ok(fixture.positions.some(([kind, id]) => kind === "focus" && id === 200));
  fixture.resolveWindow({count: 1, html: "200|8|197.198.200"});
  await settleReadingWindow();
  assert.equal(fixture.container.children[0], album, "a stale result cannot resurrect deleted members");
  fixture.runTimers(200);
  await settleReadingWindow();
  fixture.resolveWindow({count: 1, html: "197|8|197"});
  await settleReadingWindow();
  assert.equal(fixture.container.children[1].id, "msg-197");
  assert.deepEqual(fixture.container.children[1].parts.map(part => part.dataset.messageId), ["197"]);
  deleted(197);
  assert.equal(fixture.container.children.some(child => child.dataset.groupedId === "8"), false);
}

async function testAlbumRefreshFailureLeavesAnHonestReloadNotice() {
  const fixture = readingWindowFixture({id: "msg-200", top: 120}, {controlledTimers: true});
  fixture.context.console = {...console, error() {}};
  await settleReadingWindow();
  const album = fixture.container.children[0];
  album.isAlbum = true;
  album.dataset.groupedId = "8";
  album.parts = [198, 200].map(id => ({...element(), dataset: {messageId: String(id)}}));
  fixture.document.emit("tg:event", {detail: {
    type: "edited", chat_id: 42, topic_id: 7, scope_known: true,
    message_model: {id: 198, grouped_id: "8"}, rendered_html: "198",
  }});
  fixture.runTimers(200);
  await settleReadingWindow();
  fixture.resolveWindow({error: "unavailable"}, 503);
  await settleReadingWindow();
  assert.equal(fixture.container.children[0], album);
  assert.match(fixture.elements.get("reading-position-status").textContent, /reload/i);
}

async function testInterleavedAlbumUsesTheOldestMemberAsHistoryCursor() {
  const fixture = readingWindowFixture({id: "msg-200", top: 120}, {controlledTimers: true});
  await settleReadingWindow();
  const album = fixture.container.children[0];
  album.isAlbum = true;
  album.dataset.groupedId = "8";
  album.dataset.oldestId = "197";
  album.parts = [197, 200].map(id => ({...element(), dataset: {messageId: String(id)}}));
  await vm.runInContext("loadOlderMessages()", fixture.context);
  assert.ok(fixture.requests.some(request => request.url.includes("offset_id=197")));
  assert.equal(fixture.container.children[1].id, "msg-199");
}

async function testAlbumsSplitAcrossPagesMergeWithoutLosingOtherRows() {
  const fixture = readingWindowFixture({id: "msg-200", top: 120}, {controlledTimers: true});
  await settleReadingWindow();
  const album = fixture.container.children[0];
  album.isAlbum = true;
  album.dataset.groupedId = "8";
  album.dataset.oldestId = "198";
  album.parts = [198, 200].map(id => ({...element(), dataset: {messageId: String(id)}}));
  const fetch = fixture.context.fetch;
  fixture.context.fetch = (url, options) => url.includes("offset_id=198")
    ? Promise.resolve({ok: true, status: 200, headers: {get: () => "application/json"},
      json: async () => ({count: 2, html: "197|8|197,196"})})
    : fetch(url, options);
  await vm.runInContext("loadOlderMessages()", fixture.context);
  assert.deepEqual(fixture.container.children.map(child => child.id), ["msg-200", "msg-199", "msg-197", "msg-196"]);
  fixture.runTimers(200);
  await settleReadingWindow();
  fixture.resolveWindow({count: 3, html: "200|8|197.198.200,199,196"});
  await settleReadingWindow();
  assert.deepEqual(fixture.container.children.map(child => child.id), ["msg-200", "msg-199", "msg-196"]);
  assert.deepEqual(fixture.container.children[0].parts.map(part => part.dataset.messageId), ["197", "198", "200"]);
  assert.ok(fixture.requests.every(request => !request.options?.method || request.options.method === "GET"));
}

async function testAlbumRefreshRejectsMissingAndOutOfWindowMembers() {
  const fixture = readingWindowFixture({id: "msg-200", top: 120}, {controlledTimers: true});
  fixture.context.console = {...console, error() {}};
  await settleReadingWindow();
  const album = fixture.container.children[0];
  album.isAlbum = true;
  album.dataset.groupedId = "8";
  album.dataset.oldestId = "198";
  album.parts = [198, 200].map(id => ({...element(), dataset: {messageId: String(id)}}));
  const edit = () => fixture.document.emit("tg:event", {detail: {
    type: "edited", chat_id: 42, topic_id: 7, scope_known: true,
    message_model: {id: 198, grouped_id: "8"}, rendered_html: "198",
  }});
  edit();
  fixture.runTimers(200);
  await settleReadingWindow();
  fixture.resolveWindow({count: 1, html: "200|8|198"});
  await settleReadingWindow();
  assert.equal(fixture.container.children[0], album, "a missing known member is not silently removed");
  edit();
  fixture.runTimers(200);
  await settleReadingWindow();
  fixture.resolveWindow({count: 1, html: "200|8|180.198.200"});
  await settleReadingWindow();
  assert.equal(fixture.container.children[0], album, "an unknown part outside the window cannot skip history");
  assert.match(fixture.elements.get("reading-position-status").textContent, /reload/i);
}

async function testDeletedAlbumPartsStayDeletedAfterARefreshFailure() {
  const fixture = readingWindowFixture({id: "msg-200", top: 120}, {controlledTimers: true});
  fixture.context.console = {...console, error() {}};
  await settleReadingWindow();
  const album = fixture.container.children[0];
  album.isAlbum = true;
  album.dataset.groupedId = "8";
  album.dataset.oldestId = "198";
  album.parts = [198, 200].map(id => ({...element(), dataset: {messageId: String(id)}}));
  fixture.document.emit("tg:event", {detail: {
    type: "deleted", chat_id: 42, topic_id: null, scope_known: true, message_model: {id: 198},
  }});
  fixture.runTimers(200);
  await settleReadingWindow();
  fixture.resolveWindow({error: "unavailable"}, 503);
  await settleReadingWindow();
  fixture.document.emit("tg:event", {detail: {
    type: "edited", chat_id: 42, topic_id: 7, scope_known: true,
    message_model: {id: 200, grouped_id: "8"}, rendered_html: "200",
  }});
  fixture.runTimers(200);
  await settleReadingWindow();
  fixture.resolveWindow({count: 1, html: "200|8|198.200"});
  await settleReadingWindow();
  assert.equal(fixture.container.children[0], album);
  assert.equal(album.parts[0].dataset.deleted, "true");
  assert.match(fixture.elements.get("reading-position-status").textContent, /reload/i);
}

async function testReactionUpdatesInvalidateAnInFlightAlbumRefresh() {
  const fixture = readingWindowFixture({id: "msg-200", top: 120}, {controlledTimers: true});
  await settleReadingWindow();
  const album = fixture.container.children[0];
  album.isAlbum = true;
  album.dataset.groupedId = "8";
  album.dataset.oldestId = "198";
  album.parts = [198, 200].map(id => ({...element(), dataset: {messageId: String(id)}}));
  fixture.document.emit("tg:event", {detail: {
    type: "edited", chat_id: 42, topic_id: 7, scope_known: true,
    message_model: {id: 198, grouped_id: "8"}, rendered_html: "198",
  }});
  fixture.runTimers(200);
  await settleReadingWindow();
  const reactions = fixture.document.getElementById("reactions-198");
  reactions.dataset.albumIndex = "1";
  fixture.document.emit("tg:event", {detail: {
    type: "reaction_update", chat_id: 42, topic_id: null, scope_known: true,
    message_model: {id: 198, reactions: [{emoji: "👍", count: 2, is_chosen: false}]},
  }});
  assert.match(reactions.innerHTML, /data-msg-id="198"/);
  assert.match(reactions.innerHTML, /React to album item 1 with 👍/);
  fixture.resolveWindow({count: 1, html: "200|8|198.200"});
  await settleReadingWindow();
  assert.equal(fixture.container.children[0], album, "an old response must not erase a newer reaction");
  fixture.runTimers(200);
  await settleReadingWindow();
  fixture.resolveWindow({count: 1, html: "200|8|198.200"});
  await settleReadingWindow();
  assert.notEqual(fixture.container.children[0], album);
}

async function testDetachedAlbumDoesNotJumpAcrossUnloadedHistory() {
  const fixture = readingWindowFixture({id: "msg-100", top: 45}, {controlledTimers: true});
  await settleReadingWindow();
  fixture.resolveWindow({count: 2, html: "104|8|100.104,99"});
  await settleReadingWindow();
  fixture.document.emit("tg:event", {detail: {
    type: "message", chat_id: 42, topic_id: 7, scope_known: true,
    message_model: {id: 105, grouped_id: "8"}, rendered_html: "105",
  }});
  fixture.runTimers(200);
  await settleReadingWindow();
  assert.equal(fixture.requests.filter(request => request.url.includes("around_id")).length, 1);
  assert.match(fixture.elements.get("jump-to-latest").textContent, /1 new message/);
  assert.deepEqual(fixture.container.children[0].parts.map(part => part.dataset.messageId), ["100", "104"]);
}

async function testReplyJumpLoadsAnUnseenTargetAndReturnsToPreviousPosition() {
  const fixture = readingWindowFixture({id: "msg-200", top: 120}, {controlledTimers: true});
  await settleReadingWindow();
  const reply = {getAttribute: name => name === "href" ? "#msg-100" : null};
  let prevented = false;
  fixture.container.emit("click", {button: 0, target: {
    closest(selector) { return selector === "a.reply-indicator" ? reply : null; },
  }, preventDefault() { prevented = true; }});
  await settleReadingWindow();
  assert.equal(prevented, true);
  assert.ok(fixture.requests.some(request => request.url === "/api/chat/42/history?around_id=100&topic_id=7"));
  fixture.resolveWindow({count: 3, html: "104,100,99"});
  await settleReadingWindow();
  assert.ok(fixture.positions.some(([kind, id]) => kind === "target" && id === 100));
  assert.ok(fixture.positions.some(([kind, id]) => kind === "focus" && id === 100));
  const back = fixture.elements.get("return-to-reading");
  assert.equal(back.hidden, false);
  const returning = Promise.all(back.emit("click", {}));
  await settleReadingWindow();
  assert.ok(fixture.requests.some(request => request.url.includes("around_id=200")));
  fixture.resolveWindow({count: 2, html: "200,199"});
  await returning;
  assert.deepEqual(fixture.container.children.map(child => child.id), ["msg-200", "msg-199"]);
  assert.equal(back.hidden, true);
  assert.ok(fixture.requests.every(request => !request.options?.method || request.options.method === "GET"));
}

async function testIncomingMessageLinkOverridesSavedPositionAndHandlesMissingTarget() {
  const fixture = readingWindowFixture({id: "msg-200", top: 120}, {controlledTimers: true, hash: "#msg-100"});
  fixture.context.console = {...console, error() {}};
  await settleReadingWindow();
  assert.ok(fixture.requests.some(request => request.url.includes("around_id=100")));
  assert.equal(fixture.requests.some(request => request.url.includes("first-unread")), false);
  fixture.resolveWindow({error: "unavailable"}, 404);
  await settleReadingWindow();
  assert.deepEqual(fixture.container.children.map(child => child.id), ["msg-200", "msg-199"]);
  assert.match(fixture.elements.get("reading-position-status").textContent, /message.*unavailable/i);
}

async function testReplyJumpDoesNotOverrideNewerUserMovement() {
  const fixture = readingWindowFixture({id: "msg-200", top: 120}, {controlledTimers: true});
  await settleReadingWindow();
  const reply = {getAttribute: () => "#msg-100"};
  fixture.container.emit("click", {button: 0, target: {
    closest(selector) { return selector === "a.reply-indicator" ? reply : null; },
  }, preventDefault() {}});
  await settleReadingWindow();
  fixture.context.emit("wheel", {});
  fixture.resolveWindow({count: 3, html: "104,100,99"});
  await settleReadingWindow();
  assert.deepEqual(fixture.container.children.map(child => child.id), ["msg-200", "msg-199"]);
  assert.equal(fixture.positions.some(([kind, id]) => kind === "target" && id === 100), false);
}

function settingsFixture(dialogOpen = false) {
  const events = listeners(), requests = [];
  const body = {};
  const document = {...events, body, activeElement: body};
  const status = element(), modalStatus = element(), checkbox = element(), save = element(), input = element();
  checkbox.checked = true;
  input.value = "([";
  for (const control of [checkbox, save]) {
    let disabled = false;
    Object.defineProperty(control, "disabled", {
      get() { return disabled; },
      set(value) {
        disabled = value;
        if (value && document.activeElement === control) document.activeElement = body;
      },
    });
    control.focus = () => { document.activeElement = control; };
  }
  const dialog = {...element(), id: "modal-regex", open: dialogOpen};
  dialog.querySelector = selector => selector === ".modal-status" ? modalStatus : input;
  dialog.querySelectorAll = selector => selector === ".btn-save" ? [save] : [];
  dialog.showModal = () => { dialog.open = true; };
  dialog.close = () => { dialog.open = false; };
  input.focus = () => { document.activeElement = input; };
  const elements = {"settings-status": status, "t-polls": checkbox, "input-regex": input,
    "modal-regex": dialog, "rules-container": element()};
  document.getElementById = id => elements[id] || null;
  document.querySelector = selector => selector === "dialog.modal-overlay[open]" && dialog.open ? dialog : null;
  document.querySelectorAll = selector => selector === ".modal-overlay" ? [dialog] : [];
  let resolveFetch;
  const context = {
    document, console, setTimeout, TG: {escapeHtml: String},
    fetch(url, options) { requests.push({url, options}); return new Promise(resolve => { resolveFetch = resolve; }); },
  };
  context.window = context;
  vm.createContext(context);
  vm.runInContext(fs.readFileSync("static/js/settings.js", "utf8"), context);
  return {context, document, checkbox, save, input, dialog, status, modalStatus, requests,
    fail() { resolveFetch({ok: false, headers: {get: () => "application/json"}}); },
    succeed() { resolveFetch({ok: true, headers: {get: () => "application/json"}}); }};
}

async function testSettingsSaveDoesNotDiscardNewerDialogEdits() {
  const fixture = settingsFixture();
  fixture.context.openModal("modal-regex");
  const saving = fixture.context.saveField("autoread_regex", "input-regex");
  fixture.input.value = "newer text";
  fixture.succeed();
  await saving;
  assert.equal(fixture.dialog.open, true);
  assert.equal(fixture.input.value, "newer text");
  assert.match(fixture.modalStatus.textContent, /newer changes/i);

  fixture.input.value = "old value";
  const pending = fixture.context.saveField("autoread_regex", "input-regex");
  fixture.context.closeModal("modal-regex");
  fixture.context.openModal("modal-regex");
  fixture.input.value = "reopened value";
  fixture.succeed();
  await pending;
  assert.equal(fixture.dialog.open, true);
  assert.equal(fixture.input.value, "reopened value");
  assert.equal(fixture.modalStatus.textContent, "");
}

async function testFailedSettingsPreserveValuesAndFocus() {
  const toggle = settingsFixture();
  toggle.checkbox.focus();
  const changed = toggle.context.toggleSetting("autoread_polls");
  assert.equal(toggle.checkbox.disabled, true);
  toggle.fail();
  await changed;
  assert.equal(toggle.checkbox.checked, false);
  assert.equal(toggle.checkbox.disabled, false);
  assert.equal(toggle.document.activeElement, toggle.checkbox);
  assert.match(toggle.status.textContent, /could not save/i);
  assert.equal(toggle.requests.length, 1);

  const modal = settingsFixture(true);
  modal.save.focus();
  const saving = modal.context.saveField("autoread_regex", "input-regex");
  await modal.context.saveField("autoread_regex", "input-regex");
  assert.equal(modal.requests.length, 1, "repeated saves while pending are coalesced");
  modal.fail();
  await saving;
  assert.equal(modal.dialog.open, true);
  assert.equal(modal.input.value, "([");
  assert.equal(modal.document.activeElement, modal.save);
  assert.match(modal.modalStatus.textContent, /could not save/i);
}

async function testOldAutoreactSaveCannotCloseAReopenedDialog() {
  const fixture = readingWindowFixture({id: "msg-200", top: 120}, {controlledTimers: true});
  await settleReadingWindow();
  const dialog = fixture.elements.get("autoreact-modal");
  const opener = fixture.elements.get("btn-open-autoreact");
  const save = fixture.elements.get("save-ar-btn");
  dialog.open = false;
  dialog.showModal = () => { dialog.open = true; };
  dialog.close = () => { dialog.open = false; dialog.emit("close", {}); };
  dialog.querySelector = selector => selector === ".modal-close" ? {focus() {}} : null;
  opener.focus = () => {};
  let resolveSave, loads = 0;
  const response = data => ({ok: true, status: 200, headers: {get: () => "application/json"},
    json: async () => data});
  fixture.context.fetch = (url, options) => {
    if (url.includes("autoreact/get")) {
      loads++;
      return Promise.resolve(response({enabled: loads === 1, config: {emoji: "💩", target_users: []}}));
    }
    if (url.includes("autoreact/config")) return new Promise(resolve => { resolveSave = resolve; });
    throw Error(`Unexpected request: ${url}`);
  };
  opener.emit("click", {});
  await settleReadingWindow();
  assert.equal(save.disabled, false);
  const firstSave = Promise.all(save.emit("click", {}));
  fixture.context.closeArModal();
  opener.emit("click", {});
  await settleReadingWindow();
  resolveSave(response({status: "ok"}));
  await firstSave;
  fixture.runTimers(800);
  assert.equal(dialog.open, true);
  assert.equal(save.disabled, false);
  assert.equal(save.textContent, "Save Configuration");
}

(async () => {
  await testBackAndEvents();
  await testIndexRetainsRenderedCardsOnReturn();
  await testContinuousCardUpdatesAreBoundedAndPreserveFocus();
  await testOldCardResponseCannotOverwriteANewerEvent();
  await testForumTopicUpdatesAreBoundedAndDiscardOldResponses();
  await testHistoryPrefetch();
  await testReadingPositionAndNewMessageNotice();
  await testFirstUnreadDoesNotOverrideUserMovement();
  await testUnloadedFirstUnreadAndNewerNavigation();
  await testMobileControlsStartCollapsedWithoutChangingDesktop();
  await testRetryMediaKeepsFocusAndRetriesOnlyTheRequestedItem();
  await testUnloadedSavedPositionAndUserMovement();
  await testReadingWindowRetriesChangesAndDiscardsOldHistoryRequests();
  await testUnavailableReadingWindowKeepsExistingHistory();
  await testLiveMessageEditReplacesTheCompleteRow();
  await testLiveEditPreservesOnlyAnUnchangedUnresolvedReplyPreview();
  await testAlbumEventsRefreshTogetherAndDiscardStaleResults();
  await testAlbumDeletionRefreshesSurvivorsAndDoesNotResurrectOldParts();
  await testAlbumRefreshFailureLeavesAnHonestReloadNotice();
  await testInterleavedAlbumUsesTheOldestMemberAsHistoryCursor();
  await testAlbumsSplitAcrossPagesMergeWithoutLosingOtherRows();
  await testAlbumRefreshRejectsMissingAndOutOfWindowMembers();
  await testDeletedAlbumPartsStayDeletedAfterARefreshFailure();
  await testReactionUpdatesInvalidateAnInFlightAlbumRefresh();
  await testDetachedAlbumDoesNotJumpAcrossUnloadedHistory();
  await testReplyJumpLoadsAnUnseenTargetAndReturnsToPreviousPosition();
  await testIncomingMessageLinkOverridesSavedPositionAndHandlesMissingTarget();
  await testReplyJumpDoesNotOverrideNewerUserMovement();
  await testFailedSettingsPreserveValuesAndFocus();
  await testSettingsSaveDoesNotDiscardNewerDialogEdits();
  await testOldAutoreactSaveCannotCloseAReopenedDialog();
})().catch(error => { console.error(error); process.exitCode = 1; });
