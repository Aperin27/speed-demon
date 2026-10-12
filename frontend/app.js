const homeScreen = document.getElementById("home-screen");
const lobbyScreen = document.getElementById("lobby-screen");
const countdownScreen = document.getElementById("countdown-screen");
const gameScreen = document.getElementById("game-screen");
const resultScreen = document.getElementById("result-screen");
const nicknameInput = document.getElementById("nickname");
const roomCodeInput = document.getElementById("room-code-input");
const roomCodeDisplay = document.getElementById("room-code-display");
const playersList = document.getElementById("players-list");
const durationSelect = document.getElementById("duration");
const createRoomBtn = document.getElementById("create-room-btn");
const joinRoomBtn = document.getElementById("join-room-btn");
const startBtn = document.getElementById("start-btn");
const backBtn = document.getElementById("back-btn");
const submitBtn = document.getElementById("submit-btn");
const playAgainBtn = document.getElementById("play-again-btn");
const countdownNumber = document.getElementById("countdown-number");
const timerDisplay = document.getElementById("timer");
const scoreDisplay = document.getElementById("score");
const questionDisplay = document.getElementById("question");
const answerInput = document.getElementById("answer-input");
const winnerText = document.getElementById("winner-text");
const resultsList = document.getElementById("results-list");
const answerFeedback = document.getElementById("answer-feedback");
const questionNumber = document.getElementById("question-number");
const timeProgress = document.getElementById("time-progress");
const playerCount = document.getElementById("player-count");
const hostSettings = document.getElementById("host-settings");
const lobbyStatus = document.getElementById("lobby-status");
const resultNote = document.getElementById("result-note");
const connectionMessage = document.getElementById("connection-message");
const appNotice = document.getElementById("app-notice");
const rankedStatus = document.getElementById("ranked-status");
const BACKEND_HTTP_URL = window.SPEED_DEMON_CONFIG.backendUrl.replace(/\/$/, "");
const BACKEND_WS_URL = BACKEND_HTTP_URL.replace(/^http/, "ws") + "/ws";
let socket = null;
let nickname = "";
let roomCode = "";
let playerId = "";
let players = [];
let host = "";
let roundId = null;
let score = 0;
let questions = [];
let currentQuestionIndex = 0;
let timerInterval = null;
let gameDuration = 60;
let startsAt = 0;
let endsAt = 0;
let phase = "home";
let lastToken;

function showScreen(screen) {
  [homeScreen, lobbyScreen, countdownScreen, gameScreen, resultScreen].forEach((element) => element.classList.add("hidden"));
  screen.classList.remove("hidden");
  document.getElementById("app-shell").setAttribute("data-screen", screen.id.replace("-screen", ""));
  if (screen === countdownScreen || screen === gameScreen) notice("");
  screen.focus();
}

function notice(message) {
  appNotice.textContent = message;
  appNotice.classList[message ? "remove" : "add"]("hidden");
}

function showConnectionMessage(message) {
  connectionMessage.textContent = message;
  connectionMessage.classList[message ? "remove" : "add"]("hidden");
}

function send(message) {
  if (!socket || socket.readyState !== WebSocket.OPEN) {
    notice("Connection unavailable. Return home and join again.");
    return false;
  }
  socket.send(JSON.stringify(message));
  return true;
}

async function syncAuthentication(connection = socket) {
  try {
    const token = await window.speedDemonAuth.getToken();
    if (connection !== socket || connection?.readyState !== WebSocket.OPEN) return;
    if (token !== lastToken) {
      send({ type: "authenticate", id_token: token });
      lastToken = token;
    }
  } catch {
    if (connection === socket && connection?.readyState === WebSocket.OPEN) {
      send({ type: "authenticate", id_token: null });
      lastToken = null;
    }
    notice("Could not refresh Google login. This connection is playing as a guest.");
  }
}

function resetRound() {
  clearInterval(timerInterval);
  timerInterval = null;
  roundId = null;
  questions = [];
  currentQuestionIndex = 0;
  score = 0;
}

function disconnect() {
  resetRound();
  const oldSocket = socket;
  socket = null;
  if (oldSocket) oldSocket.close();
  roomCode = "";
  playerId = "";
  phase = "home";
  createRoomBtn.disabled = false;
  joinRoomBtn.disabled = false;
  showConnectionMessage("");
  showScreen(homeScreen);
}

