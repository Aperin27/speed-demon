const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const frontend = path.join(__dirname, "../frontend");
const flush = () => new Promise((resolve) => setImmediate(resolve));

function eventTarget() {
  const listeners = new Map();
  return {
    listeners,
    addEventListener(name, callback) {
      if (!listeners.has(name)) listeners.set(name, new Set());
      listeners.get(name).add(callback);
    },
    removeEventListener(name, callback) { listeners.get(name)?.delete(callback); },
    dispatch(name, details = {}) {
      return Promise.all([...listeners.get(name) || []].map((callback) => callback({
        type: name, preventDefault() {}, ...details
      })));
    }
  };
}

function createClock() {
  let now = 0;
  let id = 0;
  const scheduled = new Map();
  const schedule = (kind, callback, delay) => {
    scheduled.set(++id, { kind, callback, due: now + delay, delay });
    return id;
  };
  return {
    scheduled,
    now: () => now,
    setTimeout: (callback, delay = 0) => schedule("timeout", callback, Math.max(delay, 0)),
    setInterval: (callback, delay) => schedule("interval", callback, Math.max(delay, 1)),
    clearTimeout: (timer) => scheduled.delete(timer),
    clearInterval: (timer) => scheduled.delete(timer),
    requestAnimationFrame: (callback) => schedule("frame", callback, 16),
    cancelAnimationFrame: (timer) => scheduled.delete(timer),
    advance(milliseconds) {
      const end = now + milliseconds;
      let iterations = 0;
      while (true) {
        const entry = [...scheduled.entries()].sort((left, right) => left[1].due - right[1].due)[0];
        if (!entry || entry[1].due > end) break;
        assert.ok(++iterations < 50000, "Animation must not schedule an unbounded synchronous loop");
        now = entry[1].due;
        if (entry[1].kind === "interval") entry[1].due += entry[1].delay;
        else scheduled.delete(entry[0]);
        entry[1].callback(now);
      }
      now = end;
    }
  };
}

