const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const html = fs.readFileSync(path.join(__dirname, "../frontend/index.html"), "utf8");
const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map((match) => match[1]);
assert.equal(new Set(ids).size, ids.length, "HTML IDs must be unique");
for (const match of html.matchAll(/\b(?:for|aria-labelledby|aria-controls)="([^"]+)"/g)) {
  assert.ok(ids.includes(match[1]), `Missing label target: ${match[1]}`);
}
let focusedElement;
const elements = new Map();
function createElement() {
  const classes = new Set(["hidden"]);
  return {
    value: "", disabled: false, style: {}, children: [], listeners: {},
    get textContent() { return (this.text || "") + this.children.map((child) => child.textContent).join(""); },
    set textContent(value) { this.text = String(value); this.children = []; },
    get innerHTML() { return this.html || ""; },
    set innerHTML(value) { this.html = value; this.children = []; },
    classList: {
      add: (...names) => names.forEach((name) => classes.add(name)),
      remove: (...names) => names.forEach((name) => classes.delete(name)),
      contains: (name) => classes.has(name)
    },
    addEventListener(event, callback) { this.listeners[event] = callback; },
    setAttribute(name, value) { this[name] = value; },
    appendChild(child) { this.children.push(child); },
    focus() { focusedElement = this; },
    showModal() { this.open = true; },
    close() { this.open = false; }
  };
}
const document = {
  getElementById(id) {
    assert.ok(ids.includes(id), `Missing HTML binding: ${id}`);
    if (!elements.has(id)) elements.set(id, Object.assign(createElement(), { id }));
    return elements.get(id);
  },
  createElement
};
const element = (id) => document.getElementById(id);
const intervals = new Map();
const timeouts = new Map();
let timerId = 0;
let now = 0;
let socket;
let currentToken = null;
let fetchResult = { entries: [], week: "2026-10-12" };
let fetchFailure = false;
const requests = [];
const listeners = {};
class FakeWebSocket {
  static OPEN = 1;
  constructor() { socket = this; this.sent = []; this.readyState = 0; }
  send(message) { this.sent.push(JSON.parse(message)); }
  close() { this.readyState = 3; this.onclose?.(); }
}
const window = {
  SPEED_DEMON_CONFIG: { backendUrl: "http://127.0.0.1:8000" },
  speedDemonAuth: { getToken: async () => currentToken },
  addEventListener: (name, callback) => { listeners[name] = callback; }
};
const context = vm.createContext({
  document, window, WebSocket: FakeWebSocket, performance: { now: () => now }, AbortController,
  setInterval(callback) { intervals.set(++timerId, callback); return timerId; },
  clearInterval: (id) => intervals.delete(id),
  setTimeout(callback) { timeouts.set(++timerId, callback); return timerId; },
  clearTimeout: (id) => timeouts.delete(id),
  fetch: async (url) => {
    requests.push(url);
    if (fetchFailure) throw Error("offline");
    return { ok: true, json: async () => fetchResult };
  }
});
vm.runInContext(fs.readFileSync(path.join(__dirname, "../frontend/app.js"), "utf8"), context);
const read = (expression) => vm.runInContext(expression, context);
const receive = (message) => socket.onmessage({ data: JSON.stringify(message) });
const advance = (milliseconds) => { now += milliseconds; [...intervals.values()].forEach((callback) => callback()); };
const flush = () => new Promise((resolve) => setImmediate(resolve));
const lobby = { room_code: "AB12CD", players: [
  { player_id: "host-id", nickname: "Host", authenticated: false },
  { player_id: "guest-id", nickname: "Guest", authenticated: false }
], host: "host-id" };
function round(id) {
  return { type: "game_started", round_id: id, duration: 60, server_time: 100, start_at: 103, end_at: 163,
    ranked: false, questions: [
      { question: "1 + 1", question_id: "q0", index: 0 },
      { question: "1 - 2", question_id: "q1", index: 1 }
    ] };
}
function enterLobby() {
  receive({ type: "joined", player_id: "host-id" });
  receive({ type: "room_created", ...lobby });
}

