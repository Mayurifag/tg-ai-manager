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
  };
}

function element() {
  return {
    ...listeners(),
    style: {}, dataset: {}, children: [], hidden: false,
    classList: { add() {}, remove() {}, contains() { return false; } },
    querySelectorAll() { return []; },
    querySelector() { return null; },
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
    ...winEvents, document, URL, console,
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
    async fetch(url) { requests.push(url); return {ok: true, async json() { return {count: 0}; }}; },
  };
  context.window = context;
  vm.createContext(context);
  vm.runInContext(fs.readFileSync("static/js/chat.js", "utf8"), context);
  assert.deepEqual(requests, ["/api/chat/42/history?offset_id=13"]);
  await vm.runInContext("loadOlderMessages()", context);
  assert.equal(requests.length, 1, "scroll consumes the already requested history");
}

(async () => {
  await testBackAndEvents();
  await testIndexRetainsRenderedCardsOnReturn();
  await testHistoryPrefetch();
})().catch(error => { console.error(error); process.exitCode = 1; });