function createSession({ app = false, reduced = false, realLoading = false } = {}) {
  const html = fs.readFileSync(path.join(frontend, "index.html"), "utf8");
  const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map((match) => match[1]);
  const elements = new Map();
  const clock = createClock();
  const document = { ...eventTarget(), hidden: false, visibilityState: "visible", activeElement: null };
  function createElement(tag = "div") {
    const classes = new Set();
    const attributes = {};
    const style = {
      setProperty(name, value) { this[name] = String(value); },
      removeProperty(name) { delete this[name]; }
    };
    const result = {
      ...eventTarget(), tagName: tag.toUpperCase(), style, dataset: {}, children: [],
      value: "", disabled: false, open: false, hidden: false, isConnected: true,
      clientWidth: 1280, clientHeight: 900,
      get textContent() { return (this.text || "") + this.children.map((child) => child.textContent).join(""); },
      set textContent(value) { this.text = String(value); this.replaceChildren(); },
      get innerHTML() { return this.html || ""; },
      set innerHTML(value) { this.html = value; this.replaceChildren(); },
      get className() { return [...classes].join(" "); },
      set className(value) { classes.clear(); String(value).split(/\s+/).filter(Boolean).forEach((name) => classes.add(name)); },
      classList: {
        add: (...names) => names.forEach((name) => classes.add(name)),
        remove: (...names) => names.forEach((name) => classes.delete(name)),
        contains: (name) => classes.has(name),
        toggle(name, force) {
          const add = force === undefined ? !classes.has(name) : force;
          if (add) classes.add(name); else classes.delete(name);
          return add;
        }
      },
      setAttribute(name, value) {
        attributes[name] = String(value);
        if (name.startsWith("data-")) this.dataset[name.slice(5).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase())] = String(value);
      },
      getAttribute: (name) => attributes[name] ?? null,
      removeAttribute(name) { delete attributes[name]; },
      appendChild(child) { child.parentElement = this; this.children.push(child); return child; },
      append(...children) { children.forEach((child) => this.appendChild(child)); },
      replaceChildren(...children) {
        this.children.forEach((child) => { child.parentElement = null; });
        this.children = [];
        this.append(...children);
      },
      remove() {
        if (this.parentElement) this.parentElement.children = this.parentElement.children.filter((child) => child !== this);
        this.parentElement = null;
      },
      querySelector(selector) {
        if (selector === ".demon-facing") return this.facing ||= createElement("g");
        return this.children.find((child) => child.classList.contains(selector.slice(1))) || null;
      },
      getBoundingClientRect() {
        const top = this.id === "loading-hud" ? (elements.get("loading-scene")?.clientHeight || this.clientHeight) - 100 : 0;
        return { width: this.clientWidth, height: this.clientHeight, left: 0, top };
      },
      focus(options) { this.focusOptions = options; document.activeElement = this; },
      showModal() { this.open = true; },
      close() { this.open = false; }
    };
    return result;
  }
  document.createElement = createElement;
  document.getElementById = (id) => {
    assert.ok(ids.includes(id), `Loading binding must exist in HTML: ${id}`);
    if (!elements.has(id)) elements.set(id, Object.assign(createElement(), { id }));
    return elements.get(id);
  };
  const element = (id) => document.getElementById(id);
  const media = { ...eventTarget(), matches: reduced };
  const window = { ...eventTarget(), matchMedia: () => media, innerWidth: 1280, innerHeight: 900,
    SPEED_DEMON_CONFIG: { backendUrl: "http://127.0.0.1:8000" } };
  const sockets = [];
  const requests = [];
  let tokenProvider = async () => null;
  window.speedDemonAuth = { getToken: () => tokenProvider() };
  class FakeWebSocket {
    static OPEN = 1;
    constructor(url) { this.url = url; this.readyState = 0; this.sent = []; sockets.push(this); }
    send(message) { this.sent.push(JSON.parse(message)); }
    close() { this.readyState = 3; this.onclose?.(); }
  }
  let randomSeed = 0x12345678;
  const seededMath = Object.create(Math);
  seededMath.random = () => {
    randomSeed = (Math.imul(randomSeed, 1664525) + 1013904223) >>> 0;
    return randomSeed / 0x100000000;
  };
  const context = vm.createContext({
    document, window, WebSocket: FakeWebSocket, performance: { now: clock.now }, AbortController,
    Math: seededMath,
    setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout,
    setInterval: clock.setInterval, clearInterval: clock.clearInterval,
    requestAnimationFrame: clock.requestAnimationFrame, cancelAnimationFrame: clock.cancelAnimationFrame,
    fetch: async (url) => { requests.push(url); return { ok: true, json: async () => ({ entries: [], week: null }) }; }
  });
  const loader = { starts: [], finishes: [], failures: [], statuses: [], active: false,
    start(action, onCancel) { this.active = true; this.starts.push({ action, onCancel }); },
    finish(callback) { this.finishes.push(callback); },
    fail(message, retry, onReturn) { this.failures.push({ message, retry, onReturn }); },
    cancel() { this.active = false; },
    isActive() { return this.active; },
    setStatus(message) { this.statuses.push(message); }
  };
  if (app) {
    window.roomLoading = loader;
    if (realLoading) vm.runInContext(fs.readFileSync(path.join(frontend, "loading.js"), "utf8"), context);
    vm.runInContext(fs.readFileSync(path.join(frontend, "app.js"), "utf8"), context);
  } else {
    vm.runInContext(fs.readFileSync(path.join(frontend, "loading.js"), "utf8"), context);
  }
  return { context, window, document, media, element, clock, sockets, requests, loader,
    token(provider) { tokenProvider = provider; },
    read: (expression) => vm.runInContext(expression, context),
    receive(connection, message) { connection.onmessage({ data: JSON.stringify(message) }); }
  };
}

const lobby = { room_code: "AB12CD", players: [{ player_id: "host-id", nickname: "Host", authenticated: false }], host: "host-id" };