(async () => {
  await flush();
  element("guide-btn").listeners.click();
  assert.equal(element("guide-dialog").open, true);
  element("guide-close").listeners.click();
  assert.equal(element("guide-dialog").open, false);
  assert.match(element("leaderboard-status").textContent, /No ranked rounds/);
  element("nickname").value = "Host";
  element("duration").value = "60";
  element("create-room-btn").listeners.click();
  socket.readyState = 1;
  await socket.onopen();
  assert.deepEqual(socket.sent, [{ type: "authenticate", id_token: null }, { type: "create_room", nickname: "Host" }]);
  enterLobby();
  assert.equal(focusedElement, element("lobby-screen"));
  assert.equal(element("app-shell")["data-screen"], "lobby");
  assert.equal(element("player-count").textContent, "2 players");
  receive(round("abandoned"));
  assert.equal(intervals.size, 1);
  receive({ type: "returned_to_lobby", ...lobby });
  assert.equal(intervals.size, 0);

  receive(round("first"));
  advance(3000);
  assert.equal(element("app-shell")["data-screen"], "game");
  assert.equal(element("home-screen").classList.contains("hidden"), true);
  assert.equal(element("game-room-code").textContent, "AB12CD");
  assert.equal(element("timer").textContent, "01:00");
  assert.equal(read("phase"), "game");
  assert.equal(focusedElement, element("answer-input"));
  const answer = element("answer-input");
  const key = (repeat = false) => answer.listeners.keydown({ key: "Enter", repeat, preventDefault() {} });
  answer.value = "";
  key();
  assert.equal(read("currentQuestionIndex"), 0);
  assert.match(element("answer-feedback").textContent, /Enter a number/);
  answer.value = "2";
  key(true);
  assert.equal(read("currentQuestionIndex"), 0);
  key();
  assert.equal(read("currentQuestionIndex"), 1);
  assert.equal(read("score"), 0, "No client-side scoring");
  assert.deepEqual(socket.sent.at(-1), { type: "answer", room_code: "AB12CD", round_id: "first", index: 0, question_id: "q0", answer: 2 });
  assert.equal(element("question").textContent, "1−2", "Next question immediately, without waiting for latency");
  answer.value = "-1";
  key();
  assert.equal(answer.disabled, true);
  receive({ type: "answer_result", round_id: "first", index: 0, correct: true, score: 1,
    questions: [{ question: "2 + 3", question_id: "q2", index: 2 }] });
  assert.equal(read("score"), 1);
  assert.equal(answer.disabled, false);
  assert.equal(focusedElement, answer);
  receive({ type: "answer_result", round_id: "first", index: 1, correct: false, score: 1, questions: [] });
  assert.match(element("answer-feedback").textContent, /Incorrect/);
  advance(59000);
  assert.equal(element("time-progress").value, 1);
  const sentBeforeEnd = socket.sent.length;
  advance(1000);
  assert.equal(read("phase"), "waiting");
  assert.equal(socket.sent.length, sentBeforeEnd, "No final score submission");
  receive({ type: "game_results", round_id: "first", scores: { Host: 1, Guest: 1 }, winners: ["Guest", "Host"], persistence: "queued", ranked_players: [] });
  assert.match(element("winner-text").textContent, /Tie/);
  element("play-again-btn").listeners.click();
  assert.deepEqual(socket.sent.at(-1), { type: "play_again", room_code: "AB12CD" });
  receive({ type: "returned_to_lobby", ...lobby });
  assert.equal(intervals.size, 0);
  assert.equal(read("roundId"), null);
  receive(round("second"));
  advance(3000);
  receive({ type: "answer_result", round_id: "first", score: 10000, questions: [] });
  assert.equal(read("score"), 0, "Stale acknowledgements ignored");
  receive({ type: "player_left", ...lobby, host: "guest-id", players: [lobby.players[1]] });
  assert.equal(element("start-btn").style.display, "none");
  assert.equal(read("phase"), "game", "Departure doesn't restart the round");
  currentToken = "opaque-token";
  await listeners["auth-changed"]();
  assert.deepEqual(socket.sent.at(-1), { type: "authenticate", id_token: "opaque-token" });
  receive({ type: "auth_status", authenticated: false, message: "Verification unavailable" });
  assert.match(element("app-notice").textContent, /Verification unavailable/);
  receive({ type: "auth_status", authenticated: true, name: "Late login", ranked: false, round_id: "second" });
  assert.match(element("ranked-status").textContent, /does not qualify/);
  const oldSocket = socket;
  element("back-btn").listeners.click();
  assert.equal(oldSocket.readyState, 3);
  assert.equal(intervals.size, 0);
  assert.equal(read("socket"), null);

  fetchResult = { entries: [{ name: "<script>name</script>", score: 4 }], week: null };
  element("all-time-tab").listeners.click();
  await flush();
  assert.match(requests.at(-1), /period=all_time/);
  assert.equal(element("all-time-tab")["aria-selected"], "true");
  assert.equal(element("leaderboard-list").children[0].children[0].textContent, "<script>name</script>");
  element("leaderboard-duration").value = "120";
  element("leaderboard-duration").listeners.change();
  await flush();
  assert.match(requests.at(-1), /duration=120/);
  element("all-time-tab").listeners.keydown({ key: "ArrowLeft", preventDefault() {} });
  await flush();
  assert.equal(focusedElement, element("weekly-tab"));
  fetchFailure = true;
  element("leaderboard-refresh").listeners.click();
  await flush();
  assert.match(element("leaderboard-status").textContent, /Rankings unavailable/);
  assert.equal(timeouts.size, 0);
  assert.equal(read("BACKEND_WS_URL"), "ws://127.0.0.1:8000/ws");
  console.log("Frontend: authoritative scores, buffering, replay, stale rounds, timer, keyboard/focus, host controls, auth transport, leaderboard tabs/loading/error/XSS checks passed");
})().catch((error) => { console.error(error); process.exitCode = 1; });