function connectWebSocket(request) {
  disconnect();
  notice("");
  createRoomBtn.disabled = true;
  joinRoomBtn.disabled = true;
  showConnectionMessage("Connecting to the game server… Free hosting may take 30 seconds to wake.");
  const connection = new WebSocket(BACKEND_WS_URL);
  socket = connection;
  lastToken = undefined;
  connection.onopen = async () => {
    await syncAuthentication(connection);
    if (socket === connection) send(request);
  };
  connection.onmessage = (event) => {
    if (socket !== connection) return;
    try { receiveMessage(JSON.parse(event.data)); }
    catch { notice("The server sent an invalid response. Rejoin the room."); }
  };
  connection.onerror = () => { if (socket === connection) notice("Could not reach the server. Check your connection."); };
  connection.onclose = () => {
    if (socket !== connection) return;
    disconnect();
    notice("Disconnected. Rejoin a lobby to play again; interrupted rounds do not qualify for rankings.");
  };
}

function receiveMessage(data) {
  if (data.type === "joined") playerId = data.player_id;
  if (["room_created", "player_joined", "returned_to_lobby", "player_left", "identity_changed"].includes(data.type)) {
    roomCode = data.room_code;
    players = data.players;
    host = data.host;
    roomCodeDisplay.textContent = roomCode;
    updatePlayersList();
    updateHostControls();
    if (["room_created", "player_joined", "returned_to_lobby"].includes(data.type)) {
      resetRound();
      phase = "lobby";
      showConnectionMessage("");
      showScreen(lobbyScreen);
    }
  }
  if (data.type === "auth_status") {
    notice(data.message || (data.authenticated ? `Google identity verified: ${data.name}. Sign in before the start to rank.` : "Playing as a guest. Guest rounds are not ranked."));
    if (data.round_id === roundId && data.ranked === false) rankedStatus.textContent = "Guest round · This round does not qualify for rankings.";
  }
  if (data.type === "game_started") {
    resetRound();
    roundId = data.round_id;
    gameDuration = data.duration;
    questions = data.questions;
    startsAt = performance.now() + (data.start_at - data.server_time) * 1000;
    endsAt = performance.now() + (data.end_at - data.server_time) * 1000;
    phase = "countdown";
    rankedStatus.textContent = data.ranked ? "Ranked round · Complete without disconnecting or changing accounts." : "Guest round · Sign in before the next round to rank.";
    showScreen(countdownScreen);
    tickClock();
    timerInterval = setInterval(tickClock, 100);
  }
  if (data.type === "answer_result" && data.round_id === roundId && ["game", "waiting"].includes(phase)) {
    score = data.score;
    scoreDisplay.textContent = score;
    data.questions.forEach((question) => { questions[question.index] = question; });
    setAnswerFeedback(`Question ${data.index + 1}: ${data.correct ? "Correct! +1 point" : "Incorrect. Keep going."}`, data.correct ? "correct" : "incorrect");
    if (phase === "game") showCurrentQuestion();
  }
  if (data.type === "game_results" && data.round_id === roundId) {
    clearInterval(timerInterval);
    timerInterval = null;
    phase = "results";
    winnerText.textContent = data.winners.length > 1 ? `Tie: ${data.winners.join(", ")}` : `Winner: ${data.winners[0]}`;
    renderScores(data.scores);
    updateHostControls();
    if (data.persistence === "failed") notice("Scores are final, but saving this round failed.");
    else if (data.ranked_players.length) notice("Eligible scores are queued for rankings. Refresh after a few seconds; database outages may delay updates.");
    showScreen(resultScreen);
  }
  if (data.type === "error") {
    notice(data.message);
    createRoomBtn.disabled = false;
    joinRoomBtn.disabled = false;
    if (data.request_type === "start_game") startBtn.disabled = false;
    if (data.request_type === "play_again") playAgainBtn.disabled = false;
    if (data.request_type === "answer" && phase === "game") {
      endGame();
      notice("An answer was rejected. Answering has stopped; waiting for the server's final scores.");
    }
  }
}

function createPlayerRow(playerName) {
  const row = document.createElement("li");
  const avatar = document.createElement("span");
  avatar.className = "player-avatar";
  avatar.textContent = Array.from(playerName)[0]?.toUpperCase() || "?";
  avatar.setAttribute("aria-hidden", "true");
  const name = document.createElement("span");
  name.className = "player-name";
  name.textContent = playerName;
  row.appendChild(avatar);
  row.appendChild(name);
  return row;
}

function updatePlayersList() {
  playersList.innerHTML = "";
  playerCount.textContent = `${players.length} ${players.length === 1 ? "player" : "players"}`;
  players.forEach((player) => {
    const row = createPlayerRow(player.nickname);
    const role = document.createElement("span");
    role.className = "player-role";
    role.textContent = `${player.player_id === host ? "HOST" : "PLAYER"}${player.authenticated ? " · GOOGLE" : " · GUEST"}`;
    row.appendChild(role);
    playersList.appendChild(row);
  });
}

