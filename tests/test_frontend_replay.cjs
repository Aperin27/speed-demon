const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const elements = new Map();
function createElement() {
  const classes = new Set(["hidden"]);
  return {
    value: "",
    textContent: "",
    innerHTML: "",
    style: {},
    children: [],
    listeners: {},
    classList: {
      add: (name) => classes.add(name),
      remove: (name) => classes.delete(name),
      contains: (name) => classes.has(name)
    },
    addEventListener(event, callback) { this.listeners[event] = callback; },
    appendChild(child) { this.children.push(child); },
    focus() {}
  };
}
const document = {
  getElementById(id) {
    if (!elements.has(id)) elements.set(id, createElement());
    return elements.get(id);
  },
  querySelector: () => createElement(),
  createElement
};
const timers = new Map();
let timerId = 0;
let socket;
class FakeWebSocket {
  constructor() { socket = this; this.sent = []; }
  send(message) { this.sent.push(JSON.parse(message)); }
}
const context = vm.createContext({
  document,
  WebSocket: FakeWebSocket,
  alert: (message) => assert.fail(message),
  setInterval(callback) { timers.set(++timerId, callback); return timerId; },
  clearInterval(id) { timers.delete(id); }
});
vm.runInContext(fs.readFileSync(path.join(__dirname, "../frontend/app.js"), "utf8"), context);
const read = (expression) => vm.runInContext(expression, context);
const receive = (message) => socket.onmessage({ data: JSON.stringify(message) });
const tick = () => [...timers.values()].forEach((callback) => callback());

document.getElementById("nickname").value = "Host";
document.getElementById("create-room-btn").listeners.click();
socket.onopen();
const lobby = { room_code: "AB12", players: ["Host", "Guest"], host: "Host" };
receive({ type: "room_created", ...lobby });
const round = { type: "game_started", duration: 2,
  questions: [{ question: "1 + 1", answer: 2 }] };

// A replay during countdown must prevent the abandoned round from starting.
receive(round);
assert.equal(timers.size, 1);
receive({ type: "returned_to_lobby", ...lobby });
assert.equal(timers.size, 0);
assert.equal(document.getElementById("lobby-screen").classList.contains("hidden"), false);
assert.equal(document.getElementById("room-code-display").textContent, "AB12");

// A replay during gameplay must prevent a stale score submission.
receive(round);
tick(); tick(); tick();
document.getElementById("answer-input").value = "2";
document.getElementById("submit-btn").listeners.click();
assert.equal(read("score"), 1);
receive({ type: "returned_to_lobby", ...lobby });
assert.equal(timers.size, 0);
assert.equal(read("score"), 0);
assert.equal(read("questions.length"), 0);
assert.equal(read("currentQuestionIndex"), 0);
assert.equal(read("correctAnswer"), null);

// Subsequent rounds submit fresh scores, render results, and send canonical replay.
for (let index = 0; index < 2; index++) {
  receive(round);
  tick(); tick(); tick();
  assert.equal(read("score"), 0);
  tick(); tick();
  assert.deepEqual(socket.sent.at(-1), {
    type: "submit_score", room_code: "AB12", nickname: "Host", score: 0
  });
  receive({ type: "game_results", scores: { Host: 0, Guest: 1 }, winner: "Guest" });
  assert.equal(document.getElementById("winner-text").textContent, "Winner: Guest");
  document.getElementById("play-again-btn").listeners.click();
  assert.deepEqual(socket.sent.at(-1), { type: "play_again", room_code: "AB12" });
  receive({ type: "returned_to_lobby", ...lobby });
  assert.equal(timers.size, 0);
}
console.log("Frontend replay, timer cleanup, and second-round regression checks passed");