function assertCleanAnimation(session) {
  assert.equal(session.window.roomLoading.isActive(), false);
  assert.equal(session.element("room-loading").open, false);
  assert.equal(session.element("loading-numbers").children.length, 0);
  assert.equal(session.element("loading-cascade").children.length, 0, "Exiting removes the decorative cascade");
  assert.equal(session.clock.scheduled.size, 0, "Exiting removes every animation timer/frame");
  assert.equal(session.document.listeners.get("visibilitychange")?.size || 0, 0);
  assert.equal(session.window.listeners.get("resize")?.size || 0, 0);
  assert.equal(session.media.listeners.get("change")?.size || 0, 0);
}

function readTranslation(transform) {
  const match = /translate3d\(([-\d.]+)px,\s*([-\d.]+)px,/.exec(transform);
  assert.ok(match, `A hunting transform must contain finite viewport coordinates: ${transform}`);
  return { x: Number(match[1]), y: Number(match[2]) };
}

async function testHuntingAnimation() {
  const session = createSession();
  const { element, clock, window, document, media } = session;
  const loading = window.roomLoading;
  const dialog = element("room-loading");
  const numbers = element("loading-numbers");
  const cascade = element("loading-cascade");
  const hunter = element("loading-hunter");
  element("create-room-btn").focus();
  let returned = 0;
  loading.start("create_room", () => returned++);
  assert.equal(dialog.open, true);
  assert.equal(document.activeElement, element("loading-title"));
  assert.equal(dialog.dataset.state, "waiting");
  assert.equal(numbers.children.length, 5, "The hunt starts with five local numbers");
  assert.equal(cascade.children.length, 64, "Desktop adds a dense decorative cascade without adding edible targets");
  assert.ok(cascade.children.every((number) => /^\d$/.test(number.textContent)), "The cascade contains individual mathematical digits");
  const originalCascade = [...cascade.children];
  assert.ok(cascade.children.filter((number) => parseFloat(number.style["--cascade-x"]) < 30).length > cascade.children.length * .65,
    "Most decorative digits stay along the left edge, preserving homepage readability");
  assert.ok(new Set(cascade.children.map((number) => number.style["--cascade-scale"])).size > 2,
    "The cascade has multiple static depth layers");
  const original = new Set(numbers.children);
  const actions = new Set();
  const scales = new Set();
  const observedPrey = [];
  for (let elapsed = 0; elapsed < 10000; elapsed += 50) {
    clock.advance(50);
    actions.add(hunter.dataset.action);
    const scale = /scale\(([-\d.]+)\)/.exec(hunter.style.transform);
    assert.ok(scale, "The creature's viewport travel includes a depth scale");
    assert.ok(Number(scale[1]) > 0.3 && Number(scale[1]) <= 1.5, "The creature stays readable at every depth");
    scales.add(scale[1]);
    assert.match(hunter.querySelector(".demon-facing").style.transform, /^scaleX\(-?1\)$/, "Facing changes independently of depth");
    observedPrey.push(...numbers.children.map((number) => readTranslation(number.style.transform)));
    assert.equal(numbers.children.length, 5, "Each eaten number is replaced without DOM growth");
    assert.deepEqual(cascade.children, originalCascade, "CSS moves the cascade without rebuilding DOM during the hunt");
    assert.ok(clock.scheduled.size <= 1, "A hunt uses a single pending animation step");
  }
  assert.ok(actions.has("scan") && actions.has("run") && actions.has("bite"), "The loop visibly scans, hunts and consumes");
  assert.ok(scales.size > 2, "Depth changes as the hunter travels between distant prey");
  const horizontalSpan = Math.max(...observedPrey.map((prey) => prey.x)) - Math.min(...observedPrey.map((prey) => prey.x));
  const verticalSpan = Math.max(...observedPrey.map((prey) => prey.y)) - Math.min(...observedPrey.map((prey) => prey.y));
  assert.ok(horizontalSpan > element("loading-scene").clientWidth * .55, "Prey occupies most of the viewport width");
  assert.ok(verticalSpan > element("loading-scene").clientHeight * .45, "Prey spreads across foreground and distant regions");
  assert.ok(numbers.children.some((number) => !original.has(number)), "Consumed numbers reappear elsewhere while waiting");
  assert.equal(session.requests.length, 0, "The entire animation runs without network activity");
  element("loading-scene").clientWidth = 320;
  element("loading-scene").clientHeight = 740;
  window.innerWidth = 320;
  window.innerHeight = 740;
  await window.dispatch("resize");
  assert.equal(cascade.children.length, 36, "Crossing the mobile breakpoint lowers cascade density");
  const mobileCascade = [...cascade.children];
  await window.dispatch("resize");
  assert.deepEqual(cascade.children, mobileCascade, "Resizing within one density band keeps existing particles");
  assert.doesNotMatch(hunter.style.transform, /NaN|Infinity/);
  for (const number of numbers.children) {
    const position = readTranslation(number.style.transform);
    assert.ok(position.x >= 0 && position.x <= 320 && position.y >= 0 && position.y <= 740,
      "A viewport resize keeps every target inside the mobile scene");
  }
  loading.setStatus("The server is waking up…");
  assert.match(element("loading-status").textContent, /waking/);

  let completed = 0;
  loading.finish(() => completed++);
  assert.equal(dialog.dataset.state, "finishing");
  assert.equal(numbers.children.length, 1, "Confirmation stops replenishment and clears normal prey");
  assert.equal(cascade.children.length, 0, "The ambient cascade clears for the final red bite");
  assert.equal(numbers.children[0].classList.contains("final-number"), true);
  assert.equal(numbers.children[0].textContent, "0");
  assert.equal(hunter.dataset.action, "alert", "The final red number triggers a stronger reaction");
  loading.finish(() => { throw Error("Duplicate finish callback must be ignored"); });
  clock.advance(500);
  assert.equal(completed, 0, "The lobby transition cannot happen before the final bite");
  assert.equal(numbers.children[0].classList.contains("eaten"), true);
  clock.advance(1000);
  assert.equal(completed, 1);
  clock.advance(10000);
  assert.equal(completed, 1, "The final callback runs once");
  assertCleanAnimation(session);

  element("join-room-btn").focus();
  loading.start("join_room", () => returned++);
  assert.match(element("loading-status").textContent, /your room/);
  loading.finish(() => completed++);
  loading.cancel();
  clock.advance(5000);
  assert.equal(completed, 1, "Cancelling a finale cannot call its completion callback later");
  assert.equal(document.activeElement, element("join-room-btn"));
  assertCleanAnimation(session);

  loading.start("create_room", () => returned++);
  let retries = 0;
  loading.fail("Room not found", () => retries++, () => returned++);
  assert.equal(dialog.dataset.state, "failed");
  assert.equal(clock.scheduled.size, 0, "Failure stops the hunting loop");
  assert.equal(numbers.children.length, 0);
  assert.equal(cascade.children.length, 0, "Failure clears decorative animation immediately");
  assert.equal(element("loading-retry").classList.contains("hidden"), false);
  assert.equal(document.activeElement, element("loading-retry"));
  loading.fail("Ignored duplicate", () => { throw Error("Wrong retry"); }, () => {});
  assert.equal(element("loading-status").textContent, "Room not found");
  await element("loading-retry").dispatch("click");
  await element("loading-retry").dispatch("click");
  assert.equal(retries, 1, "Retry cleans the previous scene and fires once");
  assertCleanAnimation(session);
  loading.start("join_room", () => returned++);
  await dialog.dispatch("cancel");
  assert.equal(returned, 1, "Escape returns home through the same cleanup path");
  assertCleanAnimation(session);

  loading.start("create_room", () => {});
  document.hidden = true;
  document.visibilityState = "hidden";
  await document.dispatch("visibilitychange");
  assert.equal(dialog.dataset.paused, "true");
  assert.equal(clock.scheduled.size, 0, "A hidden tab schedules no hunting work");
  assert.equal(cascade.children.length, 0, "A hidden tab retains no animated cascade particles");
  assert.equal(numbers.children.length, 0, "Hidden hunting targets are removed until visibility returns");
  const pausedNumbers = [...numbers.children];
  clock.advance(100000);
  assert.deepEqual(numbers.children, pausedNumbers);
  document.hidden = false;
  document.visibilityState = "visible";
  await document.dispatch("visibilitychange");
  assert.equal(dialog.dataset.paused, "false");
  assert.equal(numbers.children.length, 5);
  assert.equal(cascade.children.length, 36, "Returning to a visible mobile page rebuilds the bounded cascade");
  assert.equal(clock.scheduled.size, 1);
  loading.finish(() => completed++);
  document.hidden = true;
  await document.dispatch("visibilitychange");
  assert.equal(completed, 2, "Hiding during the finale completes admission without a throttled-timer wait");
  assertCleanAnimation(session);

  loading.start("create_room", () => {});
  loading.finish(() => completed++);
  assert.equal(completed, 3, "Confirmation while already hidden completes immediately");
  assertCleanAnimation(session);
  document.hidden = false;
  loading.start("create_room", () => {});
  media.matches = true;
  await media.dispatch("change");
  assert.equal(hunter.dataset.action, "still");
  assert.equal(cascade.children.length, 16, "Enabling reduced motion replaces the cascade with a sparse static scene");
  assert.equal(clock.scheduled.size, 0, "Enabling reduced motion stops the moving hunt immediately");
  element("loading-scene").clientWidth = 1280;
  element("loading-scene").clientHeight = 900;
  await window.dispatch("resize");
  assert.equal(cascade.children.length, 16, "Reduced motion retains its sparse density on resize");
  media.matches = false;
  await media.dispatch("change");
  assert.equal(cascade.children.length, 64, "Disabling reduced motion restores the current desktop density");
  assert.equal(clock.scheduled.size, 1, "The existing hunt resumes with one pending step");
  loading.cancel();
  assertCleanAnimation(session);

  const reduced = createSession({ reduced: true });
  reduced.window.roomLoading.start("create_room", () => {});
  assert.equal(reduced.element("loading-numbers").children.length, 5);
  assert.equal(reduced.element("loading-cascade").children.length, 16);
  assert.equal(reduced.clock.scheduled.size, 0);
  let reducedFinished = 0;
  reduced.window.roomLoading.finish(() => reducedFinished++);
  assert.equal(reduced.element("loading-numbers").children[0].classList.contains("final-number"), true);
  reduced.clock.advance(200);
  assert.equal(reducedFinished, 1, "Reduced motion retains the red target with a brief bounded transition");
  assertCleanAnimation(reduced);
}

async function testRoomIntegration() {
  const session = createSession({ app: true });
  const { element, loader, clock, sockets, requests, receive, read } = session;
  await flush();
  const initialRequests = requests.length;
  element("nickname").value = "Host";
  await element("create-room-btn").dispatch("click");
  assert.equal(loader.starts.length, 1, "Creating a room starts the loading experience immediately");
  assert.equal(element("create-room-btn").disabled, true);
  assert.equal(element("join-room-btn").disabled, true);
  await element("nickname").dispatch("keydown", { key: "Enter", repeat: false });
  await element("create-room-btn").dispatch("click");
  assert.equal(sockets.length, 1, "Repeated clicks and Enter cannot create extra sockets");
  const first = sockets[0];
  first.readyState = 1;
  await first.onopen();
  assert.deepEqual(first.sent, [{ type: "authenticate", id_token: null }, { type: "create_room", nickname: "Host" }]);
  clock.advance(12000);
  assert.ok(loader.statuses.some((message) => /wak|longer|starting/i.test(message)), "Long waiting acknowledges a waking backend");
  assert.equal(first.sent.length, 2, "Waiting produces no polling or repeated socket requests");
  assert.equal(requests.length, initialRequests, "Waiting produces no extra HTTP/Firebase activity");
  receive(first, { type: "joined", player_id: "host-id" });
  assert.equal(loader.finishes.length, 0, "The identity acknowledgement alone cannot finish entry");
  receive(first, { type: "room_created", ...lobby });
  assert.equal(loader.finishes.length, 1, "Only the confirmed room starts the final number sequence");
  assert.equal(element("app-shell").getAttribute("data-screen"), "home", "The home page remains beneath the final red sequence");
  assert.equal(element("home-screen").classList.contains("hidden"), false);
  assert.equal(element("lobby-screen").classList.contains("hidden"), true);
  assert.equal(read("phase"), "lobby");
  clock.advance(100000);
  assert.equal(loader.failures.length, 0, "Room acknowledgement cancels the entry deadline");
  loader.active = false;
  loader.finishes[0]();
  assert.equal(element("app-shell").getAttribute("data-screen"), "lobby", "Consuming the final number reveals the confirmed room");
  assert.equal(session.document.activeElement, element("lobby-screen"));
  await element("back-btn").dispatch("click");
  assert.equal(read("socket"), null);
  assert.equal(clock.scheduled.size, 0);

  element("room-code-input").value = " missing ";
  await element("join-room-btn").dispatch("click");
  const invalid = sockets.at(-1);
  invalid.readyState = 1;
  await invalid.onopen();
  assert.deepEqual(invalid.sent.at(-1), { type: "join_room", nickname: "Host", room_code: "MISSING" });
  receive(invalid, { type: "error", request_type: "join_room", message: "Room not found" });
  assert.equal(loader.failures.length, 1);
  assert.match(loader.failures.at(-1).message, /Room not found/);
  assert.equal(invalid.readyState, 3, "Rejected admission closes its unused socket");
  const invalidFailure = loader.failures.at(-1);
  invalidFailure.retry();
  assert.equal(sockets.length, 3, "Retry starts exactly one fresh connection");
  const retry = sockets.at(-1);
  invalid.onmessage({ data: JSON.stringify({ type: "player_joined", ...lobby }) });
  invalid.onerror();
  invalid.onclose();
  assert.equal(loader.finishes.length, 1, "A stale socket cannot reveal its lobby");
  assert.equal(loader.failures.length, 1, "Stale error/close callbacks cannot fail the retry");
  retry.readyState = 1;
  await retry.onopen();
  receive(retry, { type: "joined", player_id: "host-id" });
  receive(retry, { type: "player_joined", ...lobby });
  assert.equal(loader.finishes.length, 2, "Joining also requires confirmed admission");
  loader.active = false;
  loader.finishes.at(-1)();
  await element("back-btn").dispatch("click");

  await element("create-room-btn").dispatch("click");
  const timedOut = sockets.at(-1);
  clock.advance(90000);
  assert.equal(loader.failures.length, 2, "Cold starts are bounded by an overall entry deadline");
  assert.equal(timedOut.readyState, 3);
  assert.equal(clock.scheduled.size, 0, "A failure leaves no entry timers behind");
  loader.failures.at(-1).onReturn();
  assert.equal(read("phase"), "home");
  assert.equal(loader.active, false);

  let resolveToken;
  session.token(() => new Promise((resolve) => { resolveToken = resolve; }));
  await element("create-room-btn").dispatch("click");
  const awaitingToken = sockets.at(-1);
  awaitingToken.readyState = 1;
  const opening = awaitingToken.onopen();
  await flush();
  loader.starts.at(-1).onCancel();
  resolveToken("late-token");
  await opening;
  assert.equal(awaitingToken.sent.length, 0, "Cancelling during token refresh cannot send authentication or room requests");
  assert.equal(read("socket"), null);
  assert.equal(clock.scheduled.size, 0);
  session.token(async () => null);

  await element("create-room-btn").dispatch("click");
  const offline = sockets.at(-1);
  offline.onerror();
  assert.equal(loader.failures.length, 3, "Connection errors show actionable failure");
  offline.onclose();
  assert.equal(loader.failures.length, 3, "The subsequent socket close cannot fail twice");
  loader.failures.at(-1).onReturn();
  await element("create-room-btn").dispatch("click");
  const closed = sockets.at(-1);
  closed.onclose();
  assert.equal(loader.failures.length, 4, "A close before admission also stops waiting");
  loader.failures.at(-1).onReturn();
  assert.equal(clock.scheduled.size, 0);
}

async function testEntryRaces() {
  const session = createSession({ app: true });
  const { element, sockets, receive, loader, read, clock } = session;
  await flush();
  element("nickname").value = "Host";
  await element("create-room-btn").dispatch("click");
  const connection = sockets.at(-1);
  connection.readyState = 1;
  await connection.onopen();
  receive(connection, { type: "joined", player_id: "host-id" });
  receive(connection, { type: "room_created", ...lobby });
  assert.equal(loader.finishes.length, 1);
  receive(connection, { type: "game_started", round_id: "early-start", duration: 60,
    server_time: 100, start_at: 103, end_at: 163, ranked: false,
    questions: [{ question: "1 + 1", question_id: "q0", index: 0 }] });
  assert.equal(read("phase"), "countdown");
  assert.equal(element("app-shell").getAttribute("data-screen"), "home", "A game start during the final bite leaves the home underlay visible");
  clock.advance(3000);
  assert.equal(read("phase"), "game");
  assert.equal(element("app-shell").getAttribute("data-screen"), "home");
  loader.active = false;
  loader.finishes[0]();
  assert.equal(read("phase"), "game", "A host starting during the final bite cannot send the guest back to lobby");
  assert.equal(element("app-shell").getAttribute("data-screen"), "game", "The latest pending game screen appears after consumption");
  assert.equal(session.document.activeElement, element("answer-input"));
  await element("back-btn").dispatch("click");
  assert.equal(clock.scheduled.size, 0);

  await element("create-room-btn").dispatch("click");
  const interrupted = sockets.at(-1);
  interrupted.readyState = 1;
  await interrupted.onopen();
  receive(interrupted, { type: "room_created", ...lobby });
  const interruptedFinale = loader.finishes.at(-1);
  interrupted.onclose();
  assert.equal(loader.failures.length, 1, "Closing after acknowledgement but before final consumption still fails entry");
  assert.equal(read("phase"), "home");
  assert.equal(element("app-shell").getAttribute("data-screen"), "home");
  loader.failures[0].retry();
  const pendingRetry = sockets.at(-1);
  interruptedFinale();
  assert.equal(read("socket"), pendingRetry, "A stale finale cannot overwrite a retried room entry");
  assert.notEqual(read("roomEntry"), null);
  assert.equal(read("phase"), "home");
  assert.equal(element("app-shell").getAttribute("data-screen"), "home", "A stale finale cannot reveal an old room underneath the retry");
  loader.starts.at(-1).onCancel();
  assert.equal(clock.scheduled.size, 0);
}

async function testCombinedLifecycle() {
  const html = fs.readFileSync(path.join(frontend, "index.html"), "utf8");
  assert.ok(html.indexOf('src="loading.js"') >= 0 && html.indexOf('src="loading.js"') < html.indexOf('src="app.js"'),
    "The loading controller is available before the room flow starts");
  const session = createSession({ app: true, realLoading: true });
  const { element, sockets, receive, clock, window, read } = session;
  await flush();
  const initialRequests = session.requests.length;
  element("nickname").value = "Host";
  await element("create-room-btn").dispatch("click");
  assert.equal(element("room-loading").dataset.state, "waiting");
  assert.equal(element("home-screen").classList.contains("hidden"), false);
  const connection = sockets.at(-1);
  connection.readyState = 1;
  await connection.onopen();
  clock.advance(15000);
  assert.match(element("loading-status").textContent, /waking/);
  receive(connection, { type: "joined", player_id: "host-id" });
  receive(connection, { type: "room_created", ...lobby });
  assert.equal(element("room-loading").dataset.state, "finishing");
  assert.equal(element("app-shell").getAttribute("data-screen"), "home");
  assert.equal(element("home-screen").classList.contains("hidden"), false);
  assert.equal(element("lobby-screen").classList.contains("hidden"), true);
  assert.equal(element("loading-numbers").children[0].classList.contains("final-number"), true);
  clock.advance(1000);
  assert.equal(element("app-shell").getAttribute("data-screen"), "lobby");
  assert.equal(read("roomEntry"), null);
  assert.equal(session.document.activeElement, element("lobby-screen"));
  assertCleanAnimation(session);
  assert.equal(session.requests.length, initialRequests);
  assert.equal(connection.sent.length, 2);
  await element("back-btn").dispatch("click");

  element("room-code-input").value = "ABCDEF";
  await element("join-room-btn").dispatch("click");
  const rejected = sockets.at(-1);
  rejected.readyState = 1;
  await rejected.onopen();
  receive(rejected, { type: "error", request_type: "join_room", message: "Room not found" });
  assert.equal(element("room-loading").dataset.state, "failed");
  assert.equal(clock.scheduled.size, 0);
  await element("loading-retry").dispatch("click");
  const retry = sockets.at(-1);
  assert.equal(sockets.length, 3);
  assert.equal(element("room-loading").dataset.state, "waiting");
  retry.readyState = 1;
  await retry.onopen();
  receive(retry, { type: "player_joined", ...lobby });
  session.document.hidden = true;
  await session.document.dispatch("visibilitychange");
  assert.equal(read("phase"), "lobby");
  assert.equal(window.roomLoading.isActive(), false);
  assertCleanAnimation(session);
  await element("back-btn").dispatch("click");
}

async function testPageNavigation() {
  for (const realLoading of [false, true]) {
    const session = createSession({ app: true, realLoading });
    const { element, sockets, receive, clock, window, loader, read } = session;
    await flush();
    element("nickname").value = "Host";
    let resolveToken;
    session.token(() => new Promise((resolve) => { resolveToken = resolve; }));
    await element("create-room-btn").dispatch("click");
    assert.equal(element("home-screen").focusOptions.preventScroll, true,
      "Starting entry preserves the visible homepage scroll position");
    const pending = sockets.at(-1);
    pending.readyState = 1;
    const opening = pending.onopen();
    await window.dispatch("pagehide", { persisted: true });
    assert.equal(pending.readyState, 3, "Navigation/BFcache closes a pending socket");
    assert.equal(read("socket"), null);
    assert.equal(read("roomEntry"), null);
    assert.equal(read("pendingEntryScreen"), null);
    assert.equal(window.roomLoading.isActive(), false);
    assert.equal(clock.scheduled.size, 0);
    resolveToken("late-navigation-token");
    await opening;
    assert.equal(pending.sent.length, 0, "An old token resolution cannot resurrect entry after navigation");
    session.token(async () => null);

    await element("create-room-btn").dispatch("click");
    const finalizing = sockets.at(-1);
    finalizing.readyState = 1;
    await finalizing.onopen();
    receive(finalizing, { type: "room_created", ...lobby });
    assert.notEqual(read("pendingEntryScreen"), null);
    const staleFinale = realLoading ? null : loader.finishes.at(-1);
    await window.dispatch("pagehide", { persisted: false });
    staleFinale?.();
    receive(finalizing, { type: "player_joined", ...lobby });
    clock.advance(100000);
    assert.equal(finalizing.readyState, 3, "Navigation also closes a socket during its final bite");
    assert.equal(read("socket"), null);
    assert.equal(read("roomEntry"), null);
    assert.equal(read("pendingEntryScreen"), null);
    assert.equal(read("phase"), "home");
    assert.equal(element("app-shell").getAttribute("data-screen"), "home",
      "A cancelled finale or old response cannot reveal a room after navigation");
    assert.equal(window.roomLoading.isActive(), false);
    assert.equal(clock.scheduled.size, 0);
    if (realLoading) assertCleanAnimation(session);
  }
}

async function main() {
  await testHuntingAnimation();
  await testRoomIntegration();
  await testEntryRaces();
  await testCombinedLifecycle();
  await testPageNavigation();
  console.log("Room loading: bounded desktop/mobile/static cascade, CSS-only particle lifecycle, viewport hunt/depth, home underlay through final red bite, reduced motion, hidden tabs, navigation/BFcache cleanup, admission, timeout, failure/retry, stale sockets, token-await cancellation and no additional requests passed");
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