function updateHostControls() {
  const isHost = playerId === host;
  startBtn.style.display = isHost ? "flex" : "none";
  startBtn.disabled = players.length < 2;
  hostSettings.classList[isHost ? "remove" : "add"]("hidden");
  playAgainBtn.style.display = isHost ? "flex" : "none";
  playAgainBtn.disabled = false;
  lobbyStatus.textContent = isHost ? (players.length < 2 ? "Invite another player to start." : "You're the host. Set the pace.") : "Waiting for the host to start the round.";
  resultNote.textContent = isHost ? "Another round. Another chance." : "The host can bring everyone back for a rematch.";
}

function renderScores(scores) {
  resultsList.innerHTML = "";
  Object.entries(scores).sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0])).forEach(([name, points]) => {
    const row = createPlayerRow(name);
    const value = document.createElement("span");
    value.className = "result-points";
    value.textContent = `${points} pts`;
    row.appendChild(value);
    resultsList.appendChild(row);
  });
}

function setAnswerFeedback(message, outcome = "") {
  answerFeedback.textContent = message;
  answerFeedback.classList.remove("correct", "incorrect");
  if (outcome) answerFeedback.classList.add(outcome);
}

function showCurrentQuestion() {
  const wasDisabled = answerInput.disabled;
  const question = questions[currentQuestionIndex];
  const enabled = phase === "game" && !!question && performance.now() < endsAt;
  answerInput.disabled = !enabled;
  submitBtn.disabled = !enabled;
  questionDisplay.textContent = question?.question || "Buffering questions…";
  questionDisplay.classList[question ? "remove" : "add"]("question-buffering");
  if (question) {
    const parts = question.question.match(/^(-?\d+)\s+([+−×÷*\/\-])\s+(-?\d+)$/);
    if (parts) {
      questionDisplay.textContent = "";
      const left = document.createElement("span");
      left.textContent = parts[1];
      const operator = document.createElement("span");
      operator.className = "question-operator";
      operator.textContent = { "*": "×", "/": "÷", "-": "−" }[parts[2]] || parts[2];
      const right = document.createElement("span");
      right.textContent = parts[3];
      questionDisplay.appendChild(left);
      questionDisplay.appendChild(operator);
      questionDisplay.appendChild(right);
    }
  }
  questionNumber.textContent = `Question ${currentQuestionIndex + 1}`;
  if (wasDisabled && enabled) answerInput.focus();
}

function tickClock() {
  const now = performance.now();
  if (phase === "countdown") {
    countdownNumber.textContent = Math.max(1, Math.ceil((startsAt - now) / 1000));
    if (now >= startsAt) {
      phase = "game";
      scoreDisplay.textContent = 0;
      document.getElementById("game-room-code").textContent = roomCode;
      timeProgress.max = gameDuration;
      setAnswerFeedback("Go! One point per correct answer.");
      showScreen(gameScreen);
      showCurrentQuestion();
      answerInput.value = "";
      answerInput.focus();
    }
  }
  if (phase === "game") {
    const remaining = Math.max(0, Math.ceil((endsAt - now) / 1000));
    timerDisplay.textContent = `${String(Math.floor(remaining / 60)).padStart(2, "0")}:${String(remaining % 60).padStart(2, "0")}`;
    timeProgress.value = remaining;
    timerDisplay.classList[remaining <= 10 ? "add" : "remove"]("urgent");
    if (now >= endsAt) endGame();
  }
}

function submitAnswer() {
  const question = questions[currentQuestionIndex];
  if (phase !== "game" || !question || performance.now() >= endsAt) return;
  const answer = Number(answerInput.value);
  if (!answerInput.value.trim() || !Number.isFinite(answer) || Math.abs(answer) > 1000000) {
    setAnswerFeedback("Enter a number to answer this question.", "incorrect");
    answerInput.focus();
    return;
  }
  if (!send({ type: "answer", room_code: roomCode, round_id: roundId, question_id: question.question_id, index: question.index, answer })) return;
  currentQuestionIndex++;
  answerInput.value = "";
  showCurrentQuestion();
  if (!answerInput.disabled) answerInput.focus();
}

function endGame() {
  clearInterval(timerInterval);
  timerInterval = null;
  phase = "waiting";
  answerInput.disabled = true;
  submitBtn.disabled = true;
  playAgainBtn.style.display = "none";
  winnerText.textContent = "Waiting for server-confirmed results…";
  renderScores({ [nickname]: score });
  showScreen(resultScreen);
}

function enterRoom(type) {
  nickname = nicknameInput.value.trim();
  if (!nickname || nickname.length > 24) {
    notice("Enter a nickname of 1–24 characters.");
    nicknameInput.focus();
    return;
  }
  const code = roomCodeInput.value.trim().toUpperCase();
  if (type === "join_room" && !code) { notice("Enter a room code."); roomCodeInput.focus(); return; }
  connectWebSocket({ type, nickname, ...(type === "join_room" ? { room_code: code } : {}) });
}

async function requestStartGame() {
  startBtn.disabled = true;
  await syncAuthentication();
  send({ type: "start_game", room_code: roomCode, duration: Number(durationSelect.value) });
}

createRoomBtn.addEventListener("click", () => enterRoom("create_room"));
joinRoomBtn.addEventListener("click", () => enterRoom("join_room"));
startBtn.addEventListener("click", requestStartGame);
submitBtn.addEventListener("click", submitAnswer);
backBtn.addEventListener("click", disconnect);
answerInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.repeat) { event.preventDefault(); submitAnswer(); }
});
nicknameInput.addEventListener("keydown", (event) => { if (event.key === "Enter" && !event.repeat) enterRoom("create_room"); });
roomCodeInput.addEventListener("keydown", (event) => { if (event.key === "Enter" && !event.repeat) enterRoom("join_room"); });
playAgainBtn.addEventListener("click", () => {
  playAgainBtn.disabled = true;
  send({ type: "play_again", room_code: roomCode });
});
window.addEventListener("auth-changed", () => syncAuthentication());

const leaderboardDuration = document.getElementById("leaderboard-duration");
const leaderboardStatus = document.getElementById("leaderboard-status");
const leaderboardList = document.getElementById("leaderboard-list");
const weeklyTab = document.getElementById("weekly-tab");
const allTimeTab = document.getElementById("all-time-tab");
let leaderboardPeriod = "weekly";
let leaderboardRequest = null;
async function loadLeaderboard() {
  if (leaderboardRequest) leaderboardRequest.abort();
  const request = new AbortController();
  leaderboardRequest = request;
  const timeout = setTimeout(() => request.abort(), 15000);
  leaderboardStatus.textContent = "Loading rankings…";
  leaderboardList.innerHTML = "";
  try {
    const response = await fetch(`${BACKEND_HTTP_URL}/leaderboard?duration=${leaderboardDuration.value || 60}&period=${leaderboardPeriod}`, { signal: request.signal });
    if (!response.ok) throw new Error("Leaderboard unavailable");
    const data = await response.json();
    if (leaderboardRequest !== request) return;
    data.entries.forEach((entry) => {
      const row = document.createElement("li");
      const name = document.createElement("span");
      name.textContent = entry.name;
      const points = document.createElement("span");
      points.className = "leaderboard-score";
      points.textContent = `${entry.score} pts`;
      row.appendChild(name);
      row.appendChild(points);
      leaderboardList.appendChild(row);
    });
    leaderboardStatus.textContent = data.entries.length ? (data.week ? `Week starting ${data.week} · Johannesburg` : "All-Time · Best scores") : "No ranked rounds yet. Be the first.";
  } catch {
    if (leaderboardRequest === request) leaderboardStatus.textContent = "Rankings unavailable. Try Refresh in a moment; you can still play.";
  } finally { clearTimeout(timeout); }
}
function selectLeaderboard(period) {
  leaderboardPeriod = period;
  [weeklyTab, allTimeTab].forEach((tab, index) => {
    const selected = (index === 0) === (period === "weekly");
    tab.setAttribute("aria-selected", String(selected));
    tab.tabIndex = selected ? 0 : -1;
  });
  document.getElementById("leaderboard-panel").setAttribute("aria-labelledby", period === "weekly" ? "weekly-tab" : "all-time-tab");
  loadLeaderboard();
}
weeklyTab.addEventListener("click", () => selectLeaderboard("weekly"));
allTimeTab.addEventListener("click", () => selectLeaderboard("all_time"));
[weeklyTab, allTimeTab].forEach((tab) => tab.addEventListener("keydown", (event) => {
  if (["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) {
    event.preventDefault();
    const weekly = event.key === "Home" || (event.key !== "End" && tab === allTimeTab);
    selectLeaderboard(weekly ? "weekly" : "all_time");
    (weekly ? weeklyTab : allTimeTab).focus();
  }
}));
leaderboardDuration.addEventListener("change", loadLeaderboard);
document.getElementById("leaderboard-refresh").addEventListener("click", loadLeaderboard);
loadLeaderboard();

const guideDialog = document.getElementById("guide-dialog");
document.getElementById("guide-btn").addEventListener("click", () => guideDialog.showModal());
document.getElementById("guide-close").addEventListener("click", () => guideDialog.close());
